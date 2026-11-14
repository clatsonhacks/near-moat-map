-- circulating_supply.sql
-- Measures: NEAR circulating and total token supply per day, as published in the dataset's
-- circulating_supply table. Used by moatmap.model to size NEAR's market value.
-- Scan: the table is about 0.1 MB in total; the block_date filter is applied anyway.
SELECT
  block_date,
  circulating_tokens_supply,
  total_tokens_supply
FROM `bigquery-public-data.crypto_near_mainnet_us.circulating_supply`
WHERE block_date BETWEEN @start_date AND @end_date
ORDER BY block_date
