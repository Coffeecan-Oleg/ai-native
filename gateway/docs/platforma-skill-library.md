# Shared skill library for the Platforma sandbox

This opt-in feature adds `/platforma/skills` to the existing human browser panel.
It requires the existing corporate catalogue, reader onboarding and PostgreSQL,
plus `GATEWAY_PLATFORMA_SKILL_LIBRARY_ENABLED=true` after owner approval.
Apply migration `0024_platforma_skill_library` before enabling it. The default
is disabled; a missing database or unverifiable approval never opens access.

## User workflow

1. Sign in personally, obtain the existing owner approval, open **Каталог навыков**.
2. Select **Загрузить навык**, enter a descriptive, unique title and choose `.md`
   or a `.zip` containing exactly one `SKILL.md` and its text resources.
3. Confirm that all files may be shared with approved sandbox participants.
4. Open the card to inspect the material and copy the suggested assistant prompt.
5. Rename a personally uploaded skill in its card. The human owner may also
   rename other uploads or the display title of a bundled skill.

Uploads appear immediately as **unreviewed** shared material. Uploading is not
approval of its instructions or permission to execute commands. The Gateway
never executes uploaded resources and never fetches their links. No automatic
skill application or standalone agent runner is introduced.

## Identity and MCP compatibility

Each upload receives an immutable `shared-<UUID>` identifier. A title change
updates only metadata with optimistic revision checks. File hashes, content,
version and existing skill events remain unchanged. Titles are normalized and
unique across bundled/uploaded skills and permanent identifiers.

Existing `gateway_corporate_skill_list` returns both bundled and uploaded rows.
`gateway_corporate_skill_read(skill=...)` accepts a permanent ID or the current
unique display title. Read by permanent ID remains valid after renaming; old
display names are not permanent aliases. No extra MCP tools, OAuth scopes or
local plugin installation are required. Ask the assistant to refresh the
catalogue if its previous response is stale.

Each uploaded row includes the exact telemetry descriptor: canonical `skill_id`,
`skill_pack=platforma-shared`, stored `skill_version`, and existing project
`platforma-training`. Existing telemetry authorization still controls project,
client, metadata, routes and own statistics. Emit events only for actual skill
application, never merely for upload/rename/read. A title change does not split
the skill's identity or modify historical statistics.

## Boundaries

Only personal human browser sessions may upload or rename. Active approved
readers can upload and rename their own material. The owner may rename any
catalogue entry. A constrained MCP credential is not a browser session.
Approval/grant checks and expiry are repeated; no new administrative, personal
memory, owner-statistics or corporate-system access is granted.

Native forms require an actor-bound CSRF token and same-origin checks. Uploaded
Markdown, HTML, SVG and code are displayed as escaped text; scripts are never
served as executable browser content. ZIPs are read in memory without pathname
extraction. Absolute/parent/hidden paths, links, special files, duplicate paths,
encrypted archives and unsupported compression are rejected.

Limits: 2 MiB upload and expanded package, 32 text files, 128 KiB per file,
10 uploads per author and 100 per sandbox. Binary files are unsupported in this
release. Plain UTF-8 Markdown is accepted; when YAML frontmatter exists it must
contain textual `name` and `description`. YAML object tags and aliases are not
accepted. Secrets or real personal/corporate records must not be uploaded.

No content replacement, deletion, review/approval queue or private-per-user
skill storage is implemented. Shared material is visible to all active approved
readers. Publication/review controls can be added as a separate change.

## Persistence and rollback

The migration creates three dedicated PostgreSQL tables for metadata, inert
resource bytes, and upload/title-change events. Mutations and events commit
atomically. A form UUID makes a repeated upload idempotent; a database advisory
lock serializes name/quota decisions. Content hashes are checked on read.
Do not log raw skill contents or credentials.

Before live activation, take and verify a closed backup of configuration,
runtime catalogue, knowledge, request overlay and PostgreSQL. Preserve all
existing grants, OAuth settings, personal data boundaries and expiry.

To stop the feature, disable its flag. Existing bundled catalogue behavior
returns, uploaded records stay in PostgreSQL. A previous image can be restored
while leaving the additive migration/tables in place. Do not restore the full
database over later changes or remove uploaded records as part of a code rollback.

Tests use a disposable database named `platforma_onboarding_test`, never the
live database. Verify a personal upload, another reader's read, denied rename
of another author's upload, owner rename, MCP read by title/ID, unchanged hashes
and honest telemetry. Real-user acceptance after deployment is separate from
synthetic integration tests.
