-- Migration 0002: Bill Collector role. Collections a collector takes from customers (cash, the bank's own POS
-- terminal, or bKash typed by hand: no card number is ever stored, only the approval code and last 4 digits), and
-- the cash they hand over to whoever hired them (cash in hand = cash collected - confirmed deposits).
-- panel_schema.sql carries the same statements (idempotent), so a fresh install gets them too.
CREATE TABLE IF NOT EXISTS collections (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  receipt_no    VARCHAR(32)  NOT NULL,
  subscriber_id INT UNSIGNED NOT NULL,
  username      VARCHAR(64)  NOT NULL DEFAULT '',
  collector_id  INT UNSIGNED NOT NULL,
  parent_id     INT UNSIGNED NULL,
  amount        DECIMAL(10,2) NOT NULL,
  method        ENUM('cash','pos','bkash') NOT NULL DEFAULT 'cash',
  pos_ref       VARCHAR(64)  NULL,
  trx_id        VARCHAR(32)  NULL,
  form_token    VARCHAR(48)  NULL,
  result        VARCHAR(255) NOT NULL DEFAULT '',
  status        ENUM('posted','void') NOT NULL DEFAULT 'posted',
  void_by       VARCHAR(64)  NULL,
  void_reason   VARCHAR(255) NULL,
  void_at       DATETIME NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_receipt (receipt_no),
  UNIQUE KEY uq_trx (trx_id),
  UNIQUE KEY uq_token (form_token),
  KEY idx_collector (collector_id, created_at),
  KEY idx_parent (parent_id, created_at),
  KEY idx_sub (subscriber_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS collection_deposits (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  collector_id  INT UNSIGNED NOT NULL,
  parent_id     INT UNSIGNED NULL,
  amount        DECIMAL(10,2) NOT NULL,
  note          VARCHAR(255) NOT NULL DEFAULT '',
  status        ENUM('pending','confirmed','rejected') NOT NULL DEFAULT 'pending',
  confirmed_by  VARCHAR(64)  NULL,
  confirmed_at  DATETIME NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_collector (collector_id, status),
  KEY idx_parent (parent_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
