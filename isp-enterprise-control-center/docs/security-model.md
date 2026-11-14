# Security Model — Phase 1

## Trust boundaries
- Browser/mobile clients are untrusted.
- Public API clients are authenticated but not trusted beyond their OAuth scopes.
- Tenant context comes from a verified user membership or API-client tenant binding.
- NAS devices are trusted only by exact registered source IP and unique per-device secret.
- Payment gateway callbacks are untrusted until provider signature and transaction status are verified server-to-server.

## Required controls
- Runtime DB role is not a table owner, superuser or `BYPASSRLS` role.
- Set `app.user_id` and `app.tenant_id` with transaction-local `set_config(..., true)` only after authentication and membership validation.
- Never use a caller-supplied tenant ID as authorization.
- Keep NAS and gateway credentials in a secret manager; database stores references/hashes, not plaintext secrets.
- Use TLS for public endpoints and encrypted backups.
- Use least-privilege DB grants and separate migration, application and RADIUS DB roles.
- Log administrative and financial actions without logging passwords, API secrets, payment tokens or full card data.
- Use idempotency keys for all payment initiation and financial posting operations.
- Verify gateway callbacks using provider-documented signatures and independently query provider transaction status where supported.
- Use signed, timestamped webhooks with replay protection.

## Known Phase 1 gaps
- Authentication/JWT issuance and OAuth2 authorization server are not implemented yet.
- Tenant provisioning and membership administration APIs are not implemented yet.
- A complete grants/roles bootstrap script is not included yet.
- Custom tenant-aware FreeRADIUS SQL queries and credential decryption integration are not implemented yet.
- Payment provider adapters are not implemented or certified against merchant sandbox credentials.
- Accounting reversal workflow, period close workflow, reconciliation engine and reporting views remain to be implemented.
- API route handlers beyond health are contract-only in this phase.

## Database role groups
- `isp_app`: application group role; tenant RLS remains enforced.
- `isp_auth`: authentication group role; reads password hashes and only the user fields needed for login.
- `isp_radius`: RADIUS query group role; only required NAS/identity/attribute/accounting access is granted.
- `isp_radius_lookup`: narrowly scoped function-owner role; can read only the NAS registry through the dedicated source-IP resolver policy.
- Create separate LOGIN roles for runtime credentials and grant membership in the relevant group. Do not use group roles as direct login credentials.
- The migration owner is separate from all runtime roles.

## RADIUS credential warning
`radius.radcheck.value` may contain a `Cleartext-Password` attribute when PAP is enabled. Restrict SELECT to the dedicated RADIUS role, encrypt database volumes/backups, prevent query/log/trace exposure, and rotate credentials on suspected compromise. For deployments requiring column-level encryption, replace this storage path with ciphertext plus a KMS-backed decryption integration/custom FreeRADIUS module; do not put the decryption key in the database or repository. The API must never return this field.
