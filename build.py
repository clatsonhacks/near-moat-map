"""One command that rebuilds every chart from raw data.

    python build.py            # pull everything, analyse, render dashboard/index.html
    python build.py --offline  # skip the network pulls; re-analyse and re-render from data/processed

Steps that need credentials (Explorer API JWT, BigQuery key) skip themselves with a message
when the credential is absent; the dashboard then shows that panel as "not computed" rather
than showing anything made up.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PULLS = [
    ("coverage", "which chains each rival serves (keyless)"),
    ("quotes", "like-for-like cost quotes (keyless)"),
    ("llama", "DefiLlama fees / revenue / volume control totals (keyless)"),
    ("explorer", "NEAR Intents Explorer API swaps by route (needs NEAR_INTENTS_JWT)"),
    ("bq", "NEAR BigQuery network metrics (needs GOOGLE_APPLICATION_CREDENTIALS + GCP_PROJECT)"),
]
RENDER = [
    ("model", "valuation model: run rate, buyback yield, scenarios"),
    ("analyze", "test the two durability claims"),
    ("dashboard", "render dashboard/index.html"),
]


def run(module: str, why: str) -> bool:
    if not (ROOT / "moatmap" / f"{module}.py").exists():
        print(f"\n=== {module}: module not present, skipped")
        return False
    print(f"\n=== {module}: {why}", flush=True)
    t = time.time()
    rc = subprocess.call([sys.executable, "-m", f"moatmap.{module}"], cwd=ROOT)
    print(f"--- {module} finished in {time.time() - t:.0f}s, exit code {rc}")
    return rc == 0


def main() -> int:
    offline = "--offline" in sys.argv
    failed = []
    for module, why in ([] if offline else PULLS) + RENDER:
        if not run(module, why):
            failed.append(module)
    if failed:
        print(f"\nFinished with failures in: {', '.join(failed)}. Panels that depend on them show as not computed.")
    print(f"\nOpen {ROOT / 'dashboard' / 'index.html'}")
    return 1 if any(m in failed for m, _ in RENDER) else 0


if __name__ == "__main__":
    sys.exit(main())
