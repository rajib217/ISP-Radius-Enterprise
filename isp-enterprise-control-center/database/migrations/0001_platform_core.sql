-- ISP Enterprise Control Center
-- Migration 0001: multi-tenant platform core
-- PostgreSQL 16+. Run as a dedicated migration owner, never as the runtime app role.
BEGIN;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;

-- Provisioned as NOLOGIN group roles. Grant these to separately managed LOGIN roles.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='isp_app') THEN CREATE ROLE isp_app NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='isp_radius') THEN CREATE ROLE isp_radius NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='isp_auth') THEN CREATE ROLE isp_auth NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='isp_radius_lookup') THEN CREATE ROLE isp_radius_lookup NOLOGIN; END IF;
END $$;

CREATE SCHEMA IF NOT EXISTS platform;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS accounting;
CREATE SCHEMA IF NOT EXISTS radius;
CREATE SCHEMA IF NOT EXISTS network;
CREATE SCHEMA IF NOT EXISTS integration;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE platform.tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_key citext NOT NULL UNIQUE,
  legal_name text NOT NULL,
  display_name text NOT NULL,
  status text NOT NULL DEFAULT 'trial' CHECK (status IN ('trial','active','suspended','closed')),
  default_currency char(3) NOT NULL DEFAULT 'BDT',
  timezone text NOT NULL DEFAULT 'Asia/Dhaka',
  country_code char(2) NOT NULL DEFAULT 'BD',
  data_region text NOT NULL DEFAULT 'bd',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE platform.users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email citext NOT NULL UNIQUE,
  password_hash text NOT NULL,
  display_name text NOT NULL,
  is_platform_operator boolean NOT NULL DEFAULT false,
  mfa_enrolled_at timestamptz,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('invited','active','locked','disabled')),
  last_login_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE platform.tenant_memberships (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  user_id uuid NOT NULL REFERENCES platform.users(id) ON DELETE RESTRICT,
  role_key text NOT NULL CHECK (role_key IN ('tenant_owner','tenant_admin','noc_engineer','finance_manager','finance_operator','support_agent','reseller_manager','readonly')),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('invited','active','suspended')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,user_id)
);
CREATE INDEX ON platform.tenant_memberships(user_id,status);

CREATE TABLE platform.service_zones (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  parent_zone_id uuid,
  code text NOT NULL,
  name text NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,code),
  FOREIGN KEY (tenant_id,parent_zone_id) REFERENCES platform.service_zones(tenant_id,id)
);

CREATE TABLE platform.resellers (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  reseller_code text NOT NULL,
  name text NOT NULL,
  contact_name text,
  mobile_e164 text,
  commission_type text NOT NULL DEFAULT 'none' CHECK (commission_type IN ('none','fixed','percentage')),
  commission_value numeric(12,4) NOT NULL DEFAULT 0 CHECK (commission_value >= 0),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','closed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,reseller_code)
);

CREATE TABLE platform.subscribers (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  customer_code text NOT NULL,
  full_name text NOT NULL,
  mobile_e164 text,
  email citext,
  zone_id uuid,
  reseller_id uuid,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','grace','suspended','terminated')),
  external_legacy_id text,
  notes text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,customer_code),
  UNIQUE (tenant_id,external_legacy_id),
  FOREIGN KEY (tenant_id,zone_id) REFERENCES platform.service_zones(tenant_id,id),
  FOREIGN KEY (tenant_id,reseller_id) REFERENCES platform.resellers(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX subscribers_tenant_status_idx ON platform.subscribers(tenant_id,status);
CREATE INDEX subscribers_tenant_mobile_idx ON platform.subscribers(tenant_id,mobile_e164);

CREATE TABLE platform.subscriber_addresses (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  subscriber_id uuid NOT NULL,
  address_type text NOT NULL DEFAULT 'service' CHECK (address_type IN ('billing','service','other')),
  address_line text NOT NULL,
  area text,
  city text,
  postal_code text,
  country_code char(2) NOT NULL DEFAULT 'BD',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  FOREIGN KEY (tenant_id,subscriber_id) REFERENCES platform.subscribers(tenant_id,id) ON DELETE RESTRICT
);

CREATE TABLE platform.packages (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  code text NOT NULL,
  name text NOT NULL,
  description text,
  price_minor bigint NOT NULL CHECK (price_minor >= 0),
  currency char(3) NOT NULL DEFAULT 'BDT',
  billing_period_days integer NOT NULL DEFAULT 30 CHECK (billing_period_days > 0),
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,code)
);

CREATE TABLE platform.package_profiles (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  package_id uuid NOT NULL,
  access_type text NOT NULL CHECK (access_type IN ('pppoe','hotspot','ipoe')),
  download_kbps integer CHECK (download_kbps > 0),
  upload_kbps integer CHECK (upload_kbps > 0),
  simultaneous_use integer NOT NULL DEFAULT 1 CHECK (simultaneous_use > 0),
  ipv4_pool_id uuid,
  ipv6_pool_id uuid,
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,package_id,access_type),
  FOREIGN KEY (tenant_id,package_id) REFERENCES platform.packages(tenant_id,id) ON DELETE RESTRICT
);

CREATE TABLE platform.subscriber_services (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  subscriber_id uuid NOT NULL,
  package_profile_id uuid NOT NULL,
  service_type text NOT NULL CHECK (service_type IN ('pppoe','hotspot','ipoe')),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','grace','suspended','terminated')),
  starts_at timestamptz,
  expires_at timestamptz,
  external_legacy_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,external_legacy_id),
  FOREIGN KEY (tenant_id,subscriber_id) REFERENCES platform.subscribers(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,package_profile_id) REFERENCES platform.package_profiles(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX subscriber_services_status_expiry_idx ON platform.subscriber_services(tenant_id,status,expires_at);

CREATE TABLE network.nas_devices (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  name text NOT NULL,
  vendor text NOT NULL CHECK (vendor IN ('juniper','mikrotik','huawei','other')),
  model text,
  nas_identifier text NOT NULL,
  source_ip inet NOT NULL,
  coa_port integer NOT NULL DEFAULT 3799 CHECK (coa_port BETWEEN 1 AND 65535),
  secret_ref text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','maintenance')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,nas_identifier),
  UNIQUE (source_ip),
  UNIQUE (tenant_id,source_ip)
);
CREATE INDEX nas_source_ip_idx ON network.nas_devices(source_ip);

CREATE TABLE network.olt_devices (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  name text NOT NULL,
  vendor text NOT NULL CHECK (vendor IN ('huawei','zte','fiberhome','other')),
  model text,
  management_ip inet NOT NULL,
  software_version text,
  management_protocol text NOT NULL DEFAULT 'ssh_snmp' CHECK (management_protocol IN ('ssh','snmp','ssh_snmp')),
  credential_secret_ref text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','maintenance')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,management_ip)
);
CREATE TABLE network.pon_ports (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  olt_id uuid NOT NULL,
  frame_number integer NOT NULL DEFAULT 0,
  slot_number integer NOT NULL,
  port_number integer NOT NULL,
  port_name text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled','fault')),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,olt_id,frame_number,slot_number,port_number),
  FOREIGN KEY (tenant_id,olt_id) REFERENCES network.olt_devices(tenant_id,id) ON DELETE RESTRICT
);
CREATE TABLE network.onus (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  pon_port_id uuid NOT NULL,
  subscriber_service_id uuid,
  serial_number text NOT NULL,
  onu_number integer,
  vendor text,
  model text,
  status text NOT NULL DEFAULT 'unknown' CHECK (status IN ('online','offline','los','unknown','disabled')),
  rx_power_dbm numeric(7,3),
  last_seen_at timestamptz,
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,serial_number),
  FOREIGN KEY (tenant_id,pon_port_id) REFERENCES network.pon_ports(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,subscriber_service_id) REFERENCES platform.subscriber_services(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX onus_service_idx ON network.onus(tenant_id,subscriber_service_id);

CREATE TABLE network.ip_pools (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  name text NOT NULL,
  network cidr NOT NULL,
  gateway inet,
  is_active boolean NOT NULL DEFAULT true,
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,name),
  CHECK (family(network) IN (4,6)),
  CHECK (gateway IS NULL OR family(gateway) = family(network))
);

CREATE TABLE network.ip_assignments (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  subscriber_service_id uuid NOT NULL,
  pool_id uuid,
  address inet NOT NULL,
  status text NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','active','released')),
  allocated_at timestamptz NOT NULL DEFAULT now(),
  released_at timestamptz,
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,address),
  FOREIGN KEY (tenant_id,subscriber_service_id) REFERENCES platform.subscriber_services(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,pool_id) REFERENCES network.ip_pools(tenant_id,id) ON DELETE RESTRICT
);

-- RADIUS identity is tenant-scoped. The canonical auth_username should be globally unique
-- (recommended format: local-user@tenant-realm) to work with stock RADIUS username lookups.
CREATE TABLE radius.identities (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  subscriber_id uuid NOT NULL,
  subscriber_service_id uuid NOT NULL,
  auth_username citext NOT NULL UNIQUE,
  local_username citext NOT NULL,
  realm citext NOT NULL,
  credential_kind text NOT NULL CHECK (credential_kind IN ('cleartext_for_pap','nt_password_hash','external')),
  credential_ciphertext bytea,
  external_secret_ref text,
  enabled boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,local_username,realm),
  UNIQUE (tenant_id,auth_username),
  FOREIGN KEY (tenant_id,subscriber_id) REFERENCES platform.subscribers(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,subscriber_service_id) REFERENCES platform.subscriber_services(tenant_id,id) ON DELETE RESTRICT,
  CHECK ((credential_ciphertext IS NOT NULL) OR (external_secret_ref IS NOT NULL))
);
CREATE INDEX radius_identity_tenant_enabled_idx ON radius.identities(tenant_id,enabled);

-- Tenant-scoped FreeRADIUS SQL tables. Custom queries must resolve tenant by NAS source IP
-- and auth_username; never trust a tenant_id supplied by the RADIUS username alone.
CREATE TABLE radius.radcheck (
  id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  username citext NOT NULL,
  attribute varchar(64) NOT NULL,
  op char(2) NOT NULL DEFAULT ':=',
  value varchar(253) NOT NULL,
  FOREIGN KEY (tenant_id,username) REFERENCES radius.identities(tenant_id,auth_username) ON DELETE CASCADE
);
CREATE INDEX radcheck_user_idx ON radius.radcheck(tenant_id,username);
CREATE TABLE radius.radreply (
  id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  username citext NOT NULL,
  attribute varchar(64) NOT NULL,
  op char(2) NOT NULL DEFAULT '=',
  value varchar(253) NOT NULL,
  FOREIGN KEY (tenant_id,username) REFERENCES radius.identities(tenant_id,auth_username) ON DELETE CASCADE
);
CREATE INDEX radreply_user_idx ON radius.radreply(tenant_id,username);
CREATE TABLE radius.radusergroup (
  id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  username citext NOT NULL,
  groupname varchar(64) NOT NULL,
  priority integer NOT NULL DEFAULT 1,
  FOREIGN KEY (tenant_id,username) REFERENCES radius.identities(tenant_id,auth_username) ON DELETE CASCADE
);
CREATE INDEX radusergroup_user_idx ON radius.radusergroup(tenant_id,username);
CREATE TABLE radius.radacct (
  radacctid bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  acctsessionid varchar(64) NOT NULL,
  acctuniqueid varchar(32) NOT NULL,
  username citext NOT NULL,
  realm citext,
  nasipaddress inet NOT NULL,
  nasportid varchar(50),
  acctstarttime timestamptz,
  acctupdatetime timestamptz,
  acctstoptime timestamptz,
  acctinterval bigint,
  acctsessiontime bigint,
  acctauthentic varchar(32),
  connectinfo_start varchar(128),
  connectinfo_stop varchar(128),
  acctinputoctets bigint NOT NULL DEFAULT 0,
  acctoutputoctets bigint NOT NULL DEFAULT 0,
  calledstationid varchar(50),
  callingstationid varchar(50),
  servicetype varchar(32),
  framedprotocol varchar(32),
  framedipaddress inet,
  framedipv6prefix cidr,
  framedinterfaceid varchar(64),
  delegatedipv6prefix cidr,
  FOREIGN KEY (tenant_id,username) REFERENCES radius.identities(tenant_id,auth_username) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,nasipaddress) REFERENCES network.nas_devices(tenant_id,source_ip) ON DELETE RESTRICT,
  UNIQUE (tenant_id,acctuniqueid)
);
CREATE INDEX radacct_active_idx ON radius.radacct(tenant_id,acctstoptime) WHERE acctstoptime IS NULL;
CREATE INDEX radacct_user_start_idx ON radius.radacct(tenant_id,username,acctstarttime DESC);
CREATE INDEX radacct_nas_start_idx ON radius.radacct(tenant_id,nasipaddress,acctstarttime DESC);
CREATE TABLE radius.radpostauth (
  id bigserial PRIMARY KEY,
  tenant_id uuid NOT NULL,
  username citext NOT NULL,
  reply varchar(32),
  authdate timestamptz NOT NULL DEFAULT now(),
  -- No identity FK: failed attempts for unknown usernames must still be auditable.
  FOREIGN KEY (tenant_id) REFERENCES platform.tenants(id) ON DELETE RESTRICT
);
CREATE INDEX radpostauth_tenant_date_idx ON radius.radpostauth(tenant_id,authdate DESC);

CREATE TABLE billing.invoices (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  subscriber_id uuid NOT NULL,
  invoice_number text NOT NULL,
  currency char(3) NOT NULL DEFAULT 'BDT',
  issue_date date NOT NULL,
  due_date date NOT NULL,
  subtotal_minor bigint NOT NULL CHECK (subtotal_minor >= 0),
  discount_minor bigint NOT NULL DEFAULT 0 CHECK (discount_minor >= 0),
  tax_minor bigint NOT NULL DEFAULT 0 CHECK (tax_minor >= 0),
  total_minor bigint NOT NULL CHECK (total_minor >= 0),
  paid_minor bigint NOT NULL DEFAULT 0 CHECK (paid_minor >= 0),
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','issued','partially_paid','paid','void','overdue')),
  issued_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,invoice_number),
  FOREIGN KEY (tenant_id,subscriber_id) REFERENCES platform.subscribers(tenant_id,id) ON DELETE RESTRICT,
  CHECK (paid_minor <= total_minor)
);
CREATE INDEX invoices_due_idx ON billing.invoices(tenant_id,status,due_date);
CREATE TABLE billing.invoice_items (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL,
  description text NOT NULL,
  quantity numeric(12,3) NOT NULL DEFAULT 1 CHECK (quantity > 0),
  unit_amount_minor bigint NOT NULL CHECK (unit_amount_minor >= 0),
  line_total_minor bigint NOT NULL CHECK (line_total_minor >= 0),
  PRIMARY KEY (tenant_id,id),
  FOREIGN KEY (tenant_id,invoice_id) REFERENCES billing.invoices(tenant_id,id) ON DELETE RESTRICT
);

CREATE TABLE billing.payment_gateways (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  provider text NOT NULL CHECK (provider IN ('bkash','nagad','rocket','bank_card','aggregator','other')),
  display_name text NOT NULL,
  credential_secret_ref text NOT NULL,
  environment text NOT NULL DEFAULT 'sandbox' CHECK (environment IN ('sandbox','production')),
  is_enabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,provider,display_name)
);
CREATE TABLE billing.payments (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  subscriber_id uuid NOT NULL,
  gateway_id uuid,
  amount_minor bigint NOT NULL CHECK (amount_minor > 0),
  currency char(3) NOT NULL DEFAULT 'BDT',
  status text NOT NULL DEFAULT 'initiated' CHECK (status IN ('initiated','pending','succeeded','failed','refunded','reversed')),
  provider_transaction_id text,
  idempotency_key text NOT NULL,
  initiated_at timestamptz NOT NULL DEFAULT now(),
  confirmed_at timestamptz,
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,idempotency_key),
  UNIQUE (tenant_id,gateway_id,provider_transaction_id),
  FOREIGN KEY (tenant_id,subscriber_id) REFERENCES platform.subscribers(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,gateway_id) REFERENCES billing.payment_gateways(tenant_id,id) ON DELETE RESTRICT
);
CREATE INDEX payments_subscriber_time_idx ON billing.payments(tenant_id,subscriber_id,initiated_at DESC);
CREATE TABLE billing.payment_allocations (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  payment_id uuid NOT NULL,
  invoice_id uuid NOT NULL,
  amount_minor bigint NOT NULL CHECK (amount_minor > 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  FOREIGN KEY (tenant_id,payment_id) REFERENCES billing.payments(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,invoice_id) REFERENCES billing.invoices(tenant_id,id) ON DELETE RESTRICT
);
CREATE TABLE billing.gateway_events (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  gateway_id uuid NOT NULL,
  provider_event_id text NOT NULL,
  signature_valid boolean NOT NULL DEFAULT false,
  payload_redacted jsonb NOT NULL,
  processing_status text NOT NULL DEFAULT 'received' CHECK (processing_status IN ('received','processing','processed','rejected','failed')),
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,gateway_id,provider_event_id),
  FOREIGN KEY (tenant_id,gateway_id) REFERENCES billing.payment_gateways(tenant_id,id) ON DELETE RESTRICT
);

CREATE TABLE accounting.fiscal_periods (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  name text NOT NULL,
  starts_on date NOT NULL,
  ends_on date NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','locked')),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,name),
  CHECK (ends_on >= starts_on)
);
CREATE TABLE accounting.accounts (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  account_code text NOT NULL,
  name text NOT NULL,
  account_type text NOT NULL CHECK (account_type IN ('asset','liability','equity','income','expense')),
  parent_account_id uuid,
  is_control_account boolean NOT NULL DEFAULT false,
  is_active boolean NOT NULL DEFAULT true,
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,account_code),
  FOREIGN KEY (tenant_id,parent_account_id) REFERENCES accounting.accounts(tenant_id,id) ON DELETE RESTRICT
);
CREATE TABLE accounting.journal_entries (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  entry_number text NOT NULL,
  fiscal_period_id uuid NOT NULL,
  entry_date date NOT NULL,
  source_type text NOT NULL,
  source_id uuid,
  description text NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted','reversed')),
  idempotency_key text NOT NULL,
  posted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,entry_number),
  UNIQUE (tenant_id,idempotency_key),
  FOREIGN KEY (tenant_id,fiscal_period_id) REFERENCES accounting.fiscal_periods(tenant_id,id) ON DELETE RESTRICT
);
CREATE TABLE accounting.journal_lines (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  journal_entry_id uuid NOT NULL,
  account_id uuid NOT NULL,
  description text,
  debit_minor bigint NOT NULL DEFAULT 0 CHECK (debit_minor >= 0),
  credit_minor bigint NOT NULL DEFAULT 0 CHECK (credit_minor >= 0),
  currency char(3) NOT NULL DEFAULT 'BDT',
  PRIMARY KEY (tenant_id,id),
  FOREIGN KEY (tenant_id,journal_entry_id) REFERENCES accounting.journal_entries(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,account_id) REFERENCES accounting.accounts(tenant_id,id) ON DELETE RESTRICT,
  CHECK ((debit_minor > 0 AND credit_minor = 0) OR (credit_minor > 0 AND debit_minor = 0))
);
CREATE INDEX journal_lines_account_idx ON accounting.journal_lines(tenant_id,account_id);

CREATE TABLE integration.api_clients (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  name text NOT NULL,
  client_id text NOT NULL UNIQUE,
  client_secret_hash text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','disabled')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  UNIQUE (tenant_id,name)
);
CREATE TABLE integration.api_client_scopes (
  tenant_id uuid NOT NULL,
  api_client_id uuid NOT NULL,
  scope text NOT NULL,
  PRIMARY KEY (tenant_id,api_client_id,scope),
  FOREIGN KEY (tenant_id,api_client_id) REFERENCES integration.api_clients(tenant_id,id) ON DELETE CASCADE
);
CREATE TABLE integration.webhooks (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  api_client_id uuid NOT NULL,
  target_url text NOT NULL,
  signing_secret_ref text NOT NULL,
  event_types text[] NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  FOREIGN KEY (tenant_id,api_client_id) REFERENCES integration.api_clients(tenant_id,id) ON DELETE RESTRICT
);
CREATE TABLE integration.webhook_deliveries (
  tenant_id uuid NOT NULL,
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  webhook_id uuid NOT NULL,
  event_id uuid NOT NULL,
  attempt integer NOT NULL DEFAULT 0,
  response_code integer,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','failed')),
  next_attempt_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,id),
  FOREIGN KEY (tenant_id,webhook_id) REFERENCES integration.webhooks(tenant_id,id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id,event_id) REFERENCES platform.outbox_events(tenant_id,id) ON DELETE RESTRICT
);

CREATE TABLE analytics.daily_business_metrics (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  metric_date date NOT NULL,
  scope_key text NOT NULL DEFAULT '*',
  active_subscribers integer NOT NULL DEFAULT 0,
  new_subscribers integer NOT NULL DEFAULT 0,
  churned_subscribers integer NOT NULL DEFAULT 0,
  mrr_minor bigint NOT NULL DEFAULT 0,
  collection_minor bigint NOT NULL DEFAULT 0,
  outstanding_minor bigint NOT NULL DEFAULT 0,
  refreshed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,metric_date,scope_key)
);
CREATE TABLE analytics.daily_financial_metrics (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  metric_date date NOT NULL,
  currency char(3) NOT NULL,
  invoiced_minor bigint NOT NULL DEFAULT 0,
  collected_minor bigint NOT NULL DEFAULT 0,
  refunded_minor bigint NOT NULL DEFAULT 0,
  expense_minor bigint NOT NULL DEFAULT 0,
  receivable_minor bigint NOT NULL DEFAULT 0,
  refreshed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,metric_date,currency)
);
CREATE TABLE analytics.network_hourly_metrics (
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  hour_bucket timestamptz NOT NULL,
  nas_device_id uuid NOT NULL,
  active_sessions integer NOT NULL DEFAULT 0,
  auth_requests bigint NOT NULL DEFAULT 0,
  auth_rejects bigint NOT NULL DEFAULT 0,
  ingress_octets numeric(24,0) NOT NULL DEFAULT 0,
  egress_octets numeric(24,0) NOT NULL DEFAULT 0,
  cpu_percent numeric(5,2),
  memory_percent numeric(5,2),
  refreshed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,hour_bucket,nas_device_id),
  FOREIGN KEY (tenant_id,nas_device_id) REFERENCES network.nas_devices(tenant_id,id) ON DELETE RESTRICT
);

CREATE TABLE platform.outbox_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  aggregate_type text NOT NULL,
  aggregate_id uuid NOT NULL,
  event_type text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','published','failed')),
  attempts integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz,
  UNIQUE (tenant_id,id)
);
CREATE INDEX outbox_pending_idx ON platform.outbox_events(status,created_at) WHERE status IN ('pending','failed');
CREATE TABLE platform.audit_logs (
  id bigserial PRIMARY KEY,
  tenant_id uuid REFERENCES platform.tenants(id) ON DELETE RESTRICT,
  actor_user_id uuid REFERENCES platform.users(id) ON DELETE RESTRICT,
  action text NOT NULL,
  resource_type text NOT NULL,
  resource_id text,
  source_ip inet,
  request_id text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_tenant_created_idx ON platform.audit_logs(tenant_id,created_at DESC);

-- RADIUS bootstrap lookup: maps packet source IP to exactly one active tenant NAS.
-- Grant EXECUTE only to the dedicated FreeRADIUS database role.
CREATE OR REPLACE FUNCTION radius.resolve_tenant_for_nas(p_source_ip inet)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, network AS $$
  SELECT tenant_id FROM network.nas_devices
  WHERE source_ip = p_source_ip AND status = 'active'
$$;
REVOKE ALL ON FUNCTION radius.resolve_tenant_for_nas(inet) FROM PUBLIC;

-- Tenant isolation: application must SET LOCAL app.tenant_id inside every transaction.
-- Runtime DB role must not own these tables, be superuser, or have BYPASSRLS.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'platform.service_zones','platform.resellers','platform.subscribers','platform.subscriber_addresses',
    'platform.packages','platform.package_profiles','platform.subscriber_services',
    'network.nas_devices','network.olt_devices','network.pon_ports','network.onus','network.ip_pools','network.ip_assignments',
    'radius.identities','radius.radcheck','radius.radreply','radius.radusergroup','radius.radacct','radius.radpostauth',
    'billing.invoices','billing.invoice_items','billing.payment_gateways','billing.payments','billing.payment_allocations','billing.gateway_events',
    'accounting.fiscal_periods','accounting.accounts','accounting.journal_entries','accounting.journal_lines',
    'integration.api_clients','integration.api_client_scopes','integration.webhooks','integration.webhook_deliveries',
    'analytics.daily_business_metrics','analytics.daily_financial_metrics','analytics.network_hourly_metrics',
    'platform.outbox_events'
  ] LOOP
    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY',t);
    EXECUTE format('CREATE POLICY tenant_isolation ON %s USING (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid) WITH CHECK (tenant_id = nullif(current_setting(''app.tenant_id'', true), '''')::uuid)',t);
  END LOOP;
END $$;

-- Membership lookup occurs after user authentication but before tenant selection.
ALTER TABLE platform.tenant_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.tenant_memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY membership_self_lookup ON platform.tenant_memberships
  USING (user_id = nullif(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (user_id = nullif(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY membership_current_tenant ON platform.tenant_memberships
  USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE platform.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_self_access ON platform.tenants
  USING (id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (id = nullif(current_setting('app.tenant_id', true), '')::uuid);
ALTER TABLE platform.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.users FORCE ROW LEVEL SECURITY;
CREATE POLICY user_self_access ON platform.users
  USING (id = nullif(current_setting('app.user_id', true), '')::uuid)
  WITH CHECK (id = nullif(current_setting('app.user_id', true), '')::uuid);
CREATE POLICY user_tenant_directory ON platform.users
  USING (EXISTS (SELECT 1 FROM platform.tenant_memberships tm WHERE tm.user_id=platform.users.id AND tm.tenant_id=nullif(current_setting('app.tenant_id', true), '')::uuid))
  WITH CHECK (EXISTS (SELECT 1 FROM platform.tenant_memberships tm WHERE tm.user_id=platform.users.id AND tm.tenant_id=nullif(current_setting('app.tenant_id', true), '')::uuid));
CREATE POLICY auth_service_user_lookup ON platform.users
  FOR SELECT TO isp_auth USING (true);

ALTER TABLE platform.audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform.audit_logs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_audit_isolation ON platform.audit_logs
  USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- The dedicated SECURITY DEFINER resolver role may read only the NAS registry
-- to map a packet source IP to one tenant. It receives no subscriber/payment access.
GRANT USAGE ON SCHEMA network,radius TO isp_radius_lookup;
GRANT CREATE ON SCHEMA radius TO isp_radius_lookup;
GRANT SELECT ON network.nas_devices TO isp_radius_lookup;
CREATE POLICY nas_source_ip_lookup ON network.nas_devices
  FOR SELECT TO isp_radius_lookup USING (true);
GRANT isp_radius_lookup TO CURRENT_USER;
ALTER FUNCTION radius.resolve_tenant_for_nas(inet) OWNER TO isp_radius_lookup;
REVOKE isp_radius_lookup FROM CURRENT_USER;
REVOKE CREATE ON SCHEMA radius FROM isp_radius_lookup;
GRANT EXECUTE ON FUNCTION radius.resolve_tenant_for_nas(inet) TO isp_radius;

GRANT USAGE ON SCHEMA platform,network,radius,billing,accounting,integration,analytics TO isp_app,isp_auth;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA platform,network,radius,billing,accounting,integration,analytics TO isp_app;
REVOKE SELECT,INSERT,UPDATE,DELETE ON platform.users FROM isp_app;
GRANT SELECT (id,email,display_name,is_platform_operator,mfa_enrolled_at,status,last_login_at,created_at,updated_at) ON platform.users TO isp_app;
GRANT UPDATE (display_name,mfa_enrolled_at,status,last_login_at,updated_at) ON platform.users TO isp_app;
GRANT SELECT (id,email,password_hash,mfa_enrolled_at,status) ON platform.users TO isp_auth;
GRANT INSERT (id,email,password_hash,display_name,status) ON platform.users TO isp_auth;
GRANT UPDATE (password_hash,last_login_at,mfa_enrolled_at,updated_at) ON platform.users TO isp_auth;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA platform,network,radius,billing,accounting,integration,analytics TO isp_app;
REVOKE UPDATE,DELETE ON platform.audit_logs FROM isp_app;
GRANT EXECUTE ON FUNCTION accounting.post_journal_entry(uuid,uuid) TO isp_app;
GRANT USAGE ON SCHEMA network,radius TO isp_radius;
GRANT SELECT ON network.nas_devices,radius.identities,radius.radcheck,radius.radreply,radius.radusergroup TO isp_radius;
GRANT SELECT,INSERT,UPDATE ON radius.radacct TO isp_radius;
GRANT SELECT,INSERT ON radius.radpostauth TO isp_radius;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA radius TO isp_radius;
REVOKE SELECT ON radius.radcheck FROM isp_app;

ALTER TABLE platform.package_profiles
  ADD CONSTRAINT package_profile_ipv4_pool_fk FOREIGN KEY (tenant_id,ipv4_pool_id) REFERENCES network.ip_pools(tenant_id,id) ON DELETE RESTRICT NOT VALID;
ALTER TABLE platform.package_profiles
  ADD CONSTRAINT package_profile_ipv6_pool_fk FOREIGN KEY (tenant_id,ipv6_pool_id) REFERENCES network.ip_pools(tenant_id,id) ON DELETE RESTRICT NOT VALID;

-- Posting gate: balanced per currency and open fiscal period required.
CREATE OR REPLACE FUNCTION accounting.post_journal_entry(p_tenant_id uuid, p_entry_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE v_period_status text; v_line_count integer;
BEGIN
  IF p_tenant_id IS DISTINCT FROM nullif(current_setting('app.tenant_id', true), '')::uuid THEN
    RAISE EXCEPTION 'tenant context mismatch' USING ERRCODE = '42501';
  END IF;
  SELECT fp.status INTO v_period_status
  FROM accounting.journal_entries je
  JOIN accounting.fiscal_periods fp ON fp.tenant_id = je.tenant_id AND fp.id = je.fiscal_period_id
  WHERE je.tenant_id = p_tenant_id AND je.id = p_entry_id
  FOR UPDATE OF je, fp;
  IF NOT FOUND THEN RAISE EXCEPTION 'journal entry not found'; END IF;
  IF v_period_status <> 'open' THEN RAISE EXCEPTION 'fiscal period is not open'; END IF;
  SELECT count(*) INTO v_line_count FROM accounting.journal_lines
    WHERE tenant_id = p_tenant_id AND journal_entry_id = p_entry_id;
  IF v_line_count < 2 THEN RAISE EXCEPTION 'journal entry requires at least two lines'; END IF;
  IF EXISTS (
    SELECT currency FROM accounting.journal_lines
    WHERE tenant_id = p_tenant_id AND journal_entry_id = p_entry_id
    GROUP BY currency HAVING sum(debit_minor) <> sum(credit_minor)
  ) THEN RAISE EXCEPTION 'journal entry is not balanced per currency'; END IF;
  IF EXISTS (SELECT 1 FROM accounting.journal_entries WHERE tenant_id=p_tenant_id AND id=p_entry_id AND status <> 'draft') THEN
    RAISE EXCEPTION 'only draft journal entries can be posted';
  END IF;
  UPDATE accounting.journal_entries SET status='posted', posted_at=now()
    WHERE tenant_id=p_tenant_id AND id=p_entry_id;
END $$;

CREATE OR REPLACE FUNCTION accounting.prevent_posted_journal_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_status text; v_tenant uuid; v_entry uuid;
BEGIN
  IF TG_TABLE_NAME = 'journal_entries' THEN
    IF OLD.status = 'posted' THEN RAISE EXCEPTION 'posted journal entries are immutable; create a reversal'; END IF;
  ELSE
    IF TG_OP <> 'INSERT' THEN
      SELECT status INTO v_status FROM accounting.journal_entries
        WHERE tenant_id=OLD.tenant_id AND id=OLD.journal_entry_id;
      IF v_status = 'posted' THEN RAISE EXCEPTION 'lines of posted journal entries are immutable'; END IF;
    END IF;
    IF TG_OP <> 'DELETE' THEN
      SELECT status INTO v_status FROM accounting.journal_entries
        WHERE tenant_id=NEW.tenant_id AND id=NEW.journal_entry_id;
      IF v_status = 'posted' THEN RAISE EXCEPTION 'lines of posted journal entries are immutable'; END IF;
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
CREATE TRIGGER journal_entry_immutable BEFORE UPDATE OR DELETE ON accounting.journal_entries
  FOR EACH ROW EXECUTE FUNCTION accounting.prevent_posted_journal_mutation();
CREATE TRIGGER journal_lines_immutable BEFORE INSERT OR UPDATE OR DELETE ON accounting.journal_lines
  FOR EACH ROW EXECUTE FUNCTION accounting.prevent_posted_journal_mutation();

-- NOTE: Do not seed tenant-specific chart of accounts here. Provisioning workflow creates
-- tenant, default roles, fiscal periods and chart of accounts atomically for each new tenant.
COMMIT;
