-- fee_wallet_inflows.sql
-- Measures, per day and wallet: native NEAR transferred INTO NEAR's three Intents revenue
--   wallets (@fee_wallets = config.FEE_WALLETS), excluding transfers between those wallets.
--   Mirror of the native-NEAR leg of the DefiLlama fees/near-intents adapter:
--     near.actions WHERE action_kind = 'TRANSFER' AND execution_status = 'SUCCESS_VALUE'
--       AND receiver IN wallets AND predecessor NOT IN wallets;  amount = deposit / 1e24.
--   The adapter ALSO adds wNEAR (wrap.near FT transfers) into the same wallets; that leg is
--   in fee_wallet_wnear_inflows.sql. Revenue = native leg + wNEAR leg. Amounts are in NEAR,
--   not USD (DefiLlama prices them at the day's NEAR price).
--   system_refund_near shows how much of the inflow came from predecessor 'system' (gas /
--   deposit refunds); the adapter does not exclude it, so near_amount includes it.
-- Scan: `receipt_actions` (block_date, action_kind, receiver, predecessor, receipt_id and
--   the large `args` column, needed for the deposit amount) joined to `execution_outcomes`
--   (block_date, receipt_id, executor_account_id, status). Both sides are partition-pruned
--   on block_date; the outcome side looks one day past @end_date because a receipt can
--   execute in the block after the one that recorded it. Reading `args` makes this one of
--   the expensive queries; bq.py dry-runs it first.
-- Columns verified against near-public-lakehouse "BQ Writer Views & Data Dictionary.py":
--   receipt_actions.{block_date, action_kind, receipt_receiver_account_id,
--   receipt_predecessor_account_id, receipt_id, args};
--   execution_outcomes.{block_date, receipt_id, executor_account_id, status}.
--   Status values SUCCESS_VALUE / SUCCESS_RECEIPT_ID / FAILURE and the TRANSFER args shape
--   {"deposit": "<yoctoNEAR>"} verified in "Silver Lake Mainnet.sql".
-- UNVERIFIED column type: receipt_actions.args STRING vs JSON (JSON_VALUE accepts both).
-- Params: @start_date DATE, @end_date DATE, @fee_wallets ARRAY<STRING>
WITH transfers AS (
  SELECT
    block_date,
    receipt_id,
    receipt_receiver_account_id     AS wallet,
    receipt_predecessor_account_id  AS sender,
    SAFE_CAST(JSON_VALUE(args, '$.deposit') AS BIGNUMERIC) AS deposit_yocto
  FROM `bigquery-public-data.crypto_near_mainnet_us.receipt_actions`
  WHERE block_date BETWEEN @start_date AND @end_date
    AND action_kind = 'TRANSFER'
    AND receipt_receiver_account_id IN UNNEST(@fee_wallets)
    AND receipt_predecessor_account_id NOT IN UNNEST(@fee_wallets)
),
succeeded AS (
  SELECT DISTINCT receipt_id
  FROM `bigquery-public-data.crypto_near_mainnet_us.execution_outcomes`
  WHERE block_date BETWEEN @start_date AND DATE_ADD(@end_date, INTERVAL 1 DAY)
    AND executor_account_id IN UNNEST(@fee_wallets)
    AND status = 'SUCCESS_VALUE'
)
SELECT
  t.block_date,
  t.wallet,
  COUNT(*)                                                             AS n_transfers,
  CAST(SUM(t.deposit_yocto) / 1e24 AS FLOAT64)                         AS near_amount,
  CAST(SUM(IF(t.sender = 'system', t.deposit_yocto, 0)) / 1e24 AS FLOAT64) AS system_refund_near,
  COUNTIF(t.deposit_yocto IS NULL)                                     AS unparsed_deposits
FROM transfers AS t
INNER JOIN succeeded AS s USING (receipt_id)
GROUP BY t.block_date, t.wallet
ORDER BY t.block_date, t.wallet
