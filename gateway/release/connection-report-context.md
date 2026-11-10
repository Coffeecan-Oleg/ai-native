# Personal connection report: source and inline text

Candidate on `9144998afce3d01e4f16aaa7c75c8fe00f93c674`, prepared 9 November 2026.
Not published or installed. No new permissions, configuration or migrations.

## Why

A downloaded report from before the latest deployment cannot establish acceptance
of the new release. The report previously omitted the server address and release.
Some embedded browsers also cannot save a Blob download through their automation.
The latter is not evidence that ordinary employees cannot download files.

## Change

- The TXT includes the configured server origin and the Gateway release **when the
  report is generated**, not an assertion about the version at the time of the MCP call.
- Origin is derived from configured `public_url`, never request Host/Forwarded
  headers. Paths, query strings and fragments are removed; credentials and invalid
  origins are not printed. Only a full Git SHA is shown as a release identifier;
  development and unknown values are explicitly labelled.
- After **Подготовить файл (.txt)**, **Показать текст отчёта** reveals a labelled,
  read-only text area containing the same decoded UTF-8 text as the file.
  Employees may select and copy it into their support request themselves.
- The preview uses a textarea value, never HTML insertion. It needs no second
  request, clipboard permission, external service or new endpoint.
- File and text are cleared on retry, error, page exit and after five minutes.

## Boundaries and acceptance

Existing subject-bound authorization, 15-minute diagnostic marks, no-store,
same-origin fetch, redirect rejection, size bound and session checks stay intact.
Preparing a report does not prove a successful MCP call: a missing event remains
explicitly missing. Previewing text does not prove a saved file or an employee test.

On the copy, verify successful and missing-event reports; origin/release labels;
text identical to the downloadable UTF-8 file; literal markup not executed; keyboard
access to the disclosure and text; expiry/retry and denied sessions clearing data.
Full CI and image acceptance are required after approval to publish. The exact
accepted image then follows the existing dev and production release procedure.
For an authorized code-only reversal, no database restore is needed: this change
creates no persistent data and changes no schema. Do not use a disabled rollback command.
