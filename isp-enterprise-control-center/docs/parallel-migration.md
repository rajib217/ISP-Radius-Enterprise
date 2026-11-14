# Parallel Migration Runbook — Legacy ISP Billing/RADIUS to Enterprise Control Center

## Principles
- Keep the existing platform authoritative until the new platform passes reconciliation gates.
- Never allow both systems to independently charge the same billing period or send conflicting disconnect commands.
- Use stable `external_legacy_id` mappings for subscribers and services.
- Preserve raw import files and checksums for audit and repeatable imports.

## Stages
1. **Inventory:** export subscribers, credentials metadata, packages, service status, NAS list, active sessions, invoices, payments, opening balances and IP assignments.
2. **Normalize:** map legacy IDs, phone numbers, package codes, timezone and currency. Reject ambiguous records into a review queue.
3. **Dry-run import:** load into staging tables, report duplicate usernames, missing packages, invalid IPs and orphan invoices.
4. **Reconcile baseline:** compare subscriber counts by status, package counts, outstanding balances, payment totals and active session totals.
5. **Shadow mode:** mirror new subscriber/package changes into the new platform, but keep legacy RADIUS and billing authoritative. Compare authentication decisions and calculated invoices without executing actions.
6. **Canary:** move a small approved cohort to new RADIUS authentication and accounting. Keep a documented rollback route to the old NAS configuration.
7. **Billing cutover:** select a clear billing-cycle boundary. Freeze duplicate invoice generation in one system before enabling it in the other.
8. **Expansion:** move cohorts by zone/NAS; reconcile every cohort before proceeding.
9. **Decommission gate:** only after a full billing cycle, successful restore test, and finance/NOC sign-off.

## Reconciliation controls
- Subscriber counts by tenant, zone and status.
- Unique canonical RADIUS username count; collision report.
- Package/profile mapping and rate-limit attribute comparison.
- Opening accounts receivable by subscriber and in aggregate.
- Invoice totals, payment totals, refunds and gateway settlement balances.
- Active sessions and accounting counters at cutover timestamp.
- IPv4/IPv6 assignment collision and pool-boundary checks.

## Rollback triggers
- Tenant data leakage or RLS bypass.
- Authentication reject/latency rate above agreed threshold.
- Incorrect speed profile or simultaneous-use enforcement.
- Material invoice/ledger mismatch.
- Duplicate or missing payment processing.
- NAS CoA/Disconnect behaviour not matching the approved test matrix.

## Operational rule
Every cutover cohort must have an owner, start time, subscriber list, expected counts, rollback steps and sign-off record. Never perform a global cutover based only on a successful login test.
