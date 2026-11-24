-- ISP RADIUS Panel: extra tables and indexes.
-- Run AFTER the stock FreeRADIUS schema (radacct, radcheck, nas ...) exists.
-- Safe to run more than once.

CREATE TABLE IF NOT EXISTS panel_admins (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  username      VARCHAR(64)  NOT NULL UNIQUE,
  full_name     VARCHAR(128) NOT NULL DEFAULT '',
  password_hash VARCHAR(255) NOT NULL,
  role          ENUM('admin','support') NOT NULL DEFAULT 'support',
  is_active     TINYINT(1)   NOT NULL DEFAULT 1,
  last_login    DATETIME NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- A plan is also the RADIUS group (plans.name = radusergroup.groupname)
CREATE TABLE IF NOT EXISTS plans (
  id               INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name             VARCHAR(64)  NOT NULL UNIQUE,
  label            VARCHAR(128) NOT NULL DEFAULT '',
  download_mbps    INT UNSIGNED NOT NULL DEFAULT 10,
  upload_mbps      INT UNSIGNED NOT NULL DEFAULT 10,
  rate_custom      VARCHAR(255) NULL,
  price            DECIMAL(10,2) NOT NULL DEFAULT 0,
  validity_days    INT UNSIGNED NOT NULL DEFAULT 30,
  pool_name        VARCHAR(64)  NULL,
  simultaneous_use TINYINT UNSIGNED NOT NULL DEFAULT 1,
  created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS subscribers (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  username    VARCHAR(64)  NOT NULL UNIQUE,
  full_name   VARCHAR(128) NOT NULL DEFAULT '',
  phone       VARCHAR(32)  NOT NULL DEFAULT '',
  email       VARCHAR(128) NOT NULL DEFAULT '',
  address     VARCHAR(255) NOT NULL DEFAULT '',
  area        VARCHAR(64)  NOT NULL DEFAULT '',
  plan_id     INT UNSIGNED NULL,
  status      ENUM('active','suspended') NOT NULL DEFAULT 'active',
  expiry_date DATE NULL,
  static_ip   VARCHAR(45)  NULL,
  notes       TEXT NULL,
  created_by  VARCHAR(64)  NOT NULL DEFAULT '',
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_plan (plan_id),
  KEY idx_status_exp (status, expiry_date),
  KEY idx_area (area),
  KEY idx_phone (phone),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS payments (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subscriber_id INT UNSIGNED NULL,
  username      VARCHAR(64) NOT NULL,
  amount        DECIMAL(10,2) NOT NULL DEFAULT 0,
  days_added    INT UNSIGNED NOT NULL DEFAULT 0,
  method        VARCHAR(32) NOT NULL DEFAULT 'cash',
  note          VARCHAR(255) NOT NULL DEFAULT '',
  period_to     DATE NULL,
  received_by   VARCHAR(64) NOT NULL DEFAULT '',
  paid_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sub (subscriber_id),
  KEY idx_user (username),
  KEY idx_paid (paid_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS audit_log (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  admin      VARCHAR(64)  NOT NULL DEFAULT '',
  action     VARCHAR(64)  NOT NULL,
  target     VARCHAR(128) NOT NULL DEFAULT '',
  detail     VARCHAR(500) NOT NULL DEFAULT '',
  ip         VARCHAR(45)  NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_created (created_at),
  KEY idx_ip_action (ip, action, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS stats_snapshots (
  ts     DATETIME NOT NULL PRIMARY KEY,
  online INT UNSIGNED NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS server_stats (
  ts            DATETIME NOT NULL PRIMARY KEY,
  cpu_pct       DECIMAL(5,2) NOT NULL DEFAULT 0,
  load1         DECIMAL(6,2) NOT NULL DEFAULT 0,
  mem_used_mb   INT UNSIGNED NOT NULL DEFAULT 0,
  mem_total_mb  INT UNSIGNED NOT NULL DEFAULT 0,
  disk_used_gb  DECIMAL(10,2) NOT NULL DEFAULT 0,
  disk_total_gb DECIMAL(10,2) NOT NULL DEFAULT 0,
  db_mb         INT UNSIGNED NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS settings (
  skey   VARCHAR(64) NOT NULL PRIMARY KEY,
  svalue TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO settings (skey, svalue) VALUES
  ('isp_name', 'My ISP'),
  ('currency', 'BDT'),
  ('interim_interval', '600'),
  ('stale_minutes', '45'),
  ('authlog_days', '30'),
  ('archive_months', '12'),
  ('coa_port', '3799');

-- Indexes that matter at 20,000+ clients (MariaDB syntax)
ALTER TABLE radacct     ADD INDEX IF NOT EXISTS idx_user_stop (username, acctstoptime);
ALTER TABLE radacct     ADD INDEX IF NOT EXISTS idx_stop_nas (acctstoptime, nasipaddress);
ALTER TABLE radpostauth ADD INDEX IF NOT EXISTS idx_authdate (authdate);

-- Cold storage for old accounting rows (same structure as radacct)
CREATE TABLE IF NOT EXISTS radacct_archive LIKE radacct;


-- ====================================================================
--  Version 2: users and roles, accounting, inventory, network, tickets,
--  email / SMS, notes, documents
-- ====================================================================
-- VARCHAR (not ENUM) so that custom roles can be stored
ALTER TABLE panel_admins MODIFY role VARCHAR(32) NOT NULL DEFAULT 'support';

-- Roles the administrator creates (office-staff type); permissions live in role_perms / role_funcs under the same key
CREATE TABLE IF NOT EXISTS custom_roles (
  role_key   VARCHAR(32) NOT NULL PRIMARY KEY,
  name       VARCHAR(64) NOT NULL,
  created_by VARCHAR(64) NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS phone         VARCHAR(32)  NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS email         VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS department_id INT UNSIGNED NULL;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS zone_id       INT UNSIGNED NULL;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS parent_id     INT UNSIGNED NULL;
-- Optional TOTP two-factor auth (migration 0001; also added here so test/demo databases have the columns)
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS totp_secret       VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS totp_enabled      TINYINT(1)   NOT NULL DEFAULT 0;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS totp_backup_codes TEXT         NULL;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS totp_last_step    BIGINT       NOT NULL DEFAULT 0;

-- Changes to the built-in permissions of a role (module x none/view/edit)
CREATE TABLE IF NOT EXISTS role_funcs (
  role    VARCHAR(32) NOT NULL,
  fkey    VARCHAR(48) NOT NULL,
  allowed TINYINT(1)  NOT NULL DEFAULT 0,
  PRIMARY KEY (role, fkey)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS role_perms (
  role   VARCHAR(32) NOT NULL,
  module VARCHAR(32) NOT NULL,
  level  ENUM('none','view','edit') NOT NULL DEFAULT 'none',
  PRIMARY KEY (role, module)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- A staff member with rows here may only use these plans (no rows = all plans)
CREATE TABLE IF NOT EXISTS user_plans (
  user_id INT UNSIGNED NOT NULL,
  plan_id INT UNSIGNED NOT NULL,
  PRIMARY KEY (user_id, plan_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS departments (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(64)  NOT NULL UNIQUE,
  description VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS zones (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name        VARCHAR(64)  NOT NULL UNIQUE,
  description VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS areas (
  id      INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name    VARCHAR(64)  NOT NULL UNIQUE,
  note    VARCHAR(255) NOT NULL DEFAULT '',
  zone_id INT UNSIGNED NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- areas already typed into customer records become area entries
INSERT IGNORE INTO areas (name) SELECT DISTINCT area FROM subscribers WHERE area <> '';

CREATE TABLE IF NOT EXISTS expenses (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  spent_on   DATE NOT NULL,
  category   VARCHAR(64)  NOT NULL DEFAULT 'Other',
  amount     DECIMAL(12,2) NOT NULL DEFAULT 0,
  paid_to    VARCHAR(128) NOT NULL DEFAULT '',
  method     VARCHAR(32)  NOT NULL DEFAULT 'cash',
  note       VARCHAR(255) NOT NULL DEFAULT '',
  created_by VARCHAR(64)  NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_spent (spent_on)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS inventory_items (
  id        INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name      VARCHAR(128) NOT NULL,
  category  VARCHAR(64)  NOT NULL DEFAULT 'Other',
  sku       VARCHAR(64)  NOT NULL DEFAULT '',
  unit      VARCHAR(16)  NOT NULL DEFAULT 'pcs',
  qty       INT NOT NULL DEFAULT 0,
  min_qty   INT NOT NULL DEFAULT 0,
  unit_cost DECIMAL(12,2) NOT NULL DEFAULT 0,
  location  VARCHAR(128) NOT NULL DEFAULT '',
  note      VARCHAR(255) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_cat (category)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS inventory_moves (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  item_id       INT UNSIGNED NOT NULL,
  kind          ENUM('in','out','assign','return','adjust') NOT NULL,
  qty           INT NOT NULL,
  subscriber_id INT UNSIGNED NULL,
  username      VARCHAR(64)  NOT NULL DEFAULT '',
  serial        VARCHAR(128) NOT NULL DEFAULT '',
  note          VARCHAR(255) NOT NULL DEFAULT '',
  by_user       VARCHAR(64)  NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_item (item_id, created_at),
  KEY idx_sub (subscriber_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS net_devices (
  id           INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name         VARCHAR(128) NOT NULL,
  kind         VARCHAR(32)  NOT NULL DEFAULT 'olt',
  ip           VARCHAR(45)  NOT NULL DEFAULT '',
  location     VARCHAR(128) NOT NULL DEFAULT '',
  zone_id      INT UNSIGNED NULL,
  notes        VARCHAR(255) NOT NULL DEFAULT '',
  last_status  VARCHAR(8)   NOT NULL DEFAULT '',
  last_checked DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ip_pools (
  id      INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name    VARCHAR(64)  NOT NULL UNIQUE,
  ranges  TEXT NOT NULL,
  gateway VARCHAR(45)  NOT NULL DEFAULT '',
  nas_id  INT NULL,
  note    VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS tickets (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subscriber_id INT UNSIGNED NULL,
  username      VARCHAR(64)  NOT NULL DEFAULT '',
  subject       VARCHAR(200) NOT NULL,
  category      VARCHAR(32)  NOT NULL DEFAULT 'other',
  priority      VARCHAR(16)  NOT NULL DEFAULT 'normal',
  status        VARCHAR(16)  NOT NULL DEFAULT 'open',
  department_id INT UNSIGNED NULL,
  assigned_to   INT UNSIGNED NULL,
  created_by    VARCHAR(64)  NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at     DATETIME NULL,
  KEY idx_status (status, updated_at),
  KEY idx_sub (subscriber_id),
  KEY idx_assigned (assigned_to)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ticket_replies (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  ticket_id  INT UNSIGNED NOT NULL,
  author     VARCHAR(64) NOT NULL DEFAULT '',
  body       TEXT NOT NULL,
  internal   TINYINT(1) NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_ticket (ticket_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Waiting / sent / failed SMS and emails. The isp-panel-sender service delivers them.
CREATE TABLE IF NOT EXISTS outbox (
  id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  channel       ENUM('sms','email') NOT NULL,
  to_addr       VARCHAR(160) NOT NULL,
  subject       VARCHAR(200) NOT NULL DEFAULT '',
  body          TEXT NOT NULL,
  subscriber_id INT UNSIGNED NULL,
  dedupe        VARCHAR(100) NULL,
  batch         VARCHAR(16) NULL,
  status        ENUM('pending','sent','failed') NOT NULL DEFAULT 'pending',
  tries         TINYINT UNSIGNED NOT NULL DEFAULT 0,
  error         VARCHAR(255) NOT NULL DEFAULT '',
  created_by    VARCHAR(64) NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  sent_at       DATETIME NULL,
  UNIQUE KEY uq_dedupe (dedupe),
  KEY idx_queue (channel, status, id),
  KEY idx_batch (batch),
  KEY idx_sub (subscriber_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS message_templates (
  id      INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  kind    ENUM('sms','email') NOT NULL,
  name    VARCHAR(64) NOT NULL,
  subject VARCHAR(200) NOT NULL DEFAULT '',
  body    TEXT NOT NULL,
  UNIQUE KEY uq_kind_name (kind, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS notes (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id       INT UNSIGNED NOT NULL,
  author        VARCHAR(64) NOT NULL DEFAULT '',
  title         VARCHAR(120) NOT NULL DEFAULT '',
  body          TEXT NOT NULL,
  color         VARCHAR(16) NOT NULL DEFAULT 'yellow',
  shared        TINYINT(1) NOT NULL DEFAULT 0,
  pinned        TINYINT(1) NOT NULL DEFAULT 0,
  subscriber_id INT UNSIGNED NULL,
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_user (user_id),
  KEY idx_sub (subscriber_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS documents (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  title         VARCHAR(200) NOT NULL,
  category      VARCHAR(64)  NOT NULL DEFAULT 'Other',
  subscriber_id INT UNSIGNED NULL,
  filename      VARCHAR(255) NOT NULL,
  stored        VARCHAR(80)  NOT NULL,
  size          BIGINT UNSIGNED NOT NULL DEFAULT 0,
  uploaded_by   VARCHAR(64)  NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sub (subscriber_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS fees (
  id     INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name   VARCHAR(64) NOT NULL,
  kind   ENUM('tax','fee') NOT NULL DEFAULT 'fee',
  mode   ENUM('percent','fixed') NOT NULL DEFAULT 'fixed',
  value  DECIMAL(10,2) NOT NULL DEFAULT 0,
  active TINYINT(1) NOT NULL DEFAULT 1,
  note   VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS mac_bind    VARCHAR(17) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS grace_until DATE NULL;

CREATE TABLE IF NOT EXISTS sub_charges (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subscriber_id INT UNSIGNED NOT NULL,
  name          VARCHAR(64) NOT NULL,
  amount        DECIMAL(10,2) NOT NULL DEFAULT 0,
  paid          TINYINT(1) NOT NULL DEFAULT 0,
  note          VARCHAR(255) NOT NULL DEFAULT '',
  created_by    VARCHAR(64) NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  paid_at       DATETIME NULL,
  KEY idx_sub (subscriber_id, paid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE plans ADD COLUMN IF NOT EXISTS description VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS invoice_desc VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS billing_type VARCHAR(10) NOT NULL DEFAULT 'prepaid';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS service_type VARCHAR(16) NOT NULL DEFAULT 'Internet';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS duration INT UNSIGNED NOT NULL DEFAULT 30;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS duration_type VARCHAR(8) NOT NULL DEFAULT 'day';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS auto_renew TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS require_balance TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS auto_invoice TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS invoice_day TINYINT UNSIGNED NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS fixed_expire_day TINYINT UNSIGNED NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS fixed_expire_acct TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS fixed_expire_acct_mode VARCHAR(8) NOT NULL DEFAULT 'daily';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS fixed_expire_time VARCHAR(5) NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS bandwidth_value DECIMAL(12,2) NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS bandwidth_type  VARCHAR(8) NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS speed_from_policy TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS policy_group VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS expired_policy VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS disabled_policy VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_total_gb DECIMAL(10,2) NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_total_action VARCHAR(12) NOT NULL DEFAULT 'disconnect';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_total_policy VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_daily_gb DECIMAL(10,2) NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_daily_action VARCHAR(12) NOT NULL DEFAULT 'disconnect';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_daily_policy VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_weekly_gb DECIMAL(10,2) NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_weekly_action VARCHAR(12) NOT NULL DEFAULT 'disconnect';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_weekly_policy VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_weekly_reset TINYINT UNSIGNED NOT NULL DEFAULT 0;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_session_min INT UNSIGNED NULL;
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_session_action VARCHAR(12) NOT NULL DEFAULT 'disconnect';
ALTER TABLE plans ADD COLUMN IF NOT EXISTS quota_session_policy VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS quota_hit VARCHAR(8) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS balance      DECIMAL(10,2) NOT NULL DEFAULT 0;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS net_disabled TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS sms_on       TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS email_on     TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS auto_mac     TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS auto_renew   TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS photo        VARCHAR(8) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS identity     VARCHAR(32) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS serial       VARCHAR(32) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS nas_ip       VARCHAR(45) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS conn_type     VARCHAR(16) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS vol_total_gb  DECIMAL(10,2) NULL;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS vol_used_gb   DECIMAL(10,2) NULL;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS sess_total_min INT UNSIGNED NULL;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS sess_used_min  INT UNSIGNED NULL;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS disc_type     VARCHAR(8) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS discount      DECIMAL(10,2) NOT NULL DEFAULT 0;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS box_pop_no    VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS box_pop_addr  VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS mc_board      VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS electricity   VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS cable_type    VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS uplink_port   VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS fiber_code    VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS fiber_color   VARCHAR(64)  NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS coa_log (
  id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  username   VARCHAR(64) NOT NULL,
  nas_ip     VARCHAR(45) NOT NULL DEFAULT '',
  ok         TINYINT(1) NOT NULL DEFAULT 0,
  message    VARCHAR(255) NOT NULL DEFAULT '',
  created_by VARCHAR(64) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_user (username, id),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- In-app notifications for customers (portal / its app): payment, balance, expiring, expired, custom (appnotify.py)
CREATE TABLE IF NOT EXISTS sub_notifications (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subscriber_id INT UNSIGNED NOT NULL,
  kind          VARCHAR(16)  NOT NULL DEFAULT 'custom',
  title         VARCHAR(120) NOT NULL DEFAULT '',
  body          VARCHAR(600) NOT NULL DEFAULT '',
  link          VARCHAR(255) NOT NULL DEFAULT '',
  dedupe        VARCHAR(120) NULL,
  created_by    VARCHAR(64)  NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  read_at       DATETIME NULL,
  UNIQUE KEY uq_dedupe (subscriber_id, dedupe),
  KEY idx_sub (subscriber_id, id),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE sub_notifications ADD COLUMN IF NOT EXISTS pushed_at DATETIME NULL;
ALTER TABLE sub_notifications ADD INDEX IF NOT EXISTS idx_push (pushed_at, id);

-- Web Push subscriptions of the customers' browsers / installed apps (webpush.py, sent by isp-panel-sender)
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subscriber_id INT UNSIGNED NOT NULL,
  endpoint_hash CHAR(40)      NOT NULL,
  endpoint      VARCHAR(1000) NOT NULL,
  p256dh        VARCHAR(255)  NOT NULL,
  auth          VARCHAR(64)   NOT NULL,
  user_agent    VARCHAR(255)  NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_ok       DATETIME NULL,
  fails         INT UNSIGNED NOT NULL DEFAULT 0,
  UNIQUE KEY uq_endpoint (endpoint_hash),
  KEY idx_sub (subscriber_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS sub_ledger (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subscriber_id INT UNSIGNED NOT NULL,
  amount        DECIMAL(10,2) NOT NULL,
  balance_after DECIMAL(10,2) NOT NULL DEFAULT 0,
  note          VARCHAR(255) NOT NULL DEFAULT '',
  created_by    VARCHAR(64) NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sub (subscriber_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS plan_fees (
  plan_id INT UNSIGNED NOT NULL,
  fee_id  INT UNSIGNED NOT NULL,
  PRIMARY KEY (plan_id, fee_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE nas ADD COLUMN IF NOT EXISTS group_id     INT UNSIGNED NULL;
ALTER TABLE nas ADD COLUMN IF NOT EXISTS coa_enabled  TINYINT(1) NOT NULL DEFAULT 1;
ALTER TABLE nas ADD COLUMN IF NOT EXISTS last_status  VARCHAR(8) NOT NULL DEFAULT '';
ALTER TABLE nas ADD COLUMN IF NOT EXISTS last_checked DATETIME NULL;
ALTER TABLE net_devices ADD COLUMN IF NOT EXISTS nas_id INT UNSIGNED NULL;
ALTER TABLE net_devices ADD COLUMN IF NOT EXISTS created_by VARCHAR(64) NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS nas_groups (
  id   INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(64) NOT NULL UNIQUE,
  note VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS mac_vendors (
  prefix CHAR(6) NOT NULL PRIMARY KEY,
  vendor VARCHAR(128) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS onus (
  id       INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  olt_id   INT UNSIGNED NULL,
  name     VARCHAR(64) NOT NULL DEFAULT '',
  serial   VARCHAR(64) NOT NULL DEFAULT '',
  mac      VARCHAR(17) NOT NULL DEFAULT '',
  username VARCHAR(64) NOT NULL DEFAULT '',
  pon_port VARCHAR(32) NOT NULL DEFAULT '',
  rx_dbm   DECIMAL(5,2) NULL,
  status   VARCHAR(12) NOT NULL DEFAULT 'online',
  note     VARCHAR(255) NOT NULL DEFAULT '',
  KEY idx_olt (olt_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS olt_snmp (
  device_id   INT UNSIGNED NOT NULL PRIMARY KEY,
  vendor      VARCHAR(16) NOT NULL DEFAULT 'other',
  model       VARCHAR(64) NOT NULL DEFAULT '',
  pon_type    VARCHAR(10) NOT NULL DEFAULT 'EPON',
  version     VARCHAR(4)  NOT NULL DEFAULT 'v2c',
  community   VARCHAR(64) NOT NULL DEFAULT 'public',
  port        INT UNSIGNED NOT NULL DEFAULT 161,
  timeout     TINYINT UNSIGNED NOT NULL DEFAULT 5,
  retries     TINYINT UNSIGNED NOT NULL DEFAULT 3,
  auto_poll   TINYINT(1) NOT NULL DEFAULT 1,
  if_regex    VARCHAR(128) NOT NULL DEFAULT '',
  oid_status  VARCHAR(128) NOT NULL DEFAULT '',
  oid_name    VARCHAR(128) NOT NULL DEFAULT '',
  oid_serial  VARCHAR(128) NOT NULL DEFAULT '',
  oid_rx      VARCHAR(128) NOT NULL DEFAULT '',
  oid_mac     VARCHAR(128) NOT NULL DEFAULT '',
  status_map  VARCHAR(128) NOT NULL DEFAULT '',
  rx_div      INT UNSIGNED NOT NULL DEFAULT 1,
  last_poll   DATETIME NULL,
  last_ok     TINYINT(1) NOT NULL DEFAULT 0,
  last_msg    VARCHAR(255) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE onus ADD COLUMN IF NOT EXISTS onu_key   VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE onus ADD COLUMN IF NOT EXISTS last_seen DATETIME NULL;
ALTER TABLE onus ADD COLUMN IF NOT EXISTS source    VARCHAR(8) NOT NULL DEFAULT 'manual';
ALTER TABLE onus ADD COLUMN IF NOT EXISTS tx_dbm    DECIMAL(5,2) NULL;
ALTER TABLE onus ADD COLUMN IF NOT EXISTS temp_c    DECIMAL(4,1) NULL;
ALTER TABLE onus ADD COLUMN IF NOT EXISTS volt_v    DECIMAL(6,2) NULL;
ALTER TABLE onus ADD COLUMN IF NOT EXISTS curr_ma   DECIMAL(7,2) NULL;
ALTER TABLE onus ADD COLUMN IF NOT EXISTS dist_m    INT NULL;
ALTER TABLE olt_snmp ADD COLUMN IF NOT EXISTS oid_volt VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE olt_snmp ADD COLUMN IF NOT EXISTS oid_curr VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE olt_snmp ADD COLUMN IF NOT EXISTS oid_dist VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE olt_snmp ADD COLUMN IF NOT EXISTS volt_div INT UNSIGNED NOT NULL DEFAULT 1;
ALTER TABLE olt_snmp ADD COLUMN IF NOT EXISTS curr_div INT UNSIGNED NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS onu_metrics (
  onu_id  INT UNSIGNED NOT NULL,
  ts      DATETIME NOT NULL,
  rx_dbm  DECIMAL(5,2) NULL,
  tx_dbm  DECIMAL(5,2) NULL,
  temp_c  DECIMAL(4,1) NULL,
  KEY idx_onu_ts (onu_id, ts),
  KEY idx_ts (ts)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
ALTER TABLE olt_snmp ADD COLUMN IF NOT EXISTS oid_tx   VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE olt_snmp ADD COLUMN IF NOT EXISTS oid_temp VARCHAR(128) NOT NULL DEFAULT '';
ALTER TABLE olt_snmp ADD COLUMN IF NOT EXISTS temp_div INT UNSIGNED NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS notices (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  title      VARCHAR(160) NOT NULL DEFAULT '',
  body       TEXT NOT NULL,
  kind       VARCHAR(8) NOT NULL DEFAULT 'info',
  active     TINYINT(1) NOT NULL DEFAULT 1,
  starts     DATE NULL,
  ends       DATE NULL,
  created_by VARCHAR(64) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Partner / staff wallet balance and who created each user
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS balance    DECIMAL(12,2) NOT NULL DEFAULT 0;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS created_by VARCHAR(64) NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS user_ledger (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id       INT UNSIGNED NOT NULL,
  amount        DECIMAL(12,2) NOT NULL,
  balance_after DECIMAL(12,2) NOT NULL DEFAULT 0,
  method        VARCHAR(32) NOT NULL DEFAULT '',
  note          VARCHAR(255) NOT NULL DEFAULT '',
  created_by    VARCHAR(64) NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_user (user_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
ALTER TABLE user_ledger ADD COLUMN IF NOT EXISTS method VARCHAR(32) NOT NULL DEFAULT '';

-- Extra user details
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS sms_on    TINYINT(1)   NOT NULL DEFAULT 1;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS email_on  TINYINT(1)   NOT NULL DEFAULT 1;
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS identity  VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS country   VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS province  VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS city      VARCHAR(64)  NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS address   VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS photo     VARCHAR(8)   NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS inv_name    VARCHAR(100) NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS inv_address VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS inv_phone   VARCHAR(40)  NOT NULL DEFAULT '';
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS inv_footer  VARCHAR(255) NOT NULL DEFAULT '';

-- Per-user package pricing (what the partner pays, and the profit added on top)
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS max_children INT NULL;
ALTER TABLE user_plans ADD COLUMN IF NOT EXISTS price             DECIMAL(10,2) NULL;
ALTER TABLE user_plans ADD COLUMN IF NOT EXISTS sub_profit        DECIMAL(10,2) NOT NULL DEFAULT 0;
ALTER TABLE user_plans ADD COLUMN IF NOT EXISTS subreseller_profit DECIMAL(10,2) NOT NULL DEFAULT 0;

-- Profit report: who paid the package price for a payment, and how much (NULL = not a seller sale)
ALTER TABLE payments ADD COLUMN IF NOT EXISTS payer_id INT UNSIGNED NULL;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS cost DECIMAL(10,2) NULL;

-- Online balance top-ups (bKash)
CREATE TABLE IF NOT EXISTS topups (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  user_id     INT UNSIGNED NOT NULL,
  amount      DECIMAL(12,2) NOT NULL,
  fee         DECIMAL(12,2) NOT NULL DEFAULT 0,
  method      VARCHAR(16)  NOT NULL DEFAULT 'bkash',
  note        VARCHAR(255) NOT NULL DEFAULT '',
  token       VARCHAR(40)  NOT NULL,
  payment_id  VARCHAR(64)  NOT NULL DEFAULT '',
  trx_id      VARCHAR(64)  NOT NULL DEFAULT '',
  status      ENUM('pending','completed','failed','cancelled') NOT NULL DEFAULT 'pending',
  message     VARCHAR(255) NOT NULL DEFAULT '',
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_user (user_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- Customer portal: bKash payments started by customers themselves
CREATE TABLE IF NOT EXISTS portal_payments (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subscriber_id INT UNSIGNED NOT NULL,
  amount        DECIMAL(12,2) NOT NULL,
  fee           DECIMAL(12,2) NOT NULL DEFAULT 0,
  days          INT UNSIGNED NOT NULL DEFAULT 30,
  token         VARCHAR(40)  NOT NULL,
  payment_id    VARCHAR(64)  NOT NULL DEFAULT '',
  trx_id        VARCHAR(64)  NOT NULL DEFAULT '',
  status        ENUM('pending','completed','failed','cancelled') NOT NULL DEFAULT 'pending',
  message       VARCHAR(255) NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sub (subscriber_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Bandwidth throughput (v3 dashboard). maintenance.py fills these every 5 minutes from radacct counters.
-- traffic_last keeps the previous counter of each open session, so the next run can work out its rate.
CREATE TABLE IF NOT EXISTS traffic_snapshots (
  ts        DATETIME NOT NULL PRIMARY KEY,
  down_bps  BIGINT UNSIGNED NOT NULL DEFAULT 0,
  up_bps    BIGINT UNSIGNED NOT NULL DEFAULT 0,
  sessions  INT UNSIGNED NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS traffic_last (
  radacctid BIGINT UNSIGNED NOT NULL PRIMARY KEY,
  username  VARCHAR(64) NOT NULL DEFAULT '',
  in_oct    BIGINT UNSIGNED NOT NULL DEFAULT 0,
  out_oct   BIGINT UNSIGNED NOT NULL DEFAULT 0,
  upd       DATETIME NULL,
  down_bps  BIGINT UNSIGNED NOT NULL DEFAULT 0,
  up_bps    BIGINT UNSIGNED NOT NULL DEFAULT 0,
  KEY idx_user (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- NOC (Logs & System Health page). Filled by the isp-panel-noc service every 30 seconds.
-- Why a login was refused, the router it came from and the device MAC (FreeRADIUS post-auth query, see install.sh).
ALTER TABLE radpostauth ADD COLUMN IF NOT EXISTS reason VARCHAR(255) NOT NULL DEFAULT '';
ALTER TABLE radpostauth ADD COLUMN IF NOT EXISTS nas_ip VARCHAR(45)  NOT NULL DEFAULT '';
ALTER TABLE radpostauth ADD COLUMN IF NOT EXISTS mac    VARCHAR(64)  NOT NULL DEFAULT '';
-- SNMP read-only community per router (empty = use Settings default; both empty = ping only)
ALTER TABLE nas ADD COLUMN IF NOT EXISTS snmp_community VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE nas ADD COLUMN IF NOT EXISTS snmp_port      SMALLINT UNSIGNED NOT NULL DEFAULT 161;

CREATE TABLE IF NOT EXISTS nas_health (
  nas_id        INT UNSIGNED NOT NULL PRIMARY KEY,
  checked_at    DATETIME NULL,
  reachable     TINYINT(1) NOT NULL DEFAULT 0,
  rtt_ms        DECIMAL(8,1) NULL,
  snmp_ok       TINYINT(1) NOT NULL DEFAULT 0,
  cpu_pct       DECIMAL(5,1) NULL,
  ram_pct       DECIMAL(5,1) NULL,
  uptime_s      BIGINT UNSIGNED NULL,
  board         VARCHAR(160) NOT NULL DEFAULT '',
  pppoe         INT UNSIGNED NOT NULL DEFAULT 0,
  error         VARCHAR(255) NOT NULL DEFAULT '',
  fails         INT UNSIGNED NOT NULL DEFAULT 0,
  hot           INT UNSIGNED NOT NULL DEFAULT 0,
  down_alerted  TINYINT(1) NOT NULL DEFAULT 0,
  cpu_alerted   TINYINT(1) NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Disconnect requests the panel asks for (for example after renewing an expired customer, so the
-- customer reconnects with the full package). The isp-panel-noc service sends them within 30 seconds.
CREATE TABLE IF NOT EXISTS coa_queue (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  username   VARCHAR(64) NOT NULL,
  reason     VARCHAR(32) NOT NULL DEFAULT '',
  created_by VARCHAR(64) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_user (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- bKash: every request and answer to the payment gateway (secrets and tokens removed), for audits and disputes.
CREATE TABLE IF NOT EXISTS gateway_logs (
  id           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  gateway      VARCHAR(16)  NOT NULL DEFAULT 'bkash',
  flow         VARCHAR(16)  NOT NULL DEFAULT '',
  ref_id       INT UNSIGNED NOT NULL DEFAULT 0,
  payment_id   VARCHAR(64)  NOT NULL DEFAULT '',
  step         VARCHAR(32)  NOT NULL DEFAULT '',
  http_status  SMALLINT UNSIGNED NULL,
  ms           INT UNSIGNED NOT NULL DEFAULT 0,
  ok           TINYINT(1)   NOT NULL DEFAULT 0,
  request      TEXT NULL,
  response     TEXT NULL,
  error        VARCHAR(255) NOT NULL DEFAULT '',
  KEY idx_created (created_at),
  KEY idx_payment (payment_id),
  KEY idx_ref (flow, ref_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- bKash: when a pending payment was last confirmed with bKash, and the payment (receipt) a portal renewal made
ALTER TABLE portal_payments ADD COLUMN IF NOT EXISTS checked_at DATETIME NULL;
ALTER TABLE portal_payments ADD COLUMN IF NOT EXISTS payment_ref INT UNSIGNED NULL;
ALTER TABLE topups ADD COLUMN IF NOT EXISTS checked_at DATETIME NULL;
ALTER TABLE portal_payments ADD KEY IF NOT EXISTS idx_pending (status, created_at);
ALTER TABLE topups ADD KEY IF NOT EXISTS idx_pending (status, created_at);

-- Expiry time per subscriber ("HH:MM", NULL = the package's fixed time, else the end of the day). Activation sets it to
-- the time of activation, so a 30-day package bought at 15:30 ends at 15:30.
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS expiry_time VARCHAR(5) NULL;

-- Ledger Reports read these by date
ALTER TABLE user_ledger ADD KEY IF NOT EXISTS idx_created (created_at);
ALTER TABLE sub_ledger ADD KEY IF NOT EXISTS idx_created (created_at);

-- bKash Payments report reads these by date
ALTER TABLE topups ADD KEY IF NOT EXISTS idx_created (created_at);
ALTER TABLE portal_payments ADD KEY IF NOT EXISTS idx_created (created_at);

-- bulk app notifications (Notifications > Send): one batch id per send, for the history and "take back"
ALTER TABLE sub_notifications ADD COLUMN IF NOT EXISTS batch VARCHAR(12) NULL;
ALTER TABLE sub_notifications ADD KEY IF NOT EXISTS idx_batch (batch);

-- New PPPoE requests (Network > CoA Request Log): FreeRADIUS writes the attempted password (PAP only); keep it only
-- when the login was refused for a username that is not a customer. Accepted logins and existing customers' wrong
-- passwords are never stored (every customer has radcheck rows; radcheck and radpostauth share FreeRADIUS's
-- collation, the panel tables may not yet on a fresh install). maintenance.py wipes the kept ones after 3 days.
DROP TRIGGER IF EXISTS radpostauth_pass;
CREATE TRIGGER radpostauth_pass BEFORE INSERT ON radpostauth FOR EACH ROW SET NEW.pass = IF(NEW.reply = 'Access-Reject' AND NEW.username <> '' AND NOT EXISTS (SELECT 1 FROM radcheck WHERE username = NEW.username), LEFT(NEW.pass, 64), '');
UPDATE radpostauth p SET p.pass = '' WHERE p.pass <> '' AND (p.reply <> 'Access-Reject' OR EXISTS (SELECT 1 FROM radcheck rc WHERE rc.username = p.username));

-- the reseller a router belongs to (NAS > Edit > Reseller): they and their downline see its New PPPoE requests
ALTER TABLE nas ADD COLUMN IF NOT EXISTS seller VARCHAR(64) NOT NULL DEFAULT '';

-- bKash SMS auto-renew (Settings > bKash SMS): the "You have received ..." SMS an Android phone forwards to /sms-in;
-- each TrxID is used once
CREATE TABLE IF NOT EXISTS sms_payments (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  trx_id        VARCHAR(32)  NOT NULL,
  amount        DECIMAL(10,2) NOT NULL,
  payer         VARCHAR(32)  NOT NULL DEFAULT '',
  ref           VARCHAR(64)  NOT NULL DEFAULT '',
  sms_text      VARCHAR(500) NOT NULL DEFAULT '',
  paid_at       DATETIME NULL,
  subscriber_id INT UNSIGNED NULL,
  username      VARCHAR(64)  NOT NULL DEFAULT '',
  status        VARCHAR(16)  NOT NULL DEFAULT 'unmatched',
  message       VARCHAR(255) NOT NULL DEFAULT '',
  payment_id    INT UNSIGNED NULL,
  done_by       VARCHAR(64)  NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_trx (trx_id),
  KEY idx_status (status, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- where the SMS came from: the phone's forwarder app, or the USB GSM modem on the server (isp-panel-gsm)
ALTER TABLE sms_payments ADD COLUMN IF NOT EXISTS source VARCHAR(8) NOT NULL DEFAULT 'phone';

-- every SMS the USB GSM modem receives (bKash OTPs too), for the admin in Settings > bKash SMS; kept 7 days
CREATE TABLE IF NOT EXISTS gsm_inbox (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  sender      VARCHAR(64)  NOT NULL DEFAULT '',
  text        TEXT NOT NULL,
  sms_time    DATETIME NULL,
  result      VARCHAR(64)  NOT NULL DEFAULT '',
  received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_received (received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Accounting > Cashflow: the company's own income and expenses in one book. The expenses table holds both (kind says
-- which; rows from before Cashflow are expenses). profile = the panel user an entry is about (optional).
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS kind ENUM('income','expense') NOT NULL DEFAULT 'expense';
ALTER TABLE expenses ADD COLUMN IF NOT EXISTS profile VARCHAR(64) NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS cashflow_categories (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  kind       ENUM('income','expense') NOT NULL DEFAULT 'expense',
  name       VARCHAR(64)  NOT NULL,
  note       VARCHAR(255) NOT NULL DEFAULT '',
  created_by VARCHAR(64)  NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_kind_name (kind, name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
-- the starting list, once (a category deleted later does not come back on the next update)
INSERT INTO cashflow_categories (kind, name)
  SELECT d.kind, d.name FROM (
    SELECT 'income' AS kind, 'User Payment' AS name UNION ALL SELECT 'income', 'Installation fee'
    UNION ALL SELECT 'income', 'Device sale' UNION ALL SELECT 'income', 'Other income'
    UNION ALL SELECT 'expense', 'Bandwidth / upstream' UNION ALL SELECT 'expense', 'Salary'
    UNION ALL SELECT 'expense', 'Electricity' UNION ALL SELECT 'expense', 'Rent' UNION ALL SELECT 'expense', 'Equipment'
    UNION ALL SELECT 'expense', 'Maintenance' UNION ALL SELECT 'expense', 'Transport' UNION ALL SELECT 'expense', 'Marketing'
    UNION ALL SELECT 'expense', 'Tax / license' UNION ALL SELECT 'expense', 'Other') d
  WHERE NOT EXISTS (SELECT 1 FROM cashflow_categories);
-- categories already used by entries are on the list
INSERT IGNORE INTO cashflow_categories (kind, name) SELECT DISTINCT kind, category FROM expenses WHERE category <> '';

-- Cashflow > fix a wrong recharge: a Correction line (method 'correction') points at the recharge it corrects
ALTER TABLE user_ledger ADD COLUMN IF NOT EXISTS ref_id INT UNSIGNED NULL;
ALTER TABLE user_ledger ADD INDEX IF NOT EXISTS idx_ref (ref_id);

-- Call log: what was said to a customer, and when to call again (subscriber page > Call Log, Subscribers > Follow-ups)
CREATE TABLE IF NOT EXISTS sub_calls (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subscriber_id INT UNSIGNED NOT NULL,
  outcome       VARCHAR(16)  NOT NULL DEFAULT 'called',
  note          VARCHAR(500) NOT NULL DEFAULT '',
  follow_up     DATE NULL,
  done          TINYINT(1)   NOT NULL DEFAULT 0,
  created_by    VARCHAR(64)  NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_sub (subscriber_id, id),
  KEY idx_follow (done, follow_up)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Customer map: where the connection is (set on the subscriber page, shown on Subscribers > Map)
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS lat DECIMAL(9,6) NULL;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS lng DECIMAL(9,6) NULL;

-- Technician jobs: a photo of the finished work on the ticket reply that closed it
ALTER TABLE ticket_replies ADD COLUMN IF NOT EXISTS photo VARCHAR(64) NOT NULL DEFAULT '';

-- Tickets v2: time limit (SLA) and alerts, who owns it (office / reseller), the technician's visit (arrived,
-- materials, the customer's code), the customer's rating, ready replies, area technicians and area outages
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS due_at DATETIME NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS first_reply_at DATETIME NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS alerted TINYINT UNSIGNED NOT NULL DEFAULT 0;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'office';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'customer';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS seller VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS to_office TINYINT NOT NULL DEFAULT 1;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS arrived_at DATETIME NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS arrived_lat DECIMAL(9,6) NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS arrived_lng DECIMAL(9,6) NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS otp_hash VARCHAR(96) NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS otp_sent_at DATETIME NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS otp_tries TINYINT UNSIGNED NOT NULL DEFAULT 0;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS otp_ok TINYINT NOT NULL DEFAULT 0;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS rating TINYINT NULL;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS rating_note VARCHAR(500) NOT NULL DEFAULT '';
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS rated_at DATETIME NULL;
ALTER TABLE tickets ADD INDEX IF NOT EXISTS idx_due (status, due_at);
ALTER TABLE tickets ADD INDEX IF NOT EXISTS idx_seller (seller, status);

CREATE TABLE IF NOT EXISTS ticket_canned (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  title      VARCHAR(80)  NOT NULL,
  body       TEXT         NOT NULL,
  created_by VARCHAR(64)  NOT NULL DEFAULT '',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS ticket_area_techs (
  area     VARCHAR(64)  NOT NULL,
  admin_id INT UNSIGNED NOT NULL,
  PRIMARY KEY (area, admin_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ticket_materials (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  ticket_id  INT UNSIGNED NOT NULL,
  item_id    INT UNSIGNED NOT NULL,
  qty        INT NOT NULL,
  by_user    VARCHAR(64) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_ticket (ticket_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS outages (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  area       VARCHAR(64)  NOT NULL DEFAULT '',
  message    VARCHAR(500) NOT NULL,
  ends_at    DATETIME NULL,
  active     TINYINT NOT NULL DEFAULT 1,
  sms_sent   INT NOT NULL DEFAULT 0,
  created_by VARCHAR(64) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  closed_at  DATETIME NULL,
  KEY idx_active (active, area)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
-- which reseller each existing ticket belongs to (idempotent: only rows still empty)
UPDATE tickets t JOIN subscribers s ON s.id = t.subscriber_id JOIN panel_admins a ON a.username = s.created_by
   AND a.role IN ('reseller', 'subreseller', 'retailer') SET t.seller = s.created_by WHERE t.seller = '';
UPDATE tickets t JOIN panel_admins a ON a.username = t.created_by AND a.role IN ('reseller', 'subreseller', 'retailer')
   SET t.seller = t.created_by WHERE t.seller = '' AND t.subscriber_id IS NULL;

-- Staff app notifications (Web Push): a technician's phone rings when a job is given to them
CREATE TABLE IF NOT EXISTS staff_push_subs (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  admin_id      INT UNSIGNED NOT NULL,
  endpoint_hash CHAR(40)      NOT NULL,
  endpoint      VARCHAR(1000) NOT NULL,
  p256dh        VARCHAR(255)  NOT NULL,
  auth          VARCHAR(64)   NOT NULL,
  user_agent    VARCHAR(255)  NOT NULL DEFAULT '',
  created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_ok       DATETIME NULL,
  fails         INT UNSIGNED NOT NULL DEFAULT 0,
  UNIQUE KEY uq_endpoint (endpoint_hash),
  KEY idx_admin (admin_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS staff_notifications (
  id         INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  admin_id   INT UNSIGNED NOT NULL,
  title      VARCHAR(120) NOT NULL,
  body       VARCHAR(600) NOT NULL DEFAULT '',
  link       VARCHAR(255) NOT NULL DEFAULT '',
  dedupe     VARCHAR(120) NULL,
  urgent     TINYINT NOT NULL DEFAULT 0,
  pushed_at  DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_dedupe (dedupe),
  KEY idx_push (pushed_at, created_at),
  KEY idx_admin (admin_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Closed tickets are removed on the 1st of each month (Tickets > Settings); before that their numbers are added here,
-- one row per month (the month the ticket was closed) and per total / person / area / category
CREATE TABLE IF NOT EXISTS ticket_month_stats (
  month      CHAR(7)      NOT NULL,
  kind       VARCHAR(8)   NOT NULL,
  ref        VARCHAR(64)  NOT NULL DEFAULT '',
  name       VARCHAR(128) NOT NULL DEFAULT '',
  n          INT NOT NULL DEFAULT 0,
  fix_sum    BIGINT NOT NULL DEFAULT 0,
  fix_n      INT NOT NULL DEFAULT 0,
  reply_sum  BIGINT NOT NULL DEFAULT 0,
  reply_n    INT NOT NULL DEFAULT 0,
  met        INT NOT NULL DEFAULT 0,
  timed      INT NOT NULL DEFAULT 0,
  rating_sum INT NOT NULL DEFAULT 0,
  rating_n   INT NOT NULL DEFAULT 0,
  portal     INT NOT NULL DEFAULT 0,
  visits     INT NOT NULL DEFAULT 0,
  PRIMARY KEY (month, kind, ref)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Revert Last Invoice: what a renewal changed, so it can be put back
ALTER TABLE payments ADD COLUMN IF NOT EXISTS prev_expiry DATE NULL;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS prev_expiry_time VARCHAR(5) NULL;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS prev_status VARCHAR(16) NULL;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS from_balance DECIMAL(10,2) NOT NULL DEFAULT 0;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS prev_plan_id INT UNSIGNED NULL;

-- Login Link: a one-time sign-in link to the customer portal (only its SHA-256 is kept)
CREATE TABLE IF NOT EXISTS portal_login_links (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  subscriber_id INT UNSIGNED NOT NULL,
  token_hash    CHAR(64)    NOT NULL,
  expires_at    DATETIME    NOT NULL,
  used_at       DATETIME    NULL,
  created_by    VARCHAR(64) NOT NULL DEFAULT '',
  created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_token (token_hash),
  KEY idx_sub (subscriber_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Subscriber Limitation: the most customers a reseller / sub-reseller / retailer may have (NULL = no limit)
ALTER TABLE panel_admins ADD COLUMN IF NOT EXISTS max_subs INT UNSIGNED NULL;

-- Technician live location: the newest position each technician's phone sent (only while the panel app is open)
CREATE TABLE IF NOT EXISTS staff_locations (
  admin_id   INT UNSIGNED NOT NULL PRIMARY KEY,
  lat        DECIMAL(9,6) NOT NULL,
  lng        DECIMAL(9,6) NOT NULL,
  accuracy   INT UNSIGNED NULL,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
