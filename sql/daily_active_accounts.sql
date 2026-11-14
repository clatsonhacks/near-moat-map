-- daily_active_accounts.sql
-- Measures: daily active accounts = distinct accounts that SIGNED at least one transaction
--   that day (transactions.signer_account_id). Meta-transactions are attributed to the
--   relayer that signed the outer transaction, so this undercounts relayed users.
-- Scan: `transactions`, partition-pruned on block_date; reads block_date and
--   signer_account_id for every transaction in the window (an exact COUNT(DISTINCT) per
--   day). Scan grows linearly with the window; bq.py dry-runs it and prints the bytes.
-- Columns verified against near-public-lakehouse "BQ Writer Views & Data Dictionary.py":
--   transactions.block_date, transactions.signer_account_id.
-- Params: @start_date DATE, @end_date DATE
SELECT
  block_date,
  COUNT(DISTINCT signer_account_id) AS active_accounts
FROM `bigquery-public-data.crypto_near_mainnet_us.transactions`
WHERE block_date BETWEEN @start_date AND @end_date
GROUP BY block_date
ORDER BY block_date
