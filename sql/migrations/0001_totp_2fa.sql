-- Migration 0001: optional TOTP two-factor authentication for panel admins.
-- totp_secret holds the base32 TOTP secret encrypted with PANEL_SETTINGS_KEY (enc:v1:...),
-- totp_backup_codes holds a JSON list of sha256 hashes of one-time backup codes.
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS totp_secret       VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS totp_enabled      TINYINT(1)   NOT NULL DEFAULT 0;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS totp_backup_codes TEXT         NULL;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS totp_last_step    BIGINT       NOT NULL DEFAULT 0;
