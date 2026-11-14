"""Chain-coverage matrix: which of the five rival intent bridges serve each chain NEAR Intents serves.

Run:  python -m moatmap.coverage

What it does
------------
1. Fetches the NEAR 1Click token list; the distinct `blockchain` codes are "the chains NEAR Intents serves".
2. Fetches each rival's OWN live chain / asset listing (no API keys):
     across    GET https://app.across.to/api/available-routes   (originChainId / destinationChainId)
               GET https://app.across.to/api/swap/chains        (chainId)      -> union of both
     relay     GET https://api.relay.link/chains                (chains[].id, disabled == false)
     debridge  GET https://dln.debridge.finance/v1.0/supported-chains-info (chains[].originalChainId)
     chainflip POST https://mainnet-rpc.chainflip.io  JSON-RPC cf_supported_assets (result[].chain)
     thorchain GET https://gateway.liquify.com/chain/thorchain_api/thorchain/pools
               (chain prefix of every pool with status == "Available")
3. Saves every raw response untouched under data/raw/coverage/ (+ a _meta.json with url, status, time).
4. Writes data/processed/coverage.csv  (chain,provider,supported,evidence,fetched_at), one row per
   (chain, provider) for every 1Click chain x (five rivals + near_intents).

`supported` is 1 ONLY when the rival's live response contains the identifier that CHAIN_MAP assigns to
that chain. If CHAIN_MAP has no identifier for a (chain, rival) pair the row is 0 and `evidence` says why.
Nothing is inferred from documentation or memory.

Caveats (disclose these)
------------------------
* Chain-level support approximates route support. A rival listing both chains does not prove it can swap
  every asset pair between them (quotes.py tests the actual pairs on a panel and records failures).
* Listing != liquidity. A chain can be listed and still be paused or have no solver inventory.
* Across: `available-routes` is the legacy same-asset bridge list; `swap/chains` is the newer swap API list
  (it adds e.g. Solana, HyperCore). A chain counts if it is in either; the evidence string says which.
* THORChain: only pools with status "Available" count ("Staged" pools cannot be swapped).
* deBridge lists HyperEVM (999); 1Click's `hypercore` is Hyperliquid's HyperCore, a different venue, so it
  is NOT counted for deBridge. Across and Relay list HyperCore/Hyperliquid under id 1337 and are counted.
"""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone

import requests

from moatmap.config import NEAR, ONECLICK, PROCESSED, RAW, RIVALS

UA = {"User-Agent": "moatmap-research/0.1", "Accept": "application/json"}
RAW_DIR = RAW / "coverage"
OUT_CSV = PROCESSED / "coverage.csv"

URLS = {
    "oneclick_tokens": f"{ONECLICK}/tokens",
    "across_available_routes": "https://app.across.to/api/available-routes",
    "across_swap_chains": "https://app.across.to/api/swap/chains",
    "relay_chains": "https://api.relay.link/chains",
    "debridge_supported_chains": "https://dln.debridge.finance/v1.0/supported-chains-info",
    "chainflip_supported_assets": "https://mainnet-rpc.chainflip.io",  # JSON-RPC cf_supported_assets
    "thorchain_pools": "https://gateway.liquify.com/chain/thorchain_api/thorchain/pools",
}
CHAINFLIP_RPC_BODY = {"jsonrpc": "2.0", "id": 1, "method": "cf_supported_assets", "params": []}

# ---------------------------------------------------------------------------------------------------
# Explicit mapping: 1Click `blockchain` code -> the identifier each rival uses for the same chain.
#   evm       : EVM chain id. Across, Relay and deBridge (field originalChainId) all key EVM chains by it.
#   across    : non-EVM chain id used by Across            (overrides evm)
#   relay     : non-EVM chain id used by Relay             (overrides evm)
#   debridge  : deBridge `originalChainId` for non-EVM     (overrides evm)
#   chainflip : chain name in cf_supported_assets
#   thorchain : chain prefix of THORChain pool assets ("ETH.ETH" -> "ETH")
#   note      : why a chain has no identifiers / how confident the mapping is
# A missing key means "this rival has no identifier for that chain that we can map confidently" -> 0.
# ---------------------------------------------------------------------------------------------------
CHAIN_MAP: dict[str, dict] = {
    # --- EVM chains (chain ids are the canonical EIP-155 ids) ---
    "eth":     {"evm": 1, "chainflip": "Ethereum", "thorchain": "ETH"},
    "arb":     {"evm": 42161, "chainflip": "Arbitrum"},
    "base":    {"evm": 8453, "thorchain": "BASE"},
    "op":      {"evm": 10},
    "bsc":     {"evm": 56, "chainflip": "Bsc", "thorchain": "BSC"},
    "pol":     {"evm": 137},
    "avax":    {"evm": 43114, "thorchain": "AVAX"},
    "gnosis":  {"evm": 100},
    "bera":    {"evm": 80094},
    "monad":   {"evm": 143},
    "plasma":  {"evm": 9745},
    "scroll":  {"evm": 534352},
    "xlayer":  {"evm": 196},
    # abs = Abstract (2741): 1Click lists only native ETH there, so the id is by name, not address-checked.
    "abs":     {"evm": 2741, "note": "mapped by name to Abstract (chain id 2741)"},
    # adi = ADI Chain; id 36900 is taken from the 1Click assetId 'nep245:v2_1.omni.hot.tg:36900_...'.
    "adi":     {"evm": 36900, "note": "chain id 36900 read from the 1Click assetId"},
    # hood = Robinhood Chain (4663): 1Click's USDG contract 0x5fc5360d... equals Relay's USDG on 4663.
    "hood":    {"evm": 4663, "note": "Robinhood Chain; token contract matches Relay chain 4663"},
    # hypercore = Hyperliquid HyperCore: 1Click's USDC 0x6d1e7cde... equals Relay's USDC on chain 1337.
    "hypercore": {"across": 1337, "relay": 1337,
                  "note": "HyperCore (id 1337 at Across/Relay); deBridge's HyperEVM (999) is a different venue"},
    # --- non-EVM chains ---
    "btc":     {"relay": 8253038, "chainflip": "Bitcoin", "thorchain": "BTC"},
    "sol":     {"across": 34268394551451, "relay": 792703809, "debridge": 7565164,
                "chainflip": "Solana", "thorchain": "SOL"},
    "tron":    {"across": 728126428, "relay": 728126428, "debridge": 728126428,
                "chainflip": "Tron", "thorchain": "TRON"},
    "ton":     {"relay": 224235520},
    "xrp":     {"relay": 537724, "thorchain": "XRP"},
    "doge":    {"thorchain": "DOGE"},
    "ltc":     {"thorchain": "LTC"},
    "bch":     {"thorchain": "BCH"},
    "zec":     {"thorchain": "ZEC"},
    # --- chains for which no rival publishes an identifier we can map (verified against the live lists
    #     by name search at run time: see `unmatched_name_hits` printed by main()) ---
    "near":     {"note": "no rival lists NEAR Protocol"},
    "aleo":     {"note": "no rival lists Aleo"},
    "aptos":    {"note": "no rival lists Aptos"},
    "cardano":  {"note": "no rival lists Cardano"},
    "dash":     {"note": "no rival lists Dash"},
    "fogo":     {"note": "no rival lists Fogo"},
    "miden":    {"note": "no rival lists Miden"},
    "movement": {"note": "no rival lists Movement"},
    "prl":      {"note": "chain code 'prl' could not be mapped to any rival chain"},
    "qtc":      {"note": "chain code 'qtc' could not be mapped to any rival chain"},
    "starknet": {"note": "no rival lists Starknet"},
    "stellar":  {"note": "no rival lists Stellar"},
    "sui":      {"note": "no rival lists Sui"},
}
# Words used only for a safety-net name search (warns if a rival lists a chain we mapped to nothing).
NAME_HINTS = {
    "near": ["near"], "aleo": ["aleo"], "aptos": ["aptos"], "cardano": ["cardano", "ada"], "dash": ["dash"],
    "fogo": ["fogo"], "miden": ["miden"], "movement": ["movement"], "starknet": ["starknet"],
    "stellar": ["stellar"], "sui": ["sui"], "ton": ["ton"], "doge": ["doge"], "ltc": ["litecoin", "ltc"],
    "bch": ["bitcoin cash", "bch"], "zec": ["zcash", "zec"], "xrp": ["xrp", "ripple"], "prl": ["prl"],
    "qtc": ["qtc"], "adi": ["adi"], "abs": ["abstract"], "gnosis": ["gnosis"], "scroll": ["scroll"],
    "xlayer": ["x layer", "xlayer"], "bera": ["bera"],
}


# ------------------------------------------------------------------ shared HTTP helper (quotes.py reuses)
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http(method: str, url: str, *, params=None, body=None, tries: int = 4, timeout: int = 40,
         retry_statuses=(429, 500, 502, 503, 504)):
    """Polite request with retries + exponential backoff.

    Returns (status_code | None, parsed_json | None, text, error | None). Never raises on network errors.
    A 4xx answer (other than 429) is a real answer from the API and is returned without retrying.
    """
    last_err = None
    for i in range(tries):
        try:
            r = requests.request(method, url, params=params, json=body, headers=UA, timeout=timeout)
            if r.status_code in retry_statuses and i < tries - 1:
                last_err = f"HTTP {r.status_code}"
                time.sleep(1.5 * (2 ** i))
                continue
            try:
                parsed = r.json()
            except ValueError:
                parsed = None
            return r.status_code, parsed, r.text, None
        except requests.RequestException as e:  # connection reset, timeout, DNS...
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
            time.sleep(1.5 * (2 ** i))
    return None, None, "", last_err


def fetch_sources() -> dict:
    """Fetch every listing, save raw bodies, return parsed JSON keyed by source name."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out, meta = {}, {}
    for name, url in URLS.items():
        if name == "chainflip_supported_assets":
            status, parsed, text, err = http("POST", url, body=CHAINFLIP_RPC_BODY)
        else:
            status, parsed, text, err = http("GET", url)
        fetched = now_iso()
        ok = status == 200 and parsed is not None
        meta[name] = {"url": url, "status": status, "ok": ok, "error": err, "fetched_at": fetched,
                      "request_body": CHAINFLIP_RPC_BODY if name == "chainflip_supported_assets" else None}
        if ok:
            (RAW_DIR / f"{name}.json").write_text(text, encoding="utf-8")  # untouched body
            out[name] = parsed
        else:
            out[name] = None
            print(f"  !! {name}: fetch failed (status={status}, err={err})")
        time.sleep(0.4)
    (RAW_DIR / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"data": out, "meta": meta}


def provider_listings(data: dict) -> dict:
    """Reduce each rival's raw response to {identifier: human label}. None = listing unavailable."""
    L: dict[str, dict | None] = {}

    # Across: union of legacy route list and swap-API chain list; remember which endpoint proves each id.
    ar, sc = data.get("across_available_routes"), data.get("across_swap_chains")
    if ar is None and sc is None:
        L["across"] = None
    else:
        ids: dict = {}
        for r in ar or []:
            for k in ("originChainId", "destinationChainId"):
                ids.setdefault(r[k], set()).add("available-routes")
        for c in sc or []:
            ids.setdefault(c["chainId"], set()).add(f"swap/chains name={c.get('name')}")
        L["across"] = {k: "; ".join(sorted(v)) for k, v in ids.items()}

    rc = data.get("relay_chains")
    L["relay"] = None if rc is None else {
        c["id"]: f"name={c.get('name')}" for c in rc.get("chains", []) if not c.get("disabled")}

    dc = data.get("debridge_supported_chains")
    L["debridge"] = None if dc is None else {
        c["originalChainId"]: f"chainName={c.get('chainName')}, chainId={c.get('chainId')}"
        for c in dc.get("chains", [])}

    cf = data.get("chainflip_supported_assets")
    if cf is None or "result" not in cf:
        L["chainflip"] = None
    else:
        d: dict = {}
        for a in cf["result"]:
            d.setdefault(a["chain"], []).append(a["asset"])
        L["chainflip"] = {k: "assets=" + "/".join(v) for k, v in d.items()}

    tp = data.get("thorchain_pools")
    if tp is None:
        L["thorchain"] = None
    else:
        d = {}
        for p in tp:
            if p.get("status") == "Available":
                d.setdefault(p["asset"].split(".")[0], []).append(p["asset"].split("-")[0])
        L["thorchain"] = {k: "Available pools=" + "/".join(v[:4]) + ("..." if len(v) > 4 else "")
                          for k, v in d.items()}
    return L


EVIDENCE_URL = {
    "across": "https://app.across.to/api/{available-routes|swap/chains}",
    "relay": URLS["relay_chains"] + " chains[].id",
    "debridge": URLS["debridge_supported_chains"] + " chains[].originalChainId",
    "chainflip": URLS["chainflip_supported_assets"] + " cf_supported_assets result[].chain",
    "thorchain": URLS["thorchain_pools"] + " asset prefix",
}


def rival_identifier(chain: str, provider: str):
    m = CHAIN_MAP.get(chain, {})
    if provider in ("across", "relay", "debridge"):
        return m.get(provider, m.get("evm"))
    return m.get(provider)


def build_rows(sources: dict) -> list[dict]:
    data, meta = sources["data"], sources["meta"]
    tokens = data["oneclick_tokens"]
    if tokens is None:
        raise SystemExit("1Click token list unavailable - cannot build coverage (nothing written).")
    chains: dict[str, int] = {}
    for t in tokens:
        chains[t["blockchain"]] = chains.get(t["blockchain"], 0) + 1
    listings = provider_listings(data)
    tok_time = meta["oneclick_tokens"]["fetched_at"]
    src_time = {"across": meta["across_swap_chains"]["fetched_at"], "relay": meta["relay_chains"]["fetched_at"],
                "debridge": meta["debridge_supported_chains"]["fetched_at"],
                "chainflip": meta["chainflip_supported_assets"]["fetched_at"],
                "thorchain": meta["thorchain_pools"]["fetched_at"]}
    rows = []
    for chain in sorted(chains):
        rows.append({"chain": chain, "provider": NEAR, "supported": 1,
                     "evidence": f"{URLS['oneclick_tokens']} blockchain={chain} ({chains[chain]} tokens)",
                     "fetched_at": tok_time})
        note = CHAIN_MAP.get(chain, {}).get("note")
        for p in RIVALS:
            listing = listings[p]
            ident = rival_identifier(chain, p)
            if chain not in CHAIN_MAP:
                sup, ev = 0, (f"1Click chain code '{chain}' is not in moatmap.coverage.CHAIN_MAP "
                              f"(new chain, unmapped) - marked 0, not verified")
            elif listing is None:
                sup, ev = 0, f"listing fetch FAILED for {p} ({EVIDENCE_URL[p]}) - support unknown, marked 0"
            elif ident is None:
                sup, ev = 0, f"no {p} identifier maps to this chain" + (f" ({note})" if note else "")
            elif ident in listing:
                sup, ev = 1, f"{EVIDENCE_URL[p]} = {ident} [{listing[ident]}]" + (f" ({note})" if note else "")
            else:
                sup, ev = 0, f"{ident} not present in {EVIDENCE_URL[p]}" + (f" ({note})" if note else "")
            rows.append({"chain": chain, "provider": p, "supported": sup, "evidence": ev,
                         "fetched_at": src_time[p]})
    return rows


def unmatched_name_hits(data: dict, rows: list[dict]) -> list[str]:
    """Safety net: for every (chain, rival) marked 0, search the rival's raw chain NAMES for the chain's
    name. A hit means CHAIN_MAP may be missing an identifier and a human should look."""
    names: dict[str, list[str]] = {p: [] for p in RIVALS}
    for c in (data.get("across_swap_chains") or []):
        names["across"].append(str(c.get("name", "")))
    for c in ((data.get("relay_chains") or {}).get("chains") or []):
        names["relay"] += [str(c.get("name", "")), str(c.get("displayName", ""))]
    for c in ((data.get("debridge_supported_chains") or {}).get("chains") or []):
        names["debridge"].append(str(c.get("chainName", "")))
    for a in ((data.get("chainflip_supported_assets") or {}).get("result") or []):
        names["chainflip"].append(str(a.get("chain", "")))
    for p_ in (data.get("thorchain_pools") or []):
        names["thorchain"].append(p_["asset"].split(".")[0])
    hits = []
    for r in rows:
        if r["provider"] in RIVALS and r["supported"] == 0:
            for w in NAME_HINTS.get(r["chain"], [r["chain"]]):
                for n in set(names[r["provider"]]):
                    nl = n.lower()
                    if nl == w or nl.startswith(w + " ") or nl.startswith(w + "-"):
                        hits.append(f"{r['chain']} ~ {r['provider']} chain named '{n}'")
    return sorted(set(hits))


def main() -> None:
    print("Fetching chain listings ...")
    sources = fetch_sources()
    rows = build_rows(sources)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["chain", "provider", "supported", "evidence", "fetched_at"])
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {OUT_CSV} ({len(rows)} rows)")
    chains = sorted({r["chain"] for r in rows})
    sup = {(r["chain"], r["provider"]): r["supported"] for r in rows}
    print(f"\n{'chain':<10}" + "".join(f"{p[:9]:>10}" for p in RIVALS) + f"{'n_rivals':>10}")
    for c in chains:
        n = sum(sup[(c, p)] for p in RIVALS)
        print(f"{c:<10}" + "".join(f"{sup[(c, p)]:>10}" for p in RIVALS) + f"{n:>10}")
    for p in RIVALS:
        print(f"{p}: {sum(sup[(c, p)] for c in chains)} of {len(chains)} chains")
    hits = unmatched_name_hits(sources["data"], rows)
    print("\nName-search safety net (rows marked 0 whose chain name appears in the rival list):",
          hits if hits else "none")


if __name__ == "__main__":
    main()
