# Corporate Gateway source release (candidate, not installed)

The accepted runtime is `sha256:daf4dad646b2a6c3bac88b40b149c3f63d5395869074e70becce8f5e8e59e138`.
The source baseline is recorded in `release/baseline.json`; it contains code
fingerprints, not runtime secrets or private mount contents. All 182 installed
non-catalog code/assets were read and matched before preparing this release.
UTF-8 text is normalized to LF for checkout and wheel packaging.

## Build

Use a separate Linux amd64 builder or CI runner, not the running service host.
Inventory memory/cgroup headroom and free Docker disk space before any local build.
Per-container limits alone do not reserve resources for the other services.
The copy-test launcher now requires at least 1.5 GiB available memory headroom and
3 GiB free Docker disk before starting a 1 GiB build. These are launch gates, not
a proof of adequate capacity under load. If they fail, use a separate runner.
From the repository root on that builder:

```sh
docker build --platform linux/amd64 --pull=false -t gateway-corporate-candidate gateway
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true gateway-corporate-candidate \
  python /usr/local/share/gateway-release/verify-image.py
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --memory 384m --pids-limit 96 \
  --tmpfs /tmp:rw,nosuid,nodev,size=96m -v "$PWD/gateway:/checks:ro" \
  gateway-corporate-candidate python /checks/release/run-tests.py
```

The final image has one installed Gateway wheel and no shadow Python tree in
`/app`. It preserves the existing UID/GID 10001 and entrypoint umask077.
It intentionally does not embed the live configuration, Bitrix application
credentials, UniSender key, OAuth state, database, audit or user-uploaded skills.
The live compose, private mounts and network guard are managed separately.
Image defaults enable authentication and select Bitrix24. Without the private JWT
and application configuration it must not become a publicly usable anonymous
Gateway. Tests explicitly override defaults only inside isolated fixture processes.

Python runtime versions match the accepted installation: 73 dependencies plus
pip and Gateway. Build tooling (setuptools/wheel/packaging) is separately pinned
and absent from the final runtime. Two upstream dependencies have only sdists
(http-ece/pyaes): hash-checked inputs are converted to wheels offline with the
fixed build tooling. Other dependencies are downloaded as wheels only.
The final pip installation requires locally recorded wheel hashes and uses
neither an index nor dependency resolution. `pip check` must succeed.

The official Bitrix SDK bytes are not published in Git. The image builder downloads
the fixed official URL and verifies the accepted SHA256 and byte length before
packaging. A changed or unavailable upstream SDK fails the build; there is no
automatic runtime download or silent fallback.

Debian uses a fixed dated snapshot, exact package versions and the existing
archive keyring for signature verification. Only snapshot expiration checking
is disabled; TLS and repository signatures remain enabled. No fallback to
unversioned repositories or `trusted=yes` is allowed.

The locks preserve a known installation; they do not certify vulnerability-free
dependencies. Schedule reviewed version updates separately. Hash pinning protects
input integrity but does not guarantee permanent upstream availability or
bit-identical OCI images. Do not claim deterministic image IDs without proof.

## Validation and release boundary

The corporate workflow uses a separate Ubuntu24.04 GitHub-hosted runner and the
exact PR head commit, without persisted checkout credentials. It has contents:read
only, pinned checkout/upload actions, bounded job time and one run per PR/ref.
It first tests the runner guards with fake Docker responses, then refuses known
production container names or insufficient memory/disk before the image build.
No SSH key or deployment credential is configured.

After image checks, release/ci-check.py creates one internal network and temporary
PostgreSQL/Gateway containers with synthetic credentials, no published ports and
no production mounts. The test PostgreSQL image digest is pinned to the already
used engine image. It applies all25 migrations to a new tmpfs database and checks
health/auth, anonymous MCP/frame-session denials, closed metrics and SDK integrity.
Cleanup verifies a per-run nonce label before deleting only its own resources.
This is a fresh-schema test, not a backup restore or real Bitrix login.

The downloadable artifact contains JSON reports only, retained7 days. No Docker
image, SDK blob, live configuration, .env, secret, corporate document or database
is uploaded. Image distribution to a private destination and production rollout
remain separate steps. Legacy CI is retained with contents:read permissions;
its jobs are not replaced or declared passing by this corporate job.

The initial trigger is the existing PR's synchronize event after publishing the
candidate. workflow_dispatch becomes usable only once the workflow is present
on the default branch; do not merge into main just to make the button appear.
Read the checks for the exact candidate commit after publication. Source CI and
the installed-image workflow are separate results; neither a queued workflow nor
a successful source suite establishes image acceptance.

`release/run-tests.py` starts separate regression and metrics processes, checks
the installed package and rejects skipped tests. It uses synthetic credentials
and no external business services. The CI workflow tests the candidate but does
not push an image, publish a release, run migrations against production or deploy.

No new migration is introduced relative to the accepted live schema. SQL0025
was already deployed; it is being reconciled into Git with the other previously
applied corporate changes. Existing generic/training code remains for compatibility,
but the corporate runtime policy keeps demo operations disabled.

The old `ops/Dockerfile.nonroot` and boundary deployment helper document the
already accepted overlay release. They are historical rollback tooling, not the
build entrypoint for this source release. Never substitute a new image into their
hard-coded accepted-baseline contract.

Before any production switch: owner approval, fresh closed backup, immutable
candidate image ID, constrained Gateway-only rollout and timed rollback, then
live HTTPS/MCP/Bitrix acceptance. This source release alone does none of these.

## Bounded builds and diagnostics

The observed 53cee4c7 build completed the application wheel, then repeatedly
printed `Ign:` while fetching pinned Debian packages. Its first runner received
a shutdown signal (exit143); logs for the long second attempt were unavailable.
The second attempt was cancelled after exceeding the configured job limit.
These facts do not establish the underlying reason for that runner's delay.

A separate synthetic regression reproduced a defect in the old watchdog:
`timeout docker ... | tee ...` kept waiting for EOF after the Docker parent
exited, because a descendant retained stdout. With a one-second limit, the
six-second example eventually reported success. This is a demonstrated weakness
of the pipeline, not proof that the same descendant caused the hosted incident.

`release/build-image.py` now supervises the Docker client directly and writes
stdout/stderr to a regular file. Waiting for a pipe reader is unnecessary.
The build limit remains 1080 seconds. On timeout or cancellation it signals only
its own client process group, waits up to 5 seconds, then requests SIGKILL with
another bounded 2-second wait. Unconfirmed client termination is recorded rather
than reported as successful cleanup. This is not a daemon-wide Docker kill.

The helper writes an initial unsuccessful report, updates it every 15 seconds,
and writes the actual final result. Timeout exits 124; SIGTERM exits 143 and
SIGINT exits 130; a missing executable exits 127. Invalid source/deadline input
fails before Docker is invoked. The JSON includes a bounded log tail (last 24 KB,
at most 60 lines of 500 characters), elapsed time and source SHA. A runner shutdown
or SIGKILL can still prevent artifact upload; no script can promise an artifact
after loss of the host. Check report state as well as `successful`.

`locks/apt-build.conf` applies IPv4, one retry, a 15-second HTTP/HTTPS transport
timeout and disabled pipelining to both `apt-get update` and `apt-get install`.
Any index error, including a transient one, fails the update. Docker also limits
the entire index phase to 240 seconds and package installation to 360 seconds,
with explicit phase markers in the log. A transport timeout is not the total
operation deadline. The build-only configuration is removed after installation.
APT's transport may also retry connections internally; `Acquire::Retries=1`
alone is not a cap on network attempts. The phase deadlines remain necessary.

The base-image digest, Debian snapshot timestamp, 35 pinned OS packages,
Python dependency hashes, official SDK check and runtime permissions remain
unchanged. No mutable mirror fallback or relaxed signature checks are added.
An unavailable archive should produce a bounded, diagnosable failure; this
change does not make the external archive available.

Tests use synthetic subprocesses and a loopback-only HTTP server with the real
APT helper; they never build on the production host. Before acceptance, run a
complete Docker build and the installed-image checks on a separate builder.
Preparation on a copy does not publish code or update the live Gateway.

## Frozen indexes with package mirror fallback

The snapshot service can return 503/504 for individual package lookups even when
its signed indexes are available. The two local mirrorlists now let APT choose
another endpoint for a failed file without changing the archive state.

`locks/debian.sources` uses the built-in `mirror+file` transport. Each list
permits the regular Debian CDN only with `type:deb`; it is excluded from index
requests. The CDN can therefore serve only the exact package filename selected
from an authenticated frozen index. Index requests use only the two snapshot
frontends at the original 20261021T124057Z timestamp. Explicit priorities avoid
random selection: package CDN first, then the snapshot frontends. The security
archive also has its ordinary security CDN as a package-only alternative.

The 35 exact `package=version` pins remain in `locks/os.txt`. APT still validates
the InRelease signature with the original Debian archive keyring and verifies
the package length and cryptographic hash from that authenticated index. A
different file at a CDN URL cannot silently replace the expected bytes. Missing
versions, invalid signatures and corrupt payloads fail instead of upgrading or
relaxing authentication. All endpoints use HTTPS; no new signing key, trusted=yes,
allow-unauthenticated option, mutable release index or custom downloader is added.
The existing historical-snapshot Valid-Until policy and build time limits stay
unchanged.

`release/test_apt_mirrors.py` creates an ephemeral signing key and a tiny signed
repository on loopback. It tests package-only CDN selection, fallback after 503,
all mirrors unavailable, same-length corruption, invalid signatures and absent
pinned versions. These tests do not install packages or contact public mirrors.
The separate verification of real public files must record the exact versions,
signature identities and SHA256 matches. A successful download check is not an
installed-image or production-runtime acceptance.

## References

- [pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/)
- [Debian snapshot](https://snapshot.debian.org/)
- [Docker build best practices](https://docs.docker.com/build/building/best-practices/)
- [APT transport timeouts](https://manpages.debian.org/trixie/apt/apt-transport-http.1.en.html)
- [APT update error handling](https://manpages.debian.org/trixie/apt/apt-get.8.en.html)
- [APT mirror selection and type restrictions](https://manpages.debian.org/trixie/apt/apt-transport-mirror.1.en.html)
- [Python subprocess management](https://docs.python.org/3/library/subprocess.html)
