# Build and deployment validation — 2026-10-17

- Upstream revision: `2cbe310ce6ebab093bf83e5078de8296f03770a9`.
- Current patch SHA-256: `f07c3ce55f7a6bac4bf89a863e6bc297a4c80f28935685b2e9ad717141e555c6`.
- Current patch status: rebuilt successfully; all four affected Go package suites passed with network disabled.
- Current image ID: `sha256:3b309dd6c9dbf60b797059626ffcbec57f2ba32cb08fcf072b58bdcd25026ed6`.
- Current image size: 67,390,287 bytes (disk/image size, not RAM); runtime user remains `10001:10001`.

The current patch preserves Google's canonical `/u/N/` navigation without re-adding a removed `authuser=N` query. Initial pages without an account path still receive the selected account query, and conflicting account paths, duplicate account queries, and other origins are rejected. Added synthetic tests cover canonical account paths and the actual redirect loop shape for indexes 1, 2, 3, and 5. RPC account selection, authentication, proof generation, and quota behavior are unchanged.

## Live account and generation evidence

Two user-owned accounts were explicitly imported. Normal HTTPS reads of the official AI Studio pages returned HTTP 200, and each page's current-account email matched its exported identity. No email address or credential is included in this record.

After all Edge processes were closed, `gemini-3.8-flash` passed text and SSE generation through both locked channels:

| Mixer API and access path | AIS text / SSE | Build text / SSE |
| --- | --- | --- |
| OpenAI Chat Completions, existing public OpenFRP endpoint | Pass / Pass | Pass / Pass |
| OpenAI Responses, local endpoint | Pass / Pass | Pass / Pass |
| Gemini native API, local endpoint | Pass / Pass | Pass / Pass |

These are twelve passing generation cases. The initial verification helper incorrectly treated the successful Responses field `error: null` as a failure. Its predicate was corrected and the two affected cases were rerun successfully. Public-endpoint requests were made from the deployment host through the existing public address; no independent second-device test is claimed.

The backend ran only the Go process, with no browser process. During the single-warm-worker tests, the sampled Docker memory peak was **215.3 MiB**, while Mixer was approximately **33–35 MiB**. The two-warm-worker round-robin stage reached a sampled backend peak of **247.6 MiB**; including the Argon stage, the largest observed sample was **248.1 MiB**. These are observed samples from those stages, not guaranteed steady-state usage, exhaustive peak measurements, or a measured percentage saving against WebView2.

The current canonical-navigation patch and `2 warm / 2 active` configuration passed live round-robin verification. Four consecutive requests to the same AIS channel and Flash model all returned HTTP 200. The backend request ledger recorded `account-index-1 → account-index-0 → account-index-1 → account-index-0`, with no failed attempt for any of the four requests. This conclusion is based on actual per-request account selection, not merely the configured worker count.

Both local and public checks returned HTTP 200 for `/healthz`, `/quota`, authenticated `/v1/models`, and authenticated `/v1/quota`. The model catalogue contained 30 entries. Unauthenticated data requests returned HTTP 401. The original Sub2API root entry still returned HTTP 200.

## Argon model status

For AIS/Playground, direct `gemini-4-argon` generation succeeded:

- Non-streaming: HTTP 200, 14.625 seconds.
- SSE: HTTP 200; first visible text at 15.657 seconds, terminal event at 16.188 seconds, three content events.

`ais-gemini-4-argon` is enabled in Mixer and passed the public OpenAI Chat SSE check: HTTP 200, first visible text at **8.297 seconds**, total elapsed **8.875 seconds**, three content events containing 110 characters, and both terminal status and `[DONE]` received. Content arrived in separate transport reads.

The first public attempt reached its client's 90-second total deadline and ended with a ReadError. The backend ledger subsequently recorded HTTP 200/completed at approximately 90 seconds; this does **not** establish that the client received a successful response. Only that AIS public request was retested, with a 180-second client deadline, producing the complete successful result above. Build was not probed again.

Build `gemini-4-argon` returned HTTP 429. Further probing stopped and `build-gemini-4-argon` remains disabled. No remaining-quota amount, reset time, or low/medium/high thinking capability is inferred from these results. Existing text/SSE evidence does not verify image or tool support. Live cancellation and timeout behavior are not claimed by this generation matrix.

## Automated checks and extension scope

The current validation run passed **338 Python tests**, **41 Node tests**, and all **four affected Go package suites** (`internal/aistudio`, `internal/api`, `internal/app`, `internal/config`). Go package checks ran with network disabled. Synthetic tests do not substitute for the live generation evidence above.

Session-export extension v1.1.0 supports `/u/N/` account selection and the email in the current page's bounded `application/json` bootstrap metadata, including random script IDs. The implementation passed independent review and synthetic tests. Existing Edge installations require the user to reload the extension. Reading the actual official page shape and passing fixture tests do **not** establish that the updated extension popup has been exercised successfully in Edge.

## Previous build evidence

The image and passed checks below apply to the previous patch `be16118da6d6ba2c93a47a62adc074fc62ac386a00fd3c6a14c364ef897e3319`, not the current canonical redirect fix.

- Image built locally: `geminiapi-mixer-aistudio:local`.
- Image ID: `sha256:7bab0f57f74103fe84b88e4315edbabd746755bb395a66bffc6e612efd26b10e`.
- Docker-reported image size: 67,389,655 bytes. This is disk/image size, not runtime RAM.
- Final image user: `10001:10001`.

Passed Go package tests: `internal/aistudio`, `internal/api`, `internal/app`, `internal/config`. The final Docker test stage uses `--network=none`. Added tests cover selected Google account indexes, state serialization and redirect restrictions, authentication before accepting a channel lock, malformed/duplicate headers, error acknowledgments, quota cooldown preservation without cross-channel fallback, incompatible Build resource rejection, and a final transport lease check.

A first test iteration incorrectly expected the pool's nonblocking acquisition API to directly return `AllCoolingError`; that assertion was corrected to check the actual candidate/cooldown interface. No quota behavior was changed to satisfy the test.

The built binary's help command passed inside the final image with no network, a read-only filesystem, all capabilities dropped, and `no-new-privileges`. No account import or real Google generation was run as part of these build checks. Login validity, account identity, model capability support, generation, live SSE, and runtime memory require separate deployment verification.
