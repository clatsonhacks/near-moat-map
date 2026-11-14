-- top_contracts.sql
-- Measures: the top @top_n receiver contracts by FUNCTION_CALL actions over the whole
--   window, each with its rank, share of all network FUNCTION_CALL actions and distinct
--   predecessor accounts. @intents_contract (intents.near) is always returned, whatever
--   its rank, so its position in network usage is explicit rather than implied.
-- Scan: `receipt_actions`, partition-pruned on block_date; reads block_date, action_kind,
--   receipt_receiver_account_id and receipt_predecessor_account_id for the window. Does not
--   read `args`. Bytes grow linearly with the window; bq.py dry-runs it first.
-- Columns verified against near-public-lakehouse "BQ Writer Views & Data Dictionary.py":
--   block_date, action_kind, receipt_receiver_account_id, receipt_predecessor_account_id.
-- Params: @start_date DATE, @end_date DATE, @top_n INT64, @intents_contract STRING
WITH per_contract AS (
  SELECT
    receipt_receiver_account_id                     AS contract,
    COUNT(*)                                        AS function_calls,
    COUNT(DISTINCT receipt_predecessor_account_id)  AS distinct_predecessors,
    COUNT(DISTINCT block_date)                      AS active_days
  FROM `bigquery-public-data.crypto_near_mainnet_us.receipt_actions`
  WHERE block_date BETWEEN @start_date AND @end_date
    AND action_kind = 'FUNCTION_CALL'
  GROUP BY contract
),
ranked AS (
  SELECT
    RANK() OVER (ORDER BY function_calls DESC)                    AS rank_by_calls,
    contract,
    function_calls,
    SAFE_DIVIDE(function_calls, SUM(function_calls) OVER ())      AS share_of_network_calls,
    distinct_predecessors,
    active_days,
    COUNT(*) OVER ()                                              AS contracts_in_window
  FROM per_contract
)
SELECT *
FROM ranked
WHERE rank_by_calls <= @top_n OR contract = @intents_contract
ORDER BY rank_by_calls
