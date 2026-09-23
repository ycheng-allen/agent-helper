import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from watch_scheduler import (ZCODE_RETRY_SECONDS, Scheduler, all_task_snapshots, init_db,
                             validate_rule, zcode_retry_due, zcode_rewrite_resume_selection,
                             zcode_restore_resume_selection, zcode_recent_dirs, zcode_task_snapshots)

SESSION_ID = "sess_11111111-2222-3333-4444-555555555555"
CWD = "/tmp/demo-zcode-project"
TURN_MS = 1790067109494


def datetime_now_iso():
    return datetime.now(timezone.utc).isoformat()


def make_zcode_db(zcode_home, status="error", error_type="rate_limited"):
    os.makedirs(os.path.join(zcode_home, "cli", "db"), exist_ok=True)
    os.makedirs(CWD, exist_ok=True)
    db = sqlite3.connect(os.path.join(zcode_home, "cli", "db", "db.sqlite"))
    db.executescript("""
    DROP TABLE IF EXISTS turn_usage;
    DROP TABLE IF EXISTS session;
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
    CREATE TABLE session(
        id text primary key, project_id text not null, workspace_id text, parent_id text,
        slug text not null, directory text not null, path text, title text not null, version text not null,
        share_url text, summary_additions integer, summary_deletions integer, summary_files integer,
        summary_diffs text, revert text, permission text, time_created integer not null,
        time_updated integer not null, time_compacting integer, time_archived integer,
        task_type text not null default 'interactive');
    """)
    db.execute("INSERT INTO session(id, project_id, slug, directory, title, version, time_created, time_updated) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (SESSION_ID, "p1", "demo", CWD, "Demo session", "1.0", TURN_MS, TURN_MS + 60000))
    db.execute("""INSERT INTO turn_usage(session_id, turn_id, status, started_at, completed_at, error_type)
        VALUES(?,?,?,?,?,?)""", (SESSION_ID, "turn_a", status, TURN_MS, TURN_MS + 50000, error_type))
    db.commit()
    db.close()


class ZcodeScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.zcode_home = os.path.join(self.tmp.name, "zcode-home")
        make_zcode_db(self.zcode_home)
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.scheduler = Scheduler(self.conn, __import__("threading").Lock(),
                                   os.path.join(self.tmp.name, "codex-home"),
                                   zcode_home=self.zcode_home, agents=["zcode"])

    def snapshot(self):
        return zcode_task_snapshots(self.zcode_home)[0]

    def test_zcode_snapshot_shape(self):
        item = self.snapshot()
        self.assertEqual(SESSION_ID, item["id"])
        self.assertEqual("zcode", item["agent"])
        self.assertEqual(CWD, item["cwd"])
        self.assertEqual("failed", item["turn"]["status"])
        self.assertEqual("rate_limited", item["turn"]["error"])
        self.assertEqual("demo-zcode-project", item["project_name"])
        self.assertEqual([item], [s for s in all_task_snapshots(self.zcode_home, self.zcode_home)
                                  if s["agent"] == "zcode"])

    def test_completed_turn_maps_status(self):
        make_zcode_db(self.zcode_home, status="completed", error_type=None)
        item = self.snapshot()
        self.assertEqual("completed", item["turn"]["status"])
        self.assertEqual("", item["turn"]["error"])

    def test_recent_dirs_pseudo_projects(self):
        dirs = zcode_recent_dirs(self.zcode_home)
        self.assertEqual(1, len(dirs))
        self.assertEqual("zcode-dir:" + os.path.realpath(CWD), dirs[0]["id"])

    def test_validate_rule_zcode_resume(self):
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, [self.snapshot()])
        self.assertEqual("zcode", rule["agent"])
        self.assertIn("限流", rule["prompt"])

    def test_validate_rule_rejects_zcode_quota_new_only(self):
        projects = [{"id": "zcode-dir:" + CWD, "name": "demo-zcode-project", "path": CWD}]
        with self.assertRaises(ValueError):
            validate_rule({"kind": "new", "trigger": "quota", "agent": "zcode",
                           "project_mode": "existing", "project_id": "zcode-dir:" + CWD,
                           "prompt": "hi"}, [], projects=projects)
        # create 模式对 zcode 同样可用（规则触发时创建目录）
        rule = validate_rule({"kind": "new", "trigger": "at", "agent": "zcode",
                              "project_mode": "create", "project_name": "x-" + self.id()[-6:],
                              "project_parent": self.tmp.name, "prompt": "hi",
                              "run_at": datetime_now_iso()}, [])
        self.assertEqual("zcode", rule["agent"])

    def test_zcode_api_key_reports_missing_node(self):
        import watch_scheduler as ws
        credentials = os.path.join(self.tmp.name, "credentials.json")
        with open(credentials, "w") as fh:
            fh.write("{}")
        with patch.object(ws, "node_bin", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "node"):
                ws.zcode_api_key(credentials_path=credentials)

    def test_zcode_runtime_diagnostics(self):
        import watch_scheduler as ws
        with patch.object(ws, "zcode_bin", return_value="/opt/ZCode/resources/glm/zcode.cjs"), \
             patch.object(ws, "node_bin", return_value="/usr/bin/node"):
            diag = ws.zcode_runtime_diagnostics()
        self.assertEqual("/opt/ZCode/resources/glm/zcode.cjs", diag["cli"])
        self.assertEqual("/usr/bin/node", diag["node"])
        self.assertIn("credentials", diag)
        self.assertIn("zcode_home", diag)

    def test_zcode_env_wiring(self):
        from watch_scheduler import zcode_env
        with patch("watch_scheduler.ensure_zcode_provider_config",
                   return_value=os.path.join(self.tmp.name, "prov.json")), \
             patch("watch_scheduler.zcode_bin", return_value="/fake/Resources/glm/zcode.cjs"), \
             patch("os.path.isfile", side_effect=lambda p: p in ("/fake/Resources/config/provider/zcode-builtin.json",)):
            env, err = zcode_env()
        self.assertEqual("", err)
        self.assertEqual(os.path.join(self.tmp.name, "prov.json"),
                         env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"])
        self.assertEqual("/fake/Resources/config/provider/zcode-builtin.json",
                         env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"])
        with patch("watch_scheduler.ensure_zcode_provider_config",
                   side_effect=RuntimeError("boom")):
            env, err = zcode_env()
        self.assertIsNone(env)
        self.assertIn("boom", err)

    def test_zcode_quota_resume_due_without_quota_data(self):
        # 无 Codex 额度数据也能触发：额度不可读时 ZCode 退回固定间隔重试语义
        # （必须打桩 zcode_usage_raw，否则本机真实额度会污染判定分支）
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, [self.snapshot()])
        self._insert_rule(rule)
        with patch("watch_scheduler.zcode_usage_raw", side_effect=RuntimeError("quota unavailable")):
            self.assertEqual([rule["id"]], self._tick_dispatch())

    def test_zcode_quota_resume_held_when_quota_exhausted(self):
        # 额度可读但当前窗口已耗尽：等待实时额度恢复，不派发
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, [self.snapshot()])
        self._insert_rule(rule)
        exhausted = {"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": time.time() + 3600},
                                    "secondary": {"usedPercent": 40}}}
        with patch("watch_scheduler.zcode_usage_raw", return_value=exhausted):
            self.assertEqual([], self._tick_dispatch())

    def test_zcode_quota_resume_due_when_quota_recovers(self):
        # 额度恢复为可读且有余量：立即派发
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, [self.snapshot()])
        self._insert_rule(rule)
        ready = {"rateLimits": {"primary": {"usedPercent": 10}, "secondary": {"usedPercent": 10}}}
        with patch("watch_scheduler.zcode_usage_raw", return_value=ready):
            self.assertEqual([rule["id"]], self._tick_dispatch())

    def test_zcode_quota_retry_waits_within_interval(self):
        # 失败后 299 秒：未到重试间隔，不派发
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, [self.snapshot()])
        self._insert_rule(rule, attempts=1,
                          finished_at=time.time() - (ZCODE_RETRY_SECONDS - 1))
        with patch("watch_scheduler.zcode_usage_raw", side_effect=RuntimeError("quota unavailable")):
            self.assertEqual([], self._tick_dispatch())

    def test_zcode_quota_retry_due_after_interval(self):
        # 失败后满 300 秒：按固定间隔重试派发
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, [self.snapshot()])
        self._insert_rule(rule, attempts=1, finished_at=time.time() - ZCODE_RETRY_SECONDS)
        with patch("watch_scheduler.zcode_usage_raw", side_effect=RuntimeError("quota unavailable")):
            self.assertEqual([rule["id"]], self._tick_dispatch())

    def test_zcode_retry_due_state_semantics(self):
        # 显式状态语义：首轮派发；有 finished_at 按间隔；attempts>0 且 finished_at 为空保守立即补派
        now = 1_000_000
        self.assertTrue(zcode_retry_due({}, now))                       # 无 attempts（迁移遗留）→ 首轮
        self.assertTrue(zcode_retry_due({"attempts": 0}, now))          # 首轮
        self.assertTrue(zcode_retry_due({"attempts": 3, "finished_at": None}, now))  # 空时间戳 → 立即补派
        self.assertFalse(zcode_retry_due({"attempts": 3, "finished_at": now - ZCODE_RETRY_SECONDS + 1}, now))
        self.assertTrue(zcode_retry_due({"attempts": 3, "finished_at": now - ZCODE_RETRY_SECONDS}, now))

    def _insert_rule(self, rule, attempts=0, finished_at=None):
        self.conn.execute("""INSERT INTO schedule_rules
            (id,kind,trigger,thread_id,agent,cwd,prompt,quota_after,after_turn_id,status,auto,created_at,
             started_at,finished_at,error,output,attempts)
             VALUES(:id,:kind,:trigger,:thread_id,:agent,:cwd,:prompt,:quota_after,:after_turn_id,
                    :status,:auto,:created_at,:started_at,:finished_at,:error,:output,:attempts)""",
            {**rule, "attempts": attempts, "finished_at": finished_at})

    def _tick_dispatch(self):
        dispatched = []
        with patch.object(self.scheduler, "dispatch", lambda r: dispatched.append(r["id"])):
            self.scheduler.tick()
        return dispatched

    def test_failed_zcode_retry_returns_to_waiting(self):
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, [self.snapshot()])
        self.conn.execute("""INSERT INTO schedule_rules
            (id,kind,trigger,thread_id,agent,cwd,prompt,quota_after,after_turn_id,status,auto,created_at,
             started_at,finished_at,error,output,attempts)
             VALUES(:id,:kind,:trigger,:thread_id,:agent,:cwd,:prompt,:quota_after,:after_turn_id,
                    :status,:auto,:created_at,:started_at,:finished_at,:error,:output,1)""", rule)
        with patch("watch_scheduler.zcode_cmd", return_value=["node", "/fake/zcode.cjs"]), \
             patch("watch_scheduler.subprocess.run") as run:
            run.return_value = type("P", (), {"returncode": 1, "stdout": "", "stderr": "boom"})()
            self.scheduler._run(rule)
        row = self.conn.execute("SELECT status, error, attempts FROM schedule_rules WHERE id=?",
                                (rule["id"],)).fetchone()
        self.assertEqual("waiting", row["status"])  # 回到队列等待重试
        self.assertIn("boom", row["error"])
        self.assertGreaterEqual(row["attempts"], 1)

    def test_attempts_exhausted_marks_failed(self):
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, [self.snapshot()])
        self.conn.execute("""INSERT INTO schedule_rules
            (id,kind,trigger,thread_id,agent,cwd,prompt,quota_after,after_turn_id,status,auto,created_at,
             started_at,finished_at,error,output,attempts)
             VALUES(:id,:kind,:trigger,:thread_id,:agent,:cwd,:prompt,:quota_after,:after_turn_id,
                    :status,:auto,:created_at,:started_at,:finished_at,:error,:output,99)""", rule)
        with patch("watch_scheduler.zcode_cmd", return_value=["node", "/fake/zcode.cjs"]), \
             patch("watch_scheduler.subprocess.run") as run:
            run.return_value = type("P", (), {"returncode": 1, "stdout": "", "stderr": "still failing"})()
            self.scheduler._run(rule)
        row = self.conn.execute("SELECT status FROM schedule_rules WHERE id=?", (rule["id"],)).fetchone()
        self.assertEqual("failed", row["status"])

    def test_node_bin_falls_back_without_path(self):
        import watch_scheduler as ws
        fake_node = os.path.join(self.tmp.name, "node")
        open(fake_node, "w").write("#!/bin/sh\n")
        os.chmod(fake_node, 0o755)
        with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=False), \
             patch.object(ws, "NODE_CANDIDATES", (fake_node,)), \
             patch("watch_scheduler.shutil.which", return_value=None):
            self.assertEqual(fake_node, ws.node_bin())  # PATH 找不到时按候选路径兜底
        with patch.object(ws, "ZCODE_CLI_CANDIDATES", ("/fake/zcode.cjs",)), \
             patch.object(ws, "NODE_CANDIDATES", (fake_node,)), \
             patch("watch_scheduler.os.path.isfile", side_effect=lambda p: p in ("/fake/zcode.cjs", fake_node)), \
             patch("watch_scheduler.shutil.which", return_value=None):
            cmd = ws.zcode_cmd()
            self.assertEqual([fake_node, "/fake/zcode.cjs"], cmd)  # 命令向量用解析出的 node

    def test_successful_zcode_new_run(self):
        rule = validate_rule({"kind": "new", "trigger": "at", "agent": "zcode",
                              "project_mode": "existing", "project_id": "zcode-dir:" + CWD,
                              "project_name": "demo-zcode-project", "prompt": "写个 README",
                              "run_at": __import__("datetime").datetime.fromtimestamp(
                                  __import__("time").time() - 10).astimezone().isoformat()},
                             [], projects=[{"id": "zcode-dir:" + CWD, "name": "demo-zcode-project",
                                            "path": CWD}])
        self.conn.execute("""INSERT INTO schedule_rules
            (id,kind,trigger,thread_id,agent,cwd,prompt,run_at,status,auto,created_at,started_at,finished_at,error,output)
             VALUES(:id,:kind,:trigger,:thread_id,:agent,:cwd,:prompt,:run_at,:status,:auto,:created_at,
                    :started_at,:finished_at,:error,:output)""", rule)
        with patch("watch_scheduler.zcode_cmd", return_value=["node", "/fake/zcode.cjs"]), \
             patch("watch_scheduler.subprocess.run") as run:
            run.return_value = type("P", (), {"returncode": 0,
                                              "stdout": "starting...\n" +
                                                        "sess_99999999-aaaa-bbbb-cccc-dddddddddddd\n",
                                              "stderr": ""})()
            self.scheduler._run(rule)
        row = self.conn.execute("SELECT status, output FROM schedule_rules WHERE id=?",
                                (rule["id"],)).fetchone()
        self.assertEqual("done", row["status"])
        self.assertEqual("sess_99999999-aaaa-bbbb-cccc-dddddddddddd", row["output"])



class ScheduleRuleInsertColumnsTest(unittest.TestCase):
    """回归：POST /api/schedule 的 INSERT 必须写入 agent 列。

    曾经 INSERT 缺 agent 列，UI/API 建的 ZCode 规则落库后被默认成 codex，
    「发送下一步」等被路由给 Codex 执行而失败。
    """

    def test_api_insert_writes_agent_and_params_match(self):
        import re
        import os
        os.makedirs(CWD, exist_ok=True)  # 不依赖其他测试类先创建目录
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(here, "agent_helper.py"), encoding="utf-8").read()
        inserts = re.findall(r"INSERT INTO schedule_rules\s*\((.*?)\)", src, re.S)
        self.assertTrue(inserts, "agent_helper.py 中找不到 INSERT INTO schedule_rules")
        for cols in inserts:
            col_list = [c.strip() for c in cols.replace("\n", " ").split(",")]
            self.assertIn("agent", col_list,
                          "INSERT 缺 agent 列，ZCode 规则会静默降级为 codex: %s" % col_list)
        # 校验 validate_rule 输出覆盖所有命名参数（防止列与参数漂移）
        rule = validate_rule({"kind": "new", "trigger": "at", "agent": "zcode",
                              "prompt": "p", "project_mode": "directory", "cwd": CWD,
                              "run_at": datetime.now(timezone.utc).isoformat()}, [])
        params = set(re.findall(r":(\w+)", inserts[0]))
        missing = params - set(rule)
        self.assertFalse(missing, "validate_rule 缺少 INSERT 所需参数: %s" % missing)



class ZcodeResumeSelectionTest(unittest.TestCase):
    """桌面会话的模型选择指向账号 provider，无头 resume 前需临时改写、跑完还原。"""

    def _make_db(self, root, data):
        import sqlite3, time as t
        os.makedirs(os.path.join(root, "cli", "db"), exist_ok=True)
        conn = sqlite3.connect(os.path.join(root, "cli", "db", "db.sqlite"))
        conn.execute("""CREATE TABLE session_entry(
            id text primary key, session_id text not null, type text not null,
            time_created integer not null, time_updated integer not null, data text not null)""")
        conn.execute("INSERT INTO session_entry VALUES(?,?,?,?,?,?)",
                     ("sess_x:runtime-model-selection", "sess_x", "runtime/model_selection",
                      1, 1, data))
        conn.commit()
        conn.close()

    def test_rewrite_wrapped_account_selection(self):
        wrapped = json.dumps({"modelSelection": {"providerId": "account:bigmodel-x",
                                                 "modelId": "GLM-5.3-Flash",
                                                 "options": {"reasoningLevel": "max"}}})
        root = tempfile.mkdtemp()
        self._make_db(root, wrapped)
        backup = zcode_rewrite_resume_selection("sess_x", zcode_home=root)
        self.assertEqual(wrapped, backup)
        conn = sqlite3.connect(os.path.join(root, "cli", "db", "db.sqlite"))
        now = conn.execute("SELECT data FROM session_entry WHERE id='sess_x:runtime-model-selection'").fetchone()[0]
        conn.close()
        inner = json.loads(now)["modelSelection"]
        self.assertEqual("helper-local", inner["providerId"])   # ZCODE_PROVIDER_ID
        self.assertEqual("GLM-5.3-Flash", inner["modelId"])     # 原模型保留
        # 还原
        zcode_restore_resume_selection("sess_x", backup, zcode_home=root)
        conn = sqlite3.connect(os.path.join(root, "cli", "db", "db.sqlite"))
        back = conn.execute("SELECT data FROM session_entry WHERE id='sess_x:runtime-model-selection'").fetchone()[0]
        conn.close()
        self.assertEqual(wrapped, back)

    def test_noop_when_already_resolvable_or_missing(self):
        root = tempfile.mkdtemp()
        # 已指向注入 provider → 不改写
        self._make_db(root, json.dumps({"modelSelection": {"providerId": "helper-local",
                                                           "modelId": "GLM-5.3"}}))
        self.assertIsNone(zcode_rewrite_resume_selection("sess_x", zcode_home=root))
        # 无 entry / 无数据库 → 不改写也不报错
        root2 = tempfile.mkdtemp()
        self.assertIsNone(zcode_rewrite_resume_selection("sess_x", zcode_home=root2))
        self.assertIsNone(zcode_rewrite_resume_selection("sess_x", zcode_home=os.path.join(root2, "nope")))

if __name__ == "__main__":
    unittest.main()
