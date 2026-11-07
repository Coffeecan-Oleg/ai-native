# Real UniSender read-only integration

This corporate opt-in integration is separate from the historical fictional
`platforma.unisender-demo.stats.get`, which is disabled in corporate mode.
The real routes are
`unisender.getLists` and `unisender.getContactCount`, both require
`unisender:read`, a verified active internal Bitrix24 employee, finite OAuth delegation and
the existing backend-resource authorization. Disable with
`GATEWAY_UNISENDER_READONLY_ENABLED=false`.

The only upstream methods implemented are getLists and getContactCount.
The backend accepts no caller-supplied method, URL, key, headers, search, email,
body or free-form parameters. Read operations use HTTPS POST form bodies so the
API key never appears in a URL. Redirects and ambient proxies are disabled.
Errors are fixed codes; raw upstream errors and payloads are never returned.
Only list IDs/titles or a count are returned; unknown upstream fields are removed.
Count defaults to `params[type]=address`: email contacts, not active subscribers.

Store the credential via the existing encrypted PostgreSQL credential service:
provider `unisender-readonly`, subject `service:platforma-unisender-readonly`,
scopes exactly `getLists` and `getContactCount`. The record is not a user's
credential and has no MCP operation for retrieval. Use the existing configured
Fernet key; never put the API key in environment values, Docker layers, source,
logs, plugin configuration or a prompt. Insert over SSH stdin with no key in
process arguments and no shell expansion. Staff can invoke the tools, not read
the credential. Host/database administrators necessarily retain privileged access.

Corporate eligibility is configured through the explicit Bitrix user-ID list
`GATEWAY_BITRIX_UNISENDER_USER_IDS`. Internal employment alone does not grant
access to real-service data. Identity is rechecked and explicit bans, finite token
scopes/tool allowlists and backend-resource denials remain effective. Do not call
the historical Yandex approval-upgrade helpers for corporate users. A refresh
does not expand delegation; a new personal authorization may be needed after an
approved scope change. This package does not change the list or any roles.

Before changing a deployment, back up the closed configuration and database.
Use the approved server migration/deployment process; do not bypass the restricted
SSH interface or restore old data over later activity. Migration 0025 belongs to
the historical integration schema and is already part of the migration chain.
Validate candidates with fake credentials and disposable PostgreSQL. After an
approved change, validate an authorized employee through personal OAuth, test getLists and then count
for 19445573. Record only target ID/title/count and technical audit information.
Never test a write against the live API: reject it before any network request.

Rollback must follow the release-specific compatibility check and the server
operator's database procedure. An image rollback alone is not a safe substitute
for database rollback after new workflow writes. Disabling this integration or
rotating/revoking its credential requires a separately scoped change.

Official API references:
- https://www.unisender.com/ru/support/api/contacts/getlists/
- https://www.unisender.com/ru/support/api/contacts/getcontactcount/
- https://www.unisender.com/ru/support/api/common/bulk-email/
