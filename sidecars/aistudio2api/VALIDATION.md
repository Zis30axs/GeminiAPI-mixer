# Build and deployment validation — 2026-10-17

- Upstream revision: `2cbe310ce6ebab093bf83e5078de8296f03770a9`.
- Current patch SHA-256: `fbaa64f280c32fc01d72359c3ba3433ae5232580e6246ed13d038c021aade877`.
- Current patch status: rebuilt successfully; all four affected Go package suites passed with network disabled.
- Current image ID: `sha256:913595c0c1ec7e0c2df99a04d49891994146d01d3affa3be2dba82091ad5c5a9`.
- Current image size: 67,418,676 bytes (disk/image size, not RAM); runtime user remains `10001:10001`.

The current patch preserves Google's canonical `/u/N/` navigation without re-adding a removed `authuser=N` query. Initial pages without an account path still receive the selected account query, and conflicting account paths, duplicate account queries, and other origins are rejected. Added synthetic tests cover canonical account paths and the actual redirect loop shape for indexes 1, 2, 3, and 5. RPC account selection, authentication, proof generation, and quota behavior are unchanged.

The thinking-level update exposes only explicit `ListModels` enum values and checks the actual leased account before generation. Source-locked requests cannot silently round a requested level to a supported neighbor or discard it. Successful parameter validation is acknowledged as `X-Mixer-Thinking-Level` plus `X-Mixer-Thinking-Validation: account-catalog`. Tests verify the exact Playground enum slot and Build JSON field for LOW, MEDIUM and HIGH, strict refusal before protected transport, source/metadata handling and unchanged unlocked compatibility behavior. This acknowledgment is not a Google echo of internal reasoning compute.

## Live account and generation evidence

Five user-owned accounts have now been explicitly imported, including three added during the thinking-level verification stage. Normal HTTPS reads of the official AI Studio pages matched the current-account email to each of the five exported identities. No email address or credential is included in this record. The deployment retains `2 warm / 2 active` to control memory; importing five accounts does not establish successful generation or complete round-robin coverage for all five.

During the earlier two-account stage, after all Edge processes were closed, `gemini-3.8-flash` passed text and SSE generation through both locked channels:

| Mixer API and access path | AIS text / SSE | Build text / SSE |
| --- | --- | --- |
| OpenAI Chat Completions, existing public OpenFRP endpoint | Pass / Pass | Pass / Pass |
| OpenAI Responses, local endpoint | Pass / Pass | Pass / Pass |
| Gemini native API, local endpoint | Pass / Pass | Pass / Pass |

These are twelve passing generation cases. The initial verification helper incorrectly treated the successful Responses field `error: null` as a failure. Its predicate was corrected and the two affected cases were rerun successfully. Public-endpoint requests were made from the deployment host through the existing public address; no independent second-device test is claimed.

The backend ran only the Go process, with no browser process. During the single-warm-worker tests, the sampled Docker memory peak was **215.3 MiB**, while Mixer was approximately **33–35 MiB**. The two-warm-worker round-robin stage reached a sampled backend peak of **247.6 MiB**; including the Argon stage, the largest observed sample was **248.1 MiB**. These are observed samples from those stages, not guaranteed steady-state usage, exhaustive peak measurements, or a measured percentage saving against WebView2.

The canonical-navigation patch and `2 warm / 2 active` configuration passed live round-robin verification during that two-account stage. Four consecutive requests to the same AIS channel and Flash model all returned HTTP 200. The backend request ledger recorded `account-index-1 → account-index-0 → account-index-1 → account-index-0`, with no failed attempt for any of the four requests. This conclusion is based on actual per-request account selection, not merely the configured worker count, and does not claim coverage of the three subsequently added accounts.

During that stage, both local and public checks returned HTTP 200 for `/healthz`, `/quota`, authenticated `/v1/models`, and authenticated `/v1/quota`. The model catalogue then contained 30 entries. Unauthenticated data requests returned HTTP 401. The original Sub2API root entry still returned HTTP 200.

## Argon model status

For AIS/Playground, direct `gemini-4-argon` generation succeeded:

- Non-streaming: HTTP 200, 14.625 seconds.
- SSE: HTTP 200; first visible text at 15.657 seconds, terminal event at 16.188 seconds, three content events.

`ais-gemini-4-argon` is enabled in Mixer and passed the public OpenAI Chat SSE check: HTTP 200, first visible text at **8.297 seconds**, total elapsed **8.875 seconds**, three content events containing 110 characters, and both terminal status and `[DONE]` received. Content arrived in separate transport reads.

The first public attempt reached its client's 90-second total deadline and ended with a ReadError. The backend ledger subsequently recorded HTTP 200/completed at approximately 90 seconds; this does **not** establish that the client received a successful response. Only that AIS public request was retested, with a 180-second client deadline, producing the complete successful result above. Build was not probed again.

Build `gemini-4-argon` returned HTTP 429. Further probing stopped and `build-gemini-4-argon` remains disabled. The initial base-model results above did not establish low/medium/high thinking support; the subsequent AIS-only evidence is recorded below. No remaining-quota amount or reset time is inferred from successful generation. Existing text/SSE evidence does not verify image or tool support. Live cancellation and timeout behavior are not claimed by this generation matrix.

## AIS Argon thinking-level verification

The actual `ListModels` catalogue explicitly listed `high`, `low`, and `medium` for `gemini-4-argon`. Each AIS/Playground level was tested separately with a native non-streaming request and a native SSE request. All six requests returned HTTP 200 with the requested uppercase `X-Mixer-Thinking-Level` and `X-Mixer-Thinking-Validation: account-catalog` acknowledgment.

| Level | Non-stream elapsed | SSE first visible text | SSE terminal |
| --- | --- | --- | --- |
| low | 17.344 s | 8.297 s | 8.875 s |
| medium | 28.578 s | 14.828 s | 15.469 s |
| high | 7.906 s | 13.782 s | 14.157 s |

Every SSE request produced three content events across five transport reads, 110 visible characters, and a `STOP` terminal status. Incremental arrival in separate reads was observed. The safe probe summaries contain timings and counts, with no generated answer, email, service key, cookie, or proof.

The Go backend validates the exact level against the actual selected account's catalogue before generation. Synthetic wire tests separately confirm the precise Playground enum slot and Build JSON field, while Mixer rejects absent or mismatched validation headers. This establishes exact supported parameter transmission and successful generation; it does not establish a Google echo of internal computation, or an ordering of compute intensity from these sample timings.

Mixer now enables `ais-gemini-4-argon-low`, `ais-gemini-4-argon-medium`, and `ais-gemini-4-argon-high`; the current catalogue contains 33 entries. This level support is restricted to AIS. Build Argon and its thinking aliases remain disabled after the earlier HTTP 429.

In addition to the six backend native probes above, the existing public OpenFRP endpoint passed these three Mixer cases:

| Requested level and input | Mixer API | Observed result |
| --- | --- | --- |
| `ais-gemini-4-argon-low` alias | Chat Completions SSE | HTTP 200; first text 25.578 s; total 26.360 s; three content events, `STOP`, and `[DONE]` |
| Base model with `reasoning.effort=medium` | Responses, non-streaming | HTTP 200; `completed`; total 24.515 s |
| Base model with `thinkingConfig.thinkingLevel=HIGH` | Gemini native SSE | HTTP 200; first text 10.063 s; total 10.500 s; three content events and `STOP` |

Each result's `x_mixer` source and effective level matched AIS and the requested low, medium, or high level. Both SSE cases showed incremental delivery. The public `/v1/models` actually returned 33 entries, including the Argon base alias and all three thinking aliases. These are three representative public cases, not a claim that every combination of three levels, three API formats, and text/SSE modes has been exercised live.

The latest local and public `/healthz`, `/quota`, authenticated `/v1/models`, and authenticated `/v1/quota` checks all returned HTTP 200. Unauthenticated data endpoints returned HTTP 401, and the original Sub2API root entry remained HTTP 200.

## Automated checks and extension scope

The current validation run passed **356 Python tests** and all **four affected Go package suites** (`internal/aistudio`, `internal/api`, `internal/app`, `internal/config`). The **41 passing Node tests** apply to the unchanged extension implementation from the preceding stage; JavaScript was not changed in this iteration. Go package checks ran with network disabled. Synthetic tests do not substitute for the live generation evidence above.

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
