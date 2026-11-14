# Hybrid FreeRADIUS integration contract

FreeRADIUS is the protocol/data-plane service. The NestJS control plane provisions tenant identities, package reply attributes, NAS metadata, credential references, session operations and audit records.

## Tenant resolution
1. Accept requests only from explicitly registered NAS source IPs.
2. Resolve `tenant_id` using `radius.resolve_tenant_for_nas(Packet-Src-IP-Address)`.
3. Resolve the canonical RADIUS username (`local-user@tenant-realm`) in that tenant.
4. Fetch only that identity's check/reply/group attributes.
5. Stamp the resolved tenant ID into every accounting insert/update.
6. Never accept a tenant ID supplied by a subscriber as authoritative.

## Credential handling
- `credential_ciphertext` is encrypted-at-rest data; it is not directly usable by FreeRADIUS without a controlled decryption integration.
- Prefer an external secret/KMS reference or a dedicated credential service with narrow authorization.
- Do not store submitted passwords in `radpostauth`; the supplied schema intentionally omits a password column.
- Test PAP, CHAP, MS-CHAPv2, accounting Interim-Update, duplicate packets, stale Stop packets and CoA/Disconnect on each actual NAS firmware.

## Required work before connecting a NAS
The standard FreeRADIUS PostgreSQL query set is not tenant-aware by default. Build and review custom queries for `radcheck`, `radreply`, `radusergroup`, `radacct` and post-auth logging. Validate the exact config with `freeradius -XC` / `radiusd -XC`, then run packet-level integration tests in a lab. Do not point production NAS devices at this schema until those tests pass.
