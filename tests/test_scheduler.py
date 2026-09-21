import json
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from watch_scheduler import Scheduler, available_projects, init_db, next_reset, project_catalog, quota_ready, task_snapshots, validate_rule


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = "12345678-1234-1234-1234-123456789abc"
        directory = os.path.join(self.tmp.name, "sessions", "2026", "09", "21")
        os.makedirs(directory)
        self.path = os.path.join(directory, "rollout-" + self.session + ".jsonl")
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def write(self, event, error=None):
        rows = [
            {"type": "session_meta", "payload": {"id": self.session, "cwd": self.tmp.name}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-one"}},
        ]
        if event:
            payload = {"type": "task_complete", "turn_id": "turn-one"}
            if error:
                payload["error"] = {"message": error}
            rows.append({"timestamp": datetime.now(timezone.utc).isoformat(), "type": "event_msg", "payload": payload})
        with open(self.path, "w") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def save(self, rule):
        self.conn.execute("""INSERT INTO schedule_rules
            (id,kind,trigger,thread_id,cwd,prompt,run_at,quota_after,after_turn_id,after_mtime,
             status,auto,created_at,started_at,finished_at,error,output)
             VALUES(:id,:kind,:trigger,:thread_id,:cwd,:prompt,:run_at,:quota_after,:after_turn_id,:after_mtime,
                    :status,:auto,:created_at,:started_at,:finished_at,:error,:output)""", rule)
        self.conn.commit()

    def state_db(self, project_path):
        state = sqlite3.connect(os.path.join(self.tmp.name, "state_5.sqlite"))
        state.executescript("""CREATE TABLE projects(id TEXT,name TEXT,position INTEGER);
            CREATE TABLE project_roots(project_id TEXT,position INTEGER,path TEXT);
            CREATE TABLE threads(id TEXT,name TEXT,title TEXT,preview TEXT,source TEXT,archived INTEGER,
                updated_at INTEGER,rollout_path TEXT,cwd TEXT,project_id TEXT);""")
        state.execute("INSERT INTO projects VALUES('project-1','My Project',0)")
        state.execute("INSERT INTO project_roots VALUES('project-1',0,?)", (project_path,))
        state.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (self.session, "Fix the login flow", "", "", "vscode", 0, 1,
                       self.path, self.tmp.name, "project-1"))
        state.commit()
        state.close()

    def test_next_step_waits_for_current_turn_completion(self):
        self.write(False)
        snapshots = task_snapshots(self.tmp.name)
        rule = validate_rule({"kind": "next", "trigger": "after", "thread_id": self.session,
                              "prompt": "Check the result"}, snapshots)
        self.save(rule)
        scheduler = Scheduler(self.conn, threading.Lock(), self.tmp.name)
        fired = []
        scheduler.dispatch = lambda item: fired.append(item["id"])
        scheduler.tick()
        self.assertEqual(fired, [])
        self.write(True)
        scheduler.tick()
        self.assertEqual(fired, [rule["id"]])

    def test_quota_rule_waits_for_reset_and_fresh_capacity(self):
        self.write(False)
        rule = validate_rule({"kind": "new", "trigger": "quota", "cwd": self.tmp.name,
                              "prompt": "Build it"}, task_snapshots(self.tmp.name))
        rule["quota_after"] = 9999999999
        self.save(rule)
        scheduler = Scheduler(self.conn, threading.Lock(), self.tmp.name)
        fired = []
        scheduler.dispatch = lambda item: fired.append(item["id"])
        with patch("watch_scheduler.read_quota", return_value={"rateLimits": {"primary": {"usedPercent": 10}}}):
            scheduler.tick()
        self.assertEqual(fired, [])
        self.conn.execute("UPDATE schedule_rules SET quota_after=0")
        self.conn.commit()
        scheduler.quota_at = 0
        with patch("watch_scheduler.read_quota", return_value={"rateLimits": {"primary": {"usedPercent": 100}}}):
            scheduler.tick()
        self.assertEqual(fired, [])
        scheduler.quota_at = 0
        with patch("watch_scheduler.read_quota", return_value={"rateLimits": {"primary": {"usedPercent": 5}}}):
            scheduler.tick()
        self.assertEqual(fired, [rule["id"]])

    def test_reset_window_and_unavailable_quota(self):
        data = {"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": 50},
                               "secondary": {"usedPercent": 100, "resetsAt": 100}}}
        self.assertEqual(next_reset(data), 100)
        self.assertFalse(quota_ready(data))

    def test_auto_resume_creates_one_rule_for_quota_failure(self):
        self.write(True, "Usage limit reached")
        self.conn.execute("INSERT INTO schedule_settings VALUES('auto_resume_since',?)", (str(time.time() - 2),))
        self.conn.commit()
        scheduler = Scheduler(self.conn, threading.Lock(), self.tmp.name)
        with patch("watch_scheduler.read_quota", return_value={"rateLimits": {"primary": {"usedPercent": 100}}}):
            scheduler.tick()
            scheduler.tick()
        rows = list(self.conn.execute("SELECT * FROM schedule_rules"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["auto"], 1)
        self.assertEqual(rows[0]["status"], "waiting")

    def test_task_picker_uses_thread_title_and_saved_project(self):
        self.write(False)
        self.state_db(self.tmp.name)
        tasks = task_snapshots(self.tmp.name)
        self.assertEqual(tasks[0]["title"], "Fix the login flow")
        self.assertEqual(tasks[0]["project_name"], "My Project")
        self.assertEqual(project_catalog(self.tmp.name)[0]["id"], "project-1")

    def test_picker_includes_older_work_from_other_projects(self):
        self.write(True)
        self.state_db(self.tmp.name)
        other_id = "87654321-1234-1234-1234-123456789abc"
        other_dir = os.path.join(self.tmp.name, "other-project")
        os.mkdir(other_dir)
        other_path = os.path.join(self.tmp.name, "sessions", "2026", "09", "21", "rollout-" + other_id + ".jsonl")
        with open(other_path, "w") as stream:
            stream.write(json.dumps({"type": "session_meta", "payload": {"id": other_id, "cwd": other_dir}}) + "\n")
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old-turn"}}) + "\n")
        old = time.time() - 90 * 86400
        os.utime(other_path, (old, old))
        state = sqlite3.connect(os.path.join(self.tmp.name, "state_5.sqlite"))
        state.execute("INSERT INTO projects VALUES('project-2','Other Project',1)")
        state.execute("INSERT INTO project_roots VALUES('project-2',0,?)", (other_dir,))
        state.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (other_id, "Old finished work", "", "", "vscode", 0, 0,
                       other_path, other_dir, "project-2"))
        state.commit()
        state.close()
        tasks = task_snapshots(self.tmp.name)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(next(t for t in tasks if t["id"] == other_id)["project_name"], "Other Project")

    def test_finished_work_runs_immediately_for_next_and_resume(self):
        self.write(True)
        tasks = task_snapshots(self.tmp.name)
        scheduler = Scheduler(self.conn, threading.Lock(), self.tmp.name)
        fired = []
        scheduler.dispatch = lambda item: fired.append(item["id"])
        for kind, expected in (("next", "after"), ("resume", "quota")):
            rule = validate_rule({"kind": kind, "trigger": expected, "thread_id": self.session,
                                  "prompt": "Continue the work"}, tasks)
            self.assertEqual(rule["trigger"], "immediate")
            self.save(rule)
            scheduler.tick()
            self.assertIn(rule["id"], fired)

    def test_old_log_without_completion_is_stopped(self):
        self.write(False)
        old = time.time() - 8 * 86400
        os.utime(self.path, (old, old))
        task = task_snapshots(self.tmp.name)[0]
        self.assertEqual(task["turn"]["status"], "stopped")
        rule = validate_rule({"kind": "next", "trigger": "after", "thread_id": self.session,
                              "prompt": "Continue"}, [task])
        self.assertEqual(rule["trigger"], "immediate")

    def test_new_task_can_use_existing_or_create_project(self):
        self.state_db(self.tmp.name)
        existing = validate_rule({"kind": "new", "trigger": "quota", "prompt": "Build it",
                                  "project_mode": "existing", "project_id": "project-1"}, [], self.tmp.name)
        self.assertEqual(existing["cwd"], self.tmp.name)
        self.assertEqual(existing["project_name"], "My Project")
        created = validate_rule({"kind": "new", "trigger": "at", "prompt": "Build it",
                                 "run_at": "2030-01-01T00:00:00Z", "project_mode": "create",
                                 "project_name": "New Project", "project_parent": self.tmp.name}, [], self.tmp.name)
        self.assertEqual(created["cwd"], os.path.join(os.path.realpath(self.tmp.name), "New Project"))
        self.assertFalse(os.path.exists(created["cwd"]))
        with self.assertRaises(ValueError):
            validate_rule({"kind": "new", "trigger": "quota", "prompt": "Build it",
                           "project_mode": "create", "project_name": "../escape",
                           "project_parent": self.tmp.name}, [], self.tmp.name)

    def test_new_project_directory_is_created_when_rule_runs(self):
        rule = validate_rule({"kind": "new", "trigger": "at", "prompt": "Build it",
                              "run_at": "2030-01-01T00:00:00Z", "project_mode": "create",
                              "project_name": "New Project", "project_parent": self.tmp.name}, [], self.tmp.name)
        self.save(rule)
        scheduler = Scheduler(self.conn, threading.Lock(), self.tmp.name)
        result = SimpleNamespace(returncode=0, stdout='{"type":"thread.started","thread_id":"new-task"}', stderr='')
        with patch("watch_scheduler.subprocess.run", return_value=result) as run:
            scheduler._run(rule)
        self.assertTrue(os.path.isdir(rule["cwd"]))
        self.assertEqual(run.call_args.kwargs["cwd"], rule["cwd"])
        self.assertEqual(self.conn.execute("SELECT status,output FROM schedule_rules").fetchone()[:],
                         ("done", "new-task"))
        projects = available_projects(self.conn, self.tmp.name)
        self.assertEqual(projects[0]["name"], "New Project")
        follow_up = validate_rule({"kind": "new", "trigger": "quota", "prompt": "Continue",
                                   "project_mode": "existing", "project_id": projects[0]["id"]},
                                  [], self.tmp.name, projects)
        self.assertEqual(follow_up["cwd"], rule["cwd"])


if __name__ == "__main__":
    unittest.main()
