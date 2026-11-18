# Connection report in a Bitrix24 frame — candidate, 2026-11-09

Base: `153af4d47292c4a52401ae98404007ebcb12bc33`. This document describes a prepared
patch, not an installed release. No migration, setting, permission or credential change.

## Reproduction and evidence boundary

In the Codex in-app browser a loopback fixture with two independent sites reproduced
the production symptom: a page request contained its synthetic Partitioned cookie;
an ordinary attachment-link request did not and received a 303 login redirect.
A same-origin fetch inside that frame retained the cookie and returned 200.
Logs contain only paths, statuses, cookie-presence booleans and Fetch Metadata,
never cookie values. This does not establish that all ordinary browsers have the defect.

The candidate was served on loopback with the actual Gateway page, report route,
Bitrix frame middleware and a signed synthetic employee session. Only employee
verification and journal storage were mocked. Chrome received the report with the
frame cookie, HTTP 200 and no login redirect; the page displayed a ready Blob link.
The browser automation did not confirm a completed file download. Neither a Blob
link nor HTTP 200 is proof that the employee saved a file. Live Bitrix24 acceptance
and file-on-disk verification remain pending.

## User-visible change

1. Click **Подготовить файл (.txt)**. The page displays progress and disables repeated
   clicks. The request has a 15-second timeout.
2. Click **Скачать готовый файл (.txt)** after preparation. The link is local to the
   current page and expires after five minutes. The file is not sent anywhere.
3. An expired mark, revoked session or unavailable journal produces an inline
   explanation. The frame does not navigate to a login page.

The same personal report endpoint and subject-bound lookup remain in use. Fetch
uses `credentials: same-origin`, `mode: same-origin`, `redirect: error`, no cache and
no referrer. No token is exposed to JavaScript or appended to a URL. The client
rejects HTML, unexpected filenames, empty content and responses over 64 KiB.
Blob URLs are revoked when regenerated, expired or the page is left.

## Validation

- 117 Python tests in seven related modules, zero errors and skips: personal
  report, signed frame/ordinary sessions, revoked access, identity, frame SDK,
  corporate access and login.
- 14 Node tests execute the shipped JavaScript with controlled DOM/fetch:
  success bytes, failure statuses, response validation, size bound, timeout,
  duplicate clicks, external URL rejection, expiry and late responses after leaving.
- Both CI workflows now run these JavaScript tests with Node 22 before image
  acceptance. Node is a CI dependency only; it is not added to the Gateway image.
- Candidate fingerprints are updated for the route and new static JS asset.
  Full Linux CI and new image acceptance have not run before publication.

## Release and acceptance

Publish the reviewed package to the existing branch and PR, pass complete source
CI plus pinned image acceptance, then install in dev. Before production, retain the
normal server backup and verify the exact image/tag and unchanged schema 28.
Accept both ordinary employee and owner paths inside Bitrix24: prepare, download,
open the UTF-8 file, match the diagnostic mark and request ID, confirm the frame
stays open. Also check no-MCP-result and expired-mark messages. Do not interpret a
missing MCP event as a server outage.

If the new download path fails, keep a report of the symptom and use the previous
standalone-page workaround. The patch changes no persistent data, so a separately
authorized code-only return to 153af4d4 is structurally compatible. Do not restore an
old database for this UI regression or invoke the disabled production rollback
command. Follow the current approved release process.
