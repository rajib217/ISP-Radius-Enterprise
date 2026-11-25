-- Migration 0003: a per-account session version. Changing the password raises it, which signs that account out on every
-- other device (each signed-in browser carries the version it signed in with). panel_schema.sql has the same line.
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS session_ver INT UNSIGNED NOT NULL DEFAULT 0;
