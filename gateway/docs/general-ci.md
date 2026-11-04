# General CI: source tests and generated packs

The `CI` workflow checks the full repository test suites. The separate
`Corporate Gateway pinned release` workflow still builds and accepts the installed
Linux image; passing source tests does not replace that image acceptance or deploy
anything to the corporate server.

## Platform job

The job runs `agent-platform/tests`, validates every skill manifest and pack
membership, inventories skills, builds plugin packs, verifies the generated files,
and checks `bootstrap.sh` syntax. The historical training writer has its manifest
and telemetry marker completed and is included in the historical training pack
0.1.1. This does not install that pack or grant server permissions.

## Gateway job

1. Install the package and validate route registries / Python compilation.
2. Fetch the official Bitrix24 SDK using `release/fetch-sdk.py --source-root .`.
   The provenance URL, byte count and SHA256 remain mandatory. The existing Docker
   layout and default `/src` still work. SDK bytes remain excluded from Git.
3. Start the workflow's disposable PostgreSQL service, with a digest-pinned image.
   No repository or organization secrets are passed to these tests.
4. Run **every `test*.py` module** in `tests`, `addon_tests`, `corporate_tests`,
   `integration_tests`, `onboarding_tests`, and `release`:

   ```sh
   ONBOARDING_TEST_DATABASE_URL='postgresql://ci_test:synthetic-ci-only@127.0.0.1:5432/platforma_onboarding_test' \
     python release/run-suite.py --require-database --output /tmp/gateway-tests
   ```

   Use only a newly created disposable database. The runner accepts the exact
   database name on loopback only, refuses existing public tables, applies all
   migrations, and never drops databases or schemas. Onboarding fixtures truncate
   their synthetic tables, so this is not a production diagnostic command.

Each module runs in a fresh Python process. This prevents dependency stubs and
module reloads in legacy unit tests from affecting real ASGI/MCP tests. Child
environments contain no inherited Gateway credentials; a Python audit hook denies
external DNS / socket operations before test imports. Loopback is allowed for the
disposable database and synthetic local fixtures. This is a test guard, not a
general-purpose sandbox for untrusted code.

Failures, import errors, empty discovered modules, timeouts, missing reports and
unexpected skips fail the job. A per-module timeout is 120 seconds. The runner
itself is tested using deliberately failing, skipped, empty, timed-out and
network-attempting miniature repositories. Reports list discovered and executed
test IDs, failures, errors, skips and migrations; GitHub retains them for 7 days.

Linux is the acceptance environment: real write locks use `fcntl`, and filesystem
permission/symlink checks require POSIX. `--allow-platform-skips` allows only two
explicit Windows filesystem skips for diagnosis; it is not used in GitHub CI.
Do not replace locks with no-op mocks merely to obtain a green Windows run.

After the modules, `release/run-roundtrips.py --output /tmp/gateway-roundtrips`
runs the real local Git clone/commit/push proxy scenario and the standalone
synthetic request/telemetry/browser scenario. Git uses temporary local repositories
only. The sandbox scenario receives a newly created `sandbox_feature_test`
database in the disposable CI cluster; an existing database is refused and never
dropped. Its profile, request overlay and dates are synthetic fixtures. The old
hard-coded `/candidate/candidate/profiles-after.json` deployment path is replaced
with an explicit test-only profile input.

The onboarding server fixture permits only its exact synthetic host while keeping
DNS rebinding protection enabled. A negative test rejects an unrelated Host.
Real Bitrix24, UniSender, OAuth accounts, contact data, and the corporate database
are not required and must not be used in this workflow.
