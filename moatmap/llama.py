"""DefiLlama control totals for NEAR Intents (keyless).  Run: python -m moatmap.llama

What it pulls (free API, no key):
  https://api.llama.fi/summary/fees/near-intents?dataType=dailyFees
  https://api.llama.fi/summary/fees/near-intents?dataType=dailyRevenue
  https://api.llama.fi/summary/dexs/near-intents            (daily volume)
Each response carries `totalDataChart`: [[unix_seconds_at_00:00_UTC, usd_value], ...].

Outputs
  data/raw/llama/*.json                    untouched responses + _manifest.json (what was fetched, when)
  data/processed/llama_daily.csv           day,fees_usd,revenue_usd,volume_usd
  data/processed/llama_rivals_daily.csv    day,protocol,fees_usd,revenue_usd,volume_usd

llama_daily.csv is the aggregate control total that the route-level split
(swaps_by_route.csv) must reconcile against. A blank cell means DefiLlama has no value
for that day and series (the volume series starts earlier than the fee series); it is
never filled with 0.

DefiLlama methodology for NEAR Intents (dimension-adapters/fees/near-intents/index.ts and
the `methodology` field of the API response):
  * fees    = gross fees charged on swaps: the protocol fee plus the affiliate
              (distribution) fee each frontend collects, summed over all integrators. Read
              from NEAR's own Dune datasets (dune.near.dataset_near_intents_protocol_fees +
              dune.near.dataset_near_intents_fees), in USD.
  * revenue = NEAR's captured revenue, measured as NEAR / wNEAR arriving in the three
              revenue wallets in config.FEE_WALLETS (native TRANSFER actions with
              execution_status SUCCESS_VALUE, plus wrap.near FT transfers), excluding
              transfers between those wallets, valued in USD at the day's NEAR price.
              Revenue is realised when the wallets are swept, i.e. in periodic lumps, so
              DAILY revenue is lumpy and on a sweep day can exceed that day's fees. Compare
              revenue to fees over multi-week windows, not day by day.
  * volume  = the dexs/near-intents adapter's daily swap volume in USD.
The most recent one or two days may be partial or not yet final on DefiLlama's side.

Rivals (context only): each name in config.RIVALS is tried against a list of candidate
slugs; a slug is used only if the API answers 200 with a non-empty chart, and the slug and
the protocol name DefiLlama resolved it to are printed and stored in the manifest.
DefiLlama's bridge-volume API (bridges.llama.fi) is paid (HTTP 402), so volume is only
filled for rivals that also have a `dexs` adapter; other volume cells stay blank.
Rival "revenue" follows each adapter's own definition and is not comparable 1:1.
"""
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from .config import NEAR, PROCESSED, RAW, RIVALS

API = "https://api.llama.fi/summary"
UA = "moatmap-research/0.1"
SLUG = "near-intents"
RAW_DIR = RAW / "llama"

# Candidate slugs per rival, tried in order. `fees` and `dexs` are separate DefiLlama
# dashboards and can use different slugs for the same project.
#   thorchain: the bare slug "thorchain" resolves to the THORChain *chain* (L1 gas fees);
#   the swap protocol is "thorchain-dex", so that is tried first.
RIVAL_SLUGS = {
    "across": {"fees": ["across"], "dexs": ["across"]},
    "relay": {"fees": ["relay"], "dexs": ["relay"]},
    "debridge": {"fees": ["debridge", "dln"], "dexs": ["debridge", "dln"]},
    "chainflip": {"fees": ["chainflip", "chainflip-amm"], "dexs": ["chainflip", "chainflip-amm"]},
    "thorchain": {"fees": ["thorchain-dex", "thorchain"], "dexs": ["thorchain-dex", "thorchain"]},
}


def fetch(kind: str, slug: str, data_type: str = None, session=None, tries: int = 4):
    """GET one summary endpoint. Returns (json_or_None, http_status, url)."""
    url = f"{API}/{kind}/{slug}"
    params = {"dataType": data_type} if data_type else None
    sess = session or requests
    last = None
    for attempt in range(tries):
        try:
            r = sess.get(url, params=params, headers={"User-Agent": UA}, timeout=60)
        except requests.RequestException as e:
            last = f"network error: {type(e).__name__}"
            time.sleep(2 + 3 * attempt)
            continue
        if r.status_code == 200:
            try:
                return r.json(), 200, r.url
            except ValueError:
                last = "200 but not JSON"
        elif r.status_code in (400, 404):
            return None, r.status_code, r.url  # slug / dashboard does not exist
        else:
            last = f"HTTP {r.status_code}"
        time.sleep(2 + 3 * attempt)
    print(f"  ! {kind}/{slug} {data_type or ''}: gave up after {tries} tries ({last})")
    return None, 0, url


def series(payload) -> dict:
    """totalDataChart -> {YYYY-MM-DD: value}."""
    out = {}
    for point in (payload or {}).get("totalDataChart") or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2 or point[1] is None:
            continue
        day = datetime.fromtimestamp(int(point[0]), tz=timezone.utc).strftime("%Y-%m-%d")
        out[day] = float(point[1])
    return out


def _save_raw(name: str, payload) -> str:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path.name


def _fmt(v) -> str:
    return "" if v is None else f"{v:.2f}"


def pull_protocol(label: str, fees_slugs, dexs_slugs, manifest: list, session=None) -> dict:
    """Returns {'fees': {...}, 'revenue': {...}, 'volume': {...}} (missing series -> {})."""
    out = {"fees": {}, "revenue": {}, "volume": {}}
    plan = [("fees", "fees", "dailyFees", fees_slugs),
            ("revenue", "fees", "dailyRevenue", fees_slugs),
            ("volume", "dexs", None, dexs_slugs)]
    for metric, kind, data_type, slugs in plan:
        for slug in slugs:
            payload, status, url = fetch(kind, slug, data_type, session=session)
            entry = {"protocol": label, "metric": metric, "url": url, "http_status": status,
                     "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            s = series(payload) if payload else {}
            if s:
                entry.update(raw_file=_save_raw(f"{label}__{kind}__{slug}__{data_type or 'volume'}.json", payload),
                             resolved_name=payload.get("name"), resolved_slug=payload.get("slug"),
                             category=payload.get("category"), points=len(s),
                             first_day=min(s), last_day=max(s), used=True)
                manifest.append(entry)
                out[metric] = s
                print(f"  {label:<12} {metric:<8} ok   slug={slug} -> '{payload.get('name')}' "
                      f"({len(s)} days, {min(s)}..{max(s)})")
                break
            entry["used"] = False
            manifest.append(entry)
            why = f"HTTP {status}" if status != 200 else "empty chart"
            print(f"  {label:<12} {metric:<8} skip slug={slug} ({why})")
    return out


def window_totals(rows, days: int = 30):
    """Sum the last `days` calendar days present in rows [(day, fees, rev, vol)]."""
    if not rows:
        return None
    last = datetime.strptime(rows[-1][0], "%Y-%m-%d")
    first = (last - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    sel = [r for r in rows if r[0] >= first]
    tot = [sum(v for v in (r[i] for r in sel) if v is not None) for i in (1, 2, 3)]
    return first, rows[-1][0], len(sel), tot


def main(argv=None) -> int:
    PROCESSED.mkdir(parents=True, exist_ok=True)
    manifest = []
    session = requests.Session()

    print("DefiLlama: NEAR Intents")
    near = pull_protocol(NEAR, [SLUG], [SLUG], manifest, session)
    if not near["fees"] and not near["volume"]:
        print("llama: no NEAR Intents data could be fetched; llama_daily.csv left untouched")
        return 0

    days = sorted(set(near["fees"]) | set(near["revenue"]) | set(near["volume"]))
    rows = [(d, near["fees"].get(d), near["revenue"].get(d), near["volume"].get(d)) for d in days]
    out = PROCESSED / "llama_daily.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day", "fees_usd", "revenue_usd", "volume_usd"])
        for d, fe, re_, vo in rows:
            w.writerow([d, _fmt(fe), _fmt(re_), _fmt(vo)])
    print(f"wrote {out} ({len(rows)} rows, {days[0]}..{days[-1]})")

    print("DefiLlama: rivals (context)")
    rival_rows = []
    for name in RIVALS:
        cand = RIVAL_SLUGS.get(name, {"fees": [name], "dexs": [name]})
        got = pull_protocol(name, cand["fees"], cand["dexs"], manifest, session)
        for d in sorted(set(got["fees"]) | set(got["revenue"]) | set(got["volume"])):
            rival_rows.append((d, name, got["fees"].get(d), got["revenue"].get(d), got["volume"].get(d)))
    rival_rows.sort(key=lambda r: (r[0], r[1]))
    out_r = PROCESSED / "llama_rivals_daily.csv"
    with open(out_r, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day", "protocol", "fees_usd", "revenue_usd", "volume_usd"])
        for d, p, fe, re_, vo in rival_rows:
            w.writerow([d, p, _fmt(fe), _fmt(re_), _fmt(vo)])
    print(f"wrote {out_r} ({len(rival_rows)} rows)")

    _save_raw("_manifest.json", manifest)

    t = window_totals(rows, 30)
    if t:
        first, last, n, (fe, re_, vo) = t
        print(f"control totals, last 30 days in file ({first}..{last}, {n} rows): "
              f"fees ${fe:,.0f} | revenue ${re_:,.0f} | volume ${vo:,.0f}")
        if fe:
            print(f"  revenue/fees over that window: {re_ / fe:.1%} (daily revenue is lumpy: wallet sweeps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
