"""Join coverage, route revenue and quotes, and test the two Moat Map claims.

    python -m moatmap.analyze

Claim 1 (revenue): routes served by at most two of the five rivals bring in >= 50% of
                   NEAR Intents' cross-chain fee revenue.
Claim 2 (pricing): NEAR Intents' all-in cost on the least-contested routes (<= 2 rivals)
                   is >= 1.5x its cost on the most-contested routes (>= 4 rivals), same notional.

Both thresholds were fixed before any data was pulled. Nothing here is tuned to the result.
Every input is optional: a missing file makes the corresponding test "not computed" with the
reason, never a guessed value.

Contestation of a route = number of rivals whose live API lists BOTH chains (chain-level
approximation of route support; see THESIS.md "What the data cannot say").
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from .config import NEAR, PROCESSED, RIVALS

LOW_MAX = 2    # "least contested": at most two rivals
HIGH_MIN = 4   # "most contested": four or five rivals
REVENUE_THRESHOLD = 0.50
PRICE_RATIO_THRESHOLD = 1.5


def _read(name: str) -> pd.DataFrame | None:
    p = PROCESSED / name
    if not p.exists() or p.stat().st_size == 0:
        return None
    df = pd.read_csv(p)
    return df if len(df) else None


def contestation_table(cov: pd.DataFrame) -> dict[str, list[str]]:
    """chain -> list of rivals that support it."""
    c = cov[(cov["supported"] == 1) & (cov["provider"].isin(RIVALS))]
    out: dict[str, list[str]] = {ch: [] for ch in cov["chain"].unique()}
    for ch, grp in c.groupby("chain"):
        out[ch] = sorted(grp["provider"].unique())
    return out


def rivals_on_route(by_chain: dict[str, list[str]], o: str, d: str) -> list[str] | None:
    if o not in by_chain or d not in by_chain:
        return None  # chain not in the coverage table -> contestation unknown
    return sorted(set(by_chain[o]) & set(by_chain[d]))


def bucket_label(n) -> str:
    if n is None or pd.isna(n):
        return "unknown"
    return str(int(n))


def revenue_test(routes: pd.DataFrame, by_chain: dict[str, list[str]], source: str) -> dict:
    """routes: origin_chain, dest_chain, n_swaps, volume_usd, fee_usd[, near_fee_usd] (already summed over days)."""
    df = routes.copy()
    df["same_chain"] = df["origin_chain"] == df["dest_chain"]
    df["rivals"] = [rivals_on_route(by_chain, o, d) for o, d in zip(df["origin_chain"], df["dest_chain"])]
    df["n_rivals"] = df["rivals"].map(lambda r: None if r is None else len(r))
    df["route"] = df["origin_chain"] + ">" + df["dest_chain"]

    # Which money column carries the claim? Prefer NEAR's own captured fees, then all fees, then volume.
    measure, measure_note = None, None
    for col, note in (
        ("near_fee_usd", "fees paid to NEAR-owned recipients"),
        ("fee_usd", "all app/protocol fees (NEAR's own share not separable in this source)"),
        ("volume_usd", "swap volume (no fee data in this source, so this is a volume share, not a revenue share)"),
    ):
        if col in df.columns and pd.to_numeric(df[col], errors="coerce").fillna(0).sum() > 0:
            measure, measure_note = col, note
            break
    if measure is None:
        return {"computed": False, "reason": f"{source}: no positive fee or volume column"}
    df[measure] = pd.to_numeric(df[measure], errors="coerce").fillna(0.0)
    df["volume_usd"] = pd.to_numeric(df["volume_usd"], errors="coerce").fillna(0.0)

    cross = df[~df["same_chain"]]
    total = float(cross[measure].sum())
    known = cross[cross["n_rivals"].notna()]
    low = float(known.loc[known["n_rivals"] <= LOW_MAX, measure].sum())
    unknown = float(cross.loc[cross["n_rivals"].isna(), measure].sum())

    buckets = []
    for lab, grp in cross.groupby(cross["n_rivals"].map(bucket_label)):
        buckets.append({
            "n_rivals": lab,
            "routes": int(len(grp)),
            "n_swaps": int(grp["n_swaps"].sum()),
            "volume_usd": float(grp["volume_usd"].sum()),
            "measure_usd": float(grp[measure].sum()),
            "share": float(grp[measure].sum() / total) if total else None,
            # realised take rates on executed swaps: fees as basis points of volume
            "fee_bps": float(pd.to_numeric(grp["fee_usd"], errors="coerce").sum() / grp["volume_usd"].sum() * 1e4)
            if "fee_usd" in grp and grp["volume_usd"].sum() else None,
            "near_fee_bps": float(pd.to_numeric(grp["near_fee_usd"], errors="coerce").sum() / grp["volume_usd"].sum() * 1e4)
            if "near_fee_usd" in grp and grp["volume_usd"].sum() else None,
        })
    buckets.sort(key=lambda b: (b["n_rivals"] == "unknown", b["n_rivals"]))

    top = cross.sort_values(measure, ascending=False).head(25)
    top_routes = [{
        "route": r.route, "n_rivals": bucket_label(r.n_rivals),
        "rivals": r.rivals if r.rivals is not None else None,
        "n_swaps": int(r.n_swaps), "volume_usd": float(r.volume_usd),
        "measure_usd": float(getattr(r, measure)),
        "share": float(getattr(r, measure) / total) if total else None,
    } for r in top.itertuples()]

    alt = {}
    for col in ("near_fee_usd", "fee_usd", "volume_usd", "n_swaps"):
        if col in cross.columns:
            v = pd.to_numeric(cross[col], errors="coerce").fillna(0.0)
            if v.sum() > 0:
                alt[col] = {"total": float(v.sum()),
                            "share_low": float(v[cross["n_rivals"].notna() & (cross["n_rivals"] <= LOW_MAX)].sum() / v.sum())}
    def _rate(mask, col):
        g = cross[mask]
        v = g["volume_usd"].sum()
        return float(pd.to_numeric(g[col], errors="coerce").sum() / v * 1e4) if (col in g and v) else None
    lo_m = cross["n_rivals"].notna() & (cross["n_rivals"] <= LOW_MAX)
    hi_m = cross["n_rivals"].notna() & (cross["n_rivals"] >= HIGH_MIN)
    realised = {col: {"low_bps": _rate(lo_m, col), "high_bps": _rate(hi_m, col)} for col in ("fee_usd", "near_fee_usd")}
    for v in realised.values():
        v["ratio"] = v["low_bps"] / v["high_bps"] if v["low_bps"] and v["high_bps"] else None
    share_low = low / total if total else None
    # Unknown-contestation routes could fall either side: report the bounds.
    share_low_max = (low + unknown) / total if total else None
    return {
        "computed": True,
        "source": source,
        "measure": measure,
        "measure_note": measure_note,
        "cross_chain_total_usd": total,
        "same_chain_total_usd": float(df.loc[df["same_chain"], measure].sum()),
        "share_low_contestation": share_low,
        "share_low_contestation_upper_bound": share_low_max,
        "unknown_share": unknown / total if total else None,
        "threshold": REVENUE_THRESHOLD,
        "verdict": None if share_low is None else (
            "supported" if share_low >= REVENUE_THRESHOLD
            else ("inconclusive" if share_low_max >= REVENUE_THRESHOLD else "rejected")),
        "share_low_by_measure": alt,
        "realised_fee_rate": realised,
        "buckets": buckets,
        "top_routes": top_routes,
    }


def pricing_test(q: pd.DataFrame, by_chain: dict[str, list[str]]) -> dict:
    panel = _read("quote_panel.csv")
    ptype = dict(zip(panel["route"], panel["pair_type"])) if panel is not None and "pair_type" in panel else {}
    q = q[q["ok"] == 1].copy()
    q["cost_bps"] = pd.to_numeric(q["cost_bps"], errors="coerce")
    q = q.dropna(subset=["cost_bps"])
    if q.empty:
        return {"computed": False, "reason": "no successful quotes"}
    # Median across snapshots so one odd minute does not decide the result.
    med = (q.groupby(["route", "origin_chain", "dest_chain", "origin_symbol", "dest_symbol", "notional_usd", "provider"])
             ["cost_bps"].agg(["median", "count"]).reset_index()
             .rename(columns={"median": "cost_bps", "count": "n_quotes"}))
    med["n_rivals"] = [
        (lambda r: None if r is None else len(r))(rivals_on_route(by_chain, o, d))
        for o, d in zip(med["origin_chain"], med["dest_chain"])]

    # same_asset = the same token on both chains (a pure bridge); cross_asset = a swap as well.
    # NEAR's documented fee differs between the two, so they are only ever compared within type.
    med["pair_type"] = med["route"].map(ptype).fillna("unclassified")
    near = med[med["provider"] == NEAR]
    rivals = med[med["provider"] != NEAR]
    best = (rivals.sort_values("cost_bps").groupby(["route", "notional_usd"]).first()
            .reset_index()[["route", "notional_usd", "provider", "cost_bps"]]
            .rename(columns={"provider": "best_rival", "cost_bps": "best_rival_bps"}))
    n_quoting = (rivals.groupby(["route", "notional_usd"])["provider"].nunique()
                 .reset_index().rename(columns={"provider": "rivals_quoting"}))
    per_route = near.merge(best, on=["route", "notional_usd"], how="left").merge(
        n_quoting, on=["route", "notional_usd"], how="left")
    per_route["rivals_quoting"] = per_route["rivals_quoting"].fillna(0).astype(int)
    per_route["premium_bps"] = per_route["cost_bps"] - per_route["best_rival_bps"]

    by_notional = []
    for (pair_type, notional), grp in near.groupby(["pair_type", "notional_usd"]):
        g = grp.dropna(subset=["n_rivals"])
        lo = g[g["n_rivals"] <= LOW_MAX]["cost_bps"]
        hi = g[g["n_rivals"] >= HIGH_MIN]["cost_bps"]
        entry = {
            "pair_type": pair_type,
            "notional_usd": float(notional),
            "low_routes": int(len(lo)), "high_routes": int(len(hi)),
            "low_median_bps": float(lo.median()) if len(lo) else None,
            "high_median_bps": float(hi.median()) if len(hi) else None,
        }
        if len(lo) and len(hi) and hi.median() > 0:
            entry["ratio"] = float(lo.median() / hi.median())
            entry["verdict"] = "supported" if entry["ratio"] >= PRICE_RATIO_THRESHOLD else "rejected"
        else:
            entry["ratio"] = None
            entry["verdict"] = "not computed (need quotes in both groups and a positive contested-route cost)"
        by_notional.append(entry)

    rows = []
    for r in per_route.sort_values(["notional_usd", "n_rivals", "route"]).itertuples():
        rows.append({
            "route": r.route, "pair": f"{r.origin_symbol}>{r.dest_symbol}", "pair_type": r.pair_type,
            "notional_usd": float(r.notional_usd), "n_rivals": bucket_label(r.n_rivals),
            "near_bps": float(r.cost_bps), "n_quotes": int(r.n_quotes),
            "best_rival": None if pd.isna(r.best_rival) else r.best_rival,
            "best_rival_bps": None if pd.isna(r.best_rival_bps) else float(r.best_rival_bps),
            "premium_bps": None if pd.isna(r.premium_bps) else float(r.premium_bps),
            "rivals_quoting": int(r.rivals_quoting),
        })
    all_quotes = [{
        "route": r.route, "notional_usd": float(r.notional_usd), "provider": r.provider,
        "cost_bps": float(r.cost_bps), "n_rivals": bucket_label(r.n_rivals),
    } for r in med.itertuples()]
    return {
        "computed": True,
        "threshold": PRICE_RATIO_THRESHOLD,
        "snapshots": len(list((PROCESSED / "quote_snapshots").glob("quotes_*.csv"))) or 1,
        "first_quote": str(q["fetched_at"].min()), "last_quote": str(q["fetched_at"].max()),
        "by_notional": by_notional,
        "routes": rows,
        "all_quotes": all_quotes,
    }


def main() -> None:
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "definitions": {"low_max": LOW_MAX, "high_min": HIGH_MIN, "rivals": RIVALS}}

    cov = _read("coverage.csv")
    if cov is None:
        out["coverage"] = {"computed": False, "reason": "coverage.csv missing: run python -m moatmap.coverage"}
        by_chain = {}
    else:
        by_chain = contestation_table(cov)
        near_chains = sorted(cov.loc[(cov["provider"] == NEAR) & (cov["supported"] == 1), "chain"].unique())
        out["coverage"] = {
            "computed": True,
            "fetched_at": str(cov["fetched_at"].max()),
            "chains": [{"chain": ch, "rivals": by_chain.get(ch, []), "n_rivals": len(by_chain.get(ch, []))}
                       for ch in sorted(near_chains, key=lambda c: (-len(by_chain.get(c, [])), c))],
            "per_rival": {r: int(((cov["provider"] == r) & (cov["supported"] == 1)).sum()) for r in RIVALS},
        }

    # Revenue split: one test per available source, official source first.
    out["revenue"] = []
    ex = _read("swaps_by_route.csv")
    if ex is not None and by_chain:
        # The newest day is still in progress and the oldest day may be cut off where the pull
        # stopped, so both are left out: only complete UTC days are analysed.
        days = sorted(ex["day"].unique())
        cov_p = PROCESSED / "explorer_coverage.json"
        full = cov_p.exists() and json.loads(cov_p.read_text(encoding="utf-8")).get("window_fully_pulled")
        dropped = ([] if full else [days[0]]) + [days[-1]] if len(days) > 2 else []
        ex = ex[~ex["day"].isin(dropped)]
        agg =ex.groupby(["origin_chain", "dest_chain"], as_index=False)[
            [c for c in ("n_swaps", "volume_usd", "fee_usd", "near_fee_usd") if c in ex.columns]].sum(min_count=1)
        res = revenue_test(agg, by_chain, "NEAR Intents Explorer API")
        res["window"] = [str(ex["day"].min()), str(ex["day"].max())]
        res["partial_days_dropped"] = dropped
        # Stability and concentration: the thin-route share week by week, and with Zcash set aside.
        x = ex[ex["origin_chain"] != ex["dest_chain"]].copy()
        if "near_fee_usd" in x.columns and pd.to_numeric(x["near_fee_usd"], errors="coerce").fillna(0).sum() > 0:
            x["near_fee_usd"] = pd.to_numeric(x["near_fee_usd"], errors="coerce").fillna(0.0)
            nr = [rivals_on_route(by_chain, o, d) for o, d in zip(x["origin_chain"], x["dest_chain"])]
            x["thin"] = [r is not None and len(r) <= LOW_MAX for r in nr]
            x["week"] = pd.to_datetime(x["day"]).dt.strftime("%G-W%V")
            res["weekly"] = [{
                "week": wk, "from": str(g["day"].min()), "to": str(g["day"].max()), "days": int(g["day"].nunique()),
                "share_near_fee": float(g.loc[g["thin"], "near_fee_usd"].sum() / g["near_fee_usd"].sum()),
                "share_volume": float(g.loc[g["thin"], "volume_usd"].sum() / g["volume_usd"].sum()),
            } for wk, g in x.groupby("week")]
            z = (x["origin_chain"] == "zec") | (x["dest_chain"] == "zec")
            nz = x[~z]
            res["zcash"] = {
                "share_near_fee": float(x.loc[z, "near_fee_usd"].sum() / x["near_fee_usd"].sum()),
                "share_volume": float(x.loc[z, "volume_usd"].sum() / x["volume_usd"].sum()),
                "thin_share_ex_zcash": float(nz.loc[nz["thin"], "near_fee_usd"].sum() / nz["near_fee_usd"].sum()),
            }
        out["revenue"].append(res)
    else:
        out["revenue"].append({"computed": False, "source": "NEAR Intents Explorer API",
                               "reason": "swaps_by_route.csv missing: set NEAR_INTENTS_JWT in .env and run python -m moatmap.explorer"})
    oc = _read("onchain_by_route.csv")
    if oc is not None and by_chain:
        res = revenue_test(oc, by_chain, "public-chain block sample (intents.near logs)")
        meta_p = PROCESSED / "onchain_sample_meta.json"
        if meta_p.exists():
            res["sample_meta"] = json.loads(meta_p.read_text(encoding="utf-8"))
        out["revenue"].append(res)

    q = _read("quotes.csv")
    out["pricing"] = pricing_test(q, by_chain) if (q is not None and by_chain) else {
        "computed": False, "reason": "quotes.csv or coverage.csv missing: run python -m moatmap.quotes"}

    (PROCESSED / "results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    # Console summary: the numbers a reader checks first.
    for r in out["revenue"]:
        if r.get("computed"):
            print(f"[revenue | {r['source']}] {r['measure']}: {r['share_low_contestation']:.1%} from routes with <= {LOW_MAX} rivals "
                  f"(upper bound {r['share_low_contestation_upper_bound']:.1%}) -> {r['verdict']}")
            print("   by measure: " + ", ".join(f"{k} {v['share_low']:.1%} of {v['total']:,.0f}" for k, v in r["share_low_by_measure"].items()))
        else:
            print(f"[revenue | {r['source']}] not computed: {r['reason']}")
    p = out["pricing"]
    if p.get("computed"):
        for e in p["by_notional"]:
            ratio = "n/a" if e["ratio"] is None else f"{e['ratio']:.2f}x"
            print(f"[pricing | {e['pair_type']} ${e['notional_usd']:,.0f}] low {e['low_median_bps']} bps ({e['low_routes']} routes) vs "
                  f"high {e['high_median_bps']} bps ({e['high_routes']} routes): {ratio} -> {e['verdict']}")
    else:
        print(f"[pricing] not computed: {p['reason']}")
    print(f"wrote {PROCESSED / 'results.json'}")


if __name__ == "__main__":
    main()
