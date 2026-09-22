import os
import sqlite3
import tempfile
import unittest

from codex_model_watch import api_data, db_connect, import_zcode

SESSION_ID = "sess_test-0000"
TURN_MS = 1790067109494


def make_zcode_db(path, cwd="/Users/demo/my-app"):
    os.makedirs(os.path.join(path, "cli", "db"), exist_ok=True)
    db = sqlite3.connect(os.path.join(path, "cli", "db", "db.sqlite"))
    db.executescript("""
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
        time_updated integer not null, time_compacting integer, time_archived integer);
    """)
    db.execute("INSERT INTO session(id, project_id, slug, directory, title, version, time_created, time_updated) "
               "VALUES(?,?,?,?,?,?,?,?)", (SESSION_ID, "p1", "demo", cwd, "Demo", "1.0", TURN_MS, TURN_MS))
    db.execute("""INSERT INTO turn_usage(session_id, turn_id, status, started_at, completed_at, duration_ms,
        time_to_first_token_ms, input_tokens, output_tokens, cache_read_input_tokens)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (SESSION_ID, "turn_a", "completed", TURN_MS, TURN_MS + 5000, 5000, 900, 57000, 300, 56000))
    db.execute("""INSERT INTO model_usage(logical_request_id, session_id, turn_id, query_source, provider_id,
        model_id, variant, status, started_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        ("lr_a", SESSION_ID, "turn_a", "main_turn", "account:bigmodel", "GLM-5.3", "high", "completed", TURN_MS))
    db.commit()
    return db


class ZcodeImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.zcode_home = os.path.join(self.tmp.name, "zcode-home")
        make_zcode_db(self.zcode_home)
        self.conn = db_connect(os.path.join(self.tmp.name, "state.db"))

    def rows(self, *columns):
        return [dict(r) for r in self.conn.execute("SELECT " + ",".join(columns) +
                                                   " FROM turns WHERE agent='zcode'").fetchall()]

    def test_imports_model_tokens_and_project(self):
        stats = import_zcode(self.conn, self.zcode_home, 30)
        self.assertEqual(1, stats["turns"])
        row = self.rows("turn_id", "ts", "served", "agent", "in_tokens", "cached_tokens",
                        "out_tokens", "duration_ms", "ttft_ms", "project")[0]
        self.assertEqual("turn_a", row["turn_id"])
        self.assertEqual("GLM-5.3", row["served"])
        self.assertEqual("zcode", row["agent"])
        self.assertEqual(57000, row["in_tokens"])
        self.assertEqual(56000, row["cached_tokens"])
        self.assertEqual(300, row["out_tokens"])
        self.assertEqual(5000, row["duration_ms"])
        self.assertEqual(900, row["ttft_ms"])
        self.assertEqual("my-app", row["project"])

    def test_second_run_is_idempotent(self):
        import_zcode(self.conn, self.zcode_home, 30)
        import_zcode(self.conn, self.zcode_home, 30)
        self.assertEqual(1, len(self.rows("turn_id")))

    def test_updated_turn_is_overwritten(self):
        import_zcode(self.conn, self.zcode_home, 30)
        db = sqlite3.connect(os.path.join(self.zcode_home, "cli", "db", "db.sqlite"))
        db.execute("UPDATE turn_usage SET status='error', error_type='rate_limited', input_tokens=60000 "
                   "WHERE turn_id='turn_a'")
        db.commit()
        db.close()
        import_zcode(self.conn, self.zcode_home, 30)
        row = self.rows("in_tokens", "error_kind")[0]
        self.assertEqual(60000, row["in_tokens"])
        self.assertEqual("rate_limit", row["error_kind"])

    def test_max_age_days_skips_old_rows(self):
        import_zcode(self.conn, self.zcode_home, 0.001)
        self.assertEqual([], self.rows("turn_id"))

    def test_missing_db_reports_note(self):
        stats = import_zcode(self.conn, os.path.join(self.tmp.name, "absent"), 30)
        self.assertIn("note", stats)

    def test_api_data_filters_by_agent(self):
        import_zcode(self.conn, self.zcode_home, 30)
        self.conn.execute("""INSERT INTO turns(file, turn_id, ts, project, requested, served, agent)
                             VALUES('demo.jsonl','t1','2026-09-22T00:00:00Z','p','gpt-5.6','gpt-5.6','codex')""")
        self.conn.commit()
        codex = api_data(self.conn, 0, "codex")
        zcode = api_data(self.conn, 0, "zcode")
        both = api_data(self.conn, 0, "")
        self.assertEqual(["gpt-5.6"], [m["model"] for m in codex["models"]])
        self.assertEqual(["GLM-5.3"], [m["model"] for m in zcode["models"]])
        self.assertEqual({"codex", "zcode"}, {a["agent"] for a in both["agents"]})
        self.assertEqual(1, codex["summary"]["turns"])
        self.assertEqual(1, zcode["summary"]["turns"])
        self.assertEqual(2, both["summary"]["turns"])


if __name__ == "__main__":
    unittest.main()
