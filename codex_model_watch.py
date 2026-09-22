#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Helper —— 本地监控 Codex / ZCode 的模型使用、额度水位、容量拒单，并主动探测模型偷换。

原理（详见 README）：
  1. 日志侧（Codex）：Codex 会把每一轮会话写入 ~/.codex/sessions/**/*.jsonl（rollout 文件）。
     本工具无侵入地增量解析这些文件，得到：每个 turn 实际生效的模型（turn_context.model）、
     token 用量（token_usage_record）、时长、容量拒单错误（task_complete.error，例如
     "Selected model is at capacity"）、以及额度水位（token_count.rate_limits 里的
     5h/7d 窗口用量百分比）。
     注意：实测 Codex 会把被偷换后的模型一致化写进日志（请求字段与实际字段相同），
     因此「被偷换成了什么」无法从日志还原 —— 这正是探针存在的意义。
  2. 数据库侧（ZCode）：ZCode 自身把每轮用量写进 ~/.zcode/cli/db/db.sqlite（turn_usage /
     model_usage / session 表）。本工具以只读方式增量导入，得到模型、token、时长、TTFT、
     错误类型与项目分布。任务排程通过 ZCode.app 内置的 zcode.cjs 无头 CLI 执行：
     本工具会从 ZCode 本机凭证解密 coding-plan API key，生成一份独立的 personal
     provider 配置（~/.codex-model-watch/zcode-provider-config.json，0600）并经
     ZCODE_PERSONAL_PROVIDER_CONFIG_FILE 环境变量注入，使无头 CLI 具备默认模型。
     ZCode 没有实时额度接口，中断续跑采用定时重试直到恢复；探针与实时额度面板仅支持
     Codex。
  3. 探针侧（仅 Codex）：用你本地的 Codex 登录态（~/.codex/auth.json）向
     chatgpt.com/backend-api/codex/responses 发一条最小请求，读取 SSE
     response.created 事件里服务端实际派出的模型，即可即时验证「请求 X 会被派什么」。
     每次探针只消耗极少量额度，可手动触发也可定时执行。

所有数据只存在本机（SQLite），面板为本地网页，没有任何遥测。
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from watch_scheduler import (Scheduler, all_task_snapshots, available_projects, ensure_zcode_provider_config,
                             helper_data_dir, init_db as init_scheduler_db, next_reset, quota_snapshot, read_quota,
                             read_zcode_quota, rule_rows, sprint_window, task_snapshots, validate_rule,
                             validate_sprint, zcode_bin, zcode_cmd)
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, ".codex-model-watch")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
BACKEND_URL = "https://chatgpt.com/backend-api/codex/responses"

g_lock = threading.Lock()
g_last_scan = 0.0
g_state = {"demo": False}


# ---------------------------------------------------------------- db

def db_connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # 单进程多线程，统一用 g_lock 串行化；check_same_thread 关掉以复用连接
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, offset INTEGER DEFAULT 0, size INTEGER DEFAULT 0,
        mtime REAL DEFAULT 0, lines INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS turns(
        file TEXT, turn_id TEXT, ts TEXT, session_id TEXT, project TEXT,
        requested TEXT, served TEXT, effort TEXT, agent TEXT DEFAULT 'codex',
        in_tokens INTEGER DEFAULT 0, cached_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0,
        duration_ms INTEGER, ttft_ms INTEGER,
        error_kind TEXT, error_msg TEXT,
        UNIQUE(file, turn_id));
    CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
    CREATE TABLE IF NOT EXISTS agent_state(
        agent TEXT PRIMARY KEY, last_ms INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS quota(
        ts TEXT PRIMARY KEY, primary_used REAL, secondary_used REAL, raw TEXT);
    CREATE TABLE IF NOT EXISTS probes(
        ts TEXT PRIMARY KEY, requested TEXT, served TEXT, swapped INTEGER,
        latency_ms INTEGER, safety_header TEXT, error TEXT, agent TEXT DEFAULT 'codex');
    CREATE TABLE IF NOT EXISTS threads(
        thread_id TEXT PRIMARY KEY, requested TEXT);
    """)
    # 旧库迁移：probes 补 agent 列
    if "agent" not in {r[1] for r in conn.execute("PRAGMA table_info(probes)")}:
        conn.execute("ALTER TABLE probes ADD COLUMN agent TEXT DEFAULT 'codex'")
    # 旧库迁移：turns 补 agent 列（存量行视为 codex），索引随列就位后建
    if "agent" not in {r[1] for r in conn.execute("PRAGMA table_info(turns)")}:
        conn.execute("ALTER TABLE turns ADD COLUMN agent TEXT DEFAULT 'codex'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_agent ON turns(agent)")
    conn.commit()
    init_scheduler_db(conn)
    return conn


# ---------------------------------------------------------------- 扫描解析

def iso_now():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)


def classify_error(msg, info):
    if info:
        if not isinstance(info, str):
            info = json.dumps(info, ensure_ascii=False)
        return info[:60]
    m = (msg or "").lower()
    if "at capacity" in m:
        return "capacity"
    if "rate limit" in m:
        return "rate_limit"
    if "usage limit" in m or "limit reached" in m:
        return "usage_limit"
    return "error"


def parse_lines(lines, file_key, conn, stats):
    """解析一批 rollout 行；返回需写入的行集合。"""
    turns, quota_rows = [], []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        ts = obj.get("timestamp") or ""
        t = obj.get("type")
        p = obj.get("payload")
        if not isinstance(p, dict):
            continue
        pt = p.get("type", "")
        if t == "turn_context":
            tid = p.get("turn_id")
            if not tid:
                continue
            collab = p.get("collaboration_mode") or {}
            settings = collab.get("settings") or {}
            requested = settings.get("model") or ""
            served = p.get("model") or ""
            cwd = p.get("cwd") or ""
            project = os.path.basename(cwd.rstrip("\\/")) if cwd else ""
            # turn 行必须遇到就立刻插入：同一文件里 token_usage/task_complete 的 UPDATE 在其后到达
            conn.execute("""INSERT INTO turns(file, turn_id, ts, session_id, project, requested, served, effort)
                            VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(file, turn_id) DO UPDATE SET
                              ts=COALESCE(excluded.ts, turns.ts),
                              project=CASE WHEN excluded.project!='' THEN excluded.project ELSE turns.project END,
                              requested=COALESCE(NULLIF(excluded.requested,''), turns.requested),
                              served=COALESCE(NULLIF(excluded.served,''), turns.served),
                              effort=COALESCE(NULLIF(excluded.effort,''), turns.effort)""",
                         (file_key, tid, ts or None, "", project, requested, served, p.get("effort") or ""))
            stats["turns"] += 1
        elif t == "event_msg" and pt == "thread_settings_applied":
            tid = p.get("thread_id")
            ts_model = ((p.get("thread_settings") or {}).get("model")) or ""
            if tid and ts_model:
                conn.execute("INSERT INTO threads(thread_id, requested) VALUES(?,?) "
                             "ON CONFLICT(thread_id) DO UPDATE SET requested=excluded.requested", (tid, ts_model))
        elif t == "token_usage_record":
            tid = p.get("turn_id")
            usage = p.get("turn_token_usage") or p.get("usage") or {}
            if tid and usage:
                conn.execute("UPDATE turns SET in_tokens=?, cached_tokens=?, out_tokens=? "
                             "WHERE file=? AND turn_id=?",
                             (usage.get("input_tokens") or 0, usage.get("cached_input_tokens") or 0,
                              usage.get("output_tokens") or 0, file_key, tid))
        elif t == "event_msg" and pt == "task_complete":
            tid = p.get("turn_id")
            if not tid:
                continue
            err = p.get("error") or None
            kind = msg = None
            if isinstance(err, dict):
                msg = err.get("message") or ""
                if not isinstance(msg, str):
                    msg = json.dumps(msg, ensure_ascii=False)
                msg = msg[:300]
                kind = classify_error(msg, err.get("codex_error_info"))
            conn.execute("UPDATE turns SET duration_ms=?, ttft_ms=?, error_kind=COALESCE(?,error_kind), "
                         "error_msg=? WHERE file=? AND turn_id=?",
                         (p.get("duration_ms"), p.get("time_to_first_token_ms"), kind, msg, file_key, tid))
            if kind:
                stats["errors"] += 1
        elif t == "event_msg" and pt == "token_count":
            rl = p.get("rate_limits") or {}
            prim = (rl.get("primary") or {}).get("used_percent")
            sec = (rl.get("secondary") or {}).get("used_percent")
            if ts and (prim is not None or sec is not None):
                conn.execute("INSERT INTO quota(ts, primary_used, secondary_used, raw) VALUES(?,?,?,?) "
                             "ON CONFLICT(ts) DO NOTHING", (ts, prim, sec, json.dumps(rl)))


def scan_sessions(conn, codex_home, max_age_days):
    """增量扫描 sessions 目录；返回统计。"""
    sessions_dir = os.path.join(codex_home, "sessions")
    if not os.path.isdir(sessions_dir):
        return {"files": 0, "turns": 0, "errors": 0, "note": "sessions 目录不存在: " + sessions_dir}
    pattern = os.path.join(sessions_dir, "**", "*.jsonl")
    files = glob.glob(pattern, recursive=True)
    cutoff_ts = 0
    if max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        cutoff_ts = cutoff
    files = [f for f in files if os.path.getmtime(f) >= cutoff_ts] if max_age_days > 0 else files
    stats = {"files": 0, "turns": 0, "errors": 0}
    for fp in sorted(files):
        try:
            st = os.stat(fp)
        except OSError:
            continue
        row = conn.execute("SELECT offset, mtime, size, lines FROM files WHERE path=?", (fp,)).fetchone()
        # 续读策略：文件被截断/重写则从头解析，否则从上次 offset 续读新增部分
        start, resume, unchanged = 0, False, False
        if row:
            if st.st_size > row["offset"]:
                start, resume = row["offset"], True
            elif st.st_size == row["offset"] and row["mtime"] == st.st_mtime:
                unchanged = True
        if unchanged:
            continue  # 无新内容
        if not resume:
            conn.execute("DELETE FROM turns WHERE file=?", (fp,))
            start = 0
        with open(fp, "rb") as fh:
            fh.seek(start)
            consumed, lines = 0, []
            while True:
                chunk = fh.readline()
                if not chunk:
                    break
                consumed += len(chunk)
                lines.append(chunk)
            # 最后一行可能不完整，回退 offset 到最后一个完整换行
            if lines and not lines[-1].endswith(b"\n"):
                tail = lines.pop()
                consumed -= len(tail)
            if lines:
                text = b"".join(lines).decode("utf-8", errors="replace")
                parse_lines(text.splitlines(), fp, conn, stats)
        old_lines = row["lines"] if (row and resume) else 0
        conn.execute("""INSERT INTO files(path, offset, size, mtime, lines) VALUES(?,?,?,?,?)
                        ON CONFLICT(path) DO UPDATE SET offset=excluded.offset, size=excluded.size,
                          mtime=excluded.mtime, lines=excluded.lines""",
                     (fp, start + consumed, st.st_size, st.st_mtime, old_lines + len(lines)))
        stats["files"] += 1
    conn.commit()
    return stats


# ---------------------------------------------------------------- ZCode 导入

ZCODE_LOOKBACK_MS = 2 * 3600 * 1000  # 重读最近 2 小时的轮次，覆盖进行中/重试的状态更新
ZCODE_ERROR_KINDS = {"rate_limited": "rate_limit", "quota": "usage_limit", "usage_limit": "usage_limit"}


def import_zcode(conn, zcode_home, max_age_days):
    """增量导入 ZCode 的轮次用量（~/.zcode/cli/db/db.sqlite，只读，绝不写入）。

    ZCode 自己把每轮用量写进本地 SQLite：turn_usage 每轮一行（token/时长/TTFT/错误），
    模型名取该轮第一个 model_usage 请求的 model_id，项目名取 session.directory。
    行会随轮次进行被更新，因此除首次按 max_age_days 全量导入外，之后每次
    从 last_ms 往前 2 小时重读并用 UPSERT 覆盖。
    """
    db_path = os.path.join(zcode_home, "cli", "db", "db.sqlite")
    if not os.path.isfile(db_path):
        return {"files": 0, "turns": 0, "errors": 0, "note": "ZCode 数据库不存在: " + db_path}
    row = conn.execute("SELECT last_ms FROM agent_state WHERE agent='zcode'").fetchone()
    last_ms = row["last_ms"] if row else 0
    if last_ms:
        since = last_ms - ZCODE_LOOKBACK_MS
    elif max_age_days > 0:
        since = int((time.time() - max_age_days * 86400) * 1000)
    else:
        since = 0
    try:
        zdb = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2)
        zdb.row_factory = sqlite3.Row
        rows = zdb.execute("""
            SELECT t.session_id, t.turn_id, t.started_at, t.status, t.duration_ms,
                   t.time_to_first_token_ms, t.input_tokens, t.output_tokens,
                   t.cache_read_input_tokens, t.error_type, s.directory,
                   (SELECT m.model_id FROM model_usage m
                    WHERE m.session_id=t.session_id AND m.turn_id=t.turn_id
                    ORDER BY m.started_at LIMIT 1) AS model_id,
                   (SELECT m.variant FROM model_usage m
                    WHERE m.session_id=t.session_id AND m.turn_id=t.turn_id
                    ORDER BY m.started_at LIMIT 1) AS variant
            FROM turn_usage t LEFT JOIN session s ON s.id=t.session_id
            WHERE t.started_at >= ? ORDER BY t.started_at""", (since,)).fetchall()
        zdb.close()
    except sqlite3.Error as exc:
        return {"files": 0, "turns": 0, "errors": 0, "note": "ZCode 数据库读取失败: " + str(exc)[:120]}
    stats = {"files": 0, "turns": 0, "errors": 0}
    max_seen, sessions = last_ms, set()
    for r in rows:
        sid, tid = r["session_id"], r["turn_id"]
        if not sid or not tid or not r["started_at"]:
            continue
        model = r["model_id"] or ""
        kind = None
        if r["status"] == "error" and r["error_type"]:
            kind = ZCODE_ERROR_KINDS.get(r["error_type"], (r["error_type"] or "error")[:60])
        ts = datetime.fromtimestamp(r["started_at"] / 1000, timezone.utc)
        ts = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        directory = r["directory"] or ""
        conn.execute("""INSERT INTO turns(file, turn_id, ts, session_id, project,
                          requested, served, effort, agent,
                          in_tokens, cached_tokens, out_tokens, duration_ms, ttft_ms, error_kind, error_msg)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(file, turn_id) DO UPDATE SET
                          ts=excluded.ts,
                          project=CASE WHEN excluded.project!='' THEN excluded.project ELSE turns.project END,
                          served=COALESCE(NULLIF(excluded.served,''), turns.served),
                          effort=COALESCE(NULLIF(excluded.effort,''), turns.effort),
                          in_tokens=excluded.in_tokens, cached_tokens=excluded.cached_tokens,
                          out_tokens=excluded.out_tokens, duration_ms=excluded.duration_ms,
                          ttft_ms=excluded.ttft_ms, error_kind=COALESCE(excluded.error_kind, turns.error_kind),
                          error_msg=COALESCE(excluded.error_msg, turns.error_msg)""",
                     ("zcode:" + sid, tid, ts, sid,
                      os.path.basename(directory.rstrip("\\/")) if directory else "",
                      model, model, r["variant"] or "", "zcode",
                      r["input_tokens"] or 0, r["cache_read_input_tokens"] or 0,
                      r["output_tokens"] or 0, r["duration_ms"], r["time_to_first_token_ms"],
                      kind, ""))
        sessions.add(sid)
        max_seen = max(max_seen, r["started_at"])
        if kind:
            stats["errors"] += 1
        stats["turns"] += 1
    if max_seen > last_ms:
        conn.execute("INSERT INTO agent_state(agent,last_ms) VALUES('zcode',?) "
                     "ON CONFLICT(agent) DO UPDATE SET last_ms=excluded.last_ms", (max_seen,))
    stats["files"] = len(sessions)
    conn.commit()
    return stats


# ---------------------------------------------------------------- 探针

def load_auth(codex_home):
    path = os.path.join(codex_home, "auth.json")
    if not os.path.isfile(path):
        return None
    try:
        auth = json.load(open(path, encoding="utf-8"))
        tokens = auth.get("tokens") or {}
        tok = tokens.get("access_token")
        if not tok:
            return None
        return {"token": tok, "account": tokens.get("account_id", "")}
    except Exception:
        return None


def run_probe(codex_home, model):
    import urllib.request
    import urllib.error
    auth = load_auth(codex_home)
    if not auth:
        return {"error": "未找到 Codex 登录态（~/.codex/auth.json），请先用 Codex 登录"}
    body = json.dumps({
        "model": model,
        "instructions": "You are a helpful assistant.",
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "hi"}]}],
        "stream": True, "store": False, "reasoning": {"effort": "low"},
    }).encode()
    req = urllib.request.Request(BACKEND_URL, data=body, method="POST")
    for k, v in [("Authorization", "Bearer " + auth["token"]),
                 ("chatgpt-account-id", auth["account"]),
                 ("Content-Type", "application/json"),
                 ("Accept", "text/event-stream"),
                 ("originator", "codex_cli_rs"),
                 ("User-Agent", "codex_cli_rs/0.154.0")]:
        req.add_header(k, v)
    t0 = time.time()
    served, safety, error = "", "", None
    try:
        resp = urllib.request.urlopen(req, timeout=90)
        safety = resp.headers.get("x-codex-safety-buffering-enabled", "") or ""
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            if b"response.created" in buf:
                break
            if len(buf) > 200000:
                break
        text = buf.decode(errors="replace")
        i = text.find('"model":"')
        if i >= 0:
            served = text[i + 9:text.find('"', i + 9)]
        if not served:
            error = "响应里没有找到模型字段"
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode(errors="replace")
        except Exception:
            detail = ""
        error = "HTTP %d %s" % (e.code, detail[:200])
    except Exception as e:
        error = str(e)[:200]
    latency = int((time.time() - t0) * 1000)
    swapped = 1 if (served and model and served != model) else 0
    row = (iso_now(), model, served, swapped, latency, safety, error)
    conn = db_connect(db_path())
    with g_lock:
        conn.execute("INSERT INTO probes(ts, requested, served, swapped, latency_ms, safety_header, error) "
                     "VALUES(?,?,?,?,?,?,?)", row)
        conn.commit()
    return {"ts": row[0], "requested": model, "served": served, "swapped": bool(swapped),
            "latency_ms": latency, "safety_header": safety, "error": error}


# ---------------------------------------------------------------- 费用估算

# 公开 API 牌价（每 1M tokens）。来源：openai.com/api/pricing 与 bigmodel.cn 刊例，
# 2026-09 采集；cached 缺省按输入价 10%。可被 ~/.codex-model-watch/pricing.json 覆盖：
# {"usd_cny": 7.1, "rates": {"模型前缀": {"in":x,"cached":y,"out":z,"currency":"usd|cny"}}}
PRICING_USD_CNY = 7.1
# 每个模型给出其有刊价市场的原生生牌价（每 1M tokens）：
#   OpenAI 仅 usd；智谱国内 cny、国际(Z.ai) usd —— 两个市场价格不同，按展示语言取口径。
# cached 缺省按输入价 10%~20%。采集于 2026-09（openai.com/api/pricing、bigmodel.cn、docs.z.ai）。
PRICING_RATES = [
    # (前缀, {"cny": (入,缓存,出)|None, "usd": (...)|None})  前缀最长者优先
    ("gpt-5.6-sol", {"usd": (4.0, 0.40, 20.0)}),
    ("gpt-5.6-terra", {"usd": (2.0, 0.20, 12.0)}),
    ("gpt-5.6-luna", {"usd": (0.20, 0.02, 1.20)}),
    ("gpt-5.6", {"usd": (2.0, 0.20, 12.0)}),
    ("gpt-6-astra", {"usd": (4.0, 0.40, 20.0)}),      # 未见于牌价，按旗舰档估
    ("gpt-6", {"usd": (4.0, 0.40, 20.0)}),
    ("gpt-5.5", {"usd": (5.0, 0.50, 30.0)}),
    ("gpt-5.4", {"usd": (2.5, 0.25, 15.0)}),
    ("gpt-5", {"usd": (1.25, 0.125, 10.0)}),
    ("codex-auto-review", {"usd": (0.0, 0.0, 0.0)}),  # Codex 内部审查模型
    ("glm-5.3-flash", {"cny": (0.8, 0.16, 2.8), "usd": (0.15, 0.03, 0.50)}),
    ("glm-5.3", {"cny": (8.0, 1.6, 28.0), "usd": (1.40, 0.26, 4.40)}),
    ("glm-5.2", {"cny": (8.0, 1.6, 28.0), "usd": (1.40, 0.26, 4.40)}),
    ("glm-5", {"cny": (8.0, 1.6, 28.0), "usd": (1.40, 0.26, 4.40)}),
    ("glm-4.7", {"cny": (2.0, 0.4, 8.0), "usd": (0.60, 0.12, 2.20)}),
    ("glm-4", {"cny": (2.0, 0.4, 8.0), "usd": (0.60, 0.12, 2.20)}),
]
_pricing_cache = {}


def load_pricing():
    """Embedded rates merged with the user's optional pricing.json override.

    Override format: {"usd_cny": 7.1, "rates": {"prefix": {"in":x,"cached":y,"out":z,
    "currency":"usd|cny"}}} or {"prefix": {"cny":[i,c,o],"usd":[i,c,o]}}.
    """
    if _pricing_cache:
        return _pricing_cache["data"]
    rates = list(PRICING_RATES)
    usd_cny = PRICING_USD_CNY
    path = os.path.join(APP_DIR, "pricing.json")
    if os.path.isfile(path):
        try:
            cfg = json.load(open(path, encoding="utf-8"))
            usd_cny = float(cfg.get("usd_cny", usd_cny))
            merged = {p[0]: p for p in rates}
            for prefix, r in (cfg.get("rates") or {}).items():
                prefix = prefix.lower()
                if "cny" in r or "usd" in r:
                    entry = {"cny": tuple(r.get("cny") or ()), "usd": tuple(r.get("usd") or ())}
                else:
                    cur = r.get("currency", "usd")
                    entry = {cur: (float(r.get("in", 0)), float(r.get("cached", r.get("in", 0) * 0.1)),
                                   float(r.get("out", 0)))}
                merged[prefix] = (prefix, entry)
            rates = list(merged.values())
        except Exception:
            pass
    rates.sort(key=lambda p: -len(p[0]))
    _pricing_cache["data"] = (rates, usd_cny)
    return _pricing_cache["data"]


def model_price(model):
    """Match a model id to rate entries {"cny": tuple|None, "usd": tuple|None}; None when unpriced."""
    rates, _ = load_pricing()
    low = (model or "").lower()
    for prefix, entry in rates:
        if low.startswith(prefix):
            return {"cny": entry.get("cny") or None, "usd": entry.get("usd") or None}
    return None


def price_tokens(model, tin, tcached, tout):
    """Native-currency cost of one aggregate row.

    in_tokens already includes cached tokens: fresh input is billed at the
    full rate, cached at the cached rate. Returns {"usd": x, "cny": y}
    (only the markets where the model is priced), or None when unpriced.
    """
    p = model_price(model)
    if not p or not (p["cny"] or p["usd"]):
        return None
    fresh = max(0, (tin or 0) - (tcached or 0))
    out = {}
    for cur, rate in p.items():
        if rate:
            out[cur] = round((fresh * rate[0] + (tcached or 0) * rate[1] + (tout or 0) * rate[2]) / 1e6, 4)
    return out


def clear_probes(conn, agent):
    """Delete probe history for one agent ('codex'|'zcode'); returns rowcount."""
    if agent not in ("codex", "zcode"):
        raise ValueError("agent 必须是 codex 或 zcode")
    cur = conn.execute("DELETE FROM probes WHERE agent=?", (agent,))
    conn.commit()
    return cur.rowcount


def month_cutoff():
    """UTC timestamp string for the start of the current local month."""
    now_local = datetime.now().astimezone()
    start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_cost(conn, agent, cutoff, until=""):
    """Aggregate token cost for one agent (or both when agent=''); range [cutoff, until).

    Returns native-currency sums: "usd" (USD-priced models, e.g. OpenAI) and
    "cny" (CNY-priced models, e.g. Zhipu domestic). No conversion here — the
    UI converts per display language.
    """
    cond = {"c": cutoff, "u": until, "a": agent}
    rows = conn.execute("""SELECT COALESCE(NULLIF(served,''),'?') model, agent,
                                  COALESCE(SUM(in_tokens),0) tin, COALESCE(SUM(cached_tokens),0) tcached,
                                  COALESCE(SUM(out_tokens),0) tout, COUNT(*) turns
                           FROM turns WHERE (:c='' OR ts>=:c) AND (:u='' OR ts<:u) AND (:a='' OR agent=:a)
                           GROUP BY model, agent ORDER BY tin DESC""", cond).fetchall()
    _, usd_cny = load_pricing()
    result = {"usd": 0.0, "cny": 0.0, "unpriced_tokens": 0, "models": [],
              "cny_total": 0.0, "usd_total": 0.0}
    for r in rows:
        p = model_price(r["model"])
        if not p or not (p["cny"] or p["usd"]):
            result["unpriced_tokens"] += (r["tin"] or 0) + (r["tout"] or 0)
            continue
        fresh = max(0, (r["tin"] or 0) - (r["tcached"] or 0))
        native = {cur: round((fresh * rate[0] + (r["tcached"] or 0) * rate[1]
                              + (r["tout"] or 0) * rate[2]) / 1e6, 4)
                  for cur, rate in p.items() if rate}
        # 两种展示口径：CH 全部 ¥（usd 按汇率折），EN 全部 $（cny 按汇率折）
        if p["cny"]:
            result["cny"] += native["cny"]
            result["cny_total"] += native["cny"]
            result["usd_total"] += native["cny"] / usd_cny
        else:
            result["usd"] += native["usd"]
            result["usd_total"] += native["usd"]
            result["cny_total"] += native["usd"] * usd_cny
        if p["cny"]:
            disp_cny, disp_usd = native["cny"], native["cny"] / usd_cny
        else:
            disp_usd, disp_cny = native["usd"], native["usd"] * usd_cny
        result["models"].append({"model": r["model"], "agent": r["agent"], "turns": r["turns"],
                                 "tin": r["tin"], "tout": r["tout"],
                                 "native_usd": native.get("usd"), "native_cny": native.get("cny"),
                                 "cost_usd": round(disp_usd, 2), "cost_cny": round(disp_cny, 2)})
    for key in ("usd", "cny", "cny_total", "usd_total"):
        result[key] = round(result[key], 2)
    result["models"].sort(key=lambda m: -(m["native_usd"] or 0) - (m["native_cny"] or 0))
    return result


def prev_month_cutoff():
    """UTC string for the start of the previous local month."""
    now_local = datetime.now().astimezone()
    first = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_last = first - timedelta(days=1)
    prev_first = prev_last.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return prev_first.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def api_overview(conn):
    """Aggregate cross-agent statistics for the 全局总览 dashboard."""
    usd_cny = load_pricing()[1]
    mc, pc = month_cutoff(), prev_month_cutoff()
    d30 = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    month = compute_cost(conn, "", mc)
    prev = compute_cost(conn, "", pc, mc)
    per_agent = {a: compute_cost(conn, a, mc) for a in ("codex", "zcode")}

    totals = conn.execute("""SELECT COUNT(*) turns,
                                    COALESCE(SUM(in_tokens),0) tin, COALESCE(SUM(out_tokens),0) tout,
                                    COALESCE(SUM(cached_tokens),0) tcached,
                                    SUM(CASE WHEN error_kind IS NOT NULL THEN 1 ELSE 0 END) errors
                             FROM turns WHERE ts>=? AND ts<?""", (mc, "9")).fetchone()

    daily = []
    for r in conn.execute("""SELECT substr(ts,1,10) d, agent,
                                    COALESCE(SUM(in_tokens),0) tin, COALESCE(SUM(out_tokens),0) tout,
                                    COUNT(*) turns FROM turns
                             WHERE ts>=? AND ts IS NOT NULL GROUP BY d, agent ORDER BY d""", (d30,)):
        daily.append({"date": r["d"], "agent": r["agent"], "tin": r["tin"],
                      "tout": r["tout"], "turns": r["turns"]})
    # 逐日费用需要按模型拆分，用单条聚合查询
    priced = {}
    for r in conn.execute("""SELECT substr(ts,1,10) d, agent, served,
                                    COALESCE(SUM(in_tokens),0) tin, COALESCE(SUM(cached_tokens),0) tcached,
                                    COALESCE(SUM(out_tokens),0) tout
                             FROM turns WHERE ts>=? AND ts IS NOT NULL
                             GROUP BY d, agent, served""", (d30,)):
        c = price_tokens(r["served"], r["tin"], r["tcached"], r["tout"])
        p = model_price(r["served"])
        key = (r["d"], r["agent"])
        if c:
            acc = priced.setdefault(key, {"usd": 0.0, "cny": 0.0})
            if p.get("cny"):
                acc["cny"] += c["cny"]
                acc["usd"] += c["cny"] / usd_cny
            else:
                acc["usd"] += c["usd"]
                acc["cny"] += c["usd"] * usd_cny
    for item in daily:
        acc = priced.get((item["date"], item["agent"]), {"usd": 0.0, "cny": 0.0})
        item["cost_usd"] = round(acc["usd"], 2)
        item["cost_cny"] = round(acc["cny"], 2)

    return {"generated_at": iso_now(), "usd_cny": usd_cny,
            "month": month, "prev_month": {"usd": prev["usd"], "cny": prev["cny"]},
            "per_agent": per_agent,
            "totals": {"turns": totals["turns"], "tokens_in": totals["tin"],
                       "tokens_out": totals["tout"], "tokens_cached": totals["tcached"],
                       "errors": totals["errors"] or 0,
                       "cache_hit": round(totals["tcached"] * 100.0 / totals["tin"], 1) if totals["tin"] else 0.0},
            "daily": daily}


def run_zcode_probe(model, timeout=180):
    """Ask for a specific ZCode model headlessly and read what actually served.

    Writes a probe personal config whose defaultModelSelection is the requested
    model, runs one tiny turn, then reads the served model_id from ZCode's own
    model_usage table via the returned session id.
    """
    import tempfile
    base = zcode_cmd()
    if base is None:
        return {"error": "未找到 ZCode CLI"}
    try:
        config = ensure_zcode_provider_config()
    except Exception as exc:
        return {"error": str(exc)}
    probe_cfg = os.path.join(helper_data_dir(), "zcode-probe-config.json")
    try:
        cfg = json.load(open(config, encoding="utf-8"))
        rule = cfg["config"]["providerConfigRules"]["providerRules"][0]
        models = list(dict.fromkeys([*(rule["config"].get("personalModelIds") or []), model]))
        rule["config"]["personalModelIds"] = models
        cfg["config"]["defaultModelSelection"] = {"providerId": rule["providerId"], "modelId": model}
        with open(probe_cfg, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=1)
        os.chmod(probe_cfg, 0o600)
    except Exception as exc:
        return {"error": "生成探针配置失败: " + str(exc)[:150]}
    cli = zcode_bin()
    builtin = os.path.normpath(os.path.join(os.path.dirname(cli), "..", "config", "provider",
                                            "zcode-builtin.json")) if cli else ""
    env = {**os.environ, "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": probe_cfg}
    if builtin and os.path.isfile(builtin):
        env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = builtin
    workdir = tempfile.mkdtemp(prefix="zcode-probe-")
    cmd = [*base, "--prompt", "Reply with exactly: ok", "--cwd", workdir, "--json"]
    t0 = time.time()
    error, session = "", ""
    try:
        proc = subprocess.run(cmd, env=env, text=True, cwd=workdir, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        match = re.search(r"sess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                          proc.stdout or "")
        session = match.group(0) if match else ""
        if proc.returncode != 0:
            error = (proc.stderr or proc.stdout or "")[-300:] or "exit %d" % proc.returncode
    except subprocess.TimeoutExpired:
        error = "探针超时"
    except Exception as exc:
        error = str(exc)[:200]
    latency = int((time.time() - t0) * 1000)
    served = ""
    if session:
        try:
            zdb = sqlite3.connect("file:" + os.path.join(HOME, ".zcode", "cli", "db", "db.sqlite") +
                                  "?mode=ro", uri=True, timeout=2)
            row = zdb.execute("SELECT model_id FROM model_usage WHERE session_id=? "
                              "ORDER BY started_at LIMIT 1", (session,)).fetchone()
            served = row[0] if row else ""
            zdb.close()
        except sqlite3.Error:
            pass
    if not served and not error:
        error = "未能从 ZCode 读取实际派出模型"
    swapped = 1 if (served and model and served != model) else 0
    row = (iso_now(), model, served, swapped, latency, "zcode", error or None, "zcode")
    with g_lock:
        c = db_connect(db_path())
        c.execute("INSERT OR REPLACE INTO probes(ts, requested, served, swapped, latency_ms, "
                  "safety_header, error, agent) VALUES(?,?,?,?,?,?,?,?)", row)
        c.commit()
    return {"ts": row[0], "requested": model, "served": served, "swapped": bool(swapped),
            "latency_ms": latency, "error": error}


# ---------------------------------------------------------------- 聚合输出

def api_data(conn, days=0, agent="", win_sec=None):
    cutoff = ""
    if win_sec is not None and win_sec > 0:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                  - timedelta(seconds=win_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif days and days > 0:
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cond = {"c": cutoff, "a": agent}
    flt = "(:a='' OR agent=:a)"
    ts_flt = "(:c='' OR ts>=:c)"

    def q(sql, args=()):
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    cov = conn.execute("SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM turns "
                       "WHERE ts IS NOT NULL AND " + flt, cond).fetchone()
    win = conn.execute("""SELECT COUNT(*) turns,
                                 COALESCE(SUM(in_tokens),0) tin, COALESCE(SUM(out_tokens),0) tout,
                                 COALESCE(SUM(cached_tokens),0) tcached,
                                 SUM(CASE WHEN error_kind IS NOT NULL THEN 1 ELSE 0 END) errors,
                                 SUM(CASE WHEN error_kind IN ('capacity','server_overloaded') THEN 1 ELSE 0 END) capacity,
                                 AVG(duration_ms) avg_dur
                          FROM turns WHERE """ + ts_flt + " AND " + flt, cond).fetchone()
    agents = q("""SELECT COALESCE(NULLIF(agent,''),'codex') agent, COUNT(*) turns,
                         COALESCE(SUM(in_tokens),0) tin, COALESCE(SUM(out_tokens),0) tout,
                         SUM(CASE WHEN error_kind IS NOT NULL THEN 1 ELSE 0 END) errors
                  FROM turns WHERE """ + ts_flt + " GROUP BY agent ORDER BY turns DESC", {"c": cutoff})
    hourly = q("""SELECT substr(ts,1,13)||':00' bucket, COUNT(*) turns,
                         SUM(CASE WHEN error_kind IS NOT NULL THEN 1 ELSE 0 END) errors
                  FROM turns WHERE ts IS NOT NULL AND """ + ts_flt + " AND " + flt + " GROUP BY bucket ORDER BY bucket", cond)
    models = q("""SELECT COALESCE(NULLIF(served,''),'?') model, agent, COUNT(*) turns,
                         COALESCE(SUM(in_tokens),0) tin, COALESCE(SUM(out_tokens),0) tout,
                         COALESCE(SUM(cached_tokens),0) tcached,
                         AVG(duration_ms) avg_dur,
                         SUM(CASE WHEN error_kind IS NOT NULL THEN 1 ELSE 0 END) errors
                  FROM turns WHERE """ + ts_flt + " AND " + flt +
                " GROUP BY model, agent ORDER BY turns DESC", cond)
    projects = q("""SELECT COALESCE(NULLIF(project,''),'?') project, COUNT(*) turns,
                           COALESCE(SUM(in_tokens+out_tokens),0) tokens
                    FROM turns WHERE """ + ts_flt + " AND " + flt +
                 " GROUP BY project ORDER BY tokens DESC LIMIT 15", cond)
    errors_recent = q("""SELECT ts, COALESCE(NULLIF(served,''),'?') model, agent, error_kind, error_msg
                         FROM turns WHERE error_kind IS NOT NULL AND """ + ts_flt + " AND " + flt +
                         " ORDER BY ts DESC LIMIT 50", cond)
    quota_latest = q("SELECT * FROM quota ORDER BY ts DESC LIMIT 1")
    quota_hist = q("SELECT ts, primary_used, secondary_used FROM quota ORDER BY ts DESC LIMIT 48")
    probes = q("SELECT * FROM probes WHERE (:a='' OR agent=:a) ORDER BY ts DESC LIMIT 100", {"a": agent})
    probe_summary = conn.execute("""SELECT COUNT(*) n, COALESCE(SUM(swapped),0) swapped
                                    FROM probes WHERE (:a='' OR agent=:a)""",
                                  {"a": agent}).fetchone()
    total_models = sum(m["turns"] for m in models) or 1
    for m in models:
        m["share"] = round(m["turns"] * 100.0 / total_models, 1)
        cost = price_tokens(m["model"], m["tin"], m["tcached"], m["tout"])
        if cost:
            _, usd_cny = load_pricing()
            if cost.get("cny") is not None:
                m["cost_cny"], m["cost_usd"] = cost["cny"], round(cost["cny"] / usd_cny, 2)
            else:
                m["cost_usd"], m["cost_cny"] = cost["usd"], round(cost["usd"] * usd_cny, 2)
    cost_month = compute_cost(conn, agent, month_cutoff())
    _, usd_cny = load_pricing()
    return {
        "meta": {"generated_at": iso_now(), "demo": g_state["demo"],
                 "agents_enabled": (g_args.agents if g_args else ["codex", "zcode"])},
        "coverage": {"first": cov["a"], "last": cov["b"], "turns_total": cov["n"]},
        "summary": {"turns": win["turns"], "tokens_in": win["tin"], "tokens_out": win["tout"],
                    "tokens_cached": win["tcached"], "errors": win["errors"] or 0,
                    "capacity": win["capacity"] or 0,
                    "avg_duration_ms": int(win["avg_dur"] or 0)},
        "agents": agents,
        "cost": {"month": cost_month, "usd_cny": usd_cny},
        "hourly": hourly, "models": models, "projects": projects,
        "errors_recent": errors_recent,
        "quota": {"latest": quota_latest[0] if quota_latest else None,
                  "history": list(reversed(quota_hist)),
                  "live": quota_snapshot(g_scheduler.quota, g_scheduler.quota_sampled_at)
                          if g_scheduler and g_scheduler.quota else None,
                  "error": g_scheduler.quota_error if g_scheduler else "",
                  "zcode": {"live": g_scheduler.zcode_live if g_scheduler else None,
                            "error": g_scheduler.zcode_error if g_scheduler else ""}},
        "probes": probes,
        "probe_summary": {"total": probe_summary["n"], "swapped": probe_summary["swapped"]},
    }


# ---------------------------------------------------------------- demo 数据

def seed_demo(conn):
    """生成两周的演示数据（用于 README 截图与功能体验）。"""
    import random
    random.seed(42)
    models = [("gpt-5.6-sol", 0.52), ("gpt-6-astra", 0.24), ("gpt-5.6-terra", 0.12),
              ("gpt-5.6-luna", 0.08), ("codex-auto-review", 0.04)]
    zcode_models = ["GLM-5.3", "GLM-5.2", "GLM-4.7"]
    projects = ["my-app", "blog", "data-scripts", "learn-rust"]
    now = datetime.now(timezone.utc)
    turns, quota, probes = [], [], []
    for day in range(13, -1, -1):
        base = now - timedelta(days=day)
        n_turn = random.randint(25, 90)
        # 剧情线：第 5 天起 astra 探针开始被偷换成 luna
        swapped_day = day <= 5
        q5 = max(0.0, min(100.0, 100 - day * random.uniform(6, 14)))
        for i in range(n_turn):
            agent = "zcode" if random.random() < 0.3 else "codex"
            if agent == "zcode":
                model, session = random.choice(zcode_models), "demo-session-zcode"
            else:
                session = "demo-session"
                r = random.random()
                acc = 0.0
                model = models[-1][0]
                for m, w in models:
                    acc += w
                    if r <= acc:
                        model = m
                        break
            ts = base.replace(hour=random.randint(8, 23), minute=random.randint(0, 59),
                              second=random.randint(0, 59), microsecond=0)
            tin = random.randint(8, 180) * 1000
            tout = random.randint(1, 40) * 100
            err = None
            if random.random() < 0.03:
                if agent == "zcode":
                    err = "rate_limit" if random.random() < 0.8 else "error"
                else:
                    err = "capacity" if random.random() < 0.7 else "rate_limit"
            turns.append((("demo-%d-%d" % (day, i)), ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          session, random.choice(projects), model, model, "high",
                          tin, int(tin * 0.3), tout,
                          random.randint(8000, 180000), random.randint(1200, 9000),
                          err, "Selected model is at capacity. Please try a different model." if err == "capacity" else None,
                          agent))
        quota.append((base.strftime("%Y-%m-%dT%H:00:00Z"), round(q5, 1),
                      round(min(100.0, q5 * 2.2), 1), "{}"))
        if day % 2 == 0 or swapped_day:
            for hm in (9, 15, 21):
                ts = base.replace(hour=hm, minute=random.randint(0, 59), second=0, microsecond=0)
                probes.append((ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "gpt-6-astra",
                               "gpt-5.6-luna" if swapped_day else "gpt-6-astra",
                               1 if swapped_day else 0, random.randint(900, 4000),
                               "true" if swapped_day else "", None))
    conn.executemany("""INSERT INTO turns(file, turn_id, ts, session_id, project, requested, served, effort,
                        in_tokens, cached_tokens, out_tokens, duration_ms, ttft_ms, error_kind, error_msg, agent)
                        VALUES('demo', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(file, turn_id) DO NOTHING""",
                     [(t[0],) + t[1:] for t in turns])
    conn.executemany("INSERT INTO quota VALUES(?,?,?,?) ON CONFLICT(ts) DO NOTHING", quota)
    conn.executemany("INSERT INTO probes(ts, requested, served, swapped, latency_ms, safety_header, error) "
                     "VALUES(?,?,?,?,?,?,?) ON CONFLICT(ts) DO NOTHING", probes)
    conn.commit()


# ---------------------------------------------------------------- HTTP 服务

WEB_HTML = None


def load_index():
    global WEB_HTML
    if WEB_HTML is None:
        with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
            WEB_HTML = f.read()
    return WEB_HTML


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local_json_request(self):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        expected = "127.0.0.1:%d" % g_args.port
        return (host == expected and self.headers.get("Content-Type", "").split(";")[0] == "application/json"
                and (not origin or origin == "http://" + expected))

    def do_GET(self):
        import urllib.parse
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            data = load_index()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/data":
            global g_last_scan
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                days = int((qs.get("days") or ["0"])[0])
            except ValueError:
                days = 0
            win_sec = None
            if qs.get("win"):
                try:
                    win_sec = max(0, int(qs["win"][0]))
                except ValueError:
                    win_sec = None
            agent = (qs.get("agent") or [""])[0]
            if agent not in ("", "codex", "zcode"):
                agent = ""
            with g_lock:
                if not g_state["demo"] and time.time() - g_last_scan > 20:
                    if "codex" in g_args.agents:
                        scan_sessions(conn(), g_args.codex_home, g_args.max_age_days)
                    if "zcode" in g_args.agents:
                        import_zcode(conn(), g_args.zcode_home, g_args.max_age_days)
                    g_last_scan = time.time()
                self._json(api_data(conn(), days, agent, win_sec))
            return
        if path == "/api/schedule":
            if self.headers.get("Host", "") != "127.0.0.1:%d" % g_args.port:
                self._json({"error": "仅允许本地面板访问"}, 403)
                return
            with g_lock:
                tasks = g_scheduler.snapshots if g_scheduler else all_task_snapshots(g_args.codex_home, g_args.zcode_home)
                tasks = sorted(tasks, key=lambda item: (item.get("turn") or {}).get("status") != "active")
                self._json({"rules": rule_rows(conn()),
                            "tasks": tasks,
                            "projects": available_projects(conn(), g_args.codex_home, g_args.zcode_home),
                            "auto_resume": bool(conn().execute("SELECT 1 FROM schedule_settings WHERE key='auto_resume_since'").fetchone()),
                            "quota_error": g_scheduler.quota_error if g_scheduler else ""})
            return
        if path == "/api/sprint":
            if self.headers.get("Host", "") != "127.0.0.1:%d" % g_args.port:
                self._json({"error": "仅允许本地面板访问"}, 403)
                return
            with g_lock:
                sprints = [dict(r) for r in conn().execute(
                    "SELECT * FROM sprints ORDER BY created_at DESC LIMIT 20")]
                tasks = {}
                for s in sprints:
                    tasks[s["id"]] = [dict(r) for r in conn().execute(
                        "SELECT id,prompt,position,status,session_id,error,started_at,finished_at "
                        "FROM sprint_tasks WHERE sprint_id=? ORDER BY position", (s["id"],))]
                for s in sprints:
                    if s["status"] in ("waiting", "running"):
                        s["window"] = sprint_window(s)
                self._json({"sprints": sprints, "tasks": tasks,
                            "projects": available_projects(conn(), g_args.codex_home, g_args.zcode_home),
                            "demo": g_state["demo"]})
            return
        if path == "/api/overview":
            with g_lock:
                self._json(api_overview(conn()))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path.split("?")[0] == "/api/sprint":
            if not self._local_json_request() or g_state["demo"]:
                self._json({"error": "只接受本地面板的真实模式请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 500000:
                    raise ValueError("请求过大")
                body = json.loads(self.rfile.read(length) or b"{}")
                if str(body.get("agent") or "zcode") != "zcode":
                    raise ValueError("玩命蹬模式目前仅支持 ZCode")
                with g_lock:
                    projects = available_projects(conn(), g_args.codex_home, g_args.zcode_home)
                sp = validate_sprint(body, projects)
                with g_lock:
                    conn().execute("""INSERT INTO sprints
                        (id,name,agent,kind,start_hm,end_hm,start_at,end_at,cwd,concurrency,
                         status,created_at,stopped_at)
                        VALUES(:id,:name,:agent,:kind,:start_hm,:end_hm,:start_at,:end_at,:cwd,
                               :concurrency,:status,:created_at,:stopped_at)""", sp)
                    for t in sp["tasks"]:
                        conn().execute("""INSERT INTO sprint_tasks
                            (id,sprint_id,prompt,position,status,session_id,error,output,
                             started_at,finished_at,attempts)
                            VALUES(:id,:sprint_id,:prompt,:position,:status,:session_id,:error,:output,
                                   :started_at,:finished_at,:attempts)""", t)
                    conn().commit()
                self._json({"sprint": {k: v for k, v in sp.items() if k != "tasks"},
                            "task_count": len(sp["tasks"])}, 201)
            except (ValueError, TypeError, RuntimeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/sprint/start":
            if not self._local_json_request():
                self._json({"error": "只接受本地面板的 JSON 请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1000)) or b"{}")
                if not g_scheduler:
                    raise ValueError("调度器未运行")
                g_scheduler.start_sprint(str(body.get("id") or ""),
                                         body.get("concurrency"))
                self._json({"started": True})
            except (ValueError, TypeError, RuntimeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/sprint/stop_all":
            if not self._local_json_request():
                self._json({"error": "只接受本地面板的 JSON 请求"}, 403)
                return
            count = g_scheduler.stop_all_sprints() if g_scheduler else 0
            self._json({"stopped": count})
            return
        if self.path.split("?")[0] == "/api/sprint/delete":
            if not self._local_json_request():
                self._json({"error": "只接受本地面板的 JSON 请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1000)) or b"{}")
                if g_scheduler:
                    g_scheduler.delete_sprint(str(body.get("id") or ""))
                self._json({"deleted": True})
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/sprint/reorder":
            if not self._local_json_request():
                self._json({"error": "只接受本地面板的 JSON 请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 20000)) or b"{}")
                ids = body.get("task_ids")
                if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
                    raise ValueError("task_ids 必须是字符串数组")
                if not g_scheduler:
                    raise ValueError("调度器未运行")
                g_scheduler.reorder_sprint_tasks(str(body.get("id") or ""), ids)
                self._json({"reordered": len(ids)})
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/sprint/task/delete":
            if not self._local_json_request():
                self._json({"error": "只接受本地面板的 JSON 请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1000)) or b"{}")
                if not g_scheduler:
                    raise ValueError("调度器未运行")
                g_scheduler.delete_sprint_task(str(body.get("task_id") or ""))
                self._json({"deleted": True})
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/sprint/stop":
            if not self._local_json_request():
                self._json({"error": "只接受本地面板的 JSON 请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1000)) or b"{}")
                sid = str(body.get("id") or "")
                if g_scheduler:
                    g_scheduler.stop_sprint(sid)
                self._json({"stopped": True})
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/schedule/auto-resume":
            if not self._local_json_request() or g_state["demo"]:
                self._json({"error": "只接受本地面板的真实模式请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1000)) or b"{}")
                if not isinstance(body.get("enabled"), bool):
                    raise ValueError("enabled 必须是布尔值")
                with g_lock:
                    if body["enabled"]:
                        conn().execute("INSERT OR REPLACE INTO schedule_settings VALUES('auto_resume_since',?)", (str(time.time()),))
                    else:
                        conn().execute("DELETE FROM schedule_settings WHERE key='auto_resume_since'")
                        conn().execute("UPDATE schedule_rules SET status='cancelled' WHERE auto=1 AND status='waiting'")
                    conn().commit()
                self._json({"enabled": body["enabled"]})
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/schedule":
            if not self._local_json_request():
                self._json({"error": "只接受本地面板的 JSON 请求"}, 403)
                return
            if g_state["demo"]:
                self._json({"error": "演示模式不能执行排程"}, 400)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 30000:
                    raise ValueError("请求过大")
                body = json.loads(self.rfile.read(length) or b"{}")
                snapshots = all_task_snapshots(g_args.codex_home, g_args.zcode_home)
                with g_lock:
                    projects = available_projects(conn(), g_args.codex_home, g_args.zcode_home)
                rule = validate_rule(body, snapshots, g_args.codex_home, projects)
                if rule["kind"] == "new" and rule["trigger"] == "quota":
                    if rule.get("agent") == "zcode":
                        rule["quota_after"] = next_reset(read_zcode_quota())
                    else:
                        rule["quota_after"] = next_reset(read_quota())
                    if rule["quota_after"] is None:
                        raise ValueError("暂未获取到下一次额度刷新时间")
                with g_lock:
                    if rule["project_mode"] == "create" and conn().execute("""SELECT 1 FROM schedule_rules
                            WHERE kind='new' AND project_mode='create' AND cwd=? AND status IN ('waiting','running') LIMIT 1""",
                            (rule["cwd"],)).fetchone():
                        raise ValueError("这个新项目已经有一条待执行规则")
                    if rule["kind"] == "resume" and conn().execute("""SELECT 1 FROM schedule_rules
                            WHERE kind='resume' AND thread_id=? AND after_turn_id=?
                            AND status IN ('waiting','running') LIMIT 1""",
                            (rule["thread_id"], rule["after_turn_id"])).fetchone():
                        raise ValueError("这个任务的当前轮次已有续跑规则")
                    conn().execute("""INSERT INTO schedule_rules
                        (id,kind,trigger,thread_id,agent,attempts,cwd,project_mode,project_id,project_name,project_parent,
                         prompt,run_at,quota_after,after_turn_id,after_mtime,
                         status,auto,created_at,started_at,finished_at,error,output)
                         VALUES(:id,:kind,:trigger,:thread_id,:agent,:attempts,:cwd,:project_mode,:project_id,:project_name,:project_parent,
                                :prompt,:run_at,:quota_after,:after_turn_id,:after_mtime,
                                :status,:auto,:created_at,:started_at,:finished_at,:error,:output)""", rule)
                    conn().commit()
                if rule["trigger"] == "immediate" and g_scheduler:
                    g_scheduler.dispatch(rule)
                self._json({"rule": rule}, 201)
            except (ValueError, TypeError, RuntimeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/schedule/cancel":
            if not self._local_json_request():
                self._json({"error": "只接受本地面板的 JSON 请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1000)) or b"{}")
                with g_lock:
                    changed = conn().execute("UPDATE schedule_rules SET status='cancelled' WHERE id=? AND status='waiting'",
                                             (str(body.get("id") or ""),)).rowcount
                    conn().commit()
                self._json({"cancelled": bool(changed)})
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/probe/clear":
            if not self._local_json_request() or g_state["demo"]:
                self._json({"error": "只接受本地面板的真实模式请求"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 1000)) or b"{}")
                with g_lock:
                    deleted = clear_probes(conn(), str(body.get("agent") or ""))
                self._json({"cleared": deleted})
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            return
        if self.path.split("?")[0] == "/api/probe":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            model = (body.get("model") or "").strip()
            if not model:
                self._json({"error": "缺少 model"}, 400)
                return
            if (body.get("agent") or "codex") == "zcode":
                result = run_zcode_probe(model)
            else:
                result = run_probe(g_args.codex_home, model)
            self._json(result)
            return
        self.send_response(404)
        self.end_headers()


# ---------------------------------------------------------------- 入口

conn_inst = None
g_args = None
g_scheduler = None


def conn():
    global conn_inst
    if conn_inst is None:
        conn_inst = db_connect(db_path())
        if g_state["demo"]:
            seed_demo(conn_inst)
    return conn_inst


def db_path():
    return os.path.join(APP_DIR, "demo.db" if g_state["demo"] else "state.db")


def detect_agents(codex_home, zcode_home):
    """auto 时按本机数据目录是否存在选择要监控的 agent。"""
    agents = []
    if os.path.isdir(os.path.join(codex_home, "sessions")):
        agents.append("codex")
    if os.path.isfile(os.path.join(zcode_home, "cli", "db", "db.sqlite")):
        agents.append("zcode")
    return agents or ["codex"]


def main():
    global g_args, g_last_scan, g_scheduler
    ap = argparse.ArgumentParser(description="Agent Helper —— 本地监控 Codex / ZCode 使用情况并安排 Codex 任务")
    ap.add_argument("--port", type=int, default=8787, help="本地网页端口（默认 8787）")
    ap.add_argument("--codex-home", default=os.path.join(HOME, ".codex"), help="Codex 主目录（默认 ~/.codex）")
    ap.add_argument("--zcode-home", default=os.path.join(HOME, ".zcode"), help="ZCode 主目录（默认 ~/.zcode）")
    ap.add_argument("--agents", default="auto", help="要监控的 agent：auto（默认，按本机目录自动探测）、或逗号分隔的 codex,zcode")
    ap.add_argument("--max-age-days", type=int, default=30, help="只解析最近 N 天的会话日志，0=全部（默认 30）")
    ap.add_argument("--demo", action="store_true", help="使用内置演示数据（不读取真实日志）")
    ap.add_argument("--scan-only", action="store_true", help="只扫描解析并打印摘要，不启动网页")
    ap.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    g_args = ap.parse_args()
    g_state["demo"] = g_args.demo

    if g_args.agents == "auto":
        g_args.agents = detect_agents(g_args.codex_home, g_args.zcode_home)
    else:
        g_args.agents = [a.strip() for a in g_args.agents.split(",") if a.strip() in ("codex", "zcode")]
        if not g_args.agents:
            g_args.agents = ["codex"]

    conn_ = conn()
    notes = []
    with g_lock:
        stats = {"files": 0, "turns": 0, "errors": 0}
        if not g_args.demo:
            if "codex" in g_args.agents:
                stats = scan_sessions(conn_, g_args.codex_home, g_args.max_age_days)
                if stats.get("note"):
                    notes.append(stats["note"])
            if "zcode" in g_args.agents:
                zstats = import_zcode(conn_, g_args.zcode_home, g_args.max_age_days)
                print("[agent-helper] zcode 已导入 %d 个会话、%d 轮" % (zstats["files"], zstats["turns"]))
                if zstats.get("note"):
                    notes.append(zstats["note"])
                try:
                    ensure_zcode_provider_config()
                except Exception as exc:
                    notes.append("ZCode 无头执行环境初始化失败（排程将无法执行 ZCode 任务）: " + str(exc))
        else:
            stats = {"files": 0}
        g_last_scan = time.time()
    n_turn = conn_.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    n_probe = conn_.execute("SELECT COUNT(*) FROM probes").fetchone()[0]
    print("[agent-helper] 已解析 %d 个文件，累计 %d 轮会话、%d 次探针" %
          (stats.get("files", 0), n_turn, n_probe))
    for note in notes:
        print("[agent-helper] " + note)
    if g_args.scan_only:
        for agent in g_args.agents:
            top = conn_.execute("""SELECT served, COUNT(*) n FROM turns WHERE agent=?
                                   GROUP BY served ORDER BY n DESC LIMIT 5""", (agent,)).fetchall()
            if top:
                print("  [%s]" % agent)
                for r in top:
                    print("    %-24s %d 轮" % (r[0], r[1]))
        return

    # 实时额度只支持 Codex；ZCode 排程采用中断后定时重试
    if "codex" in g_args.agents or "zcode" in g_args.agents:
        g_scheduler = Scheduler(conn_, g_lock, g_args.codex_home, g_args.demo,
                                zcode_home=g_args.zcode_home, agents=g_args.agents)
        g_scheduler.start()

    server = ThreadingHTTPServer(("127.0.0.1", g_args.port), Handler)
    url = "http://127.0.0.1:%d" % g_args.port
    print("[agent-helper] 面板地址: %s  （Ctrl+C 退出）" % url)
    if not g_args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
