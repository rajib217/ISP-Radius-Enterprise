# ISP Enterprise Control Center — Logical ERD

```mermaid
erDiagram
  TENANTS ||--o{ TENANT_MEMBERSHIPS : has
  USERS ||--o{ TENANT_MEMBERSHIPS : joins
  TENANTS ||--o{ SUBSCRIBERS : owns
  TENANTS ||--o{ RESELLERS : manages
  RESELLERS ||--o{ SUBSCRIBERS : serves
  TENANTS ||--o{ PACKAGES : defines
  SUBSCRIBERS ||--o{ SUBSCRIBER_SERVICES : receives
  PACKAGES ||--o{ PACKAGE_PROFILES : contains
  SUBSCRIBER_SERVICES }o--|| PACKAGE_PROFILES : uses
  TENANTS ||--o{ NAS_DEVICES : owns
  TENANTS ||--o{ OLT_DEVICES : owns
  OLT_DEVICES ||--o{ PON_PORTS : contains
  PON_PORTS ||--o{ ONUS : hosts
  SUBSCRIBER_SERVICES ||--o{ ONUS : linked_to
  SUBSCRIBER_SERVICES ||--o{ IP_ASSIGNMENTS : allocates
  SUBSCRIBERS ||--o{ RADIUS_IDENTITIES : authenticates
  NAS_DEVICES ||--o{ RADACCT : reports
  SUBSCRIBERS ||--o{ INVOICES : billed
  INVOICES ||--|{ INVOICE_ITEMS : contains
  SUBSCRIBERS ||--o{ PAYMENTS : pays
  PAYMENTS ||--o{ PAYMENT_ALLOCATIONS : allocated
  INVOICES ||--o{ PAYMENT_ALLOCATIONS : settled_by
  TENANTS ||--o{ ACCOUNTS : owns
  JOURNAL_ENTRIES ||--|{ JOURNAL_LINES : contains
  ACCOUNTS ||--o{ JOURNAL_LINES : posts_to
  TENANTS ||--o{ API_CLIENTS : registers
  API_CLIENTS ||--o{ WEBHOOKS : subscribes
  TENANTS ||--o{ AUDIT_LOGS : records
  TENANTS ||--o{ DAILY_BUSINESS_METRICS : aggregates
  TENANTS ||--o{ DAILY_FINANCIAL_METRICS : aggregates
  TENANTS ||--o{ NETWORK_HOURLY_METRICS : aggregates
```

## Tenant isolation rules
- Every tenant-owned row carries `tenant_id`.
- Composite foreign keys include `tenant_id`, preventing cross-tenant references.
- PostgreSQL RLS uses transaction-local `app.tenant_id`.
- Runtime roles must not own tables and must not have `BYPASSRLS`.
- RADIUS resolves tenant from the NAS source IP and canonical realm-qualified username; it must not trust an unverified tenant hint from a client.
- Platform operators use a separate, audited control-plane path; they do not bypass tenant scoping in ordinary business queries.

## RADIUS hybrid model
FreeRADIUS performs protocol handling and accounting packet processing. The control plane owns tenant provisioning, credentials/secret references, package policy, NAS registry, CoA workflows and operational audit. FreeRADIUS SQL queries must be customized for the tenant-aware schema and validated against the exact FreeRADIUS release before any NAS migration.

## Accounting invariants
- Posted journal entries are immutable.
- A posting transaction verifies sum(debit_minor) = sum(credit_minor) per currency.
- Corrections use reversal entries.
- Payment-to-invoice allocation is many-to-many.
- Gateway clearing and settlement are distinct events.
