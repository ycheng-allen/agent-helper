"""隔离端到端 smoke：临时 HOME + 假 ZCode CLI + 本地假额度服务 + 真实子进程。

覆盖诊断报告「未完成端到端证明」中可离线自动化的部分：
- ZCode 无头执行全链路：凭据文件 → node 解密 → provider 配置生成 → 子进程环境注入
- HTTP API 建规则 → 调度器派发 → 真实子进程 → 数据库状态落库
- 额度窗口门控：额度耗尽时续跑等待，额度恢复后自动执行
- 失败 → 回等待队列 → 按固定间隔重试 → 成功（真实进程，非 mock subprocess）
- API key 轮换重试（子进程报 401 → 强制刷新 provider 配置 → 重跑成功）
- ZCode 会话写入后的导入增量与去重（真实 backend 周期重扫）

全程不触碰真实 ~/.codex、~/.zcode、~/.agent-helper，不发真实网络请求：
HOME 指向临时目录，ZCODE_QUOTA_URL 指向本地假额度服务，ZCODE_BIN 指向假 CLI。
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from watch_scheduler import Scheduler, init_db, validate_rule

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SESSION_ID = "sess_e2e1111-2222-3333-4444-555555555555"
TURN_MS = int((time.time() - 600) * 1000)
FAKE_KEY = "e2e-key-id.e2e-key-secret"
RESUME_OUTPUT = "sess_e2e2222-aaaa-bbbb-cccc-dddddddddddd"

# 假 CLI：node 脚本（zcode_cmd 恒以 [node, 脚本] 形式调用）。
# 每次调用把 argv/cwd/provider 环境追加进 E2E_LOG；行为由 E2E_CTRL 控制：
# ok=成功；fail=直接失败；fail401=首次 401 失败、之后成功（模拟 API key 轮换）。
FAKE_CLI = r'''
const fs = require("fs");
const log = process.env.E2E_LOG;
if (log) fs.appendFileSync(log, JSON.stringify({
  argv: process.argv.slice(2), cwd: process.cwd(),
  prov: process.env.ZCODE_PERSONAL_PROVIDER_CONFIG_FILE || "",
  at: Date.now() }) + "\n");
const ctrl = fs.readFileSync(process.env.E2E_CTRL, "utf8").trim();
if (ctrl === "fail") { console.error("boom"); process.exit(1); }
if (ctrl === "fail401" && !fs.existsSync(process.env.E2E_ONCE)) {
  fs.writeFileSync(process.env.E2E_ONCE, "1");
  console.error("401 invalid signature");
  process.exit(1);
}
console.log("starting...");
console.log(process.env.E2E_OUTPUT || "");
process.exit(0);
'''

ZCODE_DB_SCHEMA = """
CREATE TABLE turn_usage(
    session_id text not null, turn_id text not null, trace_id text, user_message_id text,
    status text not null, started_at integer not null, first_model_start_at integer,
    first_token_at integer, completed_at integer, duration_ms integer, time_to_first_token_ms integer,
    model_request_count integer not null default 0, model_retry_count integer not null default 0,
    tool_call_count integer not null default 0, tool_error_count integer not null default 0,
    input_tokens integer not null default 0, output_tokens integer not null default 0,
    reasoning_tokens integer not null default 0, cache_creation_input_tokens integer not null default 0,
    cache_read_input_tokens integer not null default 0, computed_total_tokens integer not null default 0,
    retryable integer not null default 0, cancelled_by_user integer not null default 0,
    context_exceeded integer not null default 0, error_type text, error_code text,
    primary key(session_id, turn_id));
CREATE TABLE model_usage(
    id text primary key, logical_request_id text not null, attempt_index integer not null default 0,
    session_id text not null, turn_id text, trace_id text, span_id text, assistant_message_id text,
    parent_user_message_id text, query_source text not null, provider_id text not null,
    model_id text not null, variant text, agent text, mode text, task_type text,
    status text not null, started_at integer not null, first_token_at integer, completed_at integer,
    duration_ms integer, time_to_first_token_ms integer, finish_reason text,
    tool_call_count integer not null default 0, input_tokens integer not null default 0,
    output_tokens integer not null default 0, reasoning_tokens integer not null default 0,
    cache_creation_input_tokens integer not null default 0, cache_read_input_tokens integer not null default 0,
    provider_total_tokens integer, computed_total_tokens integer not null default 0,
    retry_count integer not null default 0, retryable integer not null default 0,
    cancelled_by_user integer not null default 0, context_exceeded integer not null default 0,
    error_type text, error_code text, error_message text, raw_usage_json text, provider_metadata_json text);
CREATE TABLE session(
    id text primary key, project_id text not null, workspace_id text, parent_id text,
    slug text not null, directory text not null, path text, title text not null, version text not null,
    share_url text, summary_additions integer, summary_deletions integer, summary_files integer,
    summary_diffs text, revert text, permission text, time_created integer not null,
    time_updated integer not null, time_compacting integer, time_archived integer,
    task_type text not null default 'interactive');
"""


def seed_credentials(home):
    """Plaintext (non enc:v1) coding-plan key: the decrypt script passes it through."""
    path = os.path.join(home, ".zcode", "v2")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "credentials.json"), "w") as fh:
        json.dump({"oauth:zai:coding-plan:api-key": FAKE_KEY}, fh)


def seed_zcode_db(zcode_home, cwd, turns=(("turn_a", "error", "rate_limited"),)):
    os.makedirs(os.path.join(zcode_home, "cli", "db"), exist_ok=True)
    os.makedirs(cwd, exist_ok=True)
    db = sqlite3.connect(os.path.join(zcode_home, "cli", "db", "db.sqlite"))
    db.executescript(ZCODE_DB_SCHEMA)
    db.execute("INSERT INTO session(id, project_id, slug, directory, title, version, time_created, time_updated) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (SESSION_ID, "p1", "demo", cwd, "E2E session", "1.0", TURN_MS, TURN_MS + 60000))
    for turn_id, status, error_type in turns:
        db.execute("INSERT INTO turn_usage(session_id, turn_id, status, started_at, completed_at, error_type) "
                   "VALUES(?,?,?,?,?,?)", (SESSION_ID, turn_id, status, TURN_MS, TURN_MS + 50000, error_type))
        db.execute("""INSERT INTO model_usage(logical_request_id, session_id, turn_id, query_source, provider_id,
            model_id, variant, status, started_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("lr_" + turn_id, SESSION_ID, turn_id, "main_turn", "account:bigmodel", "GLM-5.3", "high", status, TURN_MS))
    db.commit()
    db.close()


class FakeQuotaServer:
    """Local stand-in for open.bigmodel.cn quota endpoint; percentage is switchable."""

    def __init__(self):
        self.state = {"percentage": 100.0}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                pct = outer.state["percentage"]
                body = json.dumps({"code": 200, "data": {"limits": [
                    {"unit": 3, "number": 5, "percentage": pct,
                     "nextResetTime": int((time.time() + 3600) * 1000)},
                    {"unit": 6, "number": 1, "percentage": max(0.0, pct - 30),
                     "nextResetTime": int((time.time() + 86400) * 1000)},
                ]}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/api/monitor/usage/quota/limit" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(fn, timeout, interval=1.0):
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    return result


class BackendHttpE2E(unittest.TestCase):
    """真实 backend 子进程（HOME 隔离）+ HTTP API + 真实调度线程 + 真实子进程派发。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = os.path.join(cls.tmp.name, "home")
        cls.zcode_home = os.path.join(cls.tmp.name, "zcode-home")
        cls.codex_home = os.path.join(cls.tmp.name, "codex-home")
        cls.proj = os.path.join(cls.tmp.name, "proj")
        os.makedirs(cls.proj, exist_ok=True)
        seed_credentials(cls.home)
        seed_zcode_db(cls.zcode_home, cls.proj)

        cls.quota = FakeQuotaServer()
        cls.cli_log = os.path.join(cls.tmp.name, "cli-log.jsonl")
        cls.ctrl = os.path.join(cls.tmp.name, "ctrl")
        cls.once = os.path.join(cls.tmp.name, "once")
        cls.fake_cli = os.path.join(cls.tmp.name, "fake-zcode.cjs")
        with open(cls.fake_cli, "w") as fh:
            fh.write(FAKE_CLI)
        with open(cls.ctrl, "w") as fh:
            fh.write("ok")

        cls.port = free_port()
        cls.out = open(os.path.join(cls.tmp.name, "backend.log"), "w")
        # AGENT_HELPER_BACKEND 可指向打包产物里的 agent_helper.py（打包回归用），默认源码
        backend = os.environ.get("AGENT_HELPER_BACKEND") or os.path.join(HERE, "agent_helper.py")
        cls.proc = subprocess.Popen(
            [sys.executable, backend,
             "--no-open", "--port", str(cls.port),
             "--codex-home", cls.codex_home, "--zcode-home", cls.zcode_home,
             "--agents", "zcode"],
            env={**os.environ, "HOME": cls.home, "ZCODE_BIN": cls.fake_cli,
                 "ZCODE_QUOTA_URL": cls.quota.url,
                 "E2E_LOG": cls.cli_log, "E2E_CTRL": cls.ctrl, "E2E_ONCE": cls.once,
                 "E2E_OUTPUT": RESUME_OUTPUT},
            stdout=cls.out, stderr=subprocess.STDOUT)
        ready = wait_until(cls._try_data, 25)
        if not ready:
            cls.out.close()
            backend_log = open(os.path.join(cls.tmp.name, "backend.log")).read()
            raise RuntimeError("backend did not start:\n" + backend_log)

    @classmethod
    def _try_data(cls):
        try:
            return cls.get("/api/data")
        except Exception:
            return None

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.out.close()
        cls.quota.stop()
        cls.tmp.cleanup()

    @classmethod
    def get(cls, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (cls.port, path), timeout=10) as resp:
            return json.loads(resp.read().decode())

    @classmethod
    def post(cls, path, body):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (cls.port, path),
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    @classmethod
    def rule_row(cls, rule_id):
        db = sqlite3.connect(os.path.join(cls.home, ".agent-helper", "state.db"))
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM schedule_rules WHERE id=?", (rule_id,)).fetchone()
        db.close()
        return dict(row) if row else None

    @classmethod
    def cli_calls(cls):
        if not os.path.isfile(cls.cli_log):
            return []
        with open(cls.cli_log) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_01_new_task_runs_to_done(self):
        # HTTP 建规则 → 调度器真实派发 → 假 CLI 子进程 → 状态/输出/环境落库
        run_at = datetime.fromtimestamp(time.time() + 2, timezone.utc).isoformat()
        res = self.post("/api/schedule", {"kind": "new", "trigger": "at", "agent": "zcode",
                                          "project_mode": "directory", "cwd": self.proj,
                                          "prompt": "e2e hello", "run_at": run_at})
        self.assertTrue(res.get("rule", {}).get("id") or res.get("id"), res)
        rule_id = (res.get("rule") or res)["id"]
        row = wait_until(lambda: (self.rule_row(rule_id) or {}).get("status") == "done"
                         and self.rule_row(rule_id) or None, 35)
        self.assertIsNotNone(row, "rule did not reach done: %s" % self.rule_row(rule_id))
        self.assertEqual("done", row["status"])
        calls = self.cli_calls()
        self.assertTrue(calls, "fake CLI was never invoked")
        call = calls[0]
        self.assertEqual(os.path.realpath(self.proj), call["cwd"])    # 工作目录（realpath 化）传入子进程
        self.assertIn("e2e hello", " ".join(call["argv"]))            # prompt 走 argv
        self.assertEqual(os.path.join(self.home, ".agent-helper", "zcode-provider-config.json"),
                         call["prov"])                                # provider 配置注入子进程环境
        config = json.load(open(call["prov"]))
        key = config["config"]["providerConfigRules"]["providerRules"][0]["config"]["access"]["apiKey"]
        self.assertEqual(FAKE_KEY, key)  # 凭据文件 → node 解密 → 配置，全程真实执行（假 key）

    def test_02_resume_waits_for_quota_then_runs(self):
        # 额度耗尽：续跑保持 waiting，不派发；额度恢复后自动执行
        self.quota.state["percentage"] = 100.0
        res = self.post("/api/schedule", {"kind": "resume", "trigger": "quota", "agent": "zcode",
                                          "thread_id": SESSION_ID, "wait_for_quota": True})
        rule_id = (res.get("rule") or res)["id"]
        time.sleep(13)  # 覆盖 ≥1 个调度 tick 与 ≥1 个额度采样窗口
        row = self.rule_row(rule_id)
        self.assertEqual("waiting", row["status"], "dispatched while quota exhausted")
        self.assertEqual(0, row["attempts"])
        calls_before = len(self.cli_calls())

        self.quota.state["percentage"] = 10.0
        row = wait_until(lambda: (self.rule_row(rule_id) or {}).get("status") == "done"
                         and self.rule_row(rule_id) or None, 45)
        self.assertIsNotNone(row, "rule did not run after quota recovery: %s" % self.rule_row(rule_id))
        self.assertEqual(SESSION_ID, row["output"])  # resume 规则的 output 固定为 thread_id
        self.assertGreater(len(self.cli_calls()), calls_before)

    def test_03_import_increment_and_no_duplicates(self):
        # 会话库写入新 turn 后，backend 周期重扫导入增量；老 turn 不重复导入
        state_db = os.path.join(self.home, ".agent-helper", "state.db")

        def turn_counts():
            db = sqlite3.connect(state_db)
            rows = db.execute("SELECT turn_id, COUNT(*) c FROM turns WHERE agent='zcode' GROUP BY turn_id")
            counts = dict(rows)
            db.close()
            return counts

        before = turn_counts()
        self.assertEqual(1, before.get("turn_a", 0), before)
        db = sqlite3.connect(os.path.join(self.zcode_home, "cli", "db", "db.sqlite"))
        db.execute("INSERT INTO turn_usage(session_id, turn_id, status, started_at, completed_at, error_type) "
                   "VALUES(?,?,?,?,?,?)",
                   (SESSION_ID, "turn_b", "completed", TURN_MS + 90000, TURN_MS + 95000, None))
        db.execute("""INSERT INTO model_usage(logical_request_id, session_id, turn_id, query_source, provider_id,
            model_id, variant, status, started_at) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("lr_turn_b", SESSION_ID, "turn_b", "main_turn", "account:bigmodel", "GLM-5.3-Flash",
             "high", "completed", TURN_MS + 90000))
        db.commit()
        db.close()
        time.sleep(21)  # 超过后端 20s 重扫间隔
        # 每次 /api/data 才会触发重扫，轮询必须在循环内调用接口
        wait_until(lambda: (self.get("/api/data"), turn_counts().get("turn_b") == 1)[1], 20)
        after = turn_counts()
        self.assertEqual(1, after.get("turn_b", 0), after)
        self.assertEqual(1, after.get("turn_a", 0), "old turn was re-imported: %s" % after)


class InProcessProcessE2E(unittest.TestCase):
    """进程内调度器 + 真实子进程：失败重试环与 401 轮换（不经过 HTTP 层）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.zcode_home = os.path.join(self.tmp.name, "zcode-home")
        self.proj = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.proj, exist_ok=True)
        seed_credentials(self.home)
        seed_zcode_db(self.zcode_home, self.proj)
        self.ctrl = os.path.join(self.tmp.name, "ctrl")
        self.once = os.path.join(self.tmp.name, "once")
        self.cli_log = os.path.join(self.tmp.name, "cli-log.jsonl")
        self.fake_cli = os.path.join(self.tmp.name, "fake-zcode.cjs")
        with open(self.fake_cli, "w") as fh:
            fh.write(FAKE_CLI)
        with open(self.ctrl, "w") as fh:
            fh.write("ok")
        env_patch = patch.dict(os.environ, {"HOME": self.home, "ZCODE_BIN": self.fake_cli,
                                            "E2E_LOG": self.cli_log, "E2E_CTRL": self.ctrl,
                                            "E2E_ONCE": self.once, "E2E_OUTPUT": RESUME_OUTPUT})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.scheduler = Scheduler(self.conn, threading.Lock(),
                                   os.path.join(self.tmp.name, "codex-home"),
                                   zcode_home=self.zcode_home, agents=["zcode"])

    def insert_resume_rule(self):
        snapshots = __import__("watch_scheduler").all_task_snapshots(
            os.path.join(self.tmp.name, "codex-home"), self.zcode_home)
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, snapshots)
        self.conn.execute("""INSERT INTO schedule_rules
            (id,kind,trigger,thread_id,agent,cwd,prompt,quota_after,after_turn_id,status,auto,created_at,
             started_at,finished_at,error,output,attempts)
             VALUES(:id,:kind,:trigger,:thread_id,:agent,:cwd,:prompt,:quota_after,:after_turn_id,
                    :status,:auto,:created_at,:started_at,:finished_at,:error,:output,:attempts)""", rule)
        return rule

    def row(self, rule_id):
        return self.conn.execute("SELECT status, attempts, finished_at, error, output FROM schedule_rules "
                                 "WHERE id=?", (rule_id,)).fetchone()

    def tick_until(self, rule_id, predicate, timeout=25):
        deadline = time.time() + timeout
        with patch("watch_scheduler.zcode_usage_raw", side_effect=RuntimeError("quota unavailable")), \
             patch("watch_scheduler.ZCODE_RETRY_SECONDS", 1):
            while time.time() < deadline:
                self.scheduler.tick()
                row = self.row(rule_id)
                if row and predicate(row):
                    return row
                time.sleep(0.3)
        self.fail("timed out waiting for rule state; last=%s" % (dict(self.row(rule_id)) if self.row(rule_id) else None))

    def cli_calls(self):
        if not os.path.isfile(self.cli_log):
            return []
        with open(self.cli_log) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_retry_loop_with_real_process(self):
        # 失败 → 回等待队列（attempts=1、finished_at 落库）→ 间隔后自动重试 → 成功
        rule = self.insert_resume_rule()
        with open(self.ctrl, "w") as fh:
            fh.write("fail")
        row = self.tick_until(rule["id"],
                              lambda r: r["status"] == "waiting" and r["attempts"] >= 1
                              and r["finished_at"] is not None)
        self.assertIn("boom", row["error"])
        calls_after_failure = len(self.cli_calls())
        self.assertGreaterEqual(calls_after_failure, 1)
        with open(self.ctrl, "w") as fh:
            fh.write("ok")
        row = self.tick_until(rule["id"], lambda r: r["status"] == "done")
        self.assertEqual(SESSION_ID, row["output"])  # resume 规则的 output 固定为 thread_id
        self.assertGreater(len(self.cli_calls()), calls_after_failure)

    def test_401_rotation_refreshes_provider_config_and_reruns(self):
        # 子进程报 401 → 强制刷新 provider 配置 → 重跑成功
        rule = self.insert_resume_rule()
        with open(self.ctrl, "w") as fh:
            fh.write("fail401")
        row = self.tick_until(rule["id"], lambda r: r["status"] == "done")
        self.assertEqual(SESSION_ID, row["output"])  # resume 规则的 output 固定为 thread_id
        calls = self.cli_calls()
        self.assertGreaterEqual(len(calls), 2, "expected an initial failure plus the rerun")
        config_path = calls[0]["prov"]
        self.assertTrue(os.path.isfile(config_path))
        config = json.load(open(config_path))
        key = config["config"]["providerConfigRules"]["providerRules"][0]["config"]["access"]["apiKey"]
        self.assertEqual(FAKE_KEY, key)


class DataDirMigrationTest(unittest.TestCase):
    """回归：数据目录缺失时必须采用新名，否则两边迁移逻辑会把 open 连接的库目录 rename 走。"""

    def test_fresh_home_adopts_new_dir(self):
        import agent_helper
        with tempfile.TemporaryDirectory() as home, \
             patch.object(agent_helper, "HOME", home):
            result = agent_helper._migrated_data_dir(".agent-helper", ".codex-model-watch")
            self.assertEqual(os.path.join(home, ".agent-helper"), result)
            self.assertTrue(os.path.isdir(result))
            self.assertFalse(os.path.exists(os.path.join(home, ".codex-model-watch")))

    def test_old_dir_is_renamed_once(self):
        import agent_helper
        with tempfile.TemporaryDirectory() as home, \
             patch.object(agent_helper, "HOME", home):
            old = os.path.join(home, ".codex-model-watch")
            os.makedirs(old)
            result = agent_helper._migrated_data_dir(".agent-helper", ".codex-model-watch")
            self.assertEqual(os.path.join(home, ".agent-helper"), result)
            self.assertTrue(os.path.isdir(result))
            self.assertFalse(os.path.exists(old))

    def test_existing_new_dir_wins(self):
        import agent_helper
        with tempfile.TemporaryDirectory() as home, \
             patch.object(agent_helper, "HOME", home):
            new = os.path.join(home, ".agent-helper")
            os.makedirs(new)
            os.makedirs(os.path.join(home, ".codex-model-watch"))
            self.assertEqual(new, agent_helper._migrated_data_dir(".agent-helper", ".codex-model-watch"))

    def test_watch_scheduler_helper_data_dir_matches(self):
        import watch_scheduler
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(os.path.join(home, ".agent-helper"), watch_scheduler.helper_data_dir())
            self.assertTrue(os.path.isdir(os.path.join(home, ".agent-helper")))


if __name__ == "__main__":
    unittest.main()
