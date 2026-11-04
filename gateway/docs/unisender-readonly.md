# Real UniSender read-only integration

This opt-in integration is separate from `platforma.unisender-demo.stats.get`.
The demo remains fictional and never contacts UniSender. The real routes are
`unisender.getLists` and `unisender.getContactCount`, both require
`unisender:read`, a current owner/reader approval, finite OAuth delegation and
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

Owner-approved deployment adds the two routes to finite tool delegation and adds
only `unisender:read` to supported/delegated scopes. Give current approved
participants linked grants and a read-only resource grant expiring with their
approval. Future approvals under the enabled feature get the same limited grant.
Revoking approval also revokes the linked UniSender grants. Old OAuth credentials
do not gain the new scope or routes; refresh does not expand delegation. Users
perform a new personal login. Existing private-data boundaries and expiry remain.

Before enabling, back up the closed deployment/config and database. Apply additive
migration 0025: it permits the existing six/nine grants plus one UniSender read
grant. Existing rows and roles are preserved. Validate the
new image with fake credentials and disposable PostgreSQL. After enabling,
validate an owner/reader through personal OAuth, test getLists and then count
for 19445573. Record only target ID/title/count and technical audit information.
Never test a write against the live API: reject it before any network request.

Rollback disables the feature, restores the preceding image/config and revokes
only integration-specific grants/credential and remove its IDs from the linked
approval arrays before reverting the image. Do not restore the whole database
over later user activity. The additive constraint may remain during rollback.

Official API references:
- https://www.unisender.com/ru/support/api/contacts/getlists/
- https://www.unisender.com/ru/support/api/contacts/getcontactcount/
- https://www.unisender.com/ru/support/api/common/bulk-email/
