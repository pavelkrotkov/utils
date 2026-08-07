-- Store `stModelId` / `stDueOn`: the scheduled-transaction model a row belongs to.
--
-- WHY. Simplifi mixes three kinds of row into one feed and state alone cannot
-- distinguish the latter two:
--
--   1. real, settled          state=CLEARED
--   2. real, not yet settled  state=PENDING
--   3. PROJECTED — Simplifi's forecast of a recurring bill that has not
--                  happened and may never happen.  ALSO state=PENDING.
--
-- Kinds 2 and 3 are indistinguishable on `state` alone. Scheduled-model
-- metadata is the discriminator that keeps forecast rows out of settled-charge
-- statistics.
--
-- `stModelId` is the discriminator: rows generated from a scheduled-transaction
-- model carry one, genuinely-downloaded transactions do not. Without it,
-- "pending" means two incompatible things and any statement about recent
-- activity is a coin flip.
--
-- Dating tells the same story more crudely — nothing real is dated a year out —
-- but a row dated next week is exactly where the ambiguity bites.

ALTER TABLE transaction_version ADD COLUMN scheduled_model_id TEXT;
ALTER TABLE transaction_version ADD COLUMN scheduled_due_on TEXT;

CREATE INDEX IF NOT EXISTS idx_txn_scheduled
    ON transaction_version (is_current, scheduled_model_id);
