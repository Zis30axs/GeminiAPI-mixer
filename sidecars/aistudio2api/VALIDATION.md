# Build validation — 2026-10-17

- Upstream revision: `2cbe310ce6ebab093bf83e5078de8296f03770a9`.
- Patch SHA-256: `be16118da6d6ba2c93a47a62adc074fc62ac386a00fd3c6a14c364ef897e3319`.
- Image built locally: `geminiapi-mixer-aistudio:local`.
- Image ID: `sha256:7bab0f57f74103fe84b88e4315edbabd746755bb395a66bffc6e612efd26b10e`.
- Docker-reported image size: 67,389,655 bytes. This is disk/image size, not runtime RAM.
- Final image user: `10001:10001`.

Passed Go package tests: `internal/aistudio`, `internal/api`, `internal/app`, `internal/config`. The final Docker test stage uses `--network=none`. Added tests cover selected Google account indexes, state serialization and redirect restrictions, authentication before accepting a channel lock, malformed/duplicate headers, error acknowledgments, quota cooldown preservation without cross-channel fallback, incompatible Build resource rejection, and a final transport lease check.

A first test iteration incorrectly expected the pool's nonblocking acquisition API to directly return `AllCoolingError`; that assertion was corrected to check the actual candidate/cooldown interface. No quota behavior was changed to satisfy the test.

The built binary's help command passed inside the final image with no network, a read-only filesystem, all capabilities dropped, and `no-new-privileges`. No account import or real Google generation was run as part of these build checks. Login validity, account identity, model capability support, generation, live SSE, and runtime memory require separate deployment verification.
