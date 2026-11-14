"""Like-for-like quote panel: all-in cost of NEAR Intents vs five rivals on a fixed set of routes.

Run:  python -m moatmap.quotes        (run `python -m moatmap.coverage` first; this reads its output)

Method
------
* PANEL (below) is a fixed list of directional routes with ONE asset pair each; every provider is asked
  for exactly that pair. Contestation (`n_rivals` = rivals whose own API lists BOTH chains) is computed
  at run time from data/processed/coverage.csv, never hard-coded.
* Sizes: $100, $1,000, $10,000. The input amount is ceil(notional / 1Click price) in smallest units.
* Before each route the 1Click token list is re-fetched (saved raw); that snapshot prices BOTH legs for
  EVERY provider on the route:
      amount_in_usd  = input units  x 1Click price(origin token)
      amount_out_usd = output units x 1Click price(dest token)      [- flat fee, deBridge only, see below]
      cost_bps       = (amount_in_usd - amount_out_usd) / amount_in_usd x 1e4
  Providers' own USD estimates are never used. cost_bps is therefore all-in: spread/price impact,
  protocol + app fees, and destination-side gas/withdrawal fees that the provider nets out of the output.
* Providers quoted on a route = near_intents + every rival with coverage.supported == 1 on both chains.
  If such a rival cannot quote the specific asset pair the row is written with ok=0 and the reason.
  Failures are never filled in. Each failed request is retried once after 2 s (same rule for everyone).

What "output" means per provider (the number a user would actually receive)
---------------------------------------------------------------------------
* near_intents : POST /v0/quote, dry=true, EXACT_INPUT, ORIGIN_CHAIN -> DESTINATION_CHAIN; quote.amountOut.
  KEYLESS FEE: no JWT / API key is sent. The docs (docs.near-intents.org/resources/fees, read 2026-11-14)
  say unauthenticated quotes pay "an additional 0.25% (25 bps)" on top of the 0.0001% (1 pip) on-chain
  protocol fee. The LIVE API, however, echoes the fee it actually applied in quoteRequest.appFees: on
  2026-11-14 keyless quotes carried a single entry, recipient 5880ad2b...9247dd, fee = 20 bps on
  cross-asset swaps and fee = 1 bp on stablecoin / same-asset cross-chain routes (and an empty list on
  btc>near BTC->BTC), i.e. the schedule the docs describe for AUTHENTICATED partners. The fee is taken
  from the input before the swap. This module does not assume either number: it stores the echoed
  appFees of every quote in quotes_notes.json. amountOut is also NET of quote.withdrawFee (destination
  network fee); `fee_probe` re-verifies this each run (INTENTS vs DESTINATION_CHAIN recipient).
  So a keyless 1Click cost_bps = solver spread + echoed appFee (20 or 1 bp) + 1 pip + withdrawFee.
* across    : GET /api/swap/approval (tradeType=exactInput) -> expectedOutputAmount.
* relay     : POST /quote EXACT_INPUT -> details.currencyOut.amount.
* debridge  : GET /v1.0/dln/order/quote, prependOperatingExpenses=false ->
              estimation.dstChainTokenOut.recommendedAmount (fallback: .amount). deBridge ALSO charges a
              flat fee in the origin chain's native coin (`fixFee`, e.g. 0.001 ETH) paid on top of the
              input. It is valued with the same 1Click snapshot and SUBTRACTED from amount_out_usd so
              the cost is all-in. (This flat fee dominates at $100.)
* chainflip : GET /v2/quote -> highest egressAmount among the returned non-boost quotes (REGULAR / DCA).
* thorchain : GET /thorchain/quote/swap (amounts in 1e8) -> expected_amount_out.

NOT included for anyone: the user's own origin-chain gas for the deposit / approval transaction.

Caveats
-------
* Dry/indicative quotes, one point in time; nothing is executed. Re-run to add snapshots (append-safe:
  each run writes data/processed/quote_snapshots/quotes_<UTC>.csv; quotes.csv = all snapshots).
* A 1Click price that lags the market shifts cost_bps of ALL providers on that route by the same amount
  (it can make a cost negative). Provider-vs-provider gaps on the same route x size are unaffected.
* Placeholder addresses are public burn/genesis/all-zero-hash style addresses (ADDR below); one is a
  publicly labelled exchange cold wallet used only as BTC sender because Relay simulates BTC funding.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from moatmap.config import NEAR, ONECLICK, PROCESSED, RAW, RIVALS
from moatmap.coverage import CHAIN_MAP, http, now_iso

RAW_ROOT = RAW / "quotes"
RAW_COVERAGE = RAW / "coverage"
SNAP_DIR = PROCESSED / "quote_snapshots"
OUT_CSV = PROCESSED / "quotes.csv"
PANEL_CSV = PROCESSED / "quote_panel.csv"
NOTES_JSON = PROCESSED / "quotes_notes.json"
HEADER = ["fetched_at", "route", "origin_chain", "dest_chain", "origin_symbol", "dest_symbol", "notional_usd",
          "provider", "ok", "amount_in_usd", "amount_out_usd", "cost_bps", "error"]
NOTIONALS = [100, 1000, 10000]
SLEEP = 0.35

# Fixed panel: (origin_chain, dest_chain, origin_symbol, dest_symbol). Symbols are 1Click symbols.
# Two blocks so that asset type (1Click prices same-asset routes at 1 bp, cross-asset at 20 bp) can be
# separated from contestation: each block spans heavily contested -> uncontested routes.
PANEL = [
    # --- same asset on both sides (pure bridging) ---
    ("eth", "base", "ETH", "ETH"),
    ("eth", "arb", "ETH", "ETH"),
    ("arb", "base", "USDC", "USDC"),
    ("eth", "sol", "USDC", "USDC"),
    ("eth", "tron", "USDT", "USDT"),
    ("tron", "eth", "USDT", "USDT"),
    ("eth", "near", "USDC", "USDC"),
    ("near", "arb", "USDC", "USDC"),
    ("eth", "sui", "USDC", "USDC"),
    ("btc", "near", "BTC", "BTC"),
    # --- native <-> native across assets (cross-chain swap) ---
    ("sol", "eth", "SOL", "ETH"),
    ("arb", "sol", "ETH", "SOL"),
    ("btc", "eth", "BTC", "ETH"),
    ("eth", "btc", "ETH", "BTC"),
    ("btc", "sol", "BTC", "SOL"),
    ("eth", "xrp", "ETH", "XRP"),
    ("eth", "ton", "ETH", "GRAM"),
    ("zec", "eth", "ZEC", "ETH"),
    ("eth", "zec", "ETH", "ZEC"),
    ("doge", "btc", "DOGE", "BTC"),
    ("ltc", "eth", "LTC", "ETH"),
    ("near", "eth", "wNEAR", "ETH"),
    ("sol", "near", "SOL", "wNEAR"),
]

# ------------------------------------------------------------------------------- placeholder addresses
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58check(version: bytes, payload: bytes) -> str:
    b = version + payload
    b += hashlib.sha256(hashlib.sha256(b).digest()).digest()[:4]
    n, s = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return _B58[0] * (len(b) - len(b.lstrip(b"\x00"))) + s


def _ton_address(hash32: bytes, tag: int = 0x51) -> str:
    """TON user-friendly address (0x51 = non-bounceable), workchain 0, CRC16-XMODEM, base64url."""
    b = bytes([tag, 0]) + hash32
    crc = 0
    for x in b:
        crc ^= x << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return base64.urlsafe_b64encode(b + crc.to_bytes(2, "big")).decode()


EVM_ADDR = "0x1111111111111111111111111111111111111111"  # repeating-byte placeholder, nobody's key
# send = address used as user/refundTo on the origin chain; recv = recipient on the destination chain.
ADDR = {
    "evm":  {"send": EVM_ADDR, "recv": EVM_ADDR},
    # BTC sender: Relay simulates funding, so it must hold coins -> publicly labelled exchange cold wallet.
    # BTC recipient: the genesis-block address.
    "btc":  {"send": "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo", "recv": "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"},
    "sol":  {"send": "1nc1nerator11111111111111111111111111111111",
             "recv": "1nc1nerator11111111111111111111111111111111"},     # Solana incinerator
    # Tron: sender = all-zero-hash burn address (holds burned USDT, passes funding simulation);
    # recipient = 0x11-repeating hash (Relay refuses the zero address as recipient).
    "tron": {"send": _b58check(b"\x41", b"\x00" * 20), "recv": _b58check(b"\x41", b"\x11" * 20)},
    "near": {"send": "near", "recv": "near"},                          # the `near` registrar account
    "zec":  {"send": _b58check(b"\x1c\xb8", b"\x00" * 20), "recv": _b58check(b"\x1c\xb8", b"\x00" * 20)},
    "doge": {"send": _b58check(b"\x1e", b"\x00" * 20), "recv": _b58check(b"\x1e", b"\x00" * 20)},
    "ltc":  {"send": _b58check(b"\x30", b"\x00" * 20), "recv": _b58check(b"\x30", b"\x00" * 20)},
    "xrp":  {"send": "rrrrrrrrrrrrrrrrrrrrBZbvji", "recv": "rrrrrrrrrrrrrrrrrrrrBZbvji"},   # ACCOUNT_ONE
    "ton":  {"send": _ton_address(b"\x11" * 32), "recv": _ton_address(b"\x11" * 32)},
    "sui":  {"send": "0x" + "0" * 63 + "6", "recv": "0x" + "0" * 63 + "6"},               # Sui clock object id
}
ZERO_EVM = "0x0000000000000000000000000000000000000000"
SOL_NATIVE = "11111111111111111111111111111111"
# Native coin of each chain, used ONLY to value deBridge's flat fixFee with the 1Click snapshot.
NATIVE_SYMBOL = {"eth": "ETH", "arb": "ETH", "base": "ETH", "op": "ETH", "sol": "SOL", "tron": "TRX",
                 "bsc": "BNB", "pol": "POL", "avax": "AVAX"}


def addr(chain: str, side: str) -> str:
    return ADDR["evm" if "evm" in CHAIN_MAP.get(chain, {}) else chain][side]


def is_evm(chain: str) -> bool:
    return "evm" in CHAIN_MAP.get(chain, {})


# --------------------------------------------------------------------------------------- reference data
def load_reference() -> dict:
    """Rival listings saved by coverage.py (relay native currencies, deBridge ids, THORChain pools...)."""
    def rd(name):
        p = RAW_COVERAGE / f"{name}.json"
        if not p.exists():
            raise SystemExit(f"{p} missing - run `python -m moatmap.coverage` first.")
        return json.loads(p.read_text(encoding="utf-8"))
    relay = {c["id"]: c for c in rd("relay_chains")["chains"]}
    deb = {c["originalChainId"]: c["chainId"] for c in rd("debridge_supported_chains")["chains"]}
    cf: dict = {}
    for a in rd("chainflip_supported_assets")["result"]:
        cf.setdefault(a["chain"], set()).add(a["asset"])
    thor = {p["asset"] for p in rd("thorchain_pools") if p.get("status") == "Available"}
    return {"relay": relay, "debridge": deb, "chainflip": cf, "thorchain": thor}


def load_coverage() -> dict:
    p = PROCESSED / "coverage.csv"
    if not p.exists():
        raise SystemExit(f"{p} missing - run `python -m moatmap.coverage` first.")
    df = pd.read_csv(p)
    return {(r.chain, r.provider): int(r.supported) for r in df.itertuples()}


def fetch_tokens(run_dir, tag: str):
    status, parsed, text, err = http("GET", f"{ONECLICK}/tokens")
    if status != 200 or not isinstance(parsed, list):
        return None, f"token list fetch failed (status={status}, err={err})"
    (run_dir / f"prices_{tag}.json").write_text(text, encoding="utf-8")
    return parsed, None


def find_token(tokens: list, chain: str, symbol: str):
    m = [t for t in tokens if t["blockchain"] == chain and t["symbol"] == symbol]
    if len(m) != 1:
        return None, f"1Click lists {len(m)} tokens for {chain}/{symbol} (need exactly 1)"
    t = m[0]
    if not t.get("price") or float(t["price"]) <= 0:
        return None, f"1Click has no price for {chain}/{symbol}"
    return t, None


def usd(units: int, decimals: int, price) -> float:
    return float(Decimal(units) / (Decimal(10) ** decimals) * Decimal(str(price)))


# ------------------------------------------------------------------------------------ provider adapters
# Each adapter returns dict(ok, out_units, extra_cost_usd, error, request, status, response, meta).
def _res(ok, out_units=None, error="", request=None, status=None, response=None, extra_cost_usd=0.0, meta=None):
    return {"ok": ok, "out_units": out_units, "error": error, "request": request, "status": status,
            "response": response, "extra_cost_usd": extra_cost_usd, "meta": meta or {}}


def _errtext(status, parsed, text, err) -> str:
    if err:
        return f"network: {err}"
    msg = None
    if isinstance(parsed, dict):
        msg = parsed.get("message") or parsed.get("error") or parsed.get("errorMessage")
        if isinstance(msg, dict):
            msg = msg.get("message") or json.dumps(msg)
        code = parsed.get("errorCode") or parsed.get("errorId") or parsed.get("code")
        if code and msg:
            msg = f"{code}: {msg}"
    return f"HTTP {status}: {(msg or text or '')[:240]}".replace("\n", " ").strip()


def q_near(ctx) -> dict:
    o, d = ctx["otok"], ctx["dtok"]
    body = {"dry": True, "swapType": "EXACT_INPUT", "slippageTolerance": 100,
            "originAsset": o["assetId"], "depositType": "ORIGIN_CHAIN",
            "destinationAsset": d["assetId"], "amount": str(ctx["amount"]),
            "refundTo": addr(ctx["oc"], "send"), "refundType": "ORIGIN_CHAIN",
            "recipient": addr(ctx["dc"], "recv"), "recipientType": "DESTINATION_CHAIN",
            "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    url = f"{ONECLICK}/quote"
    status, parsed, text, err = http("POST", url, body=body, tries=3)
    req = {"method": "POST", "url": url, "body": body}
    if status in (200, 201) and isinstance(parsed, dict) and "quote" in parsed:
        q = parsed["quote"]
        if str(q.get("amountIn")) != str(ctx["amount"]):
            return _res(0, error=f"amountIn {q.get('amountIn')} != requested {ctx['amount']}", request=req,
                        status=status, response=parsed)
        meta = {"appFees": parsed.get("quoteRequest", {}).get("appFees"), "withdrawFee": q.get("withdrawFee"),
                "amountInUsd_api": q.get("amountInUsd"), "amountOutUsd_api": q.get("amountOutUsd")}
        return _res(1, int(q["amountOut"]), request=req, status=status, response=parsed, meta=meta)
    return _res(0, error=_errtext(status, parsed, text, err), request=req, status=status, response=parsed or text)


def _evm_or_native(chain: str, tok: dict):
    """Token identifier for Across / deBridge (address-keyed APIs)."""
    if tok.get("contractAddress"):
        return tok["contractAddress"]
    if is_evm(chain):
        return ZERO_EVM
    if chain == "sol":
        return SOL_NATIVE
    return None


def q_across(ctx) -> dict:
    m_o, m_d = CHAIN_MAP[ctx["oc"]], CHAIN_MAP[ctx["dc"]]
    ti, to = _evm_or_native(ctx["oc"], ctx["otok"]), _evm_or_native(ctx["dc"], ctx["dtok"])
    if ti is None or to is None:
        return _res(0, error="no Across token identifier for this asset")
    url = "https://app.across.to/api/swap/approval"
    params = {"tradeType": "exactInput", "inputToken": ti, "outputToken": to,
              "originChainId": m_o.get("across", m_o.get("evm")),
              "destinationChainId": m_d.get("across", m_d.get("evm")),
              "amount": str(ctx["amount"]), "depositor": addr(ctx["oc"], "send"),
              "recipient": addr(ctx["dc"], "recv")}
    status, parsed, text, err = http("GET", url, params=params, tries=3)
    req = {"method": "GET", "url": url, "params": params}
    if status == 200 and isinstance(parsed, dict) and parsed.get("expectedOutputAmount"):
        ot = parsed.get("outputToken") or {}
        if ot.get("decimals") is not None and int(ot["decimals"]) != int(ctx["dtok"]["decimals"]):
            return _res(0, error=f"decimals mismatch: Across {ot.get('decimals')} vs 1Click {ctx['dtok']['decimals']}",
                        request=req, status=status, response=parsed)
        if str(parsed.get("inputAmount")) != str(ctx["amount"]):
            return _res(0, error=f"inputAmount {parsed.get('inputAmount')} != requested", request=req,
                        status=status, response=parsed)
        return _res(1, int(parsed["expectedOutputAmount"]), request=req, status=status, response=parsed)
    return _res(0, error=_errtext(status, parsed, text, err), request=req, status=status, response=parsed or text)


def q_relay(ctx) -> dict:
    ref = ctx["ref"]["relay"]
    m_o, m_d = CHAIN_MAP[ctx["oc"]], CHAIN_MAP[ctx["dc"]]
    oid, did = m_o.get("relay", m_o.get("evm")), m_d.get("relay", m_d.get("evm"))

    def cur(cid, tok):
        return tok.get("contractAddress") or ((ref.get(cid) or {}).get("currency") or {}).get("address")
    oc_, dc_ = cur(oid, ctx["otok"]), cur(did, ctx["dtok"])
    if not oc_ or not dc_:
        return _res(0, error="no Relay currency identifier for this asset")
    url = "https://api.relay.link/quote"
    body = {"user": addr(ctx["oc"], "send"), "recipient": addr(ctx["dc"], "recv"), "originChainId": oid,
            "destinationChainId": did, "originCurrency": oc_, "destinationCurrency": dc_,
            "amount": str(ctx["amount"]), "tradeType": "EXACT_INPUT"}
    status, parsed, text, err = http("POST", url, body=body, tries=3)
    req = {"method": "POST", "url": url, "body": body}
    if status == 200 and isinstance(parsed, dict) and "details" in parsed:
        co, ci = parsed["details"].get("currencyOut") or {}, parsed["details"].get("currencyIn") or {}
        dec = (co.get("currency") or {}).get("decimals")
        if dec is not None and int(dec) != int(ctx["dtok"]["decimals"]):
            return _res(0, error=f"decimals mismatch: Relay {dec} vs 1Click {ctx['dtok']['decimals']}",
                        request=req, status=status, response=parsed)
        if str(ci.get("amount")) != str(ctx["amount"]):
            return _res(0, error=f"currencyIn.amount {ci.get('amount')} != requested", request=req,
                        status=status, response=parsed)
        return _res(1, int(co["amount"]), request=req, status=status, response=parsed)
    return _res(0, error=_errtext(status, parsed, text, err), request=req, status=status, response=parsed or text)


def q_debridge(ctx) -> dict:
    ref = ctx["ref"]["debridge"]
    m_o, m_d = CHAIN_MAP[ctx["oc"]], CHAIN_MAP[ctx["dc"]]
    sid, did = ref.get(m_o.get("debridge", m_o.get("evm"))), ref.get(m_d.get("debridge", m_d.get("evm")))
    ti, to = _evm_or_native(ctx["oc"], ctx["otok"]), _evm_or_native(ctx["dc"], ctx["dtok"])
    if sid is None or did is None or ti is None or to is None:
        return _res(0, error="no deBridge chain/token identifier for this asset")
    url = "https://dln.debridge.finance/v1.0/dln/order/quote"
    params = {"srcChainId": sid, "srcChainTokenIn": ti, "srcChainTokenInAmount": str(ctx["amount"]),
              "dstChainId": did, "dstChainTokenOut": to, "prependOperatingExpenses": "false"}
    status, parsed, text, err = http("GET", url, params=params, tries=3)
    req = {"method": "GET", "url": url, "params": params}
    if status == 200 and isinstance(parsed, dict) and "estimation" in parsed:
        e = parsed["estimation"]
        out = e["dstChainTokenOut"]
        if int(out.get("decimals", ctx["dtok"]["decimals"])) != int(ctx["dtok"]["decimals"]):
            return _res(0, error=f"decimals mismatch: deBridge {out.get('decimals')} vs 1Click "
                                 f"{ctx['dtok']['decimals']}", request=req, status=status, response=parsed)
        if str(e["srcChainTokenIn"].get("amount")) != str(ctx["amount"]):
            return _res(0, error=f"srcChainTokenIn.amount {e['srcChainTokenIn'].get('amount')} != requested",
                        request=req, status=status, response=parsed)
        units = int(out.get("recommendedAmount") or out["amount"])
        fix = int(parsed.get("fixFee") or 0)
        extra, meta = 0.0, {"fixFee": parsed.get("fixFee")}
        if fix:
            nsym = NATIVE_SYMBOL.get(ctx["oc"])
            ntok, nerr = find_token(ctx["tokens"], ctx["oc"], nsym) if nsym else (None, "no native symbol mapping")
            if ntok is None:
                return _res(0, error=f"cannot value deBridge fixFee ({fix} native units): {nerr}", request=req,
                            status=status, response=parsed)
            extra = usd(fix, int(ntok["decimals"]), ntok["price"])
            meta.update({"fixFee_usd": extra, "fixFee_native": nsym, "fixFee_price": ntok["price"]})
        return _res(1, units, request=req, status=status, response=parsed, extra_cost_usd=extra, meta=meta)
    return _res(0, error=_errtext(status, parsed, text, err), request=req, status=status, response=parsed or text)


def q_chainflip(ctx) -> dict:
    ref = ctx["ref"]["chainflip"]
    sc, dc = CHAIN_MAP[ctx["oc"]].get("chainflip"), CHAIN_MAP[ctx["dc"]].get("chainflip")
    sa, da = ctx["otok"]["symbol"].upper(), ctx["dtok"]["symbol"].upper()
    if sa not in ref.get(sc, ()) or da not in ref.get(dc, ()):
        return _res(0, error=f"asset not in cf_supported_assets ({sc}.{sa} -> {dc}.{da})")
    url = "https://chainflip-swap.chainflip.io/v2/quote"
    params = {"srcChain": sc, "srcAsset": sa, "destChain": dc, "destAsset": da, "amount": str(ctx["amount"])}
    status, parsed, text, err = http("GET", url, params=params, tries=3)
    req = {"method": "GET", "url": url, "params": params}
    if status == 200 and isinstance(parsed, list) and parsed:
        best = max(parsed, key=lambda x: int(x["egressAmount"]))
        if str(best.get("depositAmount")) != str(ctx["amount"]):
            return _res(0, error=f"depositAmount {best.get('depositAmount')} != requested", request=req,
                        status=status, response=parsed)
        return _res(1, int(best["egressAmount"]), request=req, status=status, response=parsed,
                    meta={"quote_type": best.get("type"), "lowLiquidityWarning": best.get("lowLiquidityWarning")})
    return _res(0, error=_errtext(status, parsed, text, err), request=req, status=status, response=parsed or text)


def _thor_asset(pools: set, chain: str, tok: dict):
    pfx = CHAIN_MAP[chain].get("thorchain")
    sym = tok["symbol"].upper()
    if tok.get("contractAddress"):
        a = f"{pfx}.{sym}-{tok['contractAddress'].upper()}"   # same contract as the 1Click token
    else:
        a = f"{pfx}.{sym}"
    return a if a in pools else None


def q_thorchain(ctx) -> dict:
    pools = ctx["ref"]["thorchain"]
    fa, ta = _thor_asset(pools, ctx["oc"], ctx["otok"]), _thor_asset(pools, ctx["dc"], ctx["dtok"])
    if not fa or not ta:
        return _res(0, error=f"no Available THORChain pool for {ctx['oc']}/{ctx['otok']['symbol']} or "
                             f"{ctx['dc']}/{ctx['dtok']['symbol']}")
    # THORChain uses 1e8 fixed point for every asset.
    amt8 = ctx["amount"] * 10 ** 8 // 10 ** int(ctx["otok"]["decimals"])
    url = "https://gateway.liquify.com/chain/thorchain_api/thorchain/quote/swap"
    params = {"from_asset": fa, "to_asset": ta, "amount": str(amt8)}
    status, parsed, text, err = http("GET", url, params=params, tries=3)
    req = {"method": "GET", "url": url, "params": params}
    if status == 200 and isinstance(parsed, dict) and parsed.get("expected_amount_out"):
        out8 = int(parsed["expected_amount_out"])
        dd = int(ctx["dtok"]["decimals"])
        units = out8 * 10 ** dd // 10 ** 8          # convert 1e8 -> token units (exact when dd >= 8)
        return _res(1, units, request=req, status=status, response=parsed,
                    meta={"amount_1e8": amt8, "out_1e8": out8})
    return _res(0, error=_errtext(status, parsed, text, err), request=req, status=status, response=parsed or text)


ADAPTERS = {NEAR: q_near, "across": q_across, "relay": q_relay, "debridge": q_debridge,
            "chainflip": q_chainflip, "thorchain": q_thorchain}


def fee_probe(tokens: list, run_dir) -> dict:
    """Evidence for the keyless fee composition: quote USDC eth->arb $1,000 twice, once delivered to the
    destination chain and once left inside Intents. Records echoed appFees, withdrawFee and both outputs."""
    o, _ = find_token(tokens, "eth", "USDC")
    d, _ = find_token(tokens, "arb", "USDC")
    out = {"description": "USDC eth>arb, 1000 USDC, keyless dry quote; DESTINATION_CHAIN vs INTENTS recipient"}
    if not o or not d:
        out["error"] = "tokens not found"
        return out
    for rtype, rec in (("DESTINATION_CHAIN", EVM_ADDR), ("INTENTS", "intents.near")):
        body = {"dry": True, "swapType": "EXACT_INPUT", "slippageTolerance": 100, "originAsset": o["assetId"],
                "depositType": "ORIGIN_CHAIN", "destinationAsset": d["assetId"], "amount": str(1000 * 10 ** 6),
                "refundTo": EVM_ADDR, "refundType": "ORIGIN_CHAIN", "recipient": rec, "recipientType": rtype,
                "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        status, parsed, text, err = http("POST", f"{ONECLICK}/quote", body=body, tries=3)
        (run_dir / f"fee_probe_{rtype}.json").write_text(
            json.dumps({"request": body, "status": status, "response": parsed or text, "error": err}, indent=1),
            encoding="utf-8")
        if isinstance(parsed, dict) and "quote" in parsed:
            out[rtype] = {"amountIn": parsed["quote"]["amountIn"], "amountOut": parsed["quote"]["amountOut"],
                          "withdrawFee": parsed["quote"].get("withdrawFee"),
                          "appFees": parsed.get("quoteRequest", {}).get("appFees")}
        else:
            out[rtype] = {"error": _errtext(status, parsed, text, err)}
        time.sleep(SLEEP)
    a, b = out.get("DESTINATION_CHAIN", {}), out.get("INTENTS", {})
    if "amountOut" in a and "amountOut" in b:
        out["intents_minus_destination_out"] = int(b["amountOut"]) - int(a["amountOut"])
        out["amountOut_is_net_of_withdrawFee"] = out["intents_minus_destination_out"] == int(a["withdrawFee"] or 0)
    return out


def rebuild_quotes_csv() -> int:
    files = sorted(SNAP_DIR.glob("quotes_*.csv"))
    frames = [pd.read_csv(f, dtype=str, keep_default_na=False) for f in files]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=HEADER)
    df[HEADER].to_csv(OUT_CSV, index=False)
    return len(df)


def main() -> None:
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RAW_ROOT / run_ts
    run_dir.mkdir(parents=True, exist_ok=True)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    cov, ref = load_coverage(), load_reference()

    # Panel + contestation from coverage.csv
    panel_rows = []
    for oc, dc, osym, dsym in PANEL:
        rivals = [p for p in RIVALS if cov.get((oc, p)) == 1 and cov.get((dc, p)) == 1]
        panel_rows.append({"route": f"{oc}>{dc}", "origin_chain": oc, "dest_chain": dc, "origin_symbol": osym,
                           "dest_symbol": dsym, "pair_type": "same_asset" if osym == dsym else "cross_asset",
                           "n_rivals": len(rivals), "rivals": "|".join(rivals)})
    pd.DataFrame(panel_rows).to_csv(PANEL_CSV, index=False)

    tokens, terr = fetch_tokens(run_dir, "start")
    if tokens is None:
        raise SystemExit(terr)
    probe = fee_probe(tokens, run_dir)

    rows, near_fee_log, price_log = [], [], []
    for pr in panel_rows:
        oc, dc, route = pr["origin_chain"], pr["dest_chain"], pr["route"]
        fresh, terr = fetch_tokens(run_dir, route.replace(">", "_"))   # per-route price snapshot
        if fresh is not None:
            tokens = fresh
        else:
            print(f"  !! {route}: {terr}; reusing previous price snapshot")
        otok, e1 = find_token(tokens, oc, pr["origin_symbol"])
        dtok, e2 = find_token(tokens, dc, pr["dest_symbol"])
        providers = [NEAR] + pr["rivals"].split("|") if pr["rivals"] else [NEAR]
        base = {"route": route, "origin_chain": oc, "dest_chain": dc, "origin_symbol": pr["origin_symbol"],
                "dest_symbol": pr["dest_symbol"]}
        if otok is None or dtok is None:
            for n in NOTIONALS:
                for p in providers:
                    rows.append({**base, "fetched_at": now_iso(), "notional_usd": n, "provider": p, "ok": 0,
                                 "amount_in_usd": "", "amount_out_usd": "", "cost_bps": "", "error": e1 or e2})
            continue
        price_log.append({"route": route, "snapshot_file": f"prices_{route.replace('>', '_')}.json",
                          "origin_assetId": otok["assetId"], "origin_price": otok["price"],
                          "origin_priceUpdatedAt": otok.get("priceUpdatedAt"), "origin_decimals": otok["decimals"],
                          "dest_assetId": dtok["assetId"], "dest_price": dtok["price"],
                          "dest_priceUpdatedAt": dtok.get("priceUpdatedAt"), "dest_decimals": dtok["decimals"]})
        for n in NOTIONALS:
            amount = int(math.ceil(Decimal(n) / Decimal(str(otok["price"])) * Decimal(10) ** int(otok["decimals"])))
            in_usd = usd(amount, int(otok["decimals"]), otok["price"])
            ctx = {"oc": oc, "dc": dc, "otok": otok, "dtok": dtok, "amount": amount, "ref": ref, "tokens": tokens}
            for p in providers:
                attempts = []
                for attempt in range(2):                 # one retry on failure, same rule for all providers
                    try:
                        res = ADAPTERS[p](ctx)
                    except Exception as ex:               # malformed response etc. -> honest failure
                        res = _res(0, error=f"adapter exception {type(ex).__name__}: {str(ex)[:200]}")
                    attempts.append(res)
                    time.sleep(SLEEP)
                    if res["ok"] or res["request"] is None:
                        break
                    time.sleep(2.0)
                fetched = now_iso()
                row = {**base, "fetched_at": fetched, "notional_usd": n, "provider": p, "ok": res["ok"],
                       "amount_in_usd": f"{in_usd:.6f}", "amount_out_usd": "", "cost_bps": "",
                       "error": res["error"]}
                if res["ok"]:
                    out_usd = usd(res["out_units"], int(dtok["decimals"]), dtok["price"]) - res["extra_cost_usd"]
                    row["amount_out_usd"] = f"{out_usd:.6f}"
                    row["cost_bps"] = f"{(in_usd - out_usd) / in_usd * 1e4:.3f}"
                    if p == NEAR:
                        near_fee_log.append({"route": route, "notional_usd": n, **res["meta"]})
                rows.append(row)
                fname = f"{route.replace('>', '_')}_{n}_{p}.json"
                (run_dir / fname).write_text(json.dumps(
                    {"fetched_at": fetched, "route": route, "notional_usd": n, "provider": p,
                     "amount_in_units": str(amount), "attempts": [
                         {k: a[k] for k in ("ok", "error", "request", "status", "response", "meta",
                                            "extra_cost_usd")} for a in attempts]}, indent=1), encoding="utf-8")
                print(f"  {route:<10} ${n:<6} {p:<13} ok={res['ok']} "
                      f"{('cost_bps=' + row['cost_bps']) if res['ok'] else res['error'][:90]}")

    snap = SNAP_DIR / f"quotes_{run_ts}.csv"
    with open(snap, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    pd.DataFrame(price_log).to_csv(SNAP_DIR / f"prices_{run_ts}.csv", index=False)
    total = rebuild_quotes_csv()

    fee_by_route: dict = {}
    for x in near_fee_log:
        fees = tuple(sorted(int(a.get("fee", 0)) for a in (x.get("appFees") or [])))
        fee_by_route.setdefault(x["route"], set()).add(fees)
    outliers = [r for r in rows if r["ok"] and (float(r["cost_bps"]) < 0 or float(r["cost_bps"]) > 1000)]
    notes = {
        "run": run_ts,
        "keyless_1click_fee": {
            "sent_credentials": "none (no JWT / X-API-Key)",
            "documented": "docs.near-intents.org/resources/fees (read 2026-11-14): without API key an additional "
                          "0.25% (25 bps) on every non-ANY_INPUT quote, plus the 0.0001% on-chain protocol fee; "
                          "with API key 20 bps, or 1 bp on stablecoin / same-asset multichain routes.",
            "observed": "quoteRequest.appFees echoed by each keyless quote in this run (bps, charged on input "
                        "before the swap); quote.amountOut is net of quote.withdrawFee - see fee_probe.",
            "echoed_appfee_bps_by_route": {k: sorted(list(v) for v in vs) for k, vs in fee_by_route.items()},
            "fee_probe": probe,
        },
        "cost_definition": "cost_bps=(amount_in_usd-amount_out_usd)/amount_in_usd*1e4; both legs priced with the "
                           "1Click /tokens snapshot fetched just before the route (prices_<run>.csv). deBridge "
                           "amount_out_usd is reduced by its flat native fixFee valued with the same snapshot. "
                           "User's origin-chain gas is excluded for every provider.",
        "near_quote_details": near_fee_log,
        "rows_cost_negative_or_over_1000bps": outliers,
        "counts": {p: {"ok": sum(1 for r in rows if r["provider"] == p and r["ok"]),
                       "failed": sum(1 for r in rows if r["provider"] == p and not r["ok"])}
                   for p in [NEAR] + RIVALS},
    }
    (SNAP_DIR / f"notes_{run_ts}.json").write_text(json.dumps(notes, indent=1), encoding="utf-8")
    NOTES_JSON.write_text(json.dumps(notes, indent=1), encoding="utf-8")

    print(f"\nSnapshot {snap.name}: {len(rows)} rows; quotes.csv now {total} rows "
          f"({len(list(SNAP_DIR.glob('quotes_*.csv')))} snapshot(s))")
    for p, c in notes["counts"].items():
        print(f"  {p:<13} ok={c['ok']:<4} failed={c['failed']}")
    print(f"  outliers (cost<0 or >1000 bps): {len(outliers)}")
    print("  keyless 1Click appFees by route:", notes["keyless_1click_fee"]["echoed_appfee_bps_by_route"])
    print("  fee probe:", {k: v for k, v in probe.items() if k != "description"})


if __name__ == "__main__":
    main()
