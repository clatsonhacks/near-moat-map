-- daily_transactions.sql
-- Measures: NEAR mainnet transaction count per day (all transactions, any status).
-- Scan: `transactions`, partition-pruned on block_date; reads only the block_date column
--   (COUNT(*)), so it is the cheapest query on this table. One row per day returned.
-- Columns verified against near-public-lakehouse "BQ Writer Views & Data Dictionary.py":
--   transactions.block_date.
-- Params: @start_date DATE, @end_date DATE
SELECT
  block_date,
  COUNT(*) AS transactions
FROM `bigquery-public-data.crypto_near_mainnet_us.transactions`
WHERE block_date BETWEEN @start_date AND @end_date
GROUP BY block_date
ORDER BY block_date
