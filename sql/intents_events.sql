-- intents_events.sql
-- Measures, per day: events emitted by @intents_contract (intents.near), decoded from
--   NEP-297 "EVENT_JSON:{...}" execution-outcome logs and counted by standard / event name.
--   intents.near emits two standards (confirmed in near-public-lakehouse
--   rust-near-defuse-indexer/src/event_handler.rs, which filters exactly these): "dip4"
--   (intent-level events) and "nep245" (multi-token mint / burn / transfer of the bridged
--   balances). This is the path to on-chain ROUTE decoding: the per-event `data` payload
--   is where token ids and amounts live, so origin/destination asset per intent would be
--   rebuilt from it. This file stops at event counts because the layout of `data` per
--   event type could not be confirmed against live rows without credentials (UNVERIFIED,
--   so it is not decoded here rather than guessed).
-- Scan: EXPENSIVE. `execution_outcomes`, partition-pruned on block_date, but it reads the
--   `logs` column for every receipt of every contract in the window. bq.py runs it on a
--   short window (--heavy-days, default 7) and dry-runs it first.
-- Columns verified against near-public-lakehouse "BQ Writer Views & Data Dictionary.py":
--   execution_outcomes.{block_date, receipt_id, executor_account_id, status, logs}.
--   Status values verified in "Silver Lake Mainnet.sql" (silver_execution_outcomes).
-- UNVERIFIED column type: execution_outcomes.logs is ARRAY<STRING> in the silver table and
--   is assumed to be a REPEATED STRING in BigQuery (hence UNNEST). If the dry run rejects
--   UNNEST(logs), the column is a JSON-encoded STRING and the FROM clause must become
--   UNNEST(JSON_VALUE_ARRAY(logs)) AS log.
-- Payload keys standard / version / event / data verified in near-public-lakehouse
--   rust-near-defuse-indexer/src/types.rs (struct EventJson).
-- Params: @start_date DATE, @end_date DATE, @intents_contract STRING
WITH event_logs AS (
  SELECT
    eo.block_date,
    eo.receipt_id,
    SUBSTR(l.element, LENGTH('EVENT_JSON:') + 1) AS event_json
  FROM `bigquery-public-data.crypto_near_mainnet_us.execution_outcomes` AS eo,
       UNNEST(eo.logs.list) AS l  -- verified by dry run: logs is STRUCT<list ARRAY<STRUCT<element STRING>>>
  WHERE eo.block_date BETWEEN @start_date AND @end_date
    AND eo.executor_account_id = @intents_contract
    AND eo.status IN ('SUCCESS_VALUE', 'SUCCESS_RECEIPT_ID')
    AND STARTS_WITH(l.element, 'EVENT_JSON:')
)
SELECT
  block_date,
  JSON_VALUE(event_json, '$.standard')  AS standard,
  JSON_VALUE(event_json, '$.event')     AS event,
  JSON_VALUE(event_json, '$.version')   AS version,
  COUNT(*)                              AS events,
  COUNT(DISTINCT receipt_id)            AS receipts
FROM event_logs
GROUP BY block_date, standard, event, version
ORDER BY block_date, events DESC
