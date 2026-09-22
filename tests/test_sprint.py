import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from watch_scheduler import (Scheduler, init_db, sprint_window, validate_sprint)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class FakeProc:
    def __init__(self, returncode=0, stdout='sess_11111111-2222-3333-4444-555555555555', delay=0):
        self.returncode = None
        self._rc = returncode
        self._stdout = stdout
        self._delay = delay
        self.terminated = False
        self.killed = False

    def communicate(self):
        time.sleep(self._delay)
        self.returncode = self._rc
        return self._stdout + "\n", ""

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def insert_sprint(conn, sp, tasks):
    conn.execute("""INSERT INTO sprints
        (id,name,agent,kind,start_hm,end_hm,start_at,end_at,cwd,concurrency,status,created_at,stopped_at)
        VALUES(:id,:name,:agent,:kind,:start_hm,:end_hm,:start_at,:end_at,:cwd,:concurrency,
               :status,:created_at,:stopped_at)""", sp)
    for t in tasks:
        conn.execute("""INSERT INTO sprint_tasks
            (id,sprint_id,prompt,position,status,session_id,error,output,started_at,finished_at,attempts)
            VALUES(:id,:sprint_id,:prompt,:position,:status,:session_id,:error,:output,
                   :started_at,:finished_at,:attempts)""", t)
    conn.commit()


class SprintValidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def base(self, **kw):
        body = {"kind": "once", "cwd": self.tmp.name, "concurrency": 1,
                "prompts": ["task one"], "start_at": iso(time.time()),
                "end_at": iso(time.time() + 3600)}
        body.update(kw)
        return body

    def test_once_window(self):
        sp = validate_sprint(self.base(), [])
        self.assertEqual(1, len(sp["tasks"]))
        a, b = sprint_window(sp)
        self.assertEqual(sp["start_at"], a)
        self.assertEqual(sp["end_at"], b)

    def test_daily_crossing_midnight(self):
        sp = validate_sprint(self.base(kind="daily", start_hm="22:00", end_hm="06:00"), [])
        now = datetime.now().replace(hour=23, minute=0, second=0, microsecond=0).timestamp()
        a, b = sprint_window(sp, now)
        self.assertTrue(a <= now < b)
        self.assertEqual(8 * 3600, b - a)

    def test_daily_next_window_when_past(self):
        sp = validate_sprint(self.base(kind="daily", start_hm="00:00", end_hm="08:00"), [])
        late = datetime.now().replace(hour=23, minute=50, second=0, microsecond=0).timestamp()
        a, _ = sprint_window(sp, late)
        self.assertGreater(a, late)

    def test_rejects_empty_queue_and_bad_concurrency(self):
        with self.assertRaises(ValueError):
            validate_sprint(self.base(prompts=[]), [])
        with self.assertRaises(ValueError):
            validate_sprint(self.base(end_at=iso(time.time() - 10)), [])
        sp = validate_sprint(self.base(concurrency=99), [])
        self.assertEqual(6, sp["concurrency"])


class SprintRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)
        self.scheduler = Scheduler(self.conn, threading.Lock(),
                                   os.path.join(self.tmp.name, "codex-home"),
                                   zcode_home=os.path.join(self.tmp.name, "zcode-home"),
                                   agents=["zcode"])
        self.procs = []

    def fake_popen(self, returncode=0, delay=0, stdout="sess_11111111-2222-3333-4444-555555555555"):
        def factory(cmd, **kwargs):
            proc = FakeProc(returncode=returncode, delay=delay, stdout=stdout)
            self.procs.append(proc)
            return proc
        return factory

    def run_sprint(self, prompts=("a", "b", "c"), concurrency=1, window=(None, None)):
        sp = {"id": "sp1", "name": "test", "agent": "zcode", "kind": "once",
              "start_hm": None, "end_hm": None,
              "start_at": window[0] or (time.time() - 10),
              "end_at": window[1] or (time.time() + 3600),
              "cwd": self.tmp.name, "concurrency": concurrency,
              "status": "waiting", "created_at": time.time(), "stopped_at": None}
        tasks = [{"id": "t%d" % i, "sprint_id": "sp1", "prompt": p, "position": i,
                  "status": "pending", "session_id": None, "error": None, "output": None,
                  "started_at": None, "finished_at": None, "attempts": 0}
                 for i, p in enumerate(prompts)]
        insert_sprint(self.conn, sp, tasks)
        return sp, tasks

    def tick_with_mocks(self):
        with patch("watch_scheduler.zcode_cmd", return_value=["node", "/fake/zcode.cjs"]), \
             patch("watch_scheduler.zcode_env", return_value=({"Z": "1"}, "")), \
             patch("watch_scheduler.subprocess.Popen", side_effect=self.fake_popen()):
            self.scheduler.tick_sprints()
            # 等待 watcher 线程完成
            deadline = time.time() + 3
            while self.scheduler.sprint_procs and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.2)

    def test_serial_runs_one_at_a_time_and_completes(self):
        sp, _ = self.run_sprint(prompts=("a", "b", "c"), concurrency=1)
        self.tick_with_mocks()
        statuses = [r[0] for r in self.conn.execute(
            "SELECT status FROM sprint_tasks ORDER BY position").fetchall()]
        self.assertEqual(["done", "done", "done"], statuses)
        row = self.conn.execute("SELECT status, stopped_at FROM sprints WHERE id='sp1'").fetchone()
        self.assertEqual("done", row["status"])
        sess = self.conn.execute(
            "SELECT session_id FROM sprint_tasks WHERE id='t0'").fetchone()[0]
        self.assertIn("sess_", sess or "")

    def test_parallel_fills_multiple_slots(self):
        started = []

        def slow_popen(cmd, **kwargs):
            proc = FakeProc(delay=0.4)
            started.append(time.time())
            self.procs.append(proc)
            return proc

        self.run_sprint(prompts=("a", "b"), concurrency=2)
        with patch("watch_scheduler.zcode_cmd", return_value=["node", "/fake/zcode.cjs"]), \
             patch("watch_scheduler.zcode_env", return_value=({"Z": "1"}, "")), \
             patch("watch_scheduler.subprocess.Popen", side_effect=slow_popen):
            self.scheduler.tick_sprints()
            time.sleep(0.05)
            running = self.conn.execute(
                "SELECT COUNT(*) FROM sprint_tasks WHERE status='running'").fetchone()[0]
            self.assertEqual(2, running)  # 两个槽位同时占满
            deadline = time.time() + 3
            while self.scheduler.sprint_procs and time.time() < deadline:
                time.sleep(0.05)

    def test_window_end_stops_running_and_skips_pending(self):
        end = [time.time() + 3600]
        self.run_sprint(prompts=("a", "b"), concurrency=1,
                        window=(time.time() - 10, end[0]))
        with patch("watch_scheduler.zcode_cmd", return_value=["node", "/fake/zcode.cjs"]), \
             patch("watch_scheduler.zcode_env", return_value=({"Z": "1"}, "")), \
             patch("watch_scheduler.subprocess.Popen", side_effect=self.fake_popen(delay=5)):
            self.scheduler.tick_sprints()  # t0 进入 running
            time.sleep(0.2)
            self.assertEqual(1, len(self.procs))
            end[0] = time.time() - 1  # 窗口到期
            self.conn.execute("UPDATE sprints SET end_at=? WHERE id='sp1'", (end[0],))
            self.conn.commit()
            self.scheduler.tick_sprints()  # 触发 finish_sprint
        self.assertTrue(self.procs[0].terminated)
        statuses = {r[0]: r[1] for r in self.conn.execute(
            "SELECT id, status FROM sprint_tasks").fetchall()}
        self.assertEqual({"t0": "stopped", "t1": "skipped"}, statuses)
        sprint = self.conn.execute("SELECT status FROM sprints WHERE id='sp1'").fetchone()[0]
        self.assertEqual("done", sprint)

    def test_manual_stop_terminates_running(self):
        self.run_sprint(prompts=("a", "b"), concurrency=1)
        with patch("watch_scheduler.zcode_cmd", return_value=["node", "/fake/zcode.cjs"]), \
             patch("watch_scheduler.zcode_env", return_value=({"Z": "1"}, "")), \
             patch("watch_scheduler.subprocess.Popen", side_effect=self.fake_popen(delay=5)):
            self.scheduler.tick_sprints()
            time.sleep(0.2)
            self.scheduler.stop_sprint("sp1")
        self.assertTrue(self.procs[0].terminated)
        row = self.conn.execute("SELECT status FROM sprint_tasks WHERE id='t0'").fetchone()
        self.assertEqual("stopped", row["status"])
        row = self.conn.execute("SELECT status FROM sprint_tasks WHERE id='t1'").fetchone()
        self.assertEqual("skipped", row["status"])
        row = self.conn.execute("SELECT status FROM sprints WHERE id='sp1'").fetchone()[0]
        self.assertEqual("stopped", row)

    def test_failed_task_marks_failed(self):
        self.run_sprint(prompts=("bad",), concurrency=1)
        with patch("watch_scheduler.zcode_cmd", return_value=["node", "/fake/zcode.cjs"]), \
             patch("watch_scheduler.zcode_env", return_value=({"Z": "1"}, "")), \
             patch("watch_scheduler.subprocess.Popen",
                   side_effect=self.fake_popen(returncode=1, stdout="", delay=0)) as _p:
            self.scheduler.tick_sprints()
            deadline = time.time() + 3
            while self.scheduler.sprint_procs and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.2)
        row = self.conn.execute("SELECT status FROM sprint_tasks WHERE id='t0'").fetchone()
        self.assertEqual("failed", row[0])


class SprintCreateProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def base(self, **kw):
        body = {"kind": "once", "project_mode": "create",
                "project_name": "fresh-" + self.id()[-8:],
                "project_parent": self.tmp.name,
                "concurrency": 1, "prompts": ["t1"],
                "start_at": iso(time.time()), "end_at": iso(time.time() + 3600)}
        body.update(kw)
        return body

    def test_create_project_cwd_and_fields(self):
        sp = validate_sprint(self.base())
        self.assertEqual("create", sp["project_mode"])
        self.assertTrue(sp["cwd"].startswith(os.path.realpath(self.tmp.name)))
        self.assertFalse(os.path.isdir(sp["cwd"]))  # 触发前不建目录

    def test_create_project_rejects_existing_dir(self):
        body = self.base(project_name="already")
        os.makedirs(os.path.join(self.tmp.name, "already"))
        with self.assertRaises(ValueError):
            validate_sprint(body)

    def test_create_project_rejects_bad_name(self):
        with self.assertRaises(ValueError):
            validate_sprint(self.base(project_name="a/b"))
        with self.assertRaises(ValueError):
            validate_sprint(self.base(project_parent="/nonexistent-dir-xyz"))

    def test_launch_creates_project_dir(self):
        import threading as th
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        init_db(conn)
        s = Scheduler(conn, th.Lock(), os.path.join(self.tmp.name, "cx"),
                      zcode_home=os.path.join(self.tmp.name, "zc"), agents=["zcode"])
        sp = validate_sprint(self.base())
        sp["id"] = "spc"
        tasks = sp["tasks"]
        insert_sprint(conn, sp, tasks)
        procs = []
        with patch("watch_scheduler.zcode_cmd", return_value=["node", "/f.cjs"]), \
             patch("watch_scheduler.zcode_env", return_value=({"Z": "1"}, "")), \
             patch("watch_scheduler.subprocess.Popen",
                   side_effect=lambda cmd, **kw: procs.append(FakeProc()) or FakeProc()):
            s.launch_sprint_task(sp, tasks[0])
            time.sleep(0.3)
        self.assertTrue(os.path.isdir(sp["cwd"]))
        row = conn.execute("SELECT COUNT(*) FROM helper_projects").fetchone()[0]
        self.assertEqual(1, row)


if __name__ == "__main__":
    unittest.main()
