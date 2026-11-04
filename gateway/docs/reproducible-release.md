# Corporate Gateway source release (candidate, not installed)

The accepted runtime is `sha256:daf4dad646b2a6c3bac88b40b149c3f63d5395869074e70becce8f5e8e59e138`.
The source baseline is recorded in `release/baseline.json`; it contains code
fingerprints, not runtime secrets or private mount contents. All 182 installed
non-catalog code/assets were read and matched before preparing this release.
UTF-8 text is normalized to LF for checkout and wheel packaging.

## Build

From the repository root on Linux amd64 with Docker:

```sh
docker build --pull=false -t gateway-corporate-candidate gateway
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

## References

- [pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/)
- [Debian snapshot](https://snapshot.debian.org/)
- [Docker build best practices](https://docs.docker.com/build/building/best-practices/)
