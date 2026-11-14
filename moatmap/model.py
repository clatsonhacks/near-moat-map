"""Valuation model: what NEAR Intents revenue is worth to a NEAR holder, and what would have to be true.

    python -m moatmap.model

Inputs (all measured, all rebuilt by build.py):
  data/processed/llama_daily.csv        NEAR Intents daily volume, fees, revenue (DefiLlama)
  data/raw/bq/circulating_supply.json   NEAR circulating / total supply (NEAR BigQuery, sql/circulating_supply.sql)
  https://1click.chaindefuser.com/v0/tokens   NEAR price at run time (wNEAR)

Output: data/processed/model.json

The model is arithmetic, not a forecast:
  revenue = volume x (revenue per $ of volume)
  buyback yield = annual revenue / market value        (since 2026-02-23 captured revenue buys back NEAR)
The scenario grid varies the two things the evidence leaves open - how much volume grows and how much
of each dollar of volume NEAR keeps - and reports the buyback yield each combination implies.
Scenario multipliers are assumptions and are labelled as such; nothing else in the output is assumed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd
import requests

from .config import ONECLICK, PROCESSED, RAW

VOLUME_MULTIPLES = [0.5, 1, 2, 5, 10]          # assumption: annual volume relative to the trailing-90-day run rate
YOCTO = 1e24


def _sum(df: pd.DataFrame, col: str) -> float:
    return float(pd.to_numeric(df[col], errors="coerce").fillna(0).sum())


def main() -> None:
    lp = PROCESSED / "llama_daily.csv"
    if not lp.exists():
        raise SystemExit("llama_daily.csv missing: run python -m moatmap.llama")
    l = pd.read_csv(lp).sort_values("day")
    # DefiLlama's newest day is usually incomplete for fees, so the windows end one day earlier.
    l = l.iloc[:-1]
    out: dict = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "last_day": str(l["day"].iloc[-1])}

    def window(days: int, offset: int = 0) -> dict:
        w = l.iloc[len(l) - days - offset: len(l) - offset]
        v, f, r = _sum(w, "volume_usd"), _sum(w, "fees_usd"), _sum(w, "revenue_usd")
        return {"from": str(w["day"].iloc[0]), "to": str(w["day"].iloc[-1]), "days": days,
                "volume_usd": v, "fees_usd": f, "revenue_usd": r,
                "annualised_volume_usd": v * 365 / days, "annualised_fees_usd": f * 365 / days,
                "annualised_revenue_usd": r * 365 / days,
                "fee_bps_of_volume": f / v * 1e4 if v else None,
                "revenue_bps_of_volume": r / v * 1e4 if v else None,
                "revenue_share_of_fees": r / f if f else None}

    t30, t90, prev90, yago90 = window(30), window(90), window(90, 90), window(90, 365)
    out["trailing_30d"], out["trailing_90d"] = t30, t90
    out["previous_90d"], out["same_90d_a_year_earlier"] = prev90, yago90
    out["growth"] = {
        "volume_vs_previous_90d": t90["volume_usd"] / prev90["volume_usd"] - 1 if prev90["volume_usd"] else None,
        "fees_vs_previous_90d": t90["fees_usd"] / prev90["fees_usd"] - 1 if prev90["fees_usd"] else None,
        "revenue_vs_previous_90d": t90["revenue_usd"] / prev90["revenue_usd"] - 1 if prev90["revenue_usd"] else None,
        "volume_vs_year_earlier": t90["volume_usd"] / yago90["volume_usd"] - 1 if yago90["volume_usd"] else None,
        "fees_vs_year_earlier": t90["fees_usd"] / yago90["fees_usd"] - 1 if yago90["fees_usd"] else None,
    }
    m = l.assign(month=l["day"].str[:7]).groupby("month")[["volume_usd", "fees_usd", "revenue_usd"]].sum(min_count=1)
    out["monthly"] = [{"month": k, **{c: (None if pd.isna(v) else float(v)) for c, v in row.items()}}
                      for k, row in m.iterrows()]
    out["all_time"] = {"volume_usd": _sum(l, "volume_usd"), "fees_usd": _sum(l, "fees_usd"),
                       "revenue_usd": _sum(l, "revenue_usd")}

    # ---- NEAR market value: on-chain supply x live price ----
    sp = RAW / "bq" / "circulating_supply.json"
    price = None
    try:
        toks = requests.get(f"{ONECLICK}/tokens", headers={"User-Agent": "moatmap-research/0.1"}, timeout=30).json()
        price = next(float(t["price"]) for t in toks if t.get("assetId") == "nep141:wrap.near")
    except Exception as e:  # no price -> no valuation; never a guessed price
        out["valuation"] = {"computed": False, "reason": f"NEAR price unavailable from 1Click: {e}"}
    if price is not None and not sp.exists():
        out["valuation"] = {"computed": False,
                            "reason": "circulating supply missing: run python -m moatmap.bq (needs BigQuery credentials)"}
    elif price is not None:
        rows = sorted(json.loads(sp.read_text(encoding="utf-8"))["rows"], key=lambda r: r["block_date"])
        first, last = rows[0], rows[-1]
        circ = float(last["circulating_tokens_supply"]) / YOCTO
        total = float(last["total_tokens_supply"]) / YOCTO
        span = (pd.Timestamp(last["block_date"]) - pd.Timestamp(first["block_date"])).days
        d_total = (float(last["total_tokens_supply"]) - float(first["total_tokens_supply"])) / YOCTO
        issuance_near_yr = d_total * 365 / span if span else None
        mcap = circ * price
        rev = t90["annualised_revenue_usd"]
        out["valuation"] = {
            "computed": True, "near_price_usd": price, "supply_date": last["block_date"],
            "circulating_supply_near": circ, "total_supply_near": total, "market_value_usd": mcap,
            "annual_revenue_usd_90d_run_rate": rev,
            "buyback_yield": rev / mcap,
            "market_value_to_revenue": mcap / rev if rev else None,
            "market_value_to_fees": mcap / t90["annualised_fees_usd"] if t90["annualised_fees_usd"] else None,
            "supply_window": [first["block_date"], last["block_date"]],
            "net_total_supply_change_near": d_total,
            "annualised_net_issuance_near": issuance_near_yr,
            "annualised_net_issuance_usd": issuance_near_yr * price if issuance_near_yr is not None else None,
            "annualised_net_issuance_rate": issuance_near_yr / total if issuance_near_yr is not None else None,
            "buyback_as_share_of_net_issuance": rev / (issuance_near_yr * price) if issuance_near_yr and issuance_near_yr > 0 else None,
        }
        # ---- scenario grid ----
        keep_now = t90["revenue_bps_of_volume"]          # what NEAR keeps today, bps of volume
        keep_all_fees = t90["fee_bps_of_volume"]         # upper bound: NEAR keeps every fee, partners keep none
        keeps = [("half of today's take", keep_now / 2), ("today's take", keep_now),
                 ("double today's take", keep_now * 2), ("all fees, none to partners (upper bound)", keep_all_fees)]
        base_vol = t90["annualised_volume_usd"]
        grid = []
        for vm in VOLUME_MULTIPLES:
            for label, bps in keeps:
                r = base_vol * vm * bps / 1e4
                grid.append({"volume_multiple": vm, "annual_volume_usd": base_vol * vm, "take": label,
                             "take_bps": bps, "annual_revenue_usd": r, "buyback_yield": r / mcap})
        out["scenarios"] = {"assumption": "volume multiples and take variants are assumptions; base values are measured",
                            "base_annual_volume_usd": base_vol, "grid": grid,
                            "volume_multiple_for_1pct_yield": 0.01 * mcap / (base_vol * keep_now / 1e4),
                            "volume_multiple_for_5pct_yield": 0.05 * mcap / (base_vol * keep_now / 1e4),
                            "volume_multiple_to_offset_net_issuance":
                                (issuance_near_yr * price) / (base_vol * keep_now / 1e4)
                                if issuance_near_yr and issuance_near_yr > 0 else None}

    (PROCESSED / "model.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    v = out.get("valuation", {})
    print(f"90-day run rate to {out['last_day']}: volume ${t90['annualised_volume_usd']/1e9:.1f}B/yr, "
          f"fees ${t90['annualised_fees_usd']/1e6:.1f}M/yr, revenue ${t90['annualised_revenue_usd']/1e6:.1f}M/yr")
    if v.get("computed"):
        print(f"NEAR market value ${v['market_value_usd']/1e9:.2f}B ({v['circulating_supply_near']/1e9:.3f}B NEAR x ${v['near_price_usd']}); "
              f"buyback yield {v['buyback_yield']:.2%}; net issuance {v['annualised_net_issuance_rate']:.2%}/yr")
    else:
        print("valuation not computed:", v.get("reason"))
    print(f"wrote {PROCESSED / 'model.json'}")


if __name__ == "__main__":
    main()
