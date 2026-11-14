-- intents_contract_methods.sql
-- Measures, per day and method: FUNCTION_CALL actions received by @intents_contract
--   (intents.near) broken down by method name (execute_intents, ft_on_transfer,
--   mt_transfer, ...), with distinct predecessors per method.
-- Scan: EXPENSIVE. `receipt_actions`, partition-pruned on block_date, but it must read the
--   `args` column (which carries every call's base64 arguments) for ALL contracts in the
--   window, because BigQuery bills whole columns per partition. bq.py therefore runs this
--   on a short window (--heavy-days, default 7) and dry-runs it first.
-- Columns verified against near-public-lakehouse "BQ Writer Views & Data Dictionary.py":
--   block_date, action_kind, receipt_receiver_account_id, receipt_predecessor_account_id, args.
--   Shape of args for FUNCTION_CALL verified in "Silver Lake Mainnet.sql"
--   (silver_action_receipt_actions): {"gas": "...", "deposit": "...", "args_base64": "...",
--   "method_name": "..."}.
-- UNVERIFIED column type: receipt_actions.args is a JSON-formatted STRING in the silver
--   table; whether BigQuery stores it as STRING or JSON could not be confirmed without
--   credentials. JSON_VALUE accepts both types, so the query is valid either way.
-- Params: @start_date DATE, @end_date DATE, @intents_contract STRING
SELECT
  block_date,
  JSON_VALUE(args, '$.method_name')               AS method_name,
  COUNT(*)                                        AS function_calls,
  COUNT(DISTINCT receipt_predecessor_account_id)  AS distinct_predecessors
FROM `bigquery-public-data.crypto_near_mainnet_us.receipt_actions`
WHERE block_date BETWEEN @start_date AND @end_date
  AND action_kind = 'FUNCTION_CALL'
  AND receipt_receiver_account_id = @intents_contract
GROUP BY block_date, method_name
ORDER BY block_date, function_calls DESC
