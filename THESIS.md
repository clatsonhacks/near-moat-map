# NEAR's long-term case rests on Intents: what the evidence supports

NEAR hackathon · Investment Thesis track · data pulled 14 November 2026

## The thesis

**NEAR Intents is the one part of NEAR that already behaves like a business: a cross-chain swap network running at about $50B of annual volume, tripled in a year, whose captured revenue has bought back NEAR since February 2026. That revenue is still too small to carry NEAR's valuation, and its competitive protection is narrower than it looks. NEAR is a long-term hold only if Intents grows roughly 5× from here and keeps its share of fees; the evidence today supports the growth and is mixed on the durability.**

This document makes that case in two parts and reports the evidence against it alongside the evidence for it. Every figure is rebuilt by `python build.py`.

- **Part A: how much is Intents worth to a NEAR holder?** A re-runnable model from fees, revenue, supply and price.
- **Part B: will it last?** The Moat Map: a test of whether Intents' revenue comes from routes rivals cannot serve.

## Part A. What Intents is worth to a NEAR holder

All figures are the 90 days to 12 November 2026, annualised, from DefiLlama's NEAR Intents series, with supply from NEAR BigQuery and price from the 1Click token list (`moatmap/model.py`, dashboard section A).

| Measure | Now | Same 90 days a year earlier |
|---|---|---|
| Volume, annualised | $49.7B | $15.9B (3.1× growth) |
| Fees, annualised | $77.5M | $24.7M (3.1× growth) |
| Revenue captured by NEAR, annualised | $21.1M | $1.7M |
| NEAR's share of fees | 27% | 7% |
| Fees as a share of volume | 15.6 bps | 15.5 bps |

**For the case**

- **Volume and fees tripled in a year, at a steady price.** Fees held at about 15.6 bps of volume while volume grew 3.1×, so growth was not bought by cutting price.
- **NEAR is keeping more of each dollar.** Its share of fees rose from 7% to 27%, so captured revenue grew about 12× while volume grew 3×.
- **Momentum is recent, not historic.** Against the previous 90 days, volume is up 85%, fees 115% and captured revenue 199%.
- **The revenue reaches holders.** Since 23 February 2026 it is used to buy back NEAR.
- **Intents is central to the chain.** `intents.near` is the third most-called contract on NEAR, with 10.2% of all contract calls over 16 October to 13 November (NEAR BigQuery).

**Against the case**

- **The yield is small.** $21.1M a year against a market value of $4.41B (1.310B NEAR at $3.37) is a buyback yield of 0.48%. NEAR trades at 209× Intents revenue and 57× total Intents fees.
- **Issuance outruns it.** Total supply grew at an annualised 2.45% over 16 October to 13 November, about $108M a year at today's price. The buyback offsets 19% of that.
- **The required growth is large.** At today's take of 4.2 bps of volume, volume must grow 2.1× for a 1% yield, 5.1× to offset net issuance and 10.5× for a 5% yield. Even if NEAR kept every fee and partners kept nothing, today's volume gives 1.76%.
- **Growth is lumpy.** Monthly volume peaked at $3.66B in November 2025, fell to $1.90B by April 2026, and only passed the old peak in September 2026 ($4.80B). A 90-day run rate taken at a high point overstates the trend.
- **Intents does not grow NEAR's user base.** About 259 accounts a day sign transactions reaching `intents.near`, against 65,143 active accounts on the network. The flow arrives through a few solver and relayer accounts, so Intents volume adds little to NEAR as a platform beyond the fee stream.

**Scenario table: buyback yield on market value** (run-rate volume × multiple × take; multiples are assumptions, the 1× / today's-take cell is measured)

| Annual volume | Half of today's take (2.1 bps) | Today's take (4.2 bps) | Double (8.5 bps) | All fees to NEAR (15.6 bps) |
|---|---|---|---|---|
| 0.5× ($24.8B) | 0.12% | 0.24% | 0.48% | 0.88% |
| 1× ($49.7B) | 0.24% | **0.48%** | 0.96% | 1.76% |
| 2× ($99.3B) | 0.48% | 0.96% | 1.91% | 3.51% |
| 5× ($248B) | 1.19% | 2.39% | 4.78% | 8.78% |
| 10× ($497B) | 2.39% | 4.78% | 9.55% | 17.55% |

## Part B. Will it last? The Moat Map

Revenue earns a long-term multiple only if rivals cannot take it. We counted how many of five rival bridges (Across, Relay, deBridge, Chainflip, THORChain) can serve each route, and tested two claims with thresholds fixed before any data was pulled. A route is a directional chain pair such as `btc>eth`; a rival "can serve" it when its own live API lists both chains.

| Claim | Threshold | Result | Verdict |
|---|---|---|---|
| 1. Share of fee revenue from routes with at most two rivals | ≥ 50% | 47.2% of fees paid to NEAR's own wallets; 33.3% of all app fees; 31.6% of volume | Not met, narrowly on one measure |
| 2. Price premium on those routes, like for like | ≥ 1.5× | 0.71× to 1.51× in live quotes; met in 1 of 6 comparisons | Not met |

**What this means for the thesis.** Intents' revenue is not protected by exclusivity. It is protected by price on crowded stablecoin and same-token routes, where NEAR is the cheapest or near-cheapest venue, and by one concentrated niche, Zcash, where THORChain is the only listed rival. That is a weaker moat than the long-term case needs, and it is the main reason the thesis is conditional.

### Evidence that the moat exists

- **Coverage is genuinely wide.** NEAR Intents serves 39 chains. 27 of them are listed by at most two rivals and 14 by none (dashboard panel 1).
- **NEAR's own fee income leans toward thin routes.** Over 15 October to 13 November 2026 (30 days, 407,062 cross-chain swaps, $985.2M volume), routes with at most two rivals carried 31.6% of volume but 47.2% of the $383.0K in fees paid directly to NEAR's revenue wallets (panel 2).
- **Fees actually charged are higher on thin routes.** On executed swaps, all app fees ran at 51.5 bps of volume on thin routes against 20.1 bps on crowded ones (2.57×). NEAR's own slice was 5.8 bps against 2.3 bps (2.48×) (panel 5, lower table).
- **Rivals often lose on price where they do compete.** In live $1,000 quotes NEAR Intents beat the cheapest rival on `zec>eth` (19.7 against 26.8 bps), `eth>zec`, `ltc>eth`, `eth>ton`, `eth>xrp` and `eth>tron` (panel 4).

### Evidence that it is narrow

- **The 50% line is missed on every measure**, and by a wide margin on two of three: 33.3% of all app fees ($4.81M) and 31.6% of volume.
- **The share is unstable.** By ISO week, the thin-route share of NEAR-wallet fees was 39%, 62%, 59%, 23% and 30% (the first and last weeks are partial). The most recent two weeks are the lowest. A 30-day figure of 47.2% hides a falling trend.
- **It is mostly Zcash.** Routes touching Zcash account for 35.6% of NEAR-wallet fees and 20.4% of volume. Excluding Zcash routes, the thin-route share of NEAR-wallet fees falls from 47.2% to 18%. Routes touching NEAR itself, the routes no rival serves at all, bring only about 9% of NEAR-wallet fees.
- **The largest single route is contested.** `btc>tron` is the top earner (12.5% of NEAR-wallet fees) and three rivals list both chains. Of the eight largest routes, only three are thin, and all three are Zcash routes.
- **Like for like, there is no premium.** Comparing the same asset pair at the same size across two quote snapshots:

  | Pair type | Size | Thin routes (median) | Crowded routes (median) | Ratio |
  |---|---|---|---|---|
  | Same token both sides | $100 | 1.9 bps (4 routes) | 2.4 bps (5 routes) | 0.81× |
  | Same token both sides | $1,000 | 1.5 bps | 2.0 bps | 0.71× |
  | Same token both sides | $10,000 | 1.6 bps | 2.0 bps | 0.80× |
  | Different tokens | $100 | 40.3 bps (8 routes) | 27.4 bps (2 routes) | 1.47× |
  | Different tokens | $1,000 | 36.2 bps | 31.4 bps | 1.15× |
  | Different tokens | $10,000 | 42.9 bps | 28.5 bps | 1.51× |

  The single pass (1.51×) rests on two crowded routes. NEAR's published fee schedule is flat by pair type (1 bp on stablecoin and same-asset routes, 20 bps otherwise), which is what the quotes show: price follows the kind of swap, not the number of rivals.
- **The higher realised fee on thin routes is mostly mix, not pricing power.** Crowded routes are dominated by stablecoin transfers (`eth>tron`, `tron>eth`) that pay the 1 bp tier. And 92% of app fees go to distribution partners (Ledger, Trust Wallet, Tangem and others via SwapKit), who set their own markups; NEAR does not capture that.
- **Where a strong rival exists, NEAR is not cheapest.** Chainflip quoted below NEAR on `btc>eth`, `btc>sol`, `eth>btc`, `arb>sol` and `sol>eth` at $1,000.

## Network context (NEAR BigQuery)

From `bigquery-public-data.crypto_near_mainnet_us`, 16 October to 13 November 2026, every query filtered on `block_date`:

- NEAR averaged 670,075 transactions and 65,143 active accounts (distinct signers) per day.
- `intents.near` was the third most-called contract on the network with 10.2% of all contract calls, behind `token.sweat` and `game.hot.tg`.
- Only about 259 distinct accounts per day signed transactions reaching `intents.near`. Usage is heavy but comes through a small number of solver and relayer accounts, so Intents volume does not show up as NEAR active-account growth.

## What would change this conclusion

**Toward a stronger case**

- Intents volume sustains 2× today's run rate (about $100B a year) for two consecutive quarters with fees still near 15 bps.
- NEAR's share of fees keeps rising from 27% toward half, which doubles the yield at any volume.
- The buyback offsets more than half of net issuance, against 19% now.
- The thin-route share of NEAR-wallet fees stays at or above 50% over 90 days, or holds with Zcash excluded (it is 18% excluding Zcash today).

**Toward abandoning it**

- Volume falls back to the first-half 2026 level (about $2B a month) for a quarter, which would show the September to October surge was an episode, not a trend.
- Fees as a share of volume fall well below 15 bps, meaning growth is being bought with price.
- A rival undercuts NEAR on the stablecoin corridors (`eth>tron`, `tron>eth`), which carry 56% of crowded-route volume, or THORChain or Chainflip close the gap on Zcash.
- The thin-route share of NEAR-wallet fees stays below 35%, as in the last two weeks, for another month.

## What the data cannot say

- **Captured revenue per route cannot be computed.** DefiLlama reports $1.81M swept into NEAR's revenue wallets over the same 30 days. The Explorer API attributes only $402.7K in fees to those wallets directly. The remaining roughly $1.4M (partner revenue shares, quote improvement, private agreements) is not attributable to a route from any public source. The 47.2% figure describes the directly attributable slice only.
- **The Explorer API shows about 29% of reported volume.** It returned $1.09B of successful swaps against DefiLlama's $3.80B for the same days, though fees nearly reconcile ($5.27M against $5.83M). We have not established what the missing volume is; it may be flow that does not pass through the 1Click route the Explorer indexes. Route shares here describe the visible 29%.
- **No confidential-shard flow appeared.** The API has a `CONFIDENTIAL_INTENTS` type, but no swap in the pull carried it. Whether that flow is absent or simply not exposed to this key is unknown, so treat all figures as lower bounds.
- **Contestation is by chain, not by token.** A rival listing both chains may still not support a given token, so the map overstates competition if anything. Bitcoin and Tron are not uncontested: Chainflip, THORChain and Relay serve them.
- **Quotes are two snapshots taken 20 minutes apart**, for placeholder addresses, without executing. Keyless 1Click quotes echoed a 1 bp or 20 bp app fee; the docs describe a higher keyless surcharge, which we did not observe. Three rival quotes came out negative (Chainflip and THORChain beating the reference price), which reflects price movement between the snapshot and the quote, not free money.
- **The model is arithmetic, not a forecast.** It says what yield a given volume and take would produce; it does not say how likely that volume is. Net issuance is annualised from 29 days of supply data and will move with staking rewards and burns. The model values Intents only: it assigns nothing to NEAR's other activity, so the multiples describe what Intents alone would have to justify.
- **Revenue arrives in sweeps.** DefiLlama counts revenue when NEAR/wNEAR lands in the revenue wallets, so short windows are lumpy; that is why the model uses 90 days.
- **Not computed:** NEAR/wNEAR inflows to the revenue wallets from BigQuery. The native-transfer query would scan 88 GB and was refused by the 20 GB guard; the wNEAR query ran and returned no rows, which we have not verified as a true zero.
- **DefiLlama's last day (13 November) is incomplete for fees**, so its 30-day fee total is slightly understated.

## Reproduce it

```
pip install -r requirements.txt
python build.py
```

Sources, credentials and costs are in [README.md](README.md). Every chart, with the query behind it, is in [dashboard/index.html](dashboard/index.html). Every number above is read from `data/processed/results.json` or the CSVs beside it.
