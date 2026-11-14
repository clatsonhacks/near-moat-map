-- freshness.sql
-- Measures: the last block_date actually present in the public dataset (and the last block
--   timestamp), so the pipeline reports the real data cut-off instead of assuming "today".
--   Why: docs.near.org warns that NEAR Lake (the S3 source feeding this dataset) was
--   deprecated on 2026-03-24, so the dataset may be stale.
-- Scan: `blocks` only (one row per block, the smallest fact table); reads block_date and
--   block_timestamp_utc for the partitions in the window. bq.py starts with a 30-day window
--   and widens it (120, 400, 1200 days) only if the window is empty; every attempt is
--   dry-run first and the bytes are printed.
-- Columns verified against near-public-lakehouse "BQ Writer Views & Data Dictionary.py":
--   blocks.block_date, blocks.block_timestamp_utc.
-- Params: @start_date DATE, @end_date DATE
SELECT
  MAX(block_date)             AS last_block_date,
  MAX(block_timestamp_utc)    AS last_block_timestamp_utc,
  COUNT(DISTINCT block_date)  AS days_with_data,
  COUNT(*)                    AS blocks_in_window
FROM `bigquery-public-data.crypto_near_mainnet_us.blocks`
WHERE block_date BETWEEN @start_date AND @end_date
