-- Migration 0006: MAC Vendor keeps the IEEE MA-M (7 hex digits) and MA-S (9) blocks as well as MA-L (6), with the
-- block type, whether the owner is private, and the date of the entry. panel_schema.sql has the same shape for new
-- installs; this widens the prefix of an existing table once (the 6-digit prefixes already there stay as they are).
ALTER TABLE mac_vendors MODIFY prefix VARCHAR(9) NOT NULL;
ALTER TABLE mac_vendors ADD COLUMN IF NOT EXISTS block_type VARCHAR(8) NOT NULL DEFAULT 'MA-L';
ALTER TABLE mac_vendors ADD COLUMN IF NOT EXISTS private TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE mac_vendors ADD COLUMN IF NOT EXISTS updated DATE NULL;
