# Spec: Tenant Context Transaction Safety Tests — V1

Status: proposed

## Objective

Establish a small, executable regression suite for the existing NestJS `DatabaseService` transaction wrappers before adding authentication or tenant-facing routes. The first development slice will prove that tenant and user context is set transaction-locally on the checked-out PostgreSQL client, that work commits or rolls back correctly, and that pooled clients are always released.

This is a unit-level contract for the API database boundary. It does not prove that PostgreSQL RLS policies work under provisioned runtime roles; that requires a later isolated PostgreSQL integration suite.

## Tech Stack

Verified in the imported Phase 1 archive:

- NestJS 10.x and TypeScript `^5.7.2`
- `pg ^8.13.1`
- An existing `npm test` script invoking `jest`, but no Jest runner/configuration or lockfile is currently included in the API package.
- `@nestjs/testing` is listed, but this slice does not require a Nest application test module.

Use Jest for this slice to match the existing package script. Add only development-time test tooling needed to run TypeScript tests; choose versions compatible with the repository’s TypeScript version when implementation begins. No new runtime dependency is in scope.

## Commands

Run from `isp-enterprise-control-center/apps/api` after implementation:

```sh
npm install
npm run build
npm test -- --runInBand
npm run lint
```

Tests must mock the `pg.Pool` and `PoolClient`; the test suite must not connect to a live or production database. Do not run the migration.

## Project Structure

Current API source:

```text
src/common/database.service.ts
src/health/health.controller.ts
src/app.module.ts
src/main.ts
```

Expected additions for this slice:

```text
src/common/database.service.spec.ts
jest.config.js
```

Update the API package manifest only for dev-only test tooling/configuration required to make the existing `npm test` script executable. Do not create a package lock unless the project owner chooses a lockfile policy.

## Code Style

Follow the existing TypeScript style: single quotes, semicolons, explicit async return types where practical, parameterized SQL, and small focused test cases. Never log context values or database credentials.

Example test assertion:

```ts
expect(client.query).toHaveBeenCalledWith(
  "SELECT set_config('app.tenant_id', $1, true), set_config('app.user_id', $2, true)",
  [tenantId, userId],
);
```

## Testing Strategy

Mock the pool boundary and cover:

1. `withTenant` checks out one client, starts a transaction, sets tenant and user context with bound parameters and transaction-local `true`, invokes the callback on that same client, commits, and releases.
2. `withUser` sets only the authenticated user context transaction-locally, then commits and releases.
3. A callback failure causes rollback, rethrows the original error, and releases the client.
4. A setup/query failure also attempts rollback and releases the client.
5. No test assumes that a mocked SQL call proves real PostgreSQL RLS enforcement.

Run the unit tests and build. Record exact commands and results. Actual RLS enforcement, pooled-connection isolation against PostgreSQL, and role privilege checks are deferred to an integration-test spec requiring an isolated database and non-owner runtime role.

## Boundaries

- **Always:** Keep SQL parameterized; keep tenant/user context transaction-local; use one checked-out client for the entire transaction; rollback on failures; release in all paths; use synthetic test IDs only.
- **Ask first:** Production authentication/authorization semantics, new runtime dependencies, schema or migration changes, externally reachable routes, or tests requiring a real database service.
- **Never:** Treat an untrusted header/body tenant ID as trusted identity; add an endpoint that accepts arbitrary tenant/user IDs; run migrations; connect to production; weaken RLS or role boundaries; log secrets or principal identifiers.

## Success Criteria

- `npm test -- --runInBand` runs the new tests without a database connection.
- All transaction success/error paths above are covered.
- `npm run build` and `npm run lint` succeed, or any pre-existing failure is reported with observed evidence.
- No application route, schema, migration, RADIUS configuration, or production setting changes in this slice.
- Documentation clearly states unit tests do not establish real PostgreSQL RLS behavior.

## Out of Scope (Deferred)

- JWT/OAuth2 verification, login, user provisioning, tenant membership selection, and request guards.
- PostgreSQL-backed RLS integration tests and runtime-role bootstrap.
- FreeRADIUS, billing, payments, accounting, frontend, Docker Compose, and production rollout.
- Remediation of any audit finding.

## Open Questions

- The repository currently has no package lock. This spec leaves lockfile policy unchanged; dependency resolution will be recorded when the test tooling is added.
