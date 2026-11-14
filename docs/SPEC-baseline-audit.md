# Spec: ISP Enterprise Control Center Baseline Technical & Security Audit — V1

Status: proposed

## Objective

Produce an evidence-based baseline audit of the current ISP-Radius-Enterprise repository before implementation changes are approved. The audit must describe what is present, absent, verified, hypothesized, or not evidenced; assess the risks in the supplied architecture brief; and recommend a prioritized remediation sequence. The audience is the project owner and engineers responsible for network automation, RADIUS/AAA, PostgreSQL security, and SaaS security.

The success of the audit is an accurate, reviewable report—not a claim that the intended design is implemented or that the planning targets have been achieved.

## Scope and capability map

This spec follows the module IDs and build order in [CAPABILITY_MAP.md](CAPABILITY_MAP.md):

1. `repository-inventory`
2. In parallel after inventory: `tenant-security`, `data-integrity`, `radius-aaa`, `operations-readiness`
3. `vendor-interoperability` after inventory and RADIUS boundary review
4. `audit-report` after the evidence reviews

The supplied design requirements cover a multi-tenant SaaS modular monolith (NestJS/TypeScript, Next.js, PostgreSQL, Redis/BullMQ), FreeRADIUS 3.2.x, PostgreSQL RLS and tenant-aware integrity, double-entry financial records, Docker Compose/Nginx, Prometheus/Grafana/Loki/Alertmanager, and Juniper MX, MikroTik CCR/switch, and Huawei MA5800 X7 running V100R020. The 20,000-subscriber and approximately 10,000-concurrent-session values are planning targets only. None of these are treated as verified facts without repository evidence.

## Current repository evidence baseline

At specification time, the accessible `codex/baseline-audit-map` branch contains `LICENSE`, `README.md`, and `docs/CAPABILITY_MAP.md`. The README only identifies the repository. No application, schema, RADIUS, deployment, test, or CI files are currently available to inspect. This is an observed repository limitation, not a defect in an unseen implementation. The audit cannot substantively assess source code until the project files are present.

## Commands

Run only from a local checkout of the target repository. These inventory commands are read-only:

```text
git status --short
git ls-tree -r --name-only HEAD
rg --files
rg -n --hidden -g '!**/.git/**' -g '!**/vendor/**' -g '!**/node_modules/**' 'RLS|CREATE POLICY|radius|tenant|accounting|webhook|SECURITY DEFINER'
```

After inventory, execute project-specific tests or validators only when their commands are explicitly supported by repository files and they are safe and non-mutating. Record exact commands and observed results in `docs/BASELINE_AUDIT_EVIDENCE.md`. Do not run migrations, destructive commands, deployments, service restarts, production checks, or tests that require live network devices, NAS/RADIUS servers, or production services. Do not claim an unexecuted test passed.

## Project structure and deliverables

Observed repository structure is currently limited to:

```text
LICENSE
README.md
docs/
  CAPABILITY_MAP.md
```

Audit outputs, once evidence is available:

- `docs/BASELINE_AUDIT.md` — primary report with the 16 sections in the user brief.
- `docs/BASELINE_AUDIT_EVIDENCE.md` — command log and evidence index, when useful.
- Source, configuration, migrations, schemas, and application files are outside the audit's write scope and must remain untouched.

## Audit method and finding format

For every claimed issue, cite exact paths and line numbers when available. Use `Not Available` when line evidence cannot be established. Classify each finding as `Verified defect`, `Hypothesis`, or `Missing evidence`, and assign a unique ID, severity, confidence, evidence, impact, safe reproduction steps (if possible), recommendation, and regression test.

Severity levels are Critical, High, Medium, Low, and Informational. Critical and High require concrete impact and evidence. Do not infer runtime behavior from a schema alone, treat common vendor behavior as proof for a specific model/version, or report assumptions as defects.

Markdown finding example:

```markdown
| Finding ID | Title | Severity | Confidence | Classification |
|---|---|---|---|---|
| SEC-001 | Tenant ownership cannot be established from available evidence | Informational | High | Missing evidence |
```

## Testing strategy

First inventory existing tests and CI workflows; do not presume a framework or command. Separate the final test coverage assessment into:

1. Tests actually executed, with exact command and observed result.
2. Existing tests not executed, with reason.
3. Tests that need to be created.
4. Tests requiring vendor documentation, external hardware, or an isolated lab.

The recommended coverage must address cross-tenant denial, RLS under the runtime role, pooled tenant-context isolation, forged NAS identity, invalid RADIUS credentials, duplicate and out-of-order accounting events, worker recovery, tenant-aware CoA/Disconnect, webhook replay, journal balancing, queue/database outages, FreeRADIUS validation, isolated vendor interoperability, load/latency measurement, and backup restoration. These are recommendations until actually implemented and executed.

## Boundaries

- **Always:** Keep the audit read-only with respect to source/configuration/schema/data; use repository evidence; redact secrets; distinguish verified defects from hypotheses and missing evidence; record commands and results; preserve the supplied severity rubric; state unavailable evidence plainly.
- **Ask first:** Any change to source, schemas, migrations, application configuration, CI, deployment, or live environments; any remediation; any direct device/server test; expanding the architecture or version scope.
- **Never:** Run migrations or deployments; access production; connect to routers, switches, OLTs, NAS devices, or live RADIUS servers; expose credentials/secrets; invent paths, line numbers, dependencies, or observed outcomes; recommend bypassing RLS, trusting client/username tenant IDs, Redis-only accounting, synchronous app-API authentication hot paths, disabling TLS/signature checks, plaintext secrets, destructive journal edits, or unreviewed AI-generated network-device commands.

## Success criteria

- The report contains the requested repository inventory and architecture assessment, plus sections for security, tenant isolation, PostgreSQL/accounting, FreeRADIUS, vendor interoperability, performance/HA, observability/recovery, tests, finding register, unsafe recommendations, remediation plan, missing evidence/open questions, and go/no-go.
- Every repository inventory statement corresponds to a file or an explicit `Missing / Not Found` result.
- Every finding has a unique ID and all required fields; severity and confidence match the evidence.
- Tests are separated by execution status; no pass is claimed without an observed run.
- Vendor and capacity claims are bounded by repository evidence, exact model/version documentation, lab results, or explicitly marked unknown.
- Remediation is ordered P0 through P5 with dependencies, risks, and acceptance tests; no remediation is implemented during this audit.
- The final recommendation states whether evidence supports go/no-go and identifies the material limitations.

## Out of Scope (Deferred)

- Implementing remediation or changing application/network configuration.
- Connecting to production, live devices, NAS, or RADIUS services.
- Proving throughput or availability targets without controlled benchmarks and operational evidence.
- Treating vendor-specific support as confirmed without documentation for the exact model and software version.
- Building BI or analytics features; these remain P5 recommendations only if repository evidence makes them relevant.

## Open Questions

- The target repository currently has no project source or configuration files beyond its README and license. Will the project files be added to this repository before the audit proceeds? If they remain absent, the only valid audit outcome is a limited evidence report stating that the code audit could not be completed.
- Which branch should be treated as the audit baseline if project files are added on a branch other than `main`?
- Is the supplied architecture brief the authoritative target for the audit, including FreeRADIUS 3.2.x and the named vendor model/software versions? Until corrected, this spec treats it as the target baseline and labels it unverified.
