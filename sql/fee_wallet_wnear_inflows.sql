-- fee_wallet_wnear_inflows.sql   (OPTIONAL - bq.py treats a failure here as non-fatal)
-- Measures, per day and wallet: wNEAR (wrap.near) received by NEAR's three Intents revenue
--   wallets, excluding transfers between those wallets. Mirror of the second leg of the
--   DefiLlama fees/near-intents adapter:
--     near.ft_transfers WHERE contract_account_id = 'wrap.near' AND delta_amount > 0
--       AND affected_account_id IN wallets AND involved_account_id NOT IN wallets.
-- Scan: `ft_events`, partition-pruned on block_date; the lakehouse writer clusters this
--   table on contract_account_id, so the wrap.near filter should prune blocks as well.
-- UNVERIFIED table: `ft_events` is written by the lakehouse "BQ Writer Stream.py" notebook
--   (table crypto_near_mainnet.ft_events) but is NOT in the table list on docs.near.org or
--   in the repo README, so its presence in bigquery-public-data.crypto_near_mainnet_us is
--   unconfirmed. If the dry run says "Not found: Table", this leg cannot be measured here.
-- Columns as written by that notebook (verified there, unverified in the public dataset):
--   block_date, contract_account_id, affected_account_id, involved_account_id,
--   delta_amount (cast to STRING by the writer), status.
-- UNVERIFIED column: ft_events.status values are assumed to follow execution_outcomes
--   (SUCCESS_VALUE / SUCCESS_RECEIPT_ID / FAILURE); only FAILURE is filtered out.
-- Params: @start_date DATE, @end_date DATE, @fee_wallets ARRAY<STRING>
SELECT
  block_date,
  affected_account_id                                                    AS wallet,
  COUNT(*)                                                               AS n_transfers,
  CAST(SUM(SAFE_CAST(delta_amount AS BIGNUMERIC)) / 1e24 AS FLOAT64)     AS wnear_amount
FROM `bigquery-public-data.crypto_near_mainnet_us.ft_events`
WHERE block_date BETWEEN @start_date AND @end_date
  AND contract_account_id = 'wrap.near'
  AND affected_account_id IN UNNEST(@fee_wallets)
  AND SAFE_CAST(delta_amount AS BIGNUMERIC) > 0
  AND (involved_account_id IS NULL OR involved_account_id NOT IN UNNEST(@fee_wallets))
  AND (status IS NULL OR status != 'FAILURE')
GROUP BY block_date, wallet
ORDER BY block_date, wallet
