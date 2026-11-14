"""Credential-free tests for moatmap.explorer / moatmap.bq / moatmap.env.

Run:  python -m unittest discover -s tests -v

Everything here uses SYNTHETIC, hand-made fixtures (tests/fixtures/*SYNTHETIC*) and fake
HTTP objects. Nothing is written under data/ and no number produced here is evidence.
"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moatmap import bq, env, explorer  # noqa: E402
from moatmap.config import BQ_DATASET, SQL_DIR  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
D10, D11 = 1768003200, 1768089600  # 2026-01-10 / 2026-01-11 00:00 UTC


def fixture_rows():
    with open(FIX / "explorer_SYNTHETIC_fixture.jsonl", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def fixture_tokens():
    toks = json.loads((FIX / "tokens_SYNTHETIC_fixture.json").read_text(encoding="utf-8"))
    return {t["assetId"]: t["blockchain"] for t in toks}


class AggregateTest(unittest.TestCase):
    def setUp(self):
        self.agg = explorer.aggregate(fixture_rows(), fixture_tokens(), source="synthetic_fixture")

    def test_routes(self):
        self.assertEqual(self.agg["routes"], [
            ["2026-01-10", "btc", "sol", 1, "2000.00", "5.0000", "0.0000", "synthetic_fixture"],
            ["2026-01-10", "eth", "near", 2, "1500.00", "6.0000", "1.0000", "synthetic_fixture"],
            ["2026-01-11", "arb", "near", 1, "300.00", "3.0000", "0.0000", "synthetic_fixture"],
            ["2026-01-11", "btc", "sol", 1, "0.00", "0.0000", "0.0000", "synthetic_fixture"],
            ["2026-01-11", "eth", "unknown", 1, "100.00", "0.0000", "0.0000", "synthetic_fixture"],
        ])

    def test_coverage_counts(self):
        c = self.agg["coverage"]
        self.assertEqual(c["rows_read"], 8)
        self.assertEqual(c["rows_aggregated"], 6)          # duplicate of A and the REFUNDED row excluded
        self.assertEqual(c["duplicates_ignored"], 1)
        self.assertEqual(c["non_success_ignored"], 1)
        self.assertEqual(c["unpriced_rows"], 1)
        self.assertEqual(c["rows_with_unknown_chain"], 1)   # counted, not dropped
        self.assertEqual(c["asset_mapping_method_counts"], {"token_list": 10, "omft_pattern": 1, "unknown": 1})
        self.assertEqual(c["earliest_created_at"], "2026-01-10T01:00:00Z")
        self.assertEqual(c["latest_created_at"], "2026-01-11T00:00:20Z")
        self.assertEqual(c["total_volume_usd"], 3900.0)
        self.assertEqual(sum(r[3] for r in self.agg["routes"]), c["rows_aggregated"])

    def test_fee_recipients(self):
        rec = {r[0]: r for r in self.agg["recipients"]}
        self.assertEqual(rec["some-app.near"], ["some-app.near", 4, "3300.00", "13.0000", "25", "100", "225", 0])
        self.assertEqual(rec["fefundsadmin.sputnik-dao.near"][1:], [1, "1000.00", "1.0000", "10", "10", "10", 1])

    def test_privacy(self):
        self.assertEqual(self.agg["privacy"], [
            ["2026-01-10", "ORIGIN_CHAIN", "DESTINATION_CHAIN", 2, "3000.00"],
            ["2026-01-10", "ORIGIN_CHAIN", "INTENTS", 1, "500.00"],
            ["2026-01-11", "CONFIDENTIAL_INTENTS", "CONFIDENTIAL_INTENTS", 1, "100.00"],
            ["2026-01-11", "ORIGIN_CHAIN", "DESTINATION_CHAIN", 2, "300.00"],
        ])

    def test_near_fee_blank_when_no_near_wallet_recipient(self):
        rows = fixture_rows()
        for r in rows:
            r["appFees"] = [f for f in r["appFees"] if f["recipient"] != "fefundsadmin.sputnik-dao.near"]
        agg = explorer.aggregate(rows, fixture_tokens(), source="synthetic_fixture")
        self.assertTrue(all(r[6] == "" for r in agg["routes"]))
        self.assertTrue(agg["coverage"]["near_fee_attribution"].startswith("EMPTY"))

    def test_window_filter(self):
        agg = explorer.aggregate(fixture_rows(), fixture_tokens(), "synthetic_fixture", start=D11, end=D11 + 86399)
        self.assertEqual(agg["coverage"]["rows_aggregated"], 3)
        self.assertEqual({r[0] for r in agg["routes"]}, {"2026-01-11"})

    def test_write_outputs_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            explorer.write_outputs(self.agg, Path(tmp))
            head = lambda n: (Path(tmp) / n).read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(head("swaps_by_route.csv"),
                             "day,origin_chain,dest_chain,n_swaps,volume_usd,fee_usd,near_fee_usd,source")
            self.assertTrue(head("fee_recipients.csv").startswith("recipient,n_swaps,volume_usd,fee_usd"))
            self.assertEqual(head("swaps_by_privacy.csv"), "day,deposit_type,recipient_type,n_swaps,volume_usd")


class FakeApi:
    """In-memory stand-in for GET /transactions: newest first, cursor = last row of previous page."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: -r["createdAtTimestamp"])
        self.calls = []

    def get_page(self, params):
        self.calls.append(dict(params))
        assert params["statuses"] == "SUCCESS" and params["direction"] == "next"
        lo, hi = params["startTimestampUnix"], params["endTimestampUnix"]
        sel = [r for r in self.rows if lo < r["createdAtTimestamp"] < hi]   # both bounds non-inclusive
        if "lastDepositAddress" in params:
            idx = [r["depositAddress"] for r in sel].index(params["lastDepositAddress"])
            sel = sel[idx + 1:]
        return [dict(r) for r in sel[:params["numberOfTransactions"]]]


def synth(n, t0):
    return [{"depositAddress": f"synthetic-{i}", "depositMemo": None, "intentHashes": f"h{i}", "status": "SUCCESS",
             "createdAtTimestamp": t0 + i * 60, "originAsset": "nep141:wrap.near",
             "destinationAsset": "nep141:wrap.near", "amountInUsd": "1", "appFees": []} for i in range(n)]


class PullTest(unittest.TestCase):
    def test_resume_dedupe_and_incremental(self):
        t0 = D10
        api = FakeApi(synth(25, t0))
        now = t0 + 10 * 86400
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            s1 = explorer.pull(api, t0, now, raw, page_size=10, max_pages=1, now=now)
            self.assertEqual((s1["new_rows"], s1["complete"]), (10, False))
            self.assertIsNotNone(explorer.load_checkpoint(raw)["active"])

            s2 = explorer.pull(api, t0, now, raw, page_size=10, max_pages=50, now=now)   # resumes from cursor
            self.assertEqual((s2["new_rows"], s2["dup_rows"], s2["complete"]), (15, 0, True))
            self.assertEqual(api.calls[1]["lastDepositAddress"], "synthetic-15")
            rows = list(explorer.iter_raw(raw))
            self.assertEqual(len(rows), 25)
            self.assertEqual(len({explorer.row_key(r) for r in rows}), 25)
            cp = explorer.load_checkpoint(raw)
            self.assertIsNone(cp["active"])
            self.assertEqual(cp["completed"], [[t0, now - explorer.SETTLE_LAG]])   # newest 24 h never "completed"

            before = len(api.calls)
            s3 = explorer.pull(api, t0, now, raw, page_size=10, max_pages=50, now=now)   # only the open tail
            self.assertEqual((s3["new_rows"], s3["complete"]), (0, True))
            self.assertEqual(len(api.calls) - before, 1)
            self.assertEqual(api.calls[-1]["startTimestampUnix"], now - explorer.SETTLE_LAG)

            api.rows = sorted(api.rows + synth(3, now - 3600), key=lambda r: -r["createdAtTimestamp"])
            for i, r in enumerate(api.rows[:3]):
                r["depositAddress"], r["intentHashes"] = f"synthetic-late-{i}", f"late{i}"
            s4 = explorer.pull(api, t0, now, raw, page_size=10, max_pages=50, now=now)
            self.assertEqual(s4["new_rows"], 3)
            self.assertEqual(len(list(explorer.iter_raw(raw))), 28)

    def test_gaps(self):
        self.assertEqual(explorer.gaps(0, 100, [[10, 20], [21, 30], [90, 200]]), [[31, 89], [0, 9]])
        self.assertEqual(explorer.gaps(0, 100, []), [[0, 100]])
        self.assertEqual(explorer.gaps(0, 100, [[0, 100]]), [])


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.seen = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.seen.append((url, params, headers))
        return self.responses.pop(0)


class ClientTest(unittest.TestCase):
    def _client(self, responses):
        self.slept, self.t = [], [0.0]

        def sleep(s):
            self.slept.append(s)
            self.t[0] += s
        sess = FakeSession(responses)
        return explorer.Client("fake.jwt.token", session=sess, sleep=sleep, clock=lambda: self.t[0]), sess

    def test_rate_limit_and_429(self):
        c, sess = self._client([
            FakeResp(200, []),
            FakeResp(429, {"status": 429, "error": {"code": "RATE_LIMIT", "message": "wait"}}, {"Retry-After": "7"}),
            FakeResp(200, [{"x": 1}]),
        ])
        self.assertEqual(c.get_page({}), [])
        self.assertEqual(c.get_page({}), [{"x": 1}])
        self.assertGreaterEqual(self.slept[0], 5.2)          # spacing before the 2nd request
        self.assertGreaterEqual(self.slept[1], 7.0)          # honoured the 429 hint
        url, _, headers = sess.seen[0]
        self.assertTrue(url.endswith("/api/v0/transactions"))
        self.assertEqual(headers["Authorization"], "Bearer fake.jwt.token")
        self.assertEqual(headers["User-Agent"], "moatmap-research/0.1")

    def test_429_hint_in_body_and_401(self):
        r = FakeResp(429, {"status": 429, "error": {"code": "X", "message": "Try again in 4200 ms"}})
        self.assertAlmostEqual(explorer._retry_hint(r), 4.2)
        c, _ = self._client([FakeResp(401, {"status": 401})])
        with self.assertRaises(explorer.AuthError):
            c.get_page({})


class BqTest(unittest.TestCase):
    def test_every_sql_file_obeys_the_rules(self):
        files = sorted(SQL_DIR.glob("*.sql"))
        self.assertGreaterEqual(len(files), 7)
        values = {"start_date": date(2026, 1, 1), "end_date": date(2026, 1, 2), "intents_contract": "intents.near",
                  "fee_wallets": ["a.near"], "top_n": 5}
        for f in files:
            sql = f.read_text(encoding="utf-8")
            self.assertEqual(bq.check_sql(sql), [], f.name)
            self.assertIn(BQ_DATASET, sql, f.name)
            names = {p["name"] for p in bq.params_for(sql, values)}
            self.assertTrue({"start_date", "end_date"} <= names, f.name)
            self.assertTrue(sql.lstrip().startswith(f"-- {f.name}"), f.name)

    def test_check_sql_catches_missing_partition_filter(self):
        bad = f"SELECT COUNT(*) FROM `{BQ_DATASET}.transactions` WHERE signer_account_id = 'x'"
        self.assertTrue(bq.check_sql(bad))

    def test_params(self):
        p = {x["name"]: x for x in bq.params_for(
            "SELECT 1 FROM t WHERE d BETWEEN @start_date AND @end_date AND w IN UNNEST(@fee_wallets) -- @ignored",
            {"start_date": date(2026, 1, 1), "end_date": date(2026, 1, 31), "fee_wallets": ["a", "b"], "top_n": 5})}
        self.assertEqual(set(p), {"start_date", "end_date", "fee_wallets"})
        self.assertEqual(p["start_date"]["parameterType"], {"type": "DATE"})
        self.assertEqual(p["start_date"]["parameterValue"], {"value": "2026-01-01"})
        self.assertEqual(p["fee_wallets"]["parameterValue"], {"arrayValues": [{"value": "a"}, {"value": "b"}]})

    def test_parse_rows_and_tidy(self):
        schema = {"fields": [{"name": "block_date", "type": "DATE"}, {"name": "transactions", "type": "INTEGER"},
                             {"name": "share", "type": "FLOAT"}, {"name": "tags", "type": "STRING", "mode": "REPEATED"}]}
        rows = [{"f": [{"v": "2026-01-01"}, {"v": "42"}, {"v": "0.5"}, {"v": [{"v": "a"}, {"v": "b"}]}]},
                {"f": [{"v": "2026-01-02"}, {"v": None}, {"v": None}, {"v": []}]}]
        parsed = bq.parse_rows(schema, rows)
        self.assertEqual(parsed[0], {"block_date": "2026-01-01", "transactions": 42, "share": 0.5, "tags": ["a", "b"]})
        self.assertIsNone(parsed[1]["transactions"])
        tidy = bq.tidy_daily({
            "daily_transactions": [{"block_date": "2026-01-01", "transactions": 42}],
            "daily_active_accounts": [{"block_date": "2026-01-01", "active_accounts": 7}],
            "intents_contract_methods": [{"block_date": "2026-01-01", "method_name": "execute_intents",
                                          "function_calls": 3, "distinct_predecessors": 1}],
            "intents_events": [{"block_date": "2026-01-01", "standard": "dip4", "event": "e", "version": "0.1", "events": 2},
                               {"block_date": "2026-01-01", "standard": "dip4", "event": "e", "version": "0.2", "events": 5}],
        })
        self.assertEqual(tidy, [("2026-01-01", "active_accounts", 7), ("2026-01-01", "intents_event:dip4.e", 7),
                                ("2026-01-01", "intents_method:execute_intents", 3), ("2026-01-01", "transactions", 42)])

    def test_assertion_shape(self):
        info = {"client_email": "svc@example.iam.gserviceaccount.com", "private_key": "PEM", "private_key_id": "kid1"}
        tok = bq.build_assertion(info, now=1000, sign=lambda pem, msg: b"sig")
        h, c, s = tok.split(".")
        pad = lambda x: x + "=" * (-len(x) % 4)
        import base64
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(h))), {"alg": "RS256", "typ": "JWT", "kid": "kid1"})
        claims = json.loads(base64.urlsafe_b64decode(pad(c)))
        self.assertEqual(claims["iss"], info["client_email"])
        self.assertEqual(claims["aud"], "https://oauth2.googleapis.com/token")
        self.assertEqual((claims["iat"], claims["exp"]), (1000, 4600))
        self.assertEqual(base64.urlsafe_b64decode(pad(s)), b"sig")


class EnvTest(unittest.TestCase):
    def test_parse(self):
        got = env.parse_env("# c\nA=1\nexport B = 'two words'\nC=\nD=x # trailing\nnot a line\n")
        self.assertEqual(got, {"A": "1", "B": "two words", "C": "", "D": "x"})

    def test_load_does_not_override(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text("MOATMAP_T1=from_file\nMOATMAP_T2=from_file\nMOATMAP_T3=\n", encoding="utf-8")
            os.environ["MOATMAP_T1"] = "from_env"
            os.environ.pop("MOATMAP_T2", None)
            try:
                env.load_env(p)
                self.assertEqual(os.environ["MOATMAP_T1"], "from_env")
                self.assertEqual(os.environ["MOATMAP_T2"], "from_file")
                self.assertNotIn("MOATMAP_T3", os.environ)
            finally:
                os.environ.pop("MOATMAP_T1", None)
                os.environ.pop("MOATMAP_T2", None)


if __name__ == "__main__":
    unittest.main()
