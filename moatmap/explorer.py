"""NEAR Intents Explorer API puller + route aggregation.

Run:  python -m moatmap.explorer [--days 30] [--max-pages 2000] [--aggregate-only]

Needs NEAR_INTENTS_JWT (partner-dashboard JWT) in the environment or ROOT/.env. Without it
the module prints "skipped: NEAR_INTENTS_JWT not set" and exits 0, so a one-command rebuild
still completes. Nothing is ever fabricated: with no raw data on disk, no CSV is written.

API (read from https://explorer.near-intents.org/api/v0/openapi.yaml)
  GET {config.EXPLORER}/transactions      header  Authorization: Bearer <JWT>
  Rate limit: 1 request / 5 s per partner, HTTP 429 otherwise.
  The spec has exactly two paths: /transactions and /partner-any-quotes (the caller's own
  ANY_INPUT quotes, capped at 1000). There is no bulk / "transactions-pages" endpoint in
  the published spec, so /transactions with cursor pagination is the bulk path.
  Params used: numberOfTransactions (max 1000), statuses=SUCCESS, direction=next (= older),
  startTimestampUnix / endTimestampUnix (both documented as NON-inclusive),
  lastDepositAddress + lastDepositMemo (cursor = last row of the previous page).

Not verifiable without a JWT (written against the spec, flagged here on purpose):
  * that the timestamp filters apply to createdAt and that pages are ordered newest-first.
    The puller does not rely on ordering; it stops a segment on a short/empty page, and
    also when a page reaches below the requested start (in case the filter is ignored).
  * that (depositAddress, depositMemo) is unique per row. Dedupe therefore uses the wider
    key (depositAddress, depositMemo, intentHashes, createdAtTimestamp), which can only
    remove exact re-pulls of the same row, never two distinct swaps.

Resumable pull
  data/raw/explorer/YYYY-MM-DD.jsonl   raw rows, one JSON object per line, by UTC day of createdAt
  data/raw/explorer/_checkpoint.json   {"completed": [[from,to],...], "active": {...cursor...}}
  A rerun first finishes the interrupted segment from its cursor, then pulls only the parts
  of the requested window not yet completed. The most recent 24 h are never marked
  completed (a swap created earlier can still turn SUCCESS later), so they are re-pulled
  and deduped on every run.

Units
  amountInUsd / amountOutUsd: USD strings as reported by the API.
  appFees[].fee: the Explorer spec gives only `type: number`, example 50, with NO unit.
    The 1Click spec (https://1click.chaindefuser.com/docs/v0/openapi.yaml, schema AppFee),
    which is where these fees are set at quote time, defines it as: "Fee for this recipient
    as part of amountIn in basis points (1/100th of a percent), e.g. 100 for 1% fee".
    This module therefore treats fee as BASIS POINTS OF amountIn:
        fee_usd = amountInUsd * fee / 10_000
    Because the unit is taken from the sibling spec rather than the Explorer spec itself,
    fee_recipients.csv also keeps the raw values (fee_bps_min / fee_bps_max / fee_bps_x_swaps).
  appFees are the fees declared on the quote by the integrating app (affiliate /
  distribution fees). Whether NEAR's own protocol fee also appears in appFees is not stated
  in either spec; DefiLlama sources protocol fees from a separate Dune dataset. Do not read
  fee_usd as "all fees" until it reconciles with llama_daily.csv fees_usd.

Outputs (data/processed)
  swaps_by_route.csv   day,origin_chain,dest_chain,n_swaps,volume_usd,fee_usd,near_fee_usd,source
      volume_usd = sum(amountInUsd). fee_usd = all app fees. near_fee_usd = app fees whose
      recipient is one of config.FEE_WALLETS. If NO app-fee recipient in the whole pull is
      one of those wallets, the attribution cannot be made from public information and the
      near_fee_usd column is left EMPTY (not 0).
  fee_recipients.csv   recipient,n_swaps,volume_usd,fee_usd,fee_bps_min,fee_bps_max,fee_bps_x_swaps,is_near_fee_wallet
      always written, so the NEAR attribution can be made (or challenged) transparently.
  swaps_by_privacy.csv day,deposit_type,recipient_type,n_swaps,volume_usd
      share of flow touching INTENTS / CONFIDENTIAL_INTENTS balances.
  explorer_coverage.json  earliest/latest timestamp pulled, row counts, mapping stats.

Chains
  Asset id -> chain via the public 1Click token list ({config.ONECLICK}/tokens, field
  `blockchain`). Assets missing from the list fall back to the OMFT naming convention
  `nep141:<chain>[-<address>].omft.near` only when <chain> is a chain code present in the
  token list (checked: the convention agrees with the list for every listed OMFT token).
  Anything else goes to chain "unknown" and is counted, never dropped.
  Caveat: the chain is the asset's home chain. When depositType/recipientType is INTENTS
  or CONFIDENTIAL_INTENTS the funds started/ended inside NEAR Intents rather than on that
  chain; swaps_by_privacy.csv quantifies how much flow that is.
"""
import argparse
import csv
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import env
from .config import EXPLORER, FEE_WALLETS, ONECLICK, PROCESSED, RAW

UA = "moatmap-research/0.1"
RAW_DIR = RAW / "explorer"
MIN_INTERVAL = 5.2          # seconds between requests (limit is 1 per 5 s)
SETTLE_LAG = 86400          # the newest 24 h are never marked "completed"
SOURCE = "explorer_api"
OMFT_RE = re.compile(r"^nep141:([a-z0-9]+)(?:-[^.]+)?\.omft\.near$")

ROUTE_HEADER = ["day", "origin_chain", "dest_chain", "n_swaps", "volume_usd", "fee_usd", "near_fee_usd", "source"]
RECIP_HEADER = ["recipient", "n_swaps", "volume_usd", "fee_usd", "fee_bps_min", "fee_bps_max",
                "fee_bps_x_swaps", "is_near_fee_wallet"]
PRIV_HEADER = ["day", "deposit_type", "recipient_type", "n_swaps", "volume_usd"]


# ----------------------------------------------------------------------------- helpers

def row_ts(row) -> int:
    ts = row.get("createdAtTimestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return int(ts if ts < 1e11 else ts / 1000)  # tolerate milliseconds
    try:
        return int(datetime.fromisoformat(str(row.get("createdAt")).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return 0


def row_day(row) -> str:
    return datetime.fromtimestamp(row_ts(row), tz=timezone.utc).strftime("%Y-%m-%d")


def row_key(row) -> tuple:
    return (str(row.get("depositAddress")), str(row.get("depositMemo")),
            str(row.get("intentHashes")), row_ts(row))


def to_float(v):
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def iso(ts) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iter_raw(raw_dir: Path):
    for path in sorted(Path(raw_dir).glob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)


# ----------------------------------------------------------------------------- intervals

def merge_intervals(iv):
    out = []
    for a, b in sorted([list(x) for x in iv if x[0] <= x[1]]):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def gaps(start: int, end: int, completed):
    """Inclusive sub-intervals of [start, end] not covered by `completed`, newest first."""
    out, cur = [], start
    for a, b in merge_intervals(completed):
        if b < cur or a > end:
            continue
        if a > cur:
            out.append([cur, a - 1])
        cur = max(cur, b + 1)
    if cur <= end:
        out.append([cur, end])
    return out[::-1]


# ----------------------------------------------------------------------------- HTTP

class AuthError(Exception):
    pass


def _retry_hint(resp) -> float:
    """Seconds to wait after a 429: Retry-After header, else a number in the JSON body."""
    h = resp.headers.get("Retry-After")
    if h:
        f = to_float(h)
        if f is not None:
            return f
    try:
        body = resp.json()
    except ValueError:
        return MIN_INTERVAL
    blob = json.dumps(body)
    for key in ("retryAfter", "retry_after", "retryAfterSeconds", "retryAfterMs"):
        m = re.search(r'"%s"\s*:\s*"?([0-9.]+)' % key, blob)
        if m:
            v = float(m.group(1))
            return v / 1000 if key.endswith("Ms") else v
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|milliseconds|s|sec|seconds)\b", blob)
    if m:
        v = float(m.group(1))
        return v / 1000 if m.group(2).startswith("m") else v
    return MIN_INTERVAL


class Client:
    """Rate-limited GET /transactions. `sleep`/`clock` are injectable for tests."""

    def __init__(self, jwt: str, min_interval: float = MIN_INTERVAL, session=None,
                 sleep=time.sleep, clock=time.monotonic, base: str = EXPLORER):
        self.jwt, self.min_interval, self.base = jwt, max(float(min_interval), 5.0), base
        self.session = session or requests.Session()
        self.sleep, self.clock = sleep, clock
        self._last = None
        self.requests_made = 0

    def _wait(self):
        if self._last is not None:
            left = self.min_interval - (self.clock() - self._last)
            if left > 0:
                self.sleep(left)

    def get_page(self, params: dict):
        headers = {"Authorization": f"Bearer {self.jwt}", "User-Agent": UA, "Accept": "application/json"}
        for attempt in range(8):
            self._wait()
            try:
                r = self.session.get(f"{self.base}/transactions", params=params, headers=headers, timeout=90)
            except requests.RequestException as e:
                self._last = self.clock()
                print(f"  network error ({type(e).__name__}), retry {attempt + 1}/8")
                self.sleep(min(60, 5 * (attempt + 1)))
                continue
            self._last = self.clock()
            self.requests_made += 1
            if r.status_code == 200:
                data = r.json()
                if not isinstance(data, list):
                    raise RuntimeError("unexpected response shape (spec says a JSON array)")
                return data
            if r.status_code in (401, 403):
                raise AuthError(f"HTTP {r.status_code}")
            if r.status_code == 429:
                wait = min(120.0, max(self.min_interval, _retry_hint(r) + 0.2))
                print(f"  429 rate limited, waiting {wait:.1f}s")
                self.sleep(wait)
                continue
            if r.status_code >= 500:
                self.sleep(min(60, 5 * (attempt + 1)))
                continue
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        raise RuntimeError("gave up after 8 attempts")


# ----------------------------------------------------------------------------- puller

def load_checkpoint(raw_dir: Path) -> dict:
    p = Path(raw_dir) / "_checkpoint.json"
    if p.is_file():
        try:
            cp = json.loads(p.read_text(encoding="utf-8"))
            cp.setdefault("completed", [])
            cp.setdefault("active", None)
            return cp
        except ValueError:
            print("  ! unreadable checkpoint ignored; raw rows on disk are still deduped")
    return {"completed": [], "active": None}


def save_checkpoint(raw_dir: Path, cp: dict):
    p = Path(raw_dir) / "_checkpoint.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cp, indent=1), encoding="utf-8")
    tmp.replace(p)


def append_rows(raw_dir: Path, rows):
    by_day = defaultdict(list)
    for r in rows:
        by_day[row_day(r)].append(r)
    for day, rs in by_day.items():
        with open(Path(raw_dir) / f"{day}.jsonl", "a", encoding="utf-8") as f:
            for r in rs:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")


def pull(client, start: int, end: int, raw_dir: Path = RAW_DIR, page_size: int = 1000,
         max_pages: int = 2000, now: int = None) -> dict:
    """Pull SUCCESS swaps with start <= createdAt <= end (unix seconds). Resumable.

    Returns {"pages", "new_rows", "dup_rows", "complete"}. complete=False means --max-pages
    stopped the run with a cursor saved; rerun to continue.
    """
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    now = int(now if now is not None else time.time())
    cp = load_checkpoint(raw_dir)
    seen = {row_key(r) for r in iter_raw(raw_dir)}
    if seen:
        print(f"  resume: {len(seen):,} rows already on disk")
    stats = {"pages": 0, "new_rows": 0, "dup_rows": 0, "complete": True}

    def run_segment(seg) -> bool:
        while True:
            if stats["pages"] >= max_pages:
                return False
            params = {"numberOfTransactions": page_size, "statuses": "SUCCESS", "direction": "next",
                      "startTimestampUnix": seg["start"] - 1, "endTimestampUnix": seg["end"] + 1}
            cur = seg.get("cursor")
            if cur:
                params["lastDepositAddress"] = cur["addr"]
                if cur.get("memo") is not None:
                    params["lastDepositMemo"] = cur["memo"]
            rows = client.get_page(params)
            stats["pages"] += 1
            seg["pages"] = seg.get("pages", 0) + 1
            if not rows:
                return True
            fresh, below = [], False
            for r in rows:
                ts = row_ts(r)
                if ts and ts < seg["start"]:
                    below = True          # API returned rows older than asked: do not store them
                    continue
                k = row_key(r)
                if k in seen:
                    stats["dup_rows"] += 1
                    continue
                seen.add(k)
                fresh.append(r)
            append_rows(raw_dir, fresh)
            stats["new_rows"] += len(fresh)
            last = rows[-1]
            new_cur = {"addr": last.get("depositAddress"), "memo": last.get("depositMemo")}
            tss = [row_ts(r) for r in rows if row_ts(r)]
            if tss:
                seg["oldest_ts"] = min([min(tss)] + ([seg["oldest_ts"]] if seg.get("oldest_ts") else []))
            stuck = cur == new_cur
            seg["cursor"] = new_cur
            seg["rows"] = seg.get("rows", 0) + len(fresh)
            save_checkpoint(raw_dir, cp)
            print(f"  page {stats['pages']:>5}: {len(rows):>4} rows ({len(fresh)} new) "
                  f"oldest {iso(min(tss)) if tss else '?'}  total new {stats['new_rows']:,}")
            if below or len(rows) < page_size:
                return True
            if stuck or not new_cur["addr"]:
                print("  ! pagination cursor did not advance; stopping this segment as INCOMPLETE")
                return False

    def finish(seg):
        hi = min(seg["end"], now - SETTLE_LAG)
        if hi >= seg["start"]:
            cp["completed"] = merge_intervals(cp["completed"] + [[seg["start"], hi]])
        cp["active"] = None
        save_checkpoint(raw_dir, cp)

    if cp.get("active"):
        seg = cp["active"]
        print(f"  resuming interrupted segment {iso(seg['start'])}..{iso(seg['end'])} "
              f"(page {seg.get('pages', 0)}, cursor saved)")
        if run_segment(seg):
            finish(seg)
        else:
            stats["complete"] = False
            return stats

    for a, b in gaps(start, end, cp["completed"]):
        seg = {"start": a, "end": b, "cursor": None, "pages": 0, "rows": 0}
        cp["active"] = seg
        save_checkpoint(raw_dir, cp)
        print(f"  segment {iso(a)}..{iso(b)}")
        if run_segment(seg):
            finish(seg)
        else:
            stats["complete"] = False
            break
    return stats


# ----------------------------------------------------------------------------- token list

def load_token_chains(raw_dir: Path = RAW_DIR, offline: bool = False, session=None) -> dict:
    """assetId -> blockchain from the public 1Click token list (cached in raw_dir/tokens.json)."""
    cache = Path(raw_dir) / "tokens.json"
    tokens = None
    if not offline:
        try:
            r = (session or requests).get(f"{ONECLICK}/tokens", headers={"User-Agent": UA}, timeout=60)
            r.raise_for_status()
            tokens = r.json()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(tokens), encoding="utf-8")
        except (requests.RequestException, ValueError) as e:
            print(f"  ! 1Click token list fetch failed ({type(e).__name__}); using cache if present")
    if tokens is None and cache.is_file():
        tokens = json.loads(cache.read_text(encoding="utf-8"))
    return {t["assetId"]: t["blockchain"] for t in (tokens or []) if t.get("assetId") and t.get("blockchain")}


def chain_of(asset, token_chains: dict, known_chains: set):
    """Returns (chain, method) with method in {'token_list', 'omft_pattern', 'unknown'}."""
    if asset in token_chains:
        return token_chains[asset], "token_list"
    m = OMFT_RE.match(str(asset or ""))
    if m and m.group(1) in known_chains:
        return m.group(1), "omft_pattern"
    return "unknown", "unknown"


# ----------------------------------------------------------------------------- aggregation

def aggregate(rows, token_chains: dict, source: str = SOURCE, start: int = None, end: int = None) -> dict:
    """Pure aggregation of raw Explorer rows. Returns the three tables + coverage stats.

    Only status == SUCCESS rows inside [start, end] are aggregated; exact duplicates are
    counted once. A row whose amountInUsd is missing/unparseable is still counted in
    n_swaps (volume 0) and reported in coverage["unpriced_rows"].
    """
    known = set(token_chains.values())
    near_wallets = set(FEE_WALLETS)
    routes = defaultdict(lambda: [0, 0.0, 0.0, 0.0])            # n, vol, fee, near_fee
    priv = defaultdict(lambda: [0, 0.0])
    recips = {}
    cov = {"rows_read": 0, "rows_aggregated": 0, "duplicates_ignored": 0, "non_success_ignored": 0,
           "outside_window_ignored": 0, "unpriced_rows": 0, "rows_with_app_fees": 0,
           "rows_with_unknown_chain": 0, "asset_mapping": defaultdict(int), "unknown_assets": defaultdict(int),
           "min_ts": None, "max_ts": None, "near_wallet_fee_rows": 0}
    seen = set()
    for r in rows:
        cov["rows_read"] += 1
        if r.get("status") != "SUCCESS":
            cov["non_success_ignored"] += 1
            continue
        ts = row_ts(r)
        if (start is not None and ts < start) or (end is not None and ts > end):
            cov["outside_window_ignored"] += 1
            continue
        k = row_key(r)
        if k in seen:
            cov["duplicates_ignored"] += 1
            continue
        seen.add(k)
        cov["rows_aggregated"] += 1
        cov["min_ts"] = ts if cov["min_ts"] is None else min(cov["min_ts"], ts)
        cov["max_ts"] = ts if cov["max_ts"] is None else max(cov["max_ts"], ts)

        vol = to_float(r.get("amountInUsd"))
        if vol is None:
            cov["unpriced_rows"] += 1
            vol = 0.0
        day = row_day(r)
        chains = []
        for field in ("originAsset", "destinationAsset"):
            chain, method = chain_of(r.get(field), token_chains, known)
            cov["asset_mapping"][method] += 1
            if method == "unknown":
                cov["unknown_assets"][str(r.get(field))] += 1
            chains.append(chain)
        if "unknown" in chains:
            cov["rows_with_unknown_chain"] += 1

        fee_usd = near_fee = 0.0
        fees = r.get("appFees") or []
        if fees:
            cov["rows_with_app_fees"] += 1
        hit_near = False
        for af in fees:
            bps = to_float((af or {}).get("fee"))
            rec = str((af or {}).get("recipient"))
            if bps is None:
                continue
            f_usd = vol * bps / 10_000.0
            fee_usd += f_usd
            is_near = rec in near_wallets
            if is_near:
                near_fee += f_usd
                hit_near = True
            d = recips.setdefault(rec, {"n": 0, "vol": 0.0, "fee": 0.0, "min": bps, "max": bps, "bps_sum": 0.0,
                                        "near": is_near})
            d["n"] += 1
            d["vol"] += vol
            d["fee"] += f_usd
            d["min"], d["max"] = min(d["min"], bps), max(d["max"], bps)
            d["bps_sum"] += bps
        if hit_near:
            cov["near_wallet_fee_rows"] += 1

        a = routes[(day, chains[0], chains[1])]
        a[0] += 1
        a[1] += vol
        a[2] += fee_usd
        a[3] += near_fee
        p = priv[(day, str(r.get("depositType")), str(r.get("recipientType")))]
        p[0] += 1
        p[1] += vol

    attributable = cov["near_wallet_fee_rows"] > 0
    route_rows = [[d, o, t, n, f"{v:.2f}", f"{fe:.4f}", (f"{nf:.4f}" if attributable else ""), source]
                  for (d, o, t), (n, v, fe, nf) in sorted(routes.items())]
    recip_rows = [[rec, d["n"], f"{d['vol']:.2f}", f"{d['fee']:.4f}", f"{d['min']:g}", f"{d['max']:g}",
                   f"{d['bps_sum']:g}", int(d["near"])]
                  for rec, d in sorted(recips.items(), key=lambda kv: -kv[1]["fee"])]
    priv_rows = [[d, dt, rt, n, f"{v:.2f}"] for (d, dt, rt), (n, v) in sorted(priv.items())]

    unknown_sorted = sorted(cov["unknown_assets"].items(), key=lambda kv: -kv[1])
    coverage = {
        "source": source,
        "rows_read": cov["rows_read"], "rows_aggregated": cov["rows_aggregated"],
        "duplicates_ignored": cov["duplicates_ignored"], "non_success_ignored": cov["non_success_ignored"],
        "outside_window_ignored": cov["outside_window_ignored"],
        "earliest_created_at": iso(cov["min_ts"]) if cov["min_ts"] else None,
        "latest_created_at": iso(cov["max_ts"]) if cov["max_ts"] else None,
        "unpriced_rows": cov["unpriced_rows"], "rows_with_app_fees": cov["rows_with_app_fees"],
        "rows_with_unknown_chain": cov["rows_with_unknown_chain"],
        "asset_mapping_method_counts": dict(cov["asset_mapping"]),
        "distinct_unknown_assets": len(unknown_sorted), "top_unknown_assets": unknown_sorted[:25],
        "fee_unit_assumption": "appFees.fee = basis points of amountIn (1Click AppFee schema); "
                               "Explorer spec states no unit",
        "near_fee_attribution": ("recipient in config.FEE_WALLETS" if attributable else
                                 "EMPTY: no app-fee recipient matched config.FEE_WALLETS; see fee_recipients.csv"),
        "total_volume_usd": round(sum(v[1] for v in routes.values()), 2),
        "total_fee_usd": round(sum(v[2] for v in routes.values()), 4),
    }
    return {"routes": route_rows, "recipients": recip_rows, "privacy": priv_rows, "coverage": coverage}


def write_outputs(agg: dict, out_dir: Path = PROCESSED) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, header, key in (("swaps_by_route.csv", ROUTE_HEADER, "routes"),
                              ("fee_recipients.csv", RECIP_HEADER, "recipients"),
                              ("swaps_by_privacy.csv", PRIV_HEADER, "privacy")):
        with open(out_dir / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(agg[key])
    (out_dir / "explorer_coverage.json").write_text(json.dumps(agg["coverage"], indent=1), encoding="utf-8")


def coverage_line(c: dict) -> str:
    return (f"coverage: {c['rows_aggregated']:,} SUCCESS swaps, {c['earliest_created_at']} .. "
            f"{c['latest_created_at']} | volume ${c['total_volume_usd']:,.0f} | app fees ${c['total_fee_usd']:,.0f} | "
            f"rows with an unknown-chain asset: {c['rows_with_unknown_chain']:,} | unpriced: {c['unpriced_rows']:,}")


# ----------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pull NEAR Intents Explorer swaps and aggregate by route.")
    ap.add_argument("--days", type=int, default=30, help="window: the last N whole UTC days plus today so far")
    ap.add_argument("--max-pages", type=int, default=2000, help="safety cap on requests per run (1000 rows each)")
    ap.add_argument("--page-size", type=int, default=1000)
    ap.add_argument("--sleep", type=float, default=MIN_INTERVAL, help="seconds between requests (min 5.0)")
    ap.add_argument("--aggregate-only", action="store_true", help="skip the API, re-aggregate raw files on disk")
    args = ap.parse_args(argv)

    env.load_env()
    jwt = env.get("NEAR_INTENTS_JWT")
    now = int(time.time())
    today0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = int((today0 - timedelta(days=args.days)).timestamp())
    pull_stats = None

    if args.aggregate_only:
        print("explorer: --aggregate-only, not calling the API")
    elif not jwt:
        print("skipped: NEAR_INTENTS_JWT not set (get a JWT from the NEAR Intents partner dashboard and put "
              "NEAR_INTENTS_JWT=... in .env); Explorer pull not run")
    else:
        est = args.max_pages * max(args.sleep, 5.0) / 60
        print(f"explorer: pulling SUCCESS swaps {iso(start)}..{iso(now)} "
              f"(<= {args.max_pages} pages x {args.page_size} rows, worst case ~{est:.0f} min)")
        client = Client(jwt, min_interval=args.sleep)
        try:
            pull_stats = pull(client, start, now, RAW_DIR, min(max(args.page_size, 1), 1000), args.max_pages, now)
            print(f"  pull: {pull_stats['pages']} pages, {pull_stats['new_rows']:,} new rows, "
                  f"{pull_stats['dup_rows']:,} duplicates skipped, complete={pull_stats['complete']}")
            if not pull_stats["complete"]:
                print("  ! window NOT fully pulled (--max-pages reached or cursor stalled). Rerun to continue; "
                      "until then the oldest days in the CSVs are partial.")
        except AuthError as e:
            print(f"skipped: Explorer API rejected NEAR_INTENTS_JWT ({e}); check the token / its expiry")
        except (RuntimeError, requests.RequestException) as e:
            print(f"explorer: pull stopped ({e}); progress is checkpointed, rerun to continue")
            pull_stats = {"complete": False}

    if not RAW_DIR.is_dir() or not any(RAW_DIR.glob("*.jsonl")):
        print("explorer: no raw swaps on disk -> swaps_by_route.csv / fee_recipients.csv / swaps_by_privacy.csv "
              "not written (nothing is invented)")
        return 0

    token_chains = load_token_chains(RAW_DIR)
    if not token_chains:
        print("  ! no token list available: every asset maps to 'unknown'")
    agg = aggregate(iter_raw(RAW_DIR), token_chains, SOURCE, start, now)
    cp = load_checkpoint(RAW_DIR)
    agg["coverage"].update(window_start=iso(start), window_end=iso(now), days=args.days,
                           window_fully_pulled=(not cp.get("active")) and not gaps(start, now - SETTLE_LAG - 1,
                                                                                   cp["completed"]),
                           generated_at=iso(now))
    write_outputs(agg, PROCESSED)
    c = agg["coverage"]
    print(coverage_line(c))
    print(f"  window fully pulled: {c['window_fully_pulled']} | asset mapping: {c['asset_mapping_method_counts']}")
    print(f"  near_fee_usd: {c['near_fee_attribution']}")
    print(f"wrote {PROCESSED / 'swaps_by_route.csv'} ({len(agg['routes'])} rows), fee_recipients.csv "
          f"({len(agg['recipients'])}), swaps_by_privacy.csv ({len(agg['privacy'])}), explorer_coverage.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
