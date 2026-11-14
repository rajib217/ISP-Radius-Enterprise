# Phase 1 Implementation Decisions

- Tenancy: shared control plane, tenant-scoped data, PostgreSQL RLS, composite tenant foreign keys.
- RADIUS: FreeRADIUS 3.x for protocol plane; NestJS control plane for provisioning, policy and lifecycle; tenant-aware SQL query set is a separate integration gate.
- Payments: provider adapters for direct gateways; gateway secrets stored by secret reference, not in database plaintext.
- Migration: parallel run with legacy IDs, reconciliation reports, canary cohorts and rollback gates.
- Money: integer minor units (`*_minor`) and explicit ISO currency code.
- API: `/api/v1`, OpenAPI 3.1, scoped OAuth2/API clients, idempotency and signed webhooks.

## Phase 1 scope
1. Tenant, user membership and tenant context foundation.
2. Core subscriber/package/service/NAS/RADIUS schema.
3. Billing/payment/accounting schema foundations.
4. Health/readiness endpoints and API contract baseline.
5. Migration/import metadata and audit/outbox foundation.

## Not yet production-certified
No PostgreSQL server, Docker engine, FreeRADIUS binary, actual Juniper/MikroTik NAS, payment gateway sandbox, load test or penetration test was available in this workspace. The SQL and API artifacts require execution against PostgreSQL 16 and integration testing before production use.
