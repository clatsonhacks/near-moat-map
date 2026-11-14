"""Shared contract for the Moat Map pipeline. Every module imports paths and names from here."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"            # untouched API / query responses (rebuildable)
PROCESSED = ROOT / "data" / "processed"  # tidy CSV/JSON the dashboard reads
SQL_DIR = ROOT / "sql"
DASHBOARD = ROOT / "dashboard"

# Chain codes are the 1Click `blockchain` codes (near, eth, base, arb, btc, sol, tron, ...).
# A route is directional: f"{origin_chain}>{dest_chain}".
RIVALS = ["across", "relay", "debridge", "chainflip", "thorchain"]
NEAR = "near_intents"

BQ_DATASET = "bigquery-public-data.crypto_near_mainnet_us"
ONECLICK = "https://1click.chaindefuser.com/v0"
EXPLORER = "https://explorer.near-intents.org/api/v0"

# NEAR's revenue wallets (DefiLlama fees/near-intents adapter, citing NEAR's Dune query 6740088)
FEE_WALLETS = [
    "fefundsadmin.sputnik-dao.near",
    "1csfundsadmin.sputnik-dao.near",
    "buybacks.multisignature.near",
]

# ---- Output schemas (CSV headers) ----
# coverage.csv        : chain,provider,supported,evidence,fetched_at        (provider in RIVALS + NEAR; supported 0/1)
# quotes.csv          : fetched_at,route,origin_chain,dest_chain,origin_symbol,dest_symbol,notional_usd,provider,
#                       ok,amount_in_usd,amount_out_usd,cost_bps,error
#                       cost_bps = (in_usd - out_usd)/in_usd*1e4, both sides priced with the SAME 1Click token prices
# swaps_by_route.csv  : day,origin_chain,dest_chain,n_swaps,volume_usd,fee_usd,near_fee_usd,source
#                       fee_usd = all app fees; near_fee_usd = fees whose recipient is a NEAR-owned account
# bq_daily.csv        : block_date,metric,value   (active_accounts, transactions, intents_receipts, ...)
# llama_daily.csv     : day,fees_usd,revenue_usd,volume_usd
