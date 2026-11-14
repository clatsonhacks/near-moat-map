"""NEAR BigQuery public dataset runner (no google-cloud-bigquery needed).

Run:  python -m moatmap.bq [--days 90] [--heavy-days 7] [--max-gb 20] [--dry-run] [--only NAME ...]

Dataset: bigquery-public-data.crypto_near_mainnet_us (config.BQ_DATASET). Every query in
sql/ is parameterised (@start_date / @end_date) and filters on the block_date partition.

How it talks to BigQuery
  REST only (`requests`): POST https://bigquery.googleapis.com/bigquery/v2/projects/<GCP_PROJECT>/queries
  (jobs.query) with useLegacySql=false and NAMED query parameters. Each query is sent
  first with dryRun=true; the bytes it would process are printed and the query is
  SKIPPED if they exceed --max-gb (default 20). The real run also sets maximumBytesBilled
  to the same limit, so BigQuery itself refuses to bill more.
  `--dry-run` stops after the dry runs: it costs nothing and validates every table and
  column name in sql/ against the live schema (use it first; several column TYPES are
  marked UNVERIFIED in the .sql headers).

Credentials (read from the environment or ROOT/.env)
  GOOGLE_APPLICATION_CREDENTIALS  path to a JSON key. Two kinds are accepted:
      "service_account"  -> OAuth2 JWT-bearer grant, RS256-signed. Signing needs
                            `cryptography` (preferred) or `google-auth`; neither is in
                            the standard library, see requirements.txt.
      "authorized_user"  -> the file `gcloud auth application-default login` writes;
                            refresh-token grant, needs no crypto library.
  GCP_PROJECT                     project that is billed for the queries (the public
                                  dataset is free to read; queries are billed to you;
                                  the on-demand free tier is 1 TB / month).
  Missing credentials or signing library -> prints
  "skipped: pip install -r requirements.txt and set GOOGLE_APPLICATION_CREDENTIALS + GCP_PROJECT"
  and exits 0 so the one-command rebuild still completes. No output file is written.

Freshness first
  docs.near.org warns that NEAR Lake, the S3 source of this dataset, was deprecated on
  2026-03-24, so the dataset may have stopped updating. sql/freshness.sql therefore runs
  first; the window for every other query ENDS at the last block_date really present
  (never "today"), and data/processed/bq_freshness.json records that date and how stale
  it is. If the dataset is stale, the BigQuery window will not overlap a recent Explorer /
  DefiLlama window - the JSON says so explicitly rather than hiding it.

Outputs
  data/raw/bq/<query>.json                    params, bytes processed, schema, all rows
  data/processed/bq_daily.csv                 block_date,metric,value   (tidy / long)
      metrics: transactions, active_accounts, network_function_calls,
      intents_function_calls, intents_receipts, intents_distinct_predecessors,
      intents_distinct_signers, intents_method:<method_name>, intents_event:<standard>.<event>
  data/processed/bq_top_contracts.csv         rank, contract, calls, share of network calls
  data/processed/bq_fee_wallet_inflows.csv    block_date,wallet,asset,n_transfers,amount_near,...
  data/processed/bq_freshness.json            last block_date, staleness, window, per-query status
A query that fails or is skipped is reported per query and never replaced by zeros.
"""
import argparse
import base64
import csv
import importlib.util
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from . import env
from .config import BQ_DATASET, FEE_WALLETS, PROCESSED, RAW, ROOT, SQL_DIR

UA = "moatmap-research/0.1"
BQ_API = "https://bigquery.googleapis.com/bigquery/v2"
SCOPE = "https://www.googleapis.com/auth/bigquery"
LOCATION = "us-east1"  # the dataset lives in us-east1 (datasets.get), not the US multi-region
INTENTS_CONTRACT = "intents.near"
LAKE_DEPRECATED = "2026-03-24"
RAW_DIR = RAW / "bq"
SKIP_MSG = "skipped: pip install -r requirements.txt and set GOOGLE_APPLICATION_CREDENTIALS + GCP_PROJECT"

# name -> (window kind, optional?)   "full" = --days, "heavy" = --heavy-days (reads args / logs)
QUERIES = [
    ("daily_transactions", "full", False),
    ("daily_active_accounts", "full", False),
    ("intents_contract_usage", "full", False),
    ("top_contracts", "full", False),
    ("fee_wallet_inflows", "full", False),
    ("circulating_supply", "full", True),
    ("fee_wallet_wnear_inflows", "full", True),
    ("intents_contract_methods", "heavy", False),
    ("intents_events", "heavy", False),
]


class BQError(Exception):
    pass


# ----------------------------------------------------------------------------- auth

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def signing_backend():
    """Name of an importable RS256 backend, or None."""
    for mod in ("cryptography", "google.auth"):
        try:
            if importlib.util.find_spec(mod) is not None:
                return mod
        except (ImportError, ValueError):
            continue
    return None


def _sign_rs256(private_key_pem: str, message: bytes) -> bytes:
    backend = signing_backend()
    if backend == "cryptography":
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    if backend == "google.auth":
        from google.auth import crypt
        return crypt.RSASigner.from_string(private_key_pem).sign(message)
    raise BQError("no RS256 signing library (pip install cryptography)")


def build_assertion(info: dict, now: int = None, sign=_sign_rs256) -> str:
    """Service-account JWT for the OAuth2 jwt-bearer grant."""
    now = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if info.get("private_key_id"):
        header["kid"] = info["private_key_id"]
    claims = {"iss": info["client_email"], "scope": SCOPE,
              "aud": info.get("token_uri", "https://oauth2.googleapis.com/token"),
              "iat": now, "exp": now + 3600}
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    return f"{signing_input}.{_b64url(sign(info['private_key'], signing_input.encode()))}"


def get_access_token(info: dict, session=None) -> str:
    sess = session or requests
    kind = info.get("type")
    if kind == "service_account":
        uri = info.get("token_uri", "https://oauth2.googleapis.com/token")
        data = {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": build_assertion(info)}
    elif kind == "authorized_user":
        uri = "https://oauth2.googleapis.com/token"
        data = {"grant_type": "refresh_token", "client_id": info["client_id"],
                "client_secret": info["client_secret"], "refresh_token": info["refresh_token"]}
    else:
        raise BQError(f"unsupported credential type {kind!r} (need service_account or authorized_user)")
    r = sess.post(uri, data=data, headers={"User-Agent": UA}, timeout=60)
    if r.status_code != 200:
        raise BQError(f"token request failed: HTTP {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


# ----------------------------------------------------------------------------- query plumbing

def make_param(name: str, value):
    if isinstance(value, (list, tuple)):
        return {"name": name, "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
                "parameterValue": {"arrayValues": [{"value": str(v)} for v in value]}}
    if isinstance(value, date):
        return {"name": name, "parameterType": {"type": "DATE"}, "parameterValue": {"value": value.isoformat()}}
    if isinstance(value, bool):
        return {"name": name, "parameterType": {"type": "BOOL"}, "parameterValue": {"value": str(value).lower()}}
    if isinstance(value, int):
        return {"name": name, "parameterType": {"type": "INT64"}, "parameterValue": {"value": str(value)}}
    return {"name": name, "parameterType": {"type": "STRING"}, "parameterValue": {"value": str(value)}}


def strip_sql_comments(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def params_for(sql: str, values: dict) -> list:
    """Only the parameters the SQL body actually references (comments ignored)."""
    used = set(re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)", strip_sql_comments(sql)))
    missing = used - set(values)
    if missing:
        raise BQError(f"SQL references undefined parameter(s): {sorted(missing)}")
    return [make_param(k, values[k]) for k in sorted(used)]


def check_sql(sql: str) -> list:
    """Static guard for the track rules. Returns a list of problems (empty = ok)."""
    body = strip_sql_comments(sql)
    problems = []
    tables = re.findall(r"`([^`]+)`", body)
    if not tables:
        problems.append("no backticked table reference")
    for t in tables:
        if not t.startswith(BQ_DATASET + "."):
            problems.append(f"table outside {BQ_DATASET}: {t}")
    if len(re.findall(r"\bblock_date\s+BETWEEN\s+@start_date\s+AND\b", body, flags=re.I)) < len(tables):
        problems.append("not every table reference is filtered on block_date BETWEEN @start_date AND ...")
    return problems


def _cell(v, field):
    if v is None:
        return None
    if field.get("mode") == "REPEATED":
        sub = dict(field, mode="NULLABLE")
        return [_cell(x.get("v"), sub) for x in v]
    t = field.get("type", "STRING")
    if t in ("RECORD", "STRUCT"):
        return {f["name"]: _cell(c.get("v"), f) for f, c in zip(field.get("fields", []), v.get("f", []))}
    if t in ("INTEGER", "INT64"):
        return int(v)
    if t in ("FLOAT", "FLOAT64"):
        return float(v)
    if t in ("BOOLEAN", "BOOL"):
        return str(v).lower() == "true"
    if t == "TIMESTAMP":
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return v
    return v


def parse_rows(schema: dict, rows: list) -> list:
    fields = (schema or {}).get("fields", [])
    return [{f["name"]: _cell(c.get("v"), f) for f, c in zip(fields, r.get("f", []))} for r in rows or []]


class BigQuery:
    def __init__(self, project: str, token: str, session=None):
        self.project, self.token = project, token
        self.session = session or requests.Session()

    def _call(self, method: str, url: str, **kw):
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": UA}
        for attempt in range(5):
            try:
                r = self.session.request(method, url, headers=headers, timeout=180, **kw)
            except requests.RequestException as e:
                if attempt == 4:
                    raise BQError(f"network error: {type(e).__name__}")
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code != 200:
                try:
                    msg = r.json()["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    msg = r.text[:300]
                raise BQError(f"HTTP {r.status_code}: {msg}")
            return r.json()
        raise BQError("gave up")

    def dry_run(self, sql: str, params: list) -> int:
        body = {"query": sql, "useLegacySql": False, "parameterMode": "NAMED", "queryParameters": params,
                "location": LOCATION, "dryRun": True}
        res = self._call("POST", f"{BQ_API}/projects/{self.project}/queries", json=body)
        return int(res.get("totalBytesProcessed", 0))

    def query(self, sql: str, params: list, max_bytes: int) -> dict:
        body = {"query": sql, "useLegacySql": False, "parameterMode": "NAMED", "queryParameters": params,
                "location": LOCATION, "timeoutMs": 60000, "maxResults": 10000,
                "maximumBytesBilled": str(int(max_bytes)), "useQueryCache": True}
        res = self._call("POST", f"{BQ_API}/projects/{self.project}/queries", json=body)
        job = res.get("jobReference", {})
        url = f"{BQ_API}/projects/{self.project}/queries/{job.get('jobId')}"
        loc = job.get("location", LOCATION)
        waited = 0
        while not res.get("jobComplete"):
            waited += 1
            if waited > 30:
                raise BQError("query did not complete within ~30 minutes")
            res = self._call("GET", url, params={"location": loc, "timeoutMs": 60000, "maxResults": 10000})
        if res.get("errors"):
            raise BQError(str(res["errors"])[:300])
        schema, raw_rows = res.get("schema", {}), list(res.get("rows", []))
        token = res.get("pageToken")
        while token:
            page = self._call("GET", url, params={"location": loc, "pageToken": token, "maxResults": 10000})
            raw_rows.extend(page.get("rows", []))
            token = page.get("pageToken")
        return {"job_id": job.get("jobId"), "bytes_processed": int(res.get("totalBytesProcessed", 0)),
                "cache_hit": bool(res.get("cacheHit")), "schema": schema, "rows": parse_rows(schema, raw_rows)}


# ----------------------------------------------------------------------------- transforms

def tidy_daily(results: dict) -> list:
    """{query_name: [row dicts]} -> sorted [(block_date, metric, value)] for bq_daily.csv."""
    out = []
    for name in ("daily_transactions", "daily_active_accounts", "intents_contract_usage"):
        for r in results.get(name) or []:
            for k, v in r.items():
                if k != "block_date" and v is not None:
                    out.append((r["block_date"], k, v))
    for r in results.get("intents_contract_methods") or []:
        out.append((r["block_date"], f"intents_method:{r.get('method_name') or 'UNPARSED'}", r["function_calls"]))
    for r in results.get("intents_events") or []:
        label = f"{r.get('standard') or 'UNPARSED'}.{r.get('event') or 'UNPARSED'}"
        out.append((r["block_date"], f"intents_event:{label}", r["events"]))
    # several versions of one event collapse into the same metric: sum them
    summed = {}
    for d, m, v in out:
        summed[(d, m)] = summed.get((d, m), 0) + v
    return sorted((d, m, v) for (d, m), v in summed.items())


def fee_inflow_rows(results: dict) -> list:
    rows = []
    for r in results.get("fee_wallet_inflows") or []:
        rows.append([r["block_date"], r["wallet"], "NEAR", r["n_transfers"], r["near_amount"],
                     r.get("system_refund_near"), r.get("unparsed_deposits")])
    for r in results.get("fee_wallet_wnear_inflows") or []:
        rows.append([r["block_date"], r["wallet"], "wNEAR", r["n_transfers"], r["wnear_amount"], "", ""])
    return sorted(rows, key=lambda x: (x[0], x[1], x[2]))


def _write_csv(path: Path, header: list, rows: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows([["" if c is None else c for c in r] for r in rows])


def _gb(n: int) -> str:
    return f"{n / 1e9:,.2f} GB"


# ----------------------------------------------------------------------------- main

def load_credentials():
    """Returns (info, project) or (None, reason)."""
    env.load_env()
    path, project = env.get("GOOGLE_APPLICATION_CREDENTIALS"), env.get("GCP_PROJECT")
    if not path or not project:
        return None, "GOOGLE_APPLICATION_CREDENTIALS and/or GCP_PROJECT not set"
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = ROOT / p
    if not p.is_file():
        return None, f"key file not found: {p}"
    try:
        info = json.loads(p.read_text(encoding="utf-8-sig"))
    except ValueError:
        return None, f"key file is not valid JSON: {p}"
    if info.get("type") == "service_account" and not signing_backend():
        return None, "service-account key needs RS256 signing: neither `cryptography` nor `google-auth` is installed"
    if info.get("type") not in ("service_account", "authorized_user"):
        return None, f"unsupported credential type {info.get('type')!r}"
    return info, project


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run sql/*.sql against the NEAR BigQuery public dataset.")
    ap.add_argument("--days", type=int, default=90, help="window for the daily metrics")
    ap.add_argument("--heavy-days", type=int, default=7,
                    help="window for queries that must read the args / logs columns")
    ap.add_argument("--max-gb", type=float, default=20.0, help="skip any query whose dry run exceeds this")
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true", help="validate + price every query, run nothing")
    ap.add_argument("--only", nargs="*", help="run only these query names (freshness always runs)")
    args = ap.parse_args(argv)

    for name in ["freshness"] + [q[0] for q in QUERIES]:
        path = SQL_DIR / f"{name}.sql"
        if not path.is_file():
            print(f"bq: missing {path}")
            return 0
        problems = check_sql(path.read_text(encoding="utf-8"))
        if problems:
            print(f"bq: {name}.sql violates the query rules: {problems}; nothing run")
            return 0

    info, project = load_credentials()
    if info is None:
        print(f"{SKIP_MSG}  [{project}]")
        return 0
    try:
        bq = BigQuery(project, get_access_token(info))
    except (BQError, requests.RequestException, KeyError) as e:
        print(f"{SKIP_MSG}  [auth failed: {e}]")
        return 0

    max_bytes = int(args.max_gb * 1e9)
    today = datetime.now(timezone.utc).date()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    status, results, total_bytes = {}, {}, 0

    def run(name: str, values: dict, optional: bool = False):
        nonlocal total_bytes
        sql = (SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")
        st = {"window": [str(values["start_date"]), str(values["end_date"])]}
        status[name] = st
        try:
            params = params_for(sql, values)
            est = bq.dry_run(sql, params)
            st["dry_run_bytes"] = est
            print(f"  {name:<26} dry run: {_gb(est)}  window {values['start_date']}..{values['end_date']}")
            if est > max_bytes:
                st["state"] = f"skipped: dry run {_gb(est)} exceeds --max-gb {args.max_gb:g}"
                print(f"  {name:<26} SKIPPED (over --max-gb {args.max_gb:g}; shorten --days/--heavy-days or raise it)")
                return None
            if args.dry_run:
                st["state"] = "dry run only"
                return None
            res = bq.query(sql, params, max_bytes)
        except BQError as e:
            st["state"] = f"{'optional query failed' if optional else 'FAILED'}: {e}"
            print(f"  {name:<26} {st['state']}")
            return None
        total_bytes += res["bytes_processed"]
        st.update(state="ok", bytes_processed=res["bytes_processed"], rows=len(res["rows"]),
                  job_id=res["job_id"], cache_hit=res["cache_hit"])
        (RAW_DIR / f"{name}.json").write_text(json.dumps({
            "query": name, "dataset": BQ_DATASET, "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "params": {k: (list(v) if isinstance(v, (list, tuple)) else str(v)) for k, v in values.items()},
            "bytes_processed": res["bytes_processed"], "job_id": res["job_id"], "schema": res["schema"],
            "rows": res["rows"]}, indent=1), encoding="utf-8")
        print(f"  {name:<26} ok: {len(res['rows'])} rows, {_gb(res['bytes_processed'])} processed")
        return res["rows"]

    # 1) freshness: find the real last block_date, widening the look-back only if empty
    print(f"bq: project {project}, dataset {BQ_DATASET}, limit {args.max_gb:g} GB per query")
    last_date = last_ts = None
    for lookback in (30, 120, 400, 1200):
        rows = run("freshness", {"start_date": today - timedelta(days=lookback), "end_date": today})
        if rows and rows[0].get("last_block_date"):
            last_date = date.fromisoformat(rows[0]["last_block_date"])
            last_ts = rows[0].get("last_block_timestamp_utc")
            break
        if args.dry_run or rows is None:
            break
    fresh = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "dataset": BQ_DATASET,
             "last_block_date": str(last_date) if last_date else None, "last_block_timestamp_utc": last_ts,
             "days_stale": (today - last_date).days if last_date else None,
             "note": f"NEAR Lake (the S3 source of this dataset) was deprecated {LAKE_DEPRECATED} per docs.near.org; "
                     "the window below ends at the last block_date actually present, not at today."}
    if last_date:
        print(f"  freshness: last block_date in dataset = {last_date} ({fresh['days_stale']} days before today)")
        if fresh["days_stale"] > 2:
            print("  ! dataset is STALE: BigQuery metrics below stop at that date")
    elif not args.dry_run:
        print("  freshness: no blocks found in the look-back window; daily queries not run")

    # 2) metrics, windows anchored on the real last date
    end = last_date or (today - timedelta(days=1))
    base = {"end_date": end, "intents_contract": INTENTS_CONTRACT, "fee_wallets": list(FEE_WALLETS),
            "top_n": args.top_n}
    windows = {"full": end - timedelta(days=args.days - 1), "heavy": end - timedelta(days=args.heavy_days - 1)}
    fresh.update(window_start=str(windows["full"]), window_end=str(end), heavy_window_start=str(windows["heavy"]))
    if last_date or args.dry_run:
        for name, kind, optional in QUERIES:
            if args.only and name not in args.only:
                continue
            rows = run(name, dict(base, start_date=windows[kind]), optional)
            if rows is not None:
                results[name] = rows

    # 3) outputs (only from queries that really ran)
    daily = tidy_daily(results)
    if daily:
        _write_csv(PROCESSED / "bq_daily.csv", ["block_date", "metric", "value"], daily)
        print(f"wrote {PROCESSED / 'bq_daily.csv'} ({len(daily)} rows)")
    if results.get("top_contracts"):
        cols = ["rank_by_calls", "contract", "function_calls", "share_of_network_calls", "distinct_predecessors",
                "active_days", "contracts_in_window"]
        rows = [[r.get(c) for c in cols] + [str(windows["full"]), str(end)] for r in results["top_contracts"]]
        _write_csv(PROCESSED / "bq_top_contracts.csv", cols + ["window_start", "window_end"], rows)
        print(f"wrote {PROCESSED / 'bq_top_contracts.csv'} ({len(rows)} rows)")
    inflows = fee_inflow_rows(results)
    if "fee_wallet_inflows" in results:
        _write_csv(PROCESSED / "bq_fee_wallet_inflows.csv",
                   ["block_date", "wallet", "asset", "n_transfers", "amount_near", "system_refund_near",
                    "unparsed_deposits"], inflows)
        print(f"wrote {PROCESSED / 'bq_fee_wallet_inflows.csv'} ({len(inflows)} rows; "
              f"wNEAR leg {'included' if 'fee_wallet_wnear_inflows' in results else 'NOT included'})")
    per_metric_last = {}
    for d, m, _ in daily:
        if ":" not in m:
            per_metric_last[m] = max(per_metric_last.get(m, d), d)
    fresh.update(queries=status, last_date_per_metric=per_metric_last, total_bytes_processed=total_bytes,
                 dry_run_only=bool(args.dry_run))
    (PROCESSED / "bq_freshness.json").write_text(json.dumps(fresh, indent=1), encoding="utf-8")
    print(f"wrote {PROCESSED / 'bq_freshness.json'}; total processed {_gb(total_bytes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
