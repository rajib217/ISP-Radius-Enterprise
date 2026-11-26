-- Migration 0004: one bKash TrxID can be booked only once. topups and portal_payments gain the UNIQUE KEY on trx_id
-- that sms_payments and collections already have, so the check-then-insert in the manual add and in the bKash settle
-- is backed by the database itself: two adds at the same moment cannot book the same money twice.
-- The column becomes NULLABLE first: a bKash payment starts life without a TrxID (bKash fills it in when it confirms
-- the payment), so '' would collide on the second pending row. NULLs never collide in a UNIQUE KEY. The ADD is
-- IF NOT EXISTS because a fresh install already gets the key from panel_schema.sql, which carries the same final shape.
-- Existing rows: an empty TrxID becomes NULL, and where the same TrxID was booked twice only the earliest keeps it
-- (the money stays booked; the office can re-check the other row by hand).
ALTER TABLE topups MODIFY trx_id VARCHAR(64) NULL;
UPDATE topups SET trx_id = NULL WHERE trx_id = '';
UPDATE topups t JOIN (SELECT trx_id, MIN(id) AS keep_id FROM topups WHERE trx_id IS NOT NULL
                      GROUP BY trx_id HAVING COUNT(*) > 1) d
  ON d.trx_id = t.trx_id SET t.trx_id = NULL WHERE t.id <> d.keep_id;
ALTER TABLE topups ADD UNIQUE KEY IF NOT EXISTS uq_trx (trx_id);
ALTER TABLE portal_payments MODIFY trx_id VARCHAR(64) NULL;
UPDATE portal_payments SET trx_id = NULL WHERE trx_id = '';
UPDATE portal_payments t JOIN (SELECT trx_id, MIN(id) AS keep_id FROM portal_payments WHERE trx_id IS NOT NULL
                               GROUP BY trx_id HAVING COUNT(*) > 1) d
  ON d.trx_id = t.trx_id SET t.trx_id = NULL WHERE t.id <> d.keep_id;
ALTER TABLE portal_payments ADD UNIQUE KEY IF NOT EXISTS uq_trx (trx_id);
