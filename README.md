# NEAR's long-term case rests on Intents

An evidence-first investment thesis on NEAR (NEAR hackathon, Investment Thesis track).

**Thesis:** NEAR Intents is a real, fast-growing business whose revenue buys back NEAR, but that revenue is 0.48% of NEAR's market value and its competitive protection is narrow. NEAR is a long-term hold only if Intents grows about 5× and keeps its share of fees.

- **Part A, the model:** run rate, buyback yield, net issuance and a scenario grid, from DefiLlama, NEAR BigQuery and the 1Click price list.
- **Part B, the Moat Map:** how many of five rival bridges (Across, Relay, deBridge, Chainflip, THORChain) can serve each route, and whether NEAR's revenue and pricing depend on the routes rivals cannot serve.

The argument, the evidence for and against, and what would change it are in [THESIS.md](THESIS.md). The charts are in [dashboard/index.html](dashboard/index.html) (one static file; open it in a browser).

## Rebuild everything

```
pip install -r requirements.txt
copy .env.example .env        # then fill it in, see below
python build.py
```

`python build.py` pulls every source, re-runs the analysis and re-renders the dashboard. `python build.py --offline` skips the pulls and re-renders from `data/processed/`.

| Step | Module | Source | Needs |
|---|---|---|---|
| Rival coverage | `moatmap.coverage` | each rival's own chain / asset API | nothing |
| Cost quotes | `moatmap.quotes` | live quote APIs of NEAR 1Click and the five rivals | nothing |
| Control totals | `moatmap.llama` | DefiLlama fees, revenue, volume | nothing |
| Swaps by route | `moatmap.explorer` | NEAR Intents Explorer API | `NEAR_INTENTS_JWT` |
| Network usage | `moatmap.bq` | NEAR BigQuery public dataset, SQL in [sql/](sql/) | Google Cloud key |
| Valuation model | `moatmap.model` | DefiLlama series, BigQuery supply, 1Click price | nothing |
| Durability tests | `moatmap.analyze` | the files above | nothing |
| Dashboard | `moatmap.dashboard` | `data/processed/results.json` | nothing |

A step without its credential prints `skipped: ...` and the dashboard shows that panel as "not computed". Nothing is ever filled in.

## Credentials (`.env`)

- `NEAR_INTENTS_JWT`: an **Intents Explorer API** key from https://partners.near-intents.org/.
- `GOOGLE_APPLICATION_CREDENTIALS`: path to a Google Cloud service-account JSON key. The account needs the **BigQuery Job User** role on the project.
- `GCP_PROJECT`: that project's id.

`.env` and `keys/` are git-ignored.

## Cost and time

- **Explorer pull:** the API allows one request per 5 seconds and in practice returns a 1,000-row page in about 30 seconds, so a day of swaps takes roughly 6 to 7 minutes. `python -m moatmap.explorer --days N` is resumable; rerun to continue.
- **BigQuery:** every query filters on `block_date`. `python -m moatmap.bq --dry-run` is free and prints the bytes each query would scan; a query over `--max-gb` (default 20) is refused.
- **Quotes:** each run adds a timestamped snapshot under `data/processed/quote_snapshots/`; results use the median across snapshots.

## Layout

```
build.py              one-command rebuild
moatmap/              pipeline modules (config.py holds shared paths and CSV schemas)
sql/                  BigQuery queries, one per metric
data/raw/             untouched API responses (git-ignored, rebuilt by build.py)
data/processed/       tidy CSV / JSON the dashboard reads
dashboard/index.html  the static dashboard
tests/                unit tests; fixtures there are synthetic and labelled so
THESIS.md             claim, evidence for, evidence against, what would change the conclusion
```
