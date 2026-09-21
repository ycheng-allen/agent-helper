"""Local, opt-in Codex task scheduling. No prompts are sent without a saved rule."""
import glob
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
QUOTA_ERRORS = ("usage limit", "rate limit", "limit reached", "quota", "try again after")
STALE_TURN_SECONDS = 7 * 86400
_snapshot_cache = {}


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


def available_projects(conn, codex_home):
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
    return shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")


def read_quota(timeout=12):
    """Use the documented app-server read API; never infer a reset from an old log."""
    proc = subprocess.Popen([codex_bin(), "app-server"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)
    try:
        for message in (
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex-helper", "version": "0.3.2"}}},
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


def validate_rule(body, snapshots, codex_home=None, projects=None):
    kind = body.get("kind")
    trigger = body.get("trigger")
    if kind not in ("new", "next", "resume"):
        raise ValueError("请选择任务类型")
    if (kind == "new" and trigger not in ("at", "quota", "after") or
            kind == "next" and trigger != "after" or
            kind == "resume" and trigger != "quota"):
        raise ValueError("任务类型与触发条件不匹配")
    prompt = str(body.get("prompt") or "").strip()
    if kind == "resume" and not prompt:
        prompt = "额度已恢复。请继续完成刚才因额度限制中断的工作，先检查现有进度，避免重复操作。"
    if not prompt or len(prompt) > 20000:
        raise ValueError("Prompt 需要填写，且不超过 20000 字")
    thread_id = str(body.get("thread_id") or "").strip()
    if kind != "new" or trigger == "after":
        if thread_id not in {item["id"] for item in snapshots}:
            raise ValueError("请选择本机已有的 Codex 任务")
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
            "cwd": cwd, "project_mode": project_mode, "project_id": project_id,
            "project_name": project_name, "project_parent": project_parent,
            "prompt": prompt, "run_at": run_at, "quota_after": None,
            "after_turn_id": (target.get("turn") or {}).get("id") if target else None,
            "after_mtime": target["mtime"] if target else None,
            "status": "waiting", "auto": 0, "created_at": time.time(), "started_at": None,
            "finished_at": None, "error": "", "output": ""}


def init_db(conn):
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
    for name in ("project_mode", "project_id", "project_name", "project_parent"):
        if name not in cols:
            conn.execute("ALTER TABLE schedule_rules ADD COLUMN " + name + " TEXT")
    conn.execute("UPDATE schedule_rules SET status='unknown', error='应用上次退出时任务仍在运行，请检查 Codex 任务后再重建规则' WHERE status='running'")
    conn.commit()


def rule_rows(conn):
    active = conn.execute("""SELECT * FROM schedule_rules WHERE status IN ('waiting','running')
        ORDER BY COALESCE(run_at,quota_after,created_at),created_at""").fetchall()
    history = conn.execute("""SELECT * FROM schedule_rules WHERE status NOT IN ('waiting','running')
        ORDER BY created_at DESC LIMIT 100""").fetchall()
    return [dict(row) for row in [*active, *history]]


class Scheduler:
    def __init__(self, conn, lock, codex_home, demo=False):
        self.conn, self.lock, self.codex_home, self.demo = conn, lock, codex_home, demo
        self.stop_event = threading.Event()
        self.quota = None
        self.quota_error = ""
        self.quota_at = 0
        self.snapshots = []
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
        self.snapshots = task_snapshots(self.codex_home)
        with self.lock:
            setting = self.conn.execute("SELECT value FROM schedule_settings WHERE key='auto_resume_since'").fetchone()
            if setting:
                since = float(setting[0])
                for task in self.snapshots:
                    turn = task.get("turn") or {}
                    if not turn.get("finished_ts") or turn["finished_ts"] < since or turn.get("status") != "failed" or not any(
                            x in turn.get("error", "").lower() for x in QUOTA_ERRORS):
                        continue
                    exists = self.conn.execute("""SELECT 1 FROM schedule_rules WHERE kind='resume'
                        AND thread_id=? AND after_turn_id=? LIMIT 1""", (task["id"], turn.get("id"))).fetchone()
                    if not exists:
                        rule = validate_rule({"kind": "resume", "trigger": "quota", "thread_id": task["id"],
                                              "wait_for_quota": True}, [task])
                        rule["auto"] = 1
                        self.conn.execute("""INSERT INTO schedule_rules
                            (id,kind,trigger,thread_id,cwd,prompt,run_at,quota_after,after_turn_id,after_mtime,
                             status,auto,created_at,started_at,finished_at,error,output)
                             VALUES(:id,:kind,:trigger,:thread_id,:cwd,:prompt,:run_at,:quota_after,:after_turn_id,:after_mtime,
                                    :status,:auto,:created_at,:started_at,:finished_at,:error,:output)""", rule)
                self.conn.commit()
            waiting = [dict(r) for r in self.conn.execute("""SELECT * FROM schedule_rules WHERE status='waiting'
                ORDER BY COALESCE(run_at,quota_after,created_at),created_at""")]
        needs_quota = any(r["trigger"] == "quota" for r in waiting)
        if needs_quota and time.time() - self.quota_at >= QUOTA_SECONDS:
            self.quota_at = time.time()
            try:
                self.quota = read_quota()
                self.quota_error = ""
            except Exception as exc:
                self.quota = None
                self.quota_error = str(exc)[:200]
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
                    due = (turn.get("status") == "failed" and
                           any(x in turn.get("error", "").lower() for x in QUOTA_ERRORS) and
                           quota_ready(self.quota or {}))
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

    def dispatch(self, rule):
        with self.lock:
            if rule["kind"] != "new" and self.conn.execute(
                    "SELECT 1 FROM schedule_rules WHERE thread_id=? AND status='running' LIMIT 1",
                    (rule["thread_id"],)).fetchone():
                return
            updated = self.conn.execute("UPDATE schedule_rules SET status='running', started_at=? WHERE id=? AND status='waiting'",
                                        (time.time(), rule["id"])).rowcount
            self.conn.commit()
        if not updated:
            return
        threading.Thread(target=self._run, args=(rule,), daemon=True).start()

    def _run(self, rule):
        if rule["kind"] == "new":
            cmd = [codex_bin(), "exec", "--json", "--skip-git-repo-check", "-C", rule["cwd"], "-"]
        else:
            cmd = [codex_bin(), "exec", "resume", rule["thread_id"], "-"]
        try:
            if rule["kind"] == "new" and rule.get("project_mode") == "create":
                os.mkdir(rule["cwd"])
                with self.lock:
                    self.conn.execute("INSERT OR IGNORE INTO helper_projects(id,name,path,created_at) VALUES(?,?,?,?)",
                                      ("helper-" + rule["id"], rule["project_name"], rule["cwd"], time.time()))
                    self.conn.commit()
            proc = subprocess.run(cmd, input=rule["prompt"], text=True, cwd=rule["cwd"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            task_id = rule["thread_id"]
            if rule["kind"] == "new":
                for line in (proc.stdout or "").splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "thread.started":
                        task_id = event.get("thread_id") or task_id
            output = task_id or ""
            error = (proc.stderr or "")[-2000:] if proc.returncode else ""
            status = "done" if proc.returncode == 0 else "failed"
        except Exception as exc:
            output, error, status = "", str(exc), "failed"
        with self.lock:
            self.conn.execute("UPDATE schedule_rules SET status=?, finished_at=?, error=?, output=? WHERE id=?",
                              (status, time.time(), error, output, rule["id"]))
            self.conn.commit()
