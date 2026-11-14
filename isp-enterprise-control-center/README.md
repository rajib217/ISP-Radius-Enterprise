# ISP Enterprise Control Center — Phase 1 Foundation

Implementation baseline for a multi-tenant ISP control plane with hybrid FreeRADIUS integration, direct payment gateway adapters, full double-entry accounting foundations and parallel migration support.

## Included in this delivery
- PostgreSQL 16 migration with tenant-scoped composite keys and Row-Level Security policies.
- Subscriber, service, package, NAS, IPAM, tenant-aware RADIUS, billing, payments, accounting, API client, webhook, audit and outbox schema foundations.
- Logical ERD and tenant/RADIUS/accounting design notes.
- OpenAPI 3.1 contract baseline for public API.
- NestJS API foundation with liveness/readiness endpoints and transaction-local tenant RLS helper.

## Critical deployment requirements
1. Apply migrations with a dedicated migration-owner role that can create the NOLOGIN group roles. The migration creates `isp_app`, `isp_auth`, `isp_radius` and the narrowly scoped `isp_radius_lookup` roles. Create separate LOGIN roles and grant membership in the appropriate group; never give the application login table ownership, superuser or `BYPASSRLS`.
2. Configure each request's tenant from a verified authenticated membership. Never trust `X-Tenant-ID` or a body field as tenant authority.
3. All tenant-scoped queries must use `DatabaseService.withTenant(tenantId, userId, ...)` or an equivalent transaction wrapper setting both `app.tenant_id` and `app.user_id` transaction-locally.
4. RADIUS requires a tenant-aware FreeRADIUS SQL query set. The migration's `isp_radius_lookup` role is a narrowly scoped SECURITY DEFINER owner for source-IP-to-tenant lookup; give it no other cross-tenant permissions. Resolve tenant from registered NAS source IP and canonical realm-qualified username. The schema alone does not configure FreeRADIUS.
5. Store gateway and NAS secrets in a secret manager; database fields contain references only.
6. Posted accounting journals must be balanced and immutable; enforce this in a database posting function/trigger and application transaction before production.
7. Validate direct gateway integrations against each provider's current merchant API, signature scheme and certification requirements.
8. Run tenant-isolation tests, SQL migration tests, RADIUS protocol tests, payment replay tests, accounting invariants, load tests, backup/restore and penetration tests before production.

## Apply migration (PostgreSQL 16)
Use a dedicated migration owner and a fresh staging database:

```bash
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f database/migrations/0001_platform_core.sql
```

Do not use the application runtime credentials for migrations.

## API
```bash
cd apps/api
npm install
npm run build
npm run start:dev
```

Set `DATABASE_URL` before starting. `GET /api/v1/health/live` is process liveness; `GET /api/v1/health/ready` checks PostgreSQL connectivity.

## Current verification status
This workspace does not have Docker Engine or PostgreSQL client/server available. The SQL migration, FreeRADIUS query integration, OpenAPI contract, NestJS build and device/gateway integrations have not been executed here. This is an implementation foundation, not a production-certified release.
