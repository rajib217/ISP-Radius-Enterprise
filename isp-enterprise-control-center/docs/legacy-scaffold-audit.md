# Audit notes — previous Phase 1 starter ZIP

The previous ZIP was inspected before creating this implementation baseline. It was a starter, not a deployable production release.

Findings relevant to this redesign:
- Previous schema was single-tenant: subscriber, package, NAS, invoice and ledger keys were globally unique without tenant boundaries or RLS.
- Previous schema had no composite tenant foreign keys, tenant membership model or tenant-aware RADIUS identity resolution.
- Previous `radpostauth` design included a password-like `pass` column; this design intentionally omits it.
- Previous Redis healthcheck referenced `REDIS_PASSWORD` inside the container although that variable was not injected into the container environment.
- Previous NAS clients were documentation IP placeholders and shared-secret placeholders.
- Previous FreeRADIUS SQL module referenced stock/default query behaviour without a verified tenant-aware query set.
- Previous API and worker were minimal placeholders, not the selected NestJS application architecture.
- The mounted extracted folder contained only a README; the ZIP archive was separately inspected to audit its actual contents.

The new baseline replaces the single-tenant schema and documents the remaining integration gates. No claim is made that the previous Docker/RADIUS stack was run successfully.
