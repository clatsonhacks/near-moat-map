-- intents_contract_usage.sql
-- Measures, per day: FUNCTION_CALL actions and receipts received by the NEAR Intents
--   contract (@intents_contract = intents.near), the distinct accounts that sent those
--   receipts (predecessors: solvers, relayers, bridges, users), the distinct accounts that
--   signed the originating transactions, and the network-wide FUNCTION_CALL count for the
--   same day so the contract's daily share of network usage can be computed.
--   The method-name breakdown lives in intents_contract_methods.sql because it has to read
--   the large `args` column; this file deliberately does not touch `args`.
-- Scan: `receipt_actions`, partition-pruned on block_date; reads block_date, action_kind,
--   receipt_receiver_account_id, receipt_predecessor_account_id, signer_account_id and
--   receipt_id. The receiver filter does NOT reduce bytes scanned (no clustering on
--   receiver is documented), only the date window does.
-- Columns verified against near-public-lakehouse "BQ Writer Views & Data Dictionary.py":
--   all six columns above; action_kind value 'FUNCTION_CALL' verified in
--   "Silver Lake Mainnet.sql" (silver_action_receipt_actions).
-- Note: receipt_actions has one row per ACTION; a receipt can batch several actions.
-- Params: @start_date DATE, @end_date DATE, @intents_contract STRING
SELECT
  block_date,
  COUNT(*)                                                                     AS network_function_calls,
  COUNTIF(receipt_receiver_account_id = @intents_contract)                     AS intents_function_calls,
  COUNT(DISTINCT IF(receipt_receiver_account_id = @intents_contract,
                    receipt_id, NULL))                                         AS intents_receipts,
  COUNT(DISTINCT IF(receipt_receiver_account_id = @intents_contract,
                    receipt_predecessor_account_id, NULL))                     AS intents_distinct_predecessors,
  COUNT(DISTINCT IF(receipt_receiver_account_id = @intents_contract,
                    signer_account_id, NULL))                                  AS intents_distinct_signers
FROM `bigquery-public-data.crypto_near_mainnet_us.receipt_actions`
WHERE block_date BETWEEN @start_date AND @end_date
  AND action_kind = 'FUNCTION_CALL'
GROUP BY block_date
ORDER BY block_date
