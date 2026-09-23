"""Local, opt-in Codex task scheduling. No prompts are sent without a saved rule."""
import glob
import getpass
import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone


TICK_SECONDS = 5
QUOTA_SECONDS = 10
OVERVIEW_QUOTA_SECONDS = 30
QUOTA_ERRORS = ("usage limit", "rate limit", "limit reached", "quota", "try again after")
STALE_TURN_SECONDS = 7 * 86400
ZCODE_RETRY_SECONDS = 300
ZCODE_MAX_ATTEMPTS = 8
ZCODE_CLI_CANDIDATES = ("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs",)
ZCODE_PROVIDER_ID = "helper-local"
ZCODE_MODELS = ("GLM-5.3", "GLM-5.3-Flash")
_snapshot_cache = {}


# ZCode 无头 CLI 缺省没有可用 provider（模型选择由桌面 App 把守）。做法：解密本机
# coding-plan API key（App 的 enc:v1 AES-256-GCM 信封，密钥为本机确定性 fallback），
# 生成一份 personal provider 配置（含 defaultModelSelection），用环境变量喂给 CLI，
# 不改动 ZCode 自身的任何文件。
ZCODE_DECRYPT_NODE = r'''
const crypto=require("crypto"),fs=require("fs"),os=require("os");
const cred=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));
const secret=process.env.ZCODE_CREDENTIAL_SECRET||
  `zcode-credential-fallback:${process.platform}:${os.homedir()}:${os.userInfo().username}`;
const key=crypto.createHash("sha256").update(secret).digest();
for(const[k,v]of Object.entries(cred)){
  if(!k.includes("coding-plan")||!k.endsWith(":api-key"))continue;
  if(!v.startsWith("enc:v1:")){process.stdout.write(v);process.exit(0)}
  try{
    const[n,t,c]=v.slice(7).split(".");
    const d=crypto.createDecipheriv("aes-256-gcm",key,Buffer.from(n,"base64url"));
    d.setAuthTag(Buffer.from(t,"base64url"));
    process.stdout.write(Buffer.concat([d.update(Buffer.from(c,"base64url")),d.final()]).toString("utf8"));
    process.exit(0);
  }catch(e){}
}
process.exit(1);
'''


def _state_db(codex_home):
    path = os.path.join(codex_home, "state_5.sqlite")
    if not os.path.isfile(path):
        return None
    try:
        db = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        return db
    except sqlite3.Error:
        return None


def _zcode_db(zcode_home):
    path = os.path.join(zcode_home, "cli", "db", "db.sqlite")
    if not os.path.isfile(path):
        return None
    try:
        db = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        return db
    except sqlite3.Error:
        return None


def zcode_bin():
    env = os.environ.get("ZCODE_BIN")
    if env and os.path.isfile(env):
        return env
    for path in ZCODE_CLI_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


NODE_CANDIDATES = ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node",
                   "/opt/local/bin/node", os.path.expanduser("~/.local/bin/node"),
                   os.path.expanduser("~/n/bin/node"))


def node_bin():
    """Locate a node executable; packaged apps have a minimal PATH without homebrew."""
    env = os.environ.get("ZCODE_NODE_BIN") or os.environ.get("NODE_BIN")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("node")
    if found:
        return found
    candidates = list(NODE_CANDIDATES)
    try:
        candidates += sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")),
                             key=lambda p: os.path.getmtime(p), reverse=True)
    except OSError:
        pass
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


def zcode_cmd():
    """Command vector to run ZCode headless; None when the app/CLI is absent."""
    path = zcode_bin()
    if not path:
        return None
    node = node_bin()
    if not node:
        return None
    return [node, path] if path.endswith(".cjs") else [path]


def helper_data_dir():
    return os.path.join(os.path.expanduser("~"), ".codex-model-watch")


def zcode_api_key(credentials_path=None):
    """Decrypt the local coding-plan API key from ZCode's credential store."""
    path = credentials_path or os.path.join(os.path.expanduser("~"), ".zcode", "v2", "credentials.json")
    if not os.path.isfile(path):
        raise RuntimeError("未找到 ZCode 凭证文件: " + path)
    node = node_bin()
    if not node:
        raise RuntimeError("未找到 node 运行时（无法解密 ZCode 凭证），可设置 ZCODE_NODE_BIN 指向 node")
    proc = subprocess.run([node, "-e", ZCODE_DECRYPT_NODE, path],
                          capture_output=True, text=True, timeout=15)
    key = (proc.stdout or "").strip()
    if proc.returncode != 0 or not key:
        raise RuntimeError("无法从 ZCode 凭证解密 coding-plan API key（登录态或格式变化）")
    if key.count(".") != 1:
        raise RuntimeError("解密出的 API key 不是 id.secret 形式，无法用于请求签名")
    return key


def ensure_zcode_provider_config(credentials_path=None, force=False):
    """Write the personal provider config that unlocks headless model turns. Returns its path."""
    out_path = os.path.join(helper_data_dir(), "zcode-provider-config.json")
    if not force and os.path.isfile(out_path):
        try:
            with open(out_path) as fh:
                if ZCODE_PROVIDER_ID in fh.read():
                    return out_path
        except OSError:
            pass  # 缓存缺失或声明的是旧 provider id，重新生成
    key = zcode_api_key(credentials_path)
    config = {
        "schemaVersion": 1,
        "config": {
            "providerConfigRules": {
                "providerRules": [{
                    "providerId": ZCODE_PROVIDER_ID,
                    "providerName": "Codex Helper Local",
                    "enabled": True,
                    "config": {
                        "group": "standard-personal",
                        "access": {"type": "zhipu-coding-plan-api-key", "apiKey": key},
                        "api": {"type": "anthropic-messages",
                                "baseUrl": "https://open.bigmodel.cn/api/anthropic"},
                        "personalModelIds": list(ZCODE_MODELS),
                    },
                }]
            },
            "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
            "providerOrder": [ZCODE_PROVIDER_ID],
            "defaultModelSelection": {"providerId": ZCODE_PROVIDER_ID, "modelId": ZCODE_MODELS[0]},
        },
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=1)
    os.chmod(out_path, 0o600)
    return out_path


def zcode_env(credentials_path=None):
    """Environment overlay that makes headless zcode.cjs runnable; (None, error) when unavailable."""
    try:
        config = ensure_zcode_provider_config(credentials_path)
    except Exception as exc:
        return None, str(exc)
    env = {"ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": config}
    cli = zcode_bin()
    builtin = os.path.normpath(os.path.join(os.path.dirname(cli), "..", "config", "provider",
                                            "zcode-builtin.json")) if cli else ""
    if builtin and os.path.isfile(builtin):
        env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = builtin
    return env, ""


def zcode_rewrite_resume_selection(session_id, zcode_home=None):
    """无头 resume 桌面创建的 ZCode 会话前，临时改写它的模型选择。

    桌面会话把模型选择存在 session_entry(id=<sess>:runtime-model-selection)，
    providerId 指向账号 provider（account:…），无头环境不会注册它，CLI 直接报
    "Model creation failed"（底层 Select a model before continuing）。这里把
    providerId 改写为注入 provider（同一个 coding-plan key、同一网关），并返回
    原始 data 供跑完还原；无 entry、已指向注入 provider 或数据库不可写时返回
    None（调用方无需还原）。
    """
    home = zcode_home or os.path.join(os.path.expanduser("~"), ".zcode")
    path = os.path.join(home, "cli", "db", "db.sqlite")
    entry_id = session_id + ":runtime-model-selection"
    if not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect(path, timeout=3)
        try:
            row = conn.execute("SELECT data FROM session_entry WHERE id=?", (entry_id,)).fetchone()
            if not row:
                return None
            original = row[0]
            parsed = json.loads(original or "null")
            inner = parsed.get("modelSelection") if isinstance(parsed, dict) else None
            if not isinstance(inner, dict) or inner.get("providerId") == ZCODE_PROVIDER_ID:
                return None
            if inner.get("modelId") not in ZCODE_MODELS:
                inner["modelId"] = ZCODE_MODELS[0]
            inner["providerId"] = ZCODE_PROVIDER_ID
            rewritten = json.dumps({"modelSelection": inner}, ensure_ascii=False)
            conn.execute("UPDATE session_entry SET data=?, time_updated=? WHERE id=?",
                         (rewritten, int(time.time() * 1000), entry_id))
            conn.commit()
            return original
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, OSError):
        return None


def zcode_restore_resume_selection(session_id, original, zcode_home=None):
    """把 zcode_rewrite_resume_selection 改写过的模型选择还原成原值。"""
    if original is None:
        return
    home = zcode_home or os.path.join(os.path.expanduser("~"), ".zcode")
    path = os.path.join(home, "cli", "db", "db.sqlite")
    entry_id = session_id + ":runtime-model-selection"
    try:
        conn = sqlite3.connect(path, timeout=3)
        try:
            conn.execute("UPDATE session_entry SET data=? WHERE id=?", (original, entry_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


ZCODE_QUOTA_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
_ZCODE_UNIT_MINS = {2: 1, 3: 60, 4: 1440, 6: 10080}  # observed: 3&number=5 → 5h window, 6&number=1 → week


def zcode_usage_raw(credentials_path=None, timeout=10):
    """Fetch ZCode plan quota windows and normalize them to the Codex rateLimits shape.

    GET {ZCODE_QUOTA_URL} with Authorization: <coding-plan api key>. Response limits are
    CREDIT_LIMIT entries; the smallest window becomes "primary", the other "secondary".
    """
    import urllib.request
    import urllib.error
    key = zcode_api_key(credentials_path)
    req = urllib.request.Request(ZCODE_QUOTA_URL, headers={"Authorization": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") != 200:
        raise RuntimeError("ZCode 额度接口返回异常: " + str(payload.get("msg") or payload.get("code"))[:120])
    windows = []
    for limit in (payload.get("data") or {}).get("limits") or []:
        mins = _ZCODE_UNIT_MINS.get(limit.get("unit"))
        pct = limit.get("percentage")
        if mins is None or pct is None:
            continue
        windows.append({"windowDurationMins": mins * int(limit.get("number") or 1),
                        "usedPercent": float(pct),
                        "resetsAt": (limit.get("nextResetTime") or 0) / 1000 or None})
    windows.sort(key=lambda w: w["windowDurationMins"])
    if not windows:
        raise RuntimeError("ZCode 额度响应中没有可用窗口")
    names = ["primary", "secondary"]
    return {"rateLimits": {names[i]: w for i, w in enumerate(windows[:2])}}


def read_zcode_quota(credentials_path=None, timeout=10):
    """Alias kept for symmetry with read_quota()."""
    return zcode_usage_raw(credentials_path, timeout)


def project_catalog(codex_home, include_mirrors=False):
    """Read saved local Codex projects; never mutate Codex's private state database."""
    db = _state_db(codex_home)
    if db is None:
        return []
    try:
        rows = db.execute("""SELECT p.id, p.name, r.path FROM projects p
            JOIN project_roots r ON r.project_id=p.id AND r.position=0 ORDER BY p.position""").fetchall()
        return [{"id": row["id"], "name": row["name"], "path": row["path"]}
                for row in rows if row["path"] and os.path.isdir(row["path"])
                and (include_mirrors or "/.codex/.chatgpt-projects/" not in row["path"])]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def zcode_recent_dirs(zcode_home, days=30):
    """Distinct working directories of recent ZCode sessions, as pseudo projects."""
    db = _zcode_db(zcode_home)
    if db is None:
        return []
    cutoff = int((time.time() - days * 86400) * 1000)
    try:
        rows = db.execute("""SELECT DISTINCT directory FROM session
            WHERE time_archived IS NULL AND directory!='' AND time_updated>=?
            ORDER BY time_updated DESC LIMIT 30""", (cutoff,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    result = []
    for row in rows:
        path = os.path.realpath(os.path.expanduser(row["directory"]))
        if os.path.isdir(path) and not any(p["path"] == path for p in result):
            result.append({"id": "zcode-dir:" + path, "name": os.path.basename(path), "path": path})
    return result


def available_projects(conn, codex_home, zcode_home=None):
    saved = project_catalog(codex_home)
    paths = {os.path.realpath(p["path"]) for p in saved}
    try:
        for row in conn.execute("SELECT id,name,path FROM helper_projects ORDER BY created_at DESC"):
            path = os.path.realpath(row["path"])
            if os.path.isdir(path) and path not in paths:
                saved.append({"id": row["id"], "name": row["name"], "path": path})
                paths.add(path)
    except sqlite3.Error:
        pass
    if zcode_home:
        for project in zcode_recent_dirs(zcode_home):
            if project["path"] not in paths:
                saved.append(project)
                paths.add(project["path"])
    return saved


def thread_titles(codex_home):
    db = _state_db(codex_home)
    if db is None:
        return {}
    try:
        rows = db.execute("""SELECT id, name, title, preview, source, rollout_path, cwd, project_id
            FROM threads WHERE archived=0 ORDER BY updated_at DESC""").fetchall()
        return {row["id"]: {"title": (row["name"] or row["title"] or row["preview"] or "").strip(),
                             "source": row["source"], "rollout_path": row["rollout_path"],
                             "cwd": row["cwd"], "project_id": row["project_id"]}
                for row in rows if not str(row["source"] or "").startswith("{")}
    except sqlite3.Error:
        return {}
    finally:
        db.close()


def codex_bin():
    env = os.environ.get("CODEX_BIN")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("codex")
    if found:
        return found
    for path in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex", "/usr/bin/codex",
                 os.path.expanduser("~/.local/bin/codex"),
                 os.path.expanduser("~/.codex/bin/codex")):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return os.path.expanduser("~/.local/bin/codex")


def read_quota(timeout=12):
    """Use the documented app-server read API; never infer a reset from an old log."""
    proc = subprocess.Popen([codex_bin(), "app-server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)
    try:
        for message in (
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-helper", "version": "0.3.3"}}},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read"},
        ):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                result = json.loads(line)
            except ValueError:
                continue
            if result.get("id") == 2:
                if "error" in result:
                    raise RuntimeError(result["error"].get("message", "额度读取失败"))
                return result.get("result") or {}
        raise RuntimeError("额度读取超时")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        if proc.stdin:
            proc.stdin.close()
        if proc.stdout:
            proc.stdout.close()


def quota_ready(data):
    limits = data.get("rateLimitsByLimitId") or {}
    bucket = limits.get("codex") or data.get("rateLimits") or {}
    if not bucket:
        return False
    if bucket.get("rateLimitReachedType"):
        return False
    if not any((bucket.get(name) or {}).get("usedPercent") is not None for name in ("primary", "secondary")):
        return False
    for name in ("primary", "secondary"):
        window = bucket.get(name)
        if window and window.get("usedPercent") is not None and window["usedPercent"] >= 100:
            return False
    return True


def next_reset(data):
    limits = data.get("rateLimitsByLimitId") or {}
    bucket = limits.get("codex") or data.get("rateLimits") or {}
    windows = [bucket.get(name) or {} for name in ("primary", "secondary")]
    exhausted = [w for w in windows if (w.get("usedPercent") or 0) >= 100 and w.get("resetsAt")]
    candidates = exhausted or [w for w in windows if w.get("resetsAt")]
    if not candidates:
        return None
    return max(w["resetsAt"] for w in candidates) if exhausted else min(w["resetsAt"] for w in candidates)


def quota_snapshot(data, sampled_at):
    """Return only the current Codex limit windows needed by the dashboard."""
    limits = data.get("rateLimitsByLimitId") or {}
    bucket = limits.get("codex") or data.get("rateLimits") or {}
    windows = {}
    for name in ("primary", "secondary"):
        window = bucket.get(name) or {}
        windows[name] = {key: window.get(key) for key in ("usedPercent", "windowDurationMins", "resetsAt")}
    if all(window["usedPercent"] is None for window in windows.values()):
        return None
    return {"sampled_at": sampled_at, **windows}


def task_snapshots(codex_home, days=30):
    cutoff = time.time() - days * 86400
    result = {}
    titles = thread_titles(codex_home)
    paths = glob.glob(os.path.join(codex_home, "sessions", "**", "*.jsonl"), recursive=True)
    paths = sorted(paths, key=lambda path: os.path.getmtime(path), reverse=True)
    recent_paths = [path for path in paths[:200] if os.path.getmtime(path) >= cutoff]
    # Codex's task index covers older work that is still present in its project list.
    indexed_paths = [info["rollout_path"] for info in titles.values() if info["rollout_path"]]
    for path in dict.fromkeys([*recent_paths, *indexed_paths]):
        try:
            stat = os.stat(path)
            cached = _snapshot_cache.get(path)
            stamp = (stat.st_mtime_ns, stat.st_size)
            if cached and cached[0] == stamp:
                item = cached[1]
                result[item["id"]] = item
                continue
            session = None
            last_turn = None
            title = ""
            cwd = ""
            with open(path, encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    payload = item.get("payload") or {}
                    kind = item.get("type")
                    if kind == "session_meta":
                        session = payload.get("id")
                        cwd = payload.get("cwd") or cwd
                    elif kind == "turn_context":
                        cwd = payload.get("cwd") or cwd
                    elif kind == "event_msg":
                        event = payload.get("type")
                        if event == "user_message" and not title:
                            title = str(payload.get("message") or "").replace("\n", " ")[:90]
                        elif event == "task_started":
                            last_turn = {"id": payload.get("turn_id"), "status": "active", "error": ""}
                        elif event == "task_complete":
                            error = payload.get("error") or {}
                            if isinstance(error, dict):
                                error = error.get("message") or ""
                            try:
                                finished_ts = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")).timestamp()
                            except (KeyError, ValueError, TypeError):
                                finished_ts = None
                            last_turn = {"id": payload.get("turn_id"), "status": "failed" if error else "completed",
                                         "error": str(error)[:500], "finished_ts": finished_ts}
            if not session:
                # Rollout filenames end in the session UUID.
                match = re.search(r"([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\.jsonl$", path)
                session = match.group(1) if match else None
            if session:
                item = {"id": session, "title": title,
                        "cwd": cwd, "turn": last_turn, "mtime": stat.st_mtime}
                result[session] = item
                _snapshot_cache[path] = (stamp, item)
        except OSError:
            continue
    for session, info in titles.items():
        if session not in result:
            result[session] = {"id": session, "title": info["title"], "cwd": info.get("cwd") or "",
                               "turn": None, "mtime": 0}
    projects = project_catalog(codex_home, include_mirrors=True)
    projects_by_id = {p["id"]: p for p in projects}
    for session, task in list(result.items()):
        info = titles.get(session)
        if titles and info is None:
            del result[session]
            continue
        info = info or {}
        # A few older rollouts never recorded task_complete. After a week without
        # any log update, show them as stopped so a deliberate follow-up can run.
        if (task.get("turn") or {}).get("status") == "active" and time.time() - task["mtime"] > STALE_TURN_SECONDS:
            task["turn"]["status"] = "stopped"
        task["title"] = info.get("title") or task["title"] or "任务 " + session[:8]
        cwd = task["cwd"] or info.get("cwd") or ""
        task["cwd"] = cwd
        match = projects_by_id.get(info.get("project_id"))
        if match is None:
            match = next((p for p in sorted(projects, key=lambda p: len(p["path"]), reverse=True)
                          if cwd == p["path"] or cwd.startswith(p["path"] + os.sep)), None)
        task["project_name"] = match["name"] if match else os.path.basename(cwd.rstrip(os.sep)) or "未归类"
        task["project_id"] = match["id"] if match else None
        task["project_path"] = match["path"] if match else cwd
    return sorted(result.values(), key=lambda item: item["mtime"], reverse=True)


ZCODE_TURN_STATUS = {"running": "active", "completed": "completed", "error": "failed", "cancelled": "stopped"}


def zcode_task_snapshots(zcode_home, days=30):
    """Unarchived ZCode sessions mapped to the same shape as Codex task_snapshots."""
    db = _zcode_db(zcode_home)
    if db is None:
        return []
    cutoff = int((time.time() - days * 86400) * 1000)
    try:
        rows = db.execute("""
            SELECT s.id, s.title, s.directory, s.time_updated,
                   t.turn_id AS turn_id, t.status AS turn_status, t.completed_at,
                   t.error_type AS error_type
            FROM session s
            LEFT JOIN turn_usage t ON t.session_id=s.id AND t.started_at=(
                SELECT MAX(started_at) FROM turn_usage WHERE session_id=s.id)
            WHERE s.time_archived IS NULL AND s.parent_id IS NULL AND s.time_updated>=?
            ORDER BY s.time_updated DESC""", (cutoff,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    result = []
    for row in rows:
        if not row["id"]:
            continue
        turn = None
        if row["turn_id"]:
            turn = {"id": row["turn_id"],
                    "status": ZCODE_TURN_STATUS.get(row["turn_status"] or "", "stopped"),
                    "error": row["error_type"] or "",
                    "finished_ts": (row["completed_at"] or 0) / 1000 or None}
        cwd = row["directory"] or ""
        result.append({"id": row["id"], "title": (row["title"] or "").strip(),
                       "cwd": cwd, "turn": turn,
                       "mtime": (row["time_updated"] or 0) / 1000,
                       "agent": "zcode",
                       "project_name": os.path.basename(cwd.rstrip(os.sep)) or "未归类",
                       "project_id": None, "project_path": cwd})
    return result


def all_task_snapshots(codex_home, zcode_home=None, days=30):
    snapshots = task_snapshots(codex_home, days)
    for item in snapshots:
        item.setdefault("agent", "codex")
    if zcode_home:
        snapshots = snapshots + zcode_task_snapshots(zcode_home, days)
    return snapshots


def validate_rule(body, snapshots, codex_home=None, projects=None):
    kind = body.get("kind")
    trigger = body.get("trigger")
    if kind not in ("new", "next", "resume"):
        raise ValueError("请选择任务类型")
    if (kind == "new" and trigger not in ("at", "quota", "after") or
            kind == "next" and trigger != "after" or
            kind == "resume" and trigger != "quota"):
        raise ValueError("任务类型与触发条件不匹配")
    agent = "zcode" if str(body.get("agent") or "").strip() == "zcode" else "codex"
    prompt = str(body.get("prompt") or "").strip()
    if kind == "resume" and not prompt:
        prompt = ("任务此前因额度或限流中断。请继续完成刚才中断的工作，"
                  "先检查现有进度，避免重复操作。" if agent == "zcode" else
                  "额度已恢复。请继续完成刚才因额度限制中断的工作，先检查现有进度，避免重复操作。")
    if not prompt or len(prompt) > 20000:
        raise ValueError("Prompt 需要填写，且不超过 20000 字")
    thread_id = str(body.get("thread_id") or "").strip()
    if kind != "new" or trigger == "after":
        target = next((item for item in snapshots if item["id"] == thread_id), None)
        if target is None:
            raise ValueError("请选择本机已有的 Codex / ZCode 任务")
        agent = target.get("agent", "codex")
    if agent == "zcode" and kind == "new" and trigger == "quota":
        raise ValueError("ZCode 暂无实时额度接口，新任务请改用指定时间或关联任务完成触发")
    project_mode = project_id = project_name = project_parent = None
    if kind == "new":
        project_mode = body.get("project_mode")
        if project_mode == "existing":
            project_id = str(body.get("project_id") or "").strip()
            catalog = projects if projects is not None else project_catalog(codex_home or
                      os.path.join(os.path.expanduser("~"), ".codex"))
            project = next((p for p in catalog if p["id"] == project_id), None)
            if not project:
                raise ValueError("请选择已保存的本地 Codex 项目")
            cwd, project_name = project["path"], project["name"]
        elif project_mode == "create":
            project_name = str(body.get("project_name") or "").strip()
            parent_input = str(body.get("project_parent") or "").strip()
            project_parent = os.path.realpath(os.path.expanduser(parent_input)) if parent_input else ""
            if (not project_name or len(project_name) > 80 or project_name in (".", "..") or
                    re.search(r"[\x00-\x1f/\\:]", project_name)):
                raise ValueError("项目名称不能包含路径分隔符或控制字符，且不超过 80 字")
            if not os.path.isdir(project_parent):
                raise ValueError("项目保存位置不存在")
            cwd = os.path.join(project_parent, project_name)
            if os.path.lexists(cwd):
                raise ValueError("该项目目录已存在；请选择已有项目或更换名称")
        else:
            # Compatibility with rules created by older helper versions.
            project_mode = "directory"
            cwd_input = str(body.get("cwd") or "").strip()
            cwd = os.path.realpath(os.path.expanduser(cwd_input)) if cwd_input else ""
            if not os.path.isdir(cwd):
                raise ValueError("工作目录不存在")
            project_name = os.path.basename(cwd)
    else:
        cwd = next(item["cwd"] for item in snapshots if item["id"] == thread_id)
    run_at = None
    if trigger == "at":
        try:
            run_at = datetime.fromisoformat(str(body.get("run_at") or "").replace("Z", "+00:00"))
            if run_at.tzinfo is None:
                raise ValueError()
            run_at = run_at.timestamp()
        except ValueError:
            raise ValueError("请输入带时区的执行时间")
    target = next((item for item in snapshots if item["id"] == thread_id), None)
    current_status = (target.get("turn") or {}).get("status") if target else None
    if kind == "next" and current_status not in ("active", "completed", "failed", "stopped"):
        raise ValueError("请选择状态已确认的具体任务")
    if kind == "new" and trigger == "after" and (target.get("turn") or {}).get("status") != "active":
        raise ValueError("请选择正在运行的关联任务")
    if kind == "resume":
        if current_status not in ("active", "completed", "failed", "stopped"):
            raise ValueError("请选择状态已确认的具体任务")
    if kind in ("next", "resume") and current_status in ("completed", "failed", "stopped") and not body.get("wait_for_quota"):
        trigger = "immediate"
    return {"id": str(uuid.uuid4()), "kind": kind, "trigger": trigger, "thread_id": thread_id,
            "agent": agent, "cwd": cwd, "project_mode": project_mode, "project_id": project_id,
            "project_name": project_name, "project_parent": project_parent,
            "prompt": prompt, "run_at": run_at, "quota_after": None,
            "after_turn_id": (target.get("turn") or {}).get("id") if target else None,
            "after_mtime": target["mtime"] if target else None,
            "status": "waiting", "auto": 0, "created_at": time.time(), "started_at": None,
            "finished_at": None, "error": "", "output": "", "attempts": 0}


def init_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS sprints(
        id TEXT PRIMARY KEY, name TEXT, agent TEXT DEFAULT 'zcode',
        kind TEXT, start_hm TEXT, end_hm TEXT, start_at REAL, end_at REAL,
        cwd TEXT, concurrency INTEGER DEFAULT 1,
        project_mode TEXT, project_name TEXT, project_parent TEXT,
        status TEXT DEFAULT 'waiting', created_at REAL, stopped_at REAL)""")
    sprint_cols = {row[1] for row in conn.execute("PRAGMA table_info(sprints)")}
    for col in ("project_mode", "project_name", "project_parent", "manual"):
        if col not in sprint_cols:
            conn.execute("ALTER TABLE sprints ADD COLUMN " + col +
                         (" INTEGER DEFAULT 0" if col == "manual" else " TEXT"))
    conn.execute("""CREATE TABLE IF NOT EXISTS sprint_tasks(
        id TEXT PRIMARY KEY, sprint_id TEXT, prompt TEXT, position INTEGER,
        status TEXT DEFAULT 'pending', session_id TEXT, error TEXT, output TEXT,
        started_at REAL, finished_at REAL, attempts INTEGER DEFAULT 0)""")
    # 应用重启时把上一次运行遗留的 running 任务放回队列
    conn.execute("UPDATE sprint_tasks SET status='pending', started_at=NULL WHERE status='running'")
    conn.execute("CREATE TABLE IF NOT EXISTS schedule_settings(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS helper_projects(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE, created_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_rules(
        id TEXT PRIMARY KEY, kind TEXT, trigger TEXT, thread_id TEXT, cwd TEXT, prompt TEXT,
        project_mode TEXT, project_id TEXT, project_name TEXT, project_parent TEXT,
        run_at REAL, quota_after REAL, after_turn_id TEXT, after_mtime REAL, status TEXT, auto INTEGER DEFAULT 0, created_at REAL,
        started_at REAL, finished_at REAL, error TEXT, output TEXT)""")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(schedule_rules)")}
    if "quota_after" not in cols:
        conn.execute("ALTER TABLE schedule_rules ADD COLUMN quota_after REAL")
    if "auto" not in cols:
        conn.execute("ALTER TABLE schedule_rules ADD COLUMN auto INTEGER DEFAULT 0")
    if "agent" not in cols:
        conn.execute("ALTER TABLE schedule_rules ADD COLUMN agent TEXT DEFAULT 'codex'")
    if "attempts" not in cols:
        conn.execute("ALTER TABLE schedule_rules ADD COLUMN attempts INTEGER DEFAULT 0")
    for name in ("project_mode", "project_id", "project_name", "project_parent"):
        if name not in cols:
            conn.execute("ALTER TABLE schedule_rules ADD COLUMN " + name + " TEXT")
    conn.execute("UPDATE schedule_rules SET status='unknown', error='应用上次退出时任务仍在运行，请检查 Codex 任务后再重建规则' WHERE status='running'")
    conn.commit()


def parse_hm(text):
    """Parse 'HH:MM' local wall time; returns (h, m)."""
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", str(text or "").strip())
    if not m:
        raise ValueError("时间格式应为 HH:MM")
    return int(m.group(1)), int(m.group(2))


def sprint_window(sp, now=None):
    """Active or next [start, end) window in epoch seconds for a sprint row."""
    now = time.time() if now is None else now
    if sp["kind"] == "once":
        return sp["start_at"], sp["end_at"]

    def at(hm, base):
        h, m = hm
        d = datetime.fromtimestamp(base).replace(hour=h, minute=m, second=0, microsecond=0)
        return d.timestamp()

    sh, sm = parse_hm(sp["start_hm"])
    eh, em = parse_hm(sp["end_hm"])
    cands = []
    for base in (now - 86400, now, now + 86400):
        s, e = at((sh, sm), base), at((eh, em), base)
        if e <= s:
            e += 86400  # 窗口跨午夜
        cands.append((s, e))
    for a, b in cands:
        if a <= now < b:
            return a, b
    return min((c for c in cands if c[0] > now), key=lambda c: c[0])


def validate_sprint(body, projects=None):
    kind = body.get("kind")
    if kind not in ("daily", "once"):
        raise ValueError("请选择窗口类型（每日或一次性）")
    project_mode = body.get("project_mode") if body.get("project_mode") in ("existing", "create") else "existing"
    project_name = project_parent = None
    if project_mode == "create":
        project_name = str(body.get("project_name") or "").strip()
        parent_input = str(body.get("project_parent") or "").strip()
        project_parent = os.path.realpath(os.path.expanduser(parent_input)) if parent_input else ""
        if (not project_name or len(project_name) > 80 or project_name in (".", "..") or
                re.search(r"[\x00-\x1f/\\:]", project_name)):
            raise ValueError("新项目名称不能包含路径分隔符或控制字符，且不超过 80 字")
        if not os.path.isdir(project_parent):
            raise ValueError("项目保存位置不存在")
        cwd = os.path.join(project_parent, project_name)
        if os.path.lexists(cwd):
            raise ValueError("该项目目录已存在；请选择现有目录或更换名称")
    else:
        cwd_input = str(body.get("cwd") or "").strip()
        cwd = os.path.realpath(os.path.expanduser(cwd_input)) if cwd_input else ""
        if not os.path.isdir(cwd):
            raise ValueError("请选择任务工作目录")
    try:
        concurrency = int(body.get("concurrency") or 1)
    except (TypeError, ValueError):
        concurrency = 1
    concurrency = max(1, min(6, concurrency))
    prompts = [str(p).strip() for p in (body.get("prompts") or []) if str(p).strip()]
    if not prompts:
        raise ValueError("任务队列不能为空")
    if len(prompts) > 50:
        raise ValueError("单次最多 50 个任务")
    if any(len(p) > 20000 for p in prompts):
        raise ValueError("单个任务 Prompt 不能超过 20000 字")
    sp = {"id": str(uuid.uuid4()), "name": str(body.get("name") or "").strip()[:60] or "玩命蹬",
          "agent": "zcode", "kind": kind, "cwd": cwd, "concurrency": concurrency,
          "project_mode": project_mode, "project_name": project_name, "project_parent": project_parent,
          "status": "waiting", "created_at": time.time(), "stopped_at": None,
          "start_hm": None, "end_hm": None, "start_at": None, "end_at": None}
    if kind == "daily":
        parse_hm(body.get("start_hm"))  # 格式校验
        parse_hm(body.get("end_hm"))
        sp["start_hm"] = str(body.get("start_hm")).strip()
        sp["end_hm"] = str(body.get("end_hm")).strip()
    else:
        try:
            start = datetime.fromisoformat(str(body.get("start_at") or "").replace("Z", "+00:00")).timestamp()
            end = datetime.fromisoformat(str(body.get("end_at") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            raise ValueError("请输入带时区的窗口起止时间")
        if end <= start:
            raise ValueError("结束时间必须晚于开始时间")
        sp["start_at"], sp["end_at"] = start, end
    sp["tasks"] = [{"id": str(uuid.uuid4()), "sprint_id": sp["id"], "prompt": p,
                    "position": i, "status": "pending", "session_id": None, "error": None,
                    "output": None, "started_at": None, "finished_at": None, "attempts": 0}
                   for i, p in enumerate(prompts)]
    return sp


def rule_rows(conn):
    active = conn.execute("""SELECT * FROM schedule_rules WHERE status IN ('waiting','running')
        ORDER BY COALESCE(run_at,quota_after,created_at),created_at""").fetchall()
    history = conn.execute("""SELECT * FROM schedule_rules WHERE status NOT IN ('waiting','running')
        ORDER BY created_at DESC LIMIT 100""").fetchall()
    return [dict(row) for row in [*active, *history]]


class Scheduler:
    def __init__(self, conn, lock, codex_home, demo=False, zcode_home=None, agents=("codex",)):
        self.conn, self.lock, self.codex_home, self.demo = conn, lock, codex_home, demo
        self.zcode_home = zcode_home
        self.agents = list(agents)
        self.stop_event = threading.Event()
        self.quota = None
        self.quota_error = ""
        self.quota_at = 0
        self.quota_sampled_at = 0
        self.zcode_live = None
        self.zcode_quota = None
        self.zcode_error = ""
        self.zcode_quota_at = 0
        self.snapshots = []
        self.sprint_procs = {}
        self.thread = None

    def start(self):
        if self.demo:
            return
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                self.quota_error = str(exc)[:200]
            self.stop_event.wait(TICK_SECONDS)

    def tick(self):
        self.snapshots = all_task_snapshots(self.codex_home, self.zcode_home)
        with self.lock:
            setting = self.conn.execute("SELECT value FROM schedule_settings WHERE key='auto_resume_since'").fetchone()
            if setting:
                since = float(setting[0])
                for task in self.snapshots:
                    turn = task.get("turn") or {}
                    err_text = (turn.get("error") or "").replace("_", " ").lower()
                    if not turn.get("finished_ts") or turn["finished_ts"] < since or turn.get("status") != "failed" or not any(
                            x in err_text for x in QUOTA_ERRORS):
                        continue
                    exists = self.conn.execute("""SELECT 1 FROM schedule_rules WHERE kind='resume'
                        AND thread_id=? AND after_turn_id=? LIMIT 1""", (task["id"], turn.get("id"))).fetchone()
                    if not exists:
                        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": task["id"],
                                              "agent": task.get("agent", "codex"),
                                              "wait_for_quota": True}, [task])
                        rule["auto"] = 1
                        self.conn.execute("""INSERT INTO schedule_rules
                            (id,kind,trigger,thread_id,agent,cwd,prompt,run_at,quota_after,after_turn_id,after_mtime,
                             status,auto,created_at,started_at,finished_at,error,output)
                             VALUES(:id,:kind,:trigger,:thread_id,:agent,:cwd,:prompt,:run_at,:quota_after,:after_turn_id,:after_mtime,
                                    :status,:auto,:created_at,:started_at,:finished_at,:error,:output)""", rule)
                self.conn.commit()
            waiting = [dict(r) for r in self.conn.execute("""SELECT * FROM schedule_rules WHERE status='waiting'
                ORDER BY COALESCE(run_at,quota_after,created_at),created_at""")]
        needs_quota = any(r["trigger"] == "quota" for r in waiting)
        interval = QUOTA_SECONDS if needs_quota else OVERVIEW_QUOTA_SECONDS
        if "codex" in self.agents and time.time() - self.quota_at >= interval:
            self.quota_at = time.time()
            try:
                self.quota = read_quota()
                self.quota_sampled_at = time.time()
                self.quota_error = ""
            except Exception as exc:
                self.quota = None
                self.quota_error = str(exc)[:200]
        if "zcode" in self.agents and time.time() - self.zcode_quota_at >= interval:
            self.zcode_quota_at = time.time()
            try:
                self.zcode_quota = zcode_usage_raw()
                self.zcode_live = quota_snapshot(self.zcode_quota, time.time())
                self.zcode_error = ""
            except Exception as exc:
                self.zcode_quota = None
                self.zcode_live = None
                self.zcode_error = str(exc)[:200]
        by_id = {t["id"]: t for t in self.snapshots}
        for rule in waiting:
            target = by_id.get(rule["thread_id"])
            if rule["trigger"] == "at":
                due = time.time() >= rule["run_at"]
            elif rule["trigger"] == "immediate":
                due = target is not None and (target.get("turn") or {}).get("status") in ("completed", "failed", "stopped")
            elif rule["trigger"] == "quota":
                if rule["kind"] == "resume":
                    turn = (target or {}).get("turn") or {}
                    err_ok = turn.get("status") == "failed" and any(
                        x in (turn.get("error", "") or "").replace("_", " ").lower() for x in QUOTA_ERRORS)
                    if (rule.get("agent") or "codex") == "zcode":
                        # ZCode 续跑：优先用实时额度窗口判断；额度未知时退回固定间隔重试
                        if self.zcode_quota:
                            due = err_ok and quota_ready(self.zcode_quota)
                        else:
                            attempts = rule.get("attempts") or 0
                            wait_ok = attempts == 0 or time.time() - (rule["finished_at"] or 0) >= ZCODE_RETRY_SECONDS
                            due = err_ok and wait_ok
                    else:
                        due = err_ok and quota_ready(self.quota or {})
                else:
                    due = (rule["quota_after"] is not None and time.time() >= rule["quota_after"]
                           and quota_ready(self.quota or {}))
            else:
                turn = (target or {}).get("turn") or {}
                due = (target is not None and turn.get("status") == "completed" and
                       (turn.get("id") != rule["after_turn_id"] or target["mtime"] > (rule["after_mtime"] or 0)))
            if due and target and rule["kind"] != "new" and (target.get("turn") or {}).get("status") == "active":
                due = False
            if due:
                self.dispatch(rule)
        if "zcode" in self.agents:
            self.tick_sprints()

    # ---------------- 免费玩命蹬：ZCode 窗口队列 ----------------

    def tick_sprints(self):
        now = time.time()
        with self.lock:
            sprints = [dict(r) for r in self.conn.execute(
                "SELECT * FROM sprints WHERE status IN ('waiting','running')")]
        for sp in sprints:
            if sp.get("manual"):
                # 手动启动：无视窗口，跑完队列即收工
                if sp["status"] == "waiting":
                    with self.lock:
                        self.conn.execute("UPDATE sprints SET status='running' WHERE id=?", (sp["id"],))
                        self.conn.commit()
                self.fill_sprint_slots(sp)
                continue
            a, b = sprint_window(sp, now)
            if now < a:
                continue
            if now >= b:
                self.finish_sprint(sp["id"], "done", "时间窗口结束")
                continue
            if sp["status"] == "waiting":
                with self.lock:
                    self.conn.execute("UPDATE sprints SET status='running' WHERE id=?", (sp["id"],))
                    self.conn.commit()
            self.fill_sprint_slots(sp)

    def fill_sprint_slots(self, sp):
        slots = pending = []
        with self.lock:
            running = self.conn.execute(
                "SELECT COUNT(*) FROM sprint_tasks WHERE sprint_id=? AND status='running'",
                (sp["id"],)).fetchone()[0]
            slots = max(0, int(sp["concurrency"] or 1) - running)
            if slots:
                pending = [dict(r) for r in self.conn.execute(
                    "SELECT * FROM sprint_tasks WHERE sprint_id=? AND status='pending' ORDER BY position LIMIT ?",
                    (sp["id"], slots))]
            for t in pending:
                self.conn.execute(
                    "UPDATE sprint_tasks SET status='running', started_at=?, attempts=COALESCE(attempts,0)+1 WHERE id=?",
                    (time.time(), t["id"]))
            left = self.conn.execute(
                "SELECT COUNT(*) FROM sprint_tasks WHERE sprint_id=? AND status IN ('pending','running')",
                (sp["id"],)).fetchone()[0]
            if left == 0:
                self.conn.execute(
                    "UPDATE sprints SET status='done', stopped_at=? WHERE id=? AND status IN ('waiting','running')",
                    (time.time(), sp["id"]))
            self.conn.commit()
        for t in pending:
            self.launch_sprint_task(sp, t)

    def launch_sprint_task(self, sp, task):
        base = zcode_cmd()
        env, env_error = zcode_env()
        if sp.get("project_mode") == "create" and not os.path.isdir(sp["cwd"]):
            try:
                os.makedirs(sp["cwd"], exist_ok=True)
                with self.lock:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO helper_projects(id,name,path,created_at) VALUES(?,?,?,?)",
                        ("helper-" + sp["id"], sp.get("project_name") or os.path.basename(sp["cwd"]),
                         sp["cwd"], time.time()))
                    self.conn.commit()
            except OSError as exc:
                with self.lock:
                    self.conn.execute(
                        "UPDATE sprint_tasks SET status='failed', error=?, finished_at=? WHERE id=?",
                        ("创建项目目录失败: " + str(exc)[:200], time.time(), task["id"]))
                    self.conn.commit()
                return
        if base is None or env is None:
            with self.lock:
                self.conn.execute(
                    "UPDATE sprint_tasks SET status='failed', error=?, finished_at=? WHERE id=?",
                    (env_error or "未找到 ZCode CLI", time.time(), task["id"]))
                self.conn.commit()
            return
        cmd = [*base, "--prompt", task["prompt"], "--cwd", sp["cwd"], "--json"]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, cwd=sp["cwd"],
                                    env={**os.environ, **env}, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as exc:
            with self.lock:
                self.conn.execute(
                    "UPDATE sprint_tasks SET status='failed', error=?, finished_at=? WHERE id=?",
                    (str(exc)[:500], time.time(), task["id"]))
                self.conn.commit()
            return
        self.sprint_procs[task["id"]] = proc
        threading.Thread(target=self._watch_sprint_task, args=(sp["id"], sp["cwd"], task["id"], proc),
                         daemon=True).start()

    def _watch_sprint_task(self, sprint_id, cwd, task_id, proc):
        try:
            out, errout = proc.communicate()
        except Exception:
            out, errout = "", ""
        self.sprint_procs.pop(task_id, None)
        rc = proc.returncode
        sess = ""
        match = re.search(r"sess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                          out or "")
        if match:
            sess = match.group(0)
        with self.lock:
            row = self.conn.execute("SELECT status FROM sprint_tasks WHERE id=?", (task_id,)).fetchone()
            if row and row["status"] == "running":
                self.conn.execute(
                    "UPDATE sprint_tasks SET status=?, session_id=?, output=?, error=?, finished_at=? WHERE id=?",
                    ("done" if rc == 0 else "failed", sess or None, (out or "")[-2000:] or None,
                     (errout or "")[-800:] if rc else None, time.time(), task_id))
            self.conn.commit()
        # 任务完成立即补位，避免串行模式下最长 5 秒的空档
        try:
            sprint = None
            with self.lock:
                sprint = self.conn.execute(
                    "SELECT * FROM sprints WHERE id=?", (sprint_id,)).fetchone()
            if sprint and sprint["status"] == "running":
                a, b = sprint_window(sprint)
                if a <= time.time() < b:
                    self.fill_sprint_slots(dict(sprint))
        except Exception:
            pass

    def finish_sprint(self, sprint_id, status, note):
        """Terminate all running tasks, skip pending, close the sprint."""
        killed = []
        with self.lock:
            row = self.conn.execute("SELECT status FROM sprints WHERE id=?", (sprint_id,)).fetchone()
            if not row or row["status"] not in ("waiting", "running"):
                return
            self.conn.execute("UPDATE sprints SET status=?, stopped_at=? WHERE id=?",
                              (status, time.time(), sprint_id))
            for t in self.conn.execute(
                    "SELECT id FROM sprint_tasks WHERE sprint_id=? AND status='running'",
                    (sprint_id,)).fetchall():
                killed.append(t["id"])
                self.conn.execute(
                    "UPDATE sprint_tasks SET status='stopped', finished_at=?, error=? WHERE id=?",
                    (time.time(), note, t["id"]))
            self.conn.execute(
                "UPDATE sprint_tasks SET status='skipped', finished_at=? WHERE sprint_id=? AND status='pending'",
                (time.time(), sprint_id))
            self.conn.commit()
        procs = [(tid, self.sprint_procs.pop(tid)) for tid in killed if tid in self.sprint_procs]
        for _, proc in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        if procs:
            def kill_late():
                time.sleep(5)
                for _, proc in procs:
                    try:
                        if proc.poll() is None:
                            proc.kill()
                    except Exception:
                        pass
            threading.Thread(target=kill_late, daemon=True).start()

    def stop_sprint(self, sprint_id):
        self.finish_sprint(sprint_id, "stopped", "手动停止")

    def start_sprint(self, sprint_id, concurrency=None):
        """Manually start a waiting sprint (ignores its window) or retune a running one."""
        if concurrency is not None:
            concurrency = max(1, min(6, int(concurrency)))
        with self.lock:
            row = self.conn.execute("SELECT status FROM sprints WHERE id=?", (sprint_id,)).fetchone()
            if not row:
                raise ValueError("窗口不存在")
            if row["status"] not in ("waiting", "running"):
                raise ValueError("该窗口已结束，不能启动")
            if concurrency:
                self.conn.execute("UPDATE sprints SET concurrency=? WHERE id=?", (concurrency, sprint_id))
            if row["status"] == "waiting":
                self.conn.execute("UPDATE sprints SET manual=1, status='running' WHERE id=?", (sprint_id,))
            self.conn.commit()
        self.tick_sprints()

    def stop_all_sprints(self):
        with self.lock:
            ids = [r[0] for r in self.conn.execute(
                "SELECT id FROM sprints WHERE status IN ('waiting','running')")]
        for sid in ids:
            self.finish_sprint(sid, "stopped", "手动全部中止")
        return len(ids)

    def delete_sprint(self, sprint_id):
        if self.conn is None:
            return
        with self.lock:
            row = self.conn.execute("SELECT status FROM sprints WHERE id=?", (sprint_id,)).fetchone()
            if not row:
                return
            running = row["status"] == "running"
        if running:
            self.finish_sprint(sprint_id, "stopped", "删除前自动停止")
        with self.lock:
            self.conn.execute("DELETE FROM sprint_tasks WHERE sprint_id=?", (sprint_id,))
            self.conn.execute("DELETE FROM sprints WHERE id=?", (sprint_id,))
            self.conn.commit()

    def reorder_sprint_tasks(self, sprint_id, task_ids):
        """Persist a new order for the pending tasks of a sprint."""
        with self.lock:
            owned = {r[0] for r in self.conn.execute(
                "SELECT id FROM sprint_tasks WHERE sprint_id=? AND status='pending'", (sprint_id,))}
            if not set(task_ids) <= owned or len(task_ids) != len(owned):
                raise ValueError("任务清单与待跑队列不一致（进行中/已完成的任务不可拖动）")
            for pos, tid in enumerate(task_ids):
                self.conn.execute("UPDATE sprint_tasks SET position=? WHERE id=?", (pos, tid))
            self.conn.commit()

    def delete_sprint_task(self, task_id):
        with self.lock:
            row = self.conn.execute("SELECT sprint_id, status FROM sprint_tasks WHERE id=?",
                                    (task_id,)).fetchone()
            if not row:
                raise ValueError("任务不存在")
            if row["status"] != "pending":
                raise ValueError("只能删除待跑的任务")
            self.conn.execute("DELETE FROM sprint_tasks WHERE id=?", (task_id,))
            self.conn.commit()

    def dispatch(self, rule):
        with self.lock:
            if rule["kind"] != "new" and self.conn.execute(
                    "SELECT 1 FROM schedule_rules WHERE thread_id=? AND status='running' LIMIT 1",
                    (rule["thread_id"],)).fetchone():
                return
            updated = self.conn.execute("""UPDATE schedule_rules SET status='running', started_at=?,
                attempts=COALESCE(attempts,0)+1 WHERE id=? AND status='waiting'""",
                (time.time(), rule["id"])).rowcount
            self.conn.commit()
        if not updated:
            return
        threading.Thread(target=self._run, args=(rule,), daemon=True).start()

    def _run(self, rule):
        agent = rule.get("agent") or "codex"
        env = None
        if agent == "zcode":
            base = zcode_cmd()
            if base is None:
                self._finish(rule, "failed", "", "未找到 ZCode CLI（需要安装 ZCode.app 或设置 ZCODE_BIN 环境变量）")
                return
            env, env_error = zcode_env()
            if env is None:
                self._finish(rule, "failed", "", "ZCode 无头环境不可用：" + env_error)
                return
            if rule["kind"] == "new":
                cmd = [*base, "--prompt", rule["prompt"], "--cwd", rule["cwd"], "--json"]
            else:
                cmd = [*base, "--prompt", rule["prompt"], "--resume", rule["thread_id"], "--json"]
        elif rule["kind"] == "new":
            cmd = [codex_bin(), "exec", "--json", "--skip-git-repo-check", "-C", rule["cwd"], "-"]
        else:
            cmd = [codex_bin(), "exec", "resume", rule["thread_id"], "-"]
        selection_backup = None
        try:
            if rule["kind"] == "new" and rule.get("project_mode") == "create":
                os.mkdir(rule["cwd"])
                with self.lock:
                    self.conn.execute("INSERT OR IGNORE INTO helper_projects(id,name,path,created_at) VALUES(?,?,?,?)",
                                      ("helper-" + rule["id"], rule["project_name"], rule["cwd"], time.time()))
                    self.conn.commit()
            if env is not None:
                env = {**os.environ, **env}
            if agent == "zcode" and rule["kind"] != "new":
                selection_backup = zcode_rewrite_resume_selection(rule["thread_id"], self.zcode_home)
            proc = subprocess.run(cmd, input=None if agent == "zcode" else rule["prompt"], text=True, cwd=rule["cwd"],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # ZCode 侧 API key 轮换会导致签名失效（401/过期）；强制刷新配置后重试一次
            if (agent == "zcode" and proc.returncode != 0 and
                    any(x in (proc.stderr or "") + (proc.stdout or "") for x in ("401", "过期", "invalid signature"))):
                try:
                    ensure_zcode_provider_config(force=True)
                    env = {**os.environ, **zcode_env()[0]}
                    proc = subprocess.run(cmd, input=None if agent == "zcode" else rule["prompt"], text=True,
                                          cwd=rule["cwd"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                except Exception:
                    pass
            task_id = rule["thread_id"]
            if rule["kind"] == "new" and agent == "codex":
                for line in (proc.stdout or "").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "thread.started":
                        task_id = event.get("thread_id") or task_id
            elif rule["kind"] == "new" and agent == "zcode":
                match = re.search(r"sess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                                  proc.stdout or "")
                task_id = match.group(0) if match else task_id
            output = task_id or ""
            error = (proc.stderr or "")[-2000:] if proc.returncode else ""
            status = "done" if proc.returncode == 0 else "failed"
        except Exception as exc:
            output, error, status = "", str(exc), "failed"
        finally:
            if selection_backup is not None:
                zcode_restore_resume_selection(rule["thread_id"], selection_backup, self.zcode_home)
        # ZCode 限流重试：失败回到等待队列按间隔重试，直到成功或次数用尽
        if (status == "failed" and agent == "zcode" and rule["kind"] == "resume"
                and rule["trigger"] == "quota"):
            with self.lock:
                attempts = self.conn.execute("SELECT COALESCE(attempts,0) FROM schedule_rules WHERE id=?",
                                             (rule["id"],)).fetchone()
                attempts = attempts[0] if attempts else 0
                if attempts < ZCODE_MAX_ATTEMPTS:
                    self.conn.execute("""UPDATE schedule_rules SET status='waiting', finished_at=?, error=?, output=?
                        WHERE id=?""", (time.time(),
                                        (error or "执行失败，稍后自动重试")[:500], output, rule["id"]))
                    self.conn.commit()
                    return
                error = "重试 %d 次仍未成功，已停止：%s" % (attempts, (error or "未知错误")[:300])
        self._finish(rule, status, error, output)

    def _finish(self, rule, status, error, output):
        with self.lock:
            self.conn.execute("UPDATE schedule_rules SET status=?, finished_at=?, error=?, output=? WHERE id=?",
                              (status, time.time(), error, output, rule["id"]))
            self.conn.commit()
