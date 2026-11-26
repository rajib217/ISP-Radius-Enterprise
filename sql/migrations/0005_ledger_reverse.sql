-- Migration 0005: Ledger > Reverse (administrator only) undoes a balance top-up made by mistake.
-- kind marks a top-up made by hand ('topup': Balance on the customer's page, Mass payment) and the line that undid one
-- ('reversal'); reversed_at closes a top-up so it can be undone only once. panel_schema.sql has the same two columns.
-- Existing top-ups are marked from the audit log (kept a year): the 'balance_changed' line written with the same
-- customer, amount and new balance within a few seconds. Older ones, and money from bKash, collectors, activations or
-- reverted invoices, stay unmarked and cannot be reversed here (they have their own void / revert).
ALTER TABLE sub_ledger ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT '';
ALTER TABLE sub_ledger ADD COLUMN IF NOT EXISTS reversed_at DATETIME NULL;
UPDATE sub_ledger l JOIN subscribers s ON s.id = l.subscriber_id SET l.kind = 'topup'
 WHERE l.kind = '' AND l.amount > 0 AND EXISTS (
   SELECT 1 FROM audit_log a WHERE a.action = 'balance_changed' AND a.target = s.username
      AND a.created_at BETWEEN l.created_at - INTERVAL 5 SECOND AND l.created_at + INTERVAL 5 SECOND
      AND a.detail LIKE CONCAT('+', l.amount, ' -> ', l.balance_after, '%'));
