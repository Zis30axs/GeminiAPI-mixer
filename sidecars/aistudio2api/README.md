# Private AI Studio HTTP sidecar

Upstream: [Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API), fixed commit `2cbe310ce6ebab093bf83e5078de8296f03770a9`, MIT. This directory builds that revision with the reviewable `mixer-routing.patch`; it does not modify its WAA, proof generation, signature, subscription checks, or quota enforcement. The Dockerfile pins the upstream Git revision, Go builder image digest, and Dockerfile frontend digest; dependencies use the upstream `go.sum` checksums.

The upstream Go backend runs without a resident browser. It still depends on AI Studio's private RPC and attestation implementation; it is not Google's public supported API and upstream changes can break it. Normal user account authorization is required. No entitlement, free quota, reset time, or permanent login is promised.

## HTTP contract

Use a dedicated nonempty `PROXY_API_KEY` in the Authorization Bearer header. Send exactly one `X-Mixer-Upstream-Channel: playground` or `build`. The authenticated response returns that same header and `X-Mixer-Channel-Locked: 1`, also on upstream failures. A 401 does not acknowledge the lock. These headers attest an enforced routing constraint; on an error they do not imply a request reached Google or consumed quota.

Locking supports `GET /v1/models`, `GET /v1beta/models`, Chat Completions, Responses, and Gemini native `:generateContent` / `:streamGenerateContent` (including SSE). Other routes with this header fail explicitly. Model discovery remains the upstream catalog with `channels`; Mixer filters the catalog and only advertises capabilities it has verified. Unsupported thinking levels must not be synthesized from model names.

Every scheduling attempt reapplies the chosen channel. Build requests requiring Playground-only resources fail; unavailable Build models, account limitations, or cooldowns do not fall back to Playground. A final guard verifies the actual account lease before selecting the generation transport. Unprefixed upstream requests retain upstream behavior. The one process uses the same account worker for both sources and enforces one active worker and one request per account by default.

Model usage records are local accounting, and observed cooldowns are not a remaining-quota API. Display unavailable quota as unknown. Log/storage data may contain user content; the image disables request-body capture and file logs by default.

## Build and run boundary

From the repository root:

```sh
docker build -t geminiapi-mixer-aistudio:local sidecars/aistudio2api
```

The build runs the upstream aistudio, API, app, and config tests, including new authentication, strict-source, quota cooldown, and invalid-resource tests. Tests use fixtures, not a logged-in Google account. The final scratch image includes the static Go binary, CA certificates, timezone database, licenses, and a small static information page. It contains no Node, GTK, Camoufox, WebView2, or browser binary. The Go `GOMEMLIMIT` is a soft GC budget, not measured RAM usage or a hard bound; set container memory limits after measuring your workload.

Run as UID/GID 10001 on a private Docker network, with no public host port, all capabilities dropped, a read-only root filesystem, writable `/data` account/runtime storage, and a bounded `/tmp` tmpfs. Configure a separate admin password and `ADMIN_AUTH_ENABLED=true`; never reuse the client-facing Mixer key. Only Mixer should address this service. Do not expose the administration or account-import routes through OpenFRP.

The minimal image intentionally omits the graphical account importer. Supply explicitly authorized storage state through private local files; never put cookies, session material, keys, or generated proof into the build context, Git, image layers, or documentation. Login expiration must be surfaced for normal reauthorization; do not silently scan unrelated browser profiles.

The optional imported state field `aistudio2api.source.auth_user` preserves Google's existing multi-account index; it selects the already-authorized account and grants no additional access. A source marker other than `chrome` keeps imports out of the upstream Chrome renewal path. See the patch for exact storage schema and tests.

## Attribution

The upstream MIT license is included as `UPSTREAM-LICENSE`; the image preserves upstream, vendored Go VM, and module dependency notices under `/usr/share/licenses`. The patch is distributed under this repository's license. No credentials or Google-generated runtime challenge data are distributed here.
