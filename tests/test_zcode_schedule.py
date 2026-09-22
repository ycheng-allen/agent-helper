import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from watch_scheduler import (Scheduler, all_task_snapshots, init_db, validate_rule,
                             zcode_recent_dirs, zcode_task_snapshots)

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
        # 无 Codex 额度数据也能触发：ZCode 采用重试语义
        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": SESSION_ID,
                              "agent": "zcode", "wait_for_quota": True}, [self.snapshot()])
        self.conn.execute("""INSERT INTO schedule_rules
            (id,kind,trigger,thread_id,agent,cwd,prompt,quota_after,after_turn_id,status,auto,created_at,
             started_at,finished_at,error,output)
             VALUES(:id,:kind,:trigger,:thread_id,:agent,:cwd,:prompt,:quota_after,:after_turn_id,
                    :status,:auto,:created_at,:started_at,:finished_at,:error,:output)""", rule)
        dispatched = []
        with patch.object(self.scheduler, "dispatch", lambda r: dispatched.append(r["id"])):
            self.scheduler.tick()
        self.assertEqual([rule["id"]], dispatched)

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


if __name__ == "__main__":
    unittest.main()
