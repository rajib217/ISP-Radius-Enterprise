# Capability Map: ISP Enterprise Control Center Baseline Audit

This map decomposes the read-only baseline audit in the supplied task brief. It authorizes audit planning and documentation only; it does not authorize implementation or remediation.

| Module id | Responsibility | Depends on |
|---|---|---|
| repository-inventory | Locate applications, schemas, RADIUS configuration, deployment, tests, and operations files | — |
| tenant-security | Review identity, authorization, tenant boundaries, NAS trust, and secrets handling | repository-inventory |
| data-integrity | Review PostgreSQL isolation, accounting lifecycle, and financial records | repository-inventory |
| radius-aaa | Review FreeRADIUS configuration, authentication, accounting, and CoA assumptions | repository-inventory |
| vendor-interoperability | Assess Juniper, MikroTik, and Huawei integration evidence and gaps | repository-inventory, radius-aaa |
| operations-readiness | Assess performance, availability, observability, backups, and tests | repository-inventory |
| audit-report | Consolidate evidence, findings, priorities, and go/no-go recommendation | all modules |

Build order: `repository-inventory` → `tenant-security`, `data-integrity`, `radius-aaa`, `operations-readiness` → `vendor-interoperability` → `audit-report`.

## Scope constraints

- Architecture and capacity figures in the brief are design targets, not verified implementation facts.
- Audit activity is read-only: no source/configuration/schema changes, migrations, deployments, service restarts, production access, or network-device/RADIUS-server connections.
- Requested audit documentation may be created under `docs/`; source files remain untouched.
- Findings must distinguish verified defects, hypotheses, and missing evidence, with exact repository evidence where available.
