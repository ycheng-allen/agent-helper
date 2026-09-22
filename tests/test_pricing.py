import os
import sqlite3
import unittest
from unittest.mock import patch

import codex_model_watch as cmw


class PricingTest(unittest.TestCase):
    def setUp(self):
        cmw._pricing_cache.clear()
        self.addCleanup(cmw._pricing_cache.clear)

    def test_model_price_longest_prefix_and_case(self):
        p = cmw.model_price("GLM-5.3-Flash")
        self.assertEqual("cny", p["currency"])
        self.assertEqual(0.8, p["in"])
        p = cmw.model_price("gpt-5.6-sol")
        self.assertEqual("usd", p["currency"])
        self.assertEqual(20.0, p["out"])
        p = cmw.model_price("GPT-5.6-LUNA")
        self.assertEqual(0.20, p["in"])
        self.assertIsNone(cmw.model_price("totally-unknown-model"))

    def test_price_tokens_splits_cached_from_fresh(self):
        # 1M fresh in + 1M cached in + 1M out on glm-5.3 (8/1.6/28 CNY)
        cost = cmw.price_tokens("glm-5.3", 2_000_000, 1_000_000, 1_000_000, 7.1)
        self.assertEqual("cny", cost["currency"])
        self.assertAlmostEqual(8 + 1.6 + 28, cost["amount"], places=2)
        # usd conversion
        cost = cmw.price_tokens("gpt-5.4", 1_000_000, 0, 1_000_000, 7.1)
        self.assertAlmostEqual(2.5 + 15, cost["amount"], places=2)
        self.assertAlmostEqual((2.5 + 15) * 7.1, cost["cny"], places=2)

    def test_pricing_json_override(self):
        with tempfile_ctx() as tmp:
            import json
            os.makedirs(tmp, exist_ok=True)
            json.dump({"usd_cny": 8.0,
                       "rates": {"totally-unknown-model": {"in": 1, "out": 3, "currency": "usd"}}},
                      open(os.path.join(tmp, "pricing.json"), "w"))
            with patch.object(cmw, "APP_DIR", tmp):
                p = cmw.model_price("totally-unknown-model")
                self.assertEqual(1, p["in"])
                rates, usd_cny = cmw.load_pricing()
                self.assertEqual(8.0, usd_cny)

    def test_compute_cost_month_per_agent(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp
        conn = cmw.db_connect(os.path.join(self.tmp.name, "state.db"))
        rows = [
            ("2026-09-01T01:00:00Z", "glm-5.3", "zcode", 1_000_000, 0, 1_000_000),
            ("2026-09-15T02:00:00Z", "gpt-5.4", "codex", 1_000_000, 0, 1_000_000),
            ("2026-08-20T03:00:00Z", "gpt-5.4", "codex", 1_000_000, 0, 1_000_000),  # 上月，不计
        ]
        for i, (ts, model, agent, tin, tc, tout) in enumerate(rows):
            conn.execute("""INSERT INTO turns(file,turn_id,ts,project,requested,served,agent,
                            in_tokens,cached_tokens,out_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                         ("f%d" % i, "t%d" % i, ts, "p", model, model, agent, tin, tc, tout))
        conn.commit()
        cutoff = cmw.month_cutoff()
        from datetime import datetime, timezone as tz
        expected = datetime.now().astimezone().replace(day=1, hour=0, minute=0, second=0,
                                                       microsecond=0).astimezone(tz.utc)
        self.assertEqual(expected.strftime("%Y-%m-%dT%H:%M:%SZ"), cutoff)
        z = cmw.compute_cost(conn, "zcode", cutoff)
        self.assertAlmostEqual(36.0, z["cny"], places=1)   # glm-5.3 in+out
        c = cmw.compute_cost(conn, "codex", cutoff)
        self.assertAlmostEqual(17.5, c["usd"], places=1)   # 本月 gpt-5.4 in+out
        both = cmw.compute_cost(conn, "", cutoff)
        self.assertEqual(0, both["unpriced_tokens"])

    def test_api_data_cost_has_no_combined_fields(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp
        conn = cmw.db_connect(os.path.join(self.tmp.name, "state.db"))
        conn.execute("""INSERT INTO turns(file,turn_id,ts,project,requested,served,agent,
                        in_tokens,cached_tokens,out_tokens) VALUES('f','t','2026-09-10T00:00:00Z',
                        'p','glm-5.3','glm-5.3','zcode',1000,0,2000)""")
        conn.commit()
        data = cmw.api_data(conn, 0, "zcode")
        assert "month_total" not in data["cost"], "agent 空间不应返回合并费用"
        assert data["cost"]["month"]["cny"] > 0

    def test_clear_probes_filters_by_agent(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = cmw.db_connect(os.path.join(tmp.name, "state.db"))
        for agent in ("codex", "zcode"):
            conn.execute("INSERT INTO probes(ts,requested,served,swapped,latency_ms,safety_header,error,agent) "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         ("2026-09-22T00:00:0%sZ" % (0 if agent == "codex" else 5),
                          "m", "m", 0, 1, "", None, agent))
        conn.commit()
        self.assertEqual(1, cmw.clear_probes(conn, "zcode"))
        self.assertEqual(1, cmw.clear_probes(conn, "codex"))
        self.assertEqual(0, cmw.clear_probes(conn, "codex"))
        with self.assertRaises(ValueError):
            cmw.clear_probes(conn, "bogus")

    def test_unpriced_models_counted_separately(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp
        conn = cmw.db_connect(os.path.join(self.tmp.name, "state.db"))
        conn.execute("""INSERT INTO turns(file,turn_id,ts,project,requested,served,agent,
                        in_tokens,cached_tokens,out_tokens) VALUES('f','t','2026-09-10T00:00:00Z',
                        'p','mystery','mystery','zcode',1000,0,2000)""")
        conn.commit()
        r = cmw.compute_cost(conn, "zcode", cmw.month_cutoff())
        self.assertEqual(3000, r["unpriced_tokens"])
        self.assertEqual(0.0, r["cny"])


def tempfile_ctx():
    import tempfile
    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
