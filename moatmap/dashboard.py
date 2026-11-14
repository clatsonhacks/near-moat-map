"""Render dashboard/index.html: a single static file, data embedded, no network needed to view it.

    python -m moatmap.dashboard

Every panel has the chart, the query or API call behind it, and one sentence on what it shows.
A panel whose input file is missing renders as "not computed" with the reason.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .config import DASHBOARD, PROCESSED, SQL_DIR


def _csv(name: str):
    p = PROCESSED / name
    if not p.exists() or p.stat().st_size == 0:
        return None
    df = pd.read_csv(p)
    if not len(df):
        return None
    return json.loads(df.to_json(orient="records"))


def _json(name: str):
    p = PROCESSED / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _sql(name: str) -> str:
    p = SQL_DIR / name
    return p.read_text(encoding="utf-8") if p.exists() else f"-- {name} not found"


def main() -> None:
    results = _json("results.json")
    if results is None:
        raise SystemExit("results.json missing: run python -m moatmap.analyze first")
    data = {
        "results": results,
        "model": _json("model.json"),
        "coverage": _csv("coverage.csv"),
        "llama": _csv("llama_daily.csv"),
        "llama_rivals": _csv("llama_rivals_daily.csv"),
        "bq_daily": _csv("bq_daily.csv"),
        "bq_top": _csv("bq_top_contracts.csv"),
        "bq_freshness": _json("bq_freshness.json"),
        "privacy": _csv("swaps_by_privacy.csv"),
        "onchain_ci": _csv("onchain_route_share_ci.csv"),
        "onchain_meta": _json("onchain_sample_meta.json"),
        "sql": {p.name: p.read_text(encoding="utf-8") for p in sorted(SQL_DIR.glob("*.sql"))},
    }
    template = (Path(__file__).parent / "dashboard_template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    DASHBOARD.mkdir(exist_ok=True)
    out = DASHBOARD / "index.html"
    out.write_text(template.replace("/*__DATA__*/null", payload), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
