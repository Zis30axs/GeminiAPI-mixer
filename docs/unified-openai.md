# One Mixer URL and key for Gemini, Claude and GPT

Use the existing Mixer OpenAI-compatible base URL and **Mixer key** for every
listed model. For a deployment behind the documented `/gemini/` reverse proxy,
the client base URL is `https://your-gateway.example/gemini/v1`. An HTTP tunnel
retains its existing scheme and port; it does not encrypt the API key.

| Model ID | Provider | Inference APIs |
| --- | --- | --- |
| Existing Gemini Web names | Gemini Web | Chat, Responses, Gemini native |
| `ais-...` | AI Studio Playground profile | Existing configured capabilities |
| `build-...` | AI Studio Build profile | Existing configured capabilities |
| `ag-...` | Antigravity-only Sub2API group | Chat and Responses |
| Exact allowlisted `gpt-...`, `chatgpt-...`, `o...` IDs | OpenAI-only Sub2API group | Chat and Responses |

`GET /v1/models` returns one combined list. Model rows, response metadata,
source headers and request logs distinguish `source=openai` and
`transport=sub2api-openai` for the new route. Existing model IDs and `ag-`
aliases are unchanged. GPT/o-series names never fall back to Gemini Web, even
when the new provider is disabled or a model is unavailable.

## Configure the upstream once

Create a dedicated inference key in your own Sub2API instance, attached to a
group containing only the OpenAI accounts you authorize. Confirm that the
group has no fallback to other providers. Put its values in the private
deployment `.env`, without sharing them with client applications:

```dotenv
# The Sub2API gateway root, WITHOUT /v1; never use Mixer's own URL here.
MIXER_OPENAI_BASE_URL=http://host.docker.internal:18081
MIXER_OPENAI_API_KEY=replace-with-private-sub2api-inference-key
# Comma-separated exact text model IDs actually returned by this key's /v1/models.
MIXER_OPENAI_MODELS=
MIXER_OPENAI_GROUP_CONFIRMED=true
# Explicitly opt in only for a trusted private HTTP hop.
MIXER_OPENAI_ALLOW_HTTP=true
MIXER_OPENAI_CONNECT_TIMEOUT_SEC=10
MIXER_OPENAI_READ_TIMEOUT_SEC=90
MIXER_OPENAI_TOTAL_TIMEOUT_SEC=180
```

The default `compose.yml` passes these options through. Recreate only the
Mixer service using **all the deployment's existing Compose overlays**;
retain Web-account, AI Studio and quota overlays and their private mounts.
No new container, database or browser process is needed. Leave the new settings
empty and `MIXER_OPENAI_GROUP_CONFIRMED=false` to retain the previous providers
without enabling GPT. Antigravity configuration and quota collection remain
independent.

Only the intersection of the explicit allowlist and the upstream model list
is advertised. Discovery means that the group lists a model, not that a
generation succeeded, quota remains, or every tool/image/reasoning feature is
supported. Exclude audio, realtime, image-generation and internal review models
that require API endpoints this adapter does not expose. Set only reviewed text
model IDs in the allowlist; example names are deliberately not supplied as a
claim of account access.

## DeepSeekHarness or another OpenAI client

Configure one custom OpenAI provider with the Mixer `/v1` base URL, the existing
Mixer key and exact IDs from `/v1/models`. Use the client's `openai-completions`
or `openai-responses` API mode according to the target endpoint. Both
`POST /v1/chat/completions` and `POST /v1/responses` support ordinary JSON and
SSE for the GPT route. The client timeout should cover the selected model's
startup latency and the configured total upstream timeout.

Tools, tool results, inline images, output token limits and reasoning fields
are forwarded to the same selected upstream without protocol conversion.
No artificial `-low`, `-medium`, or `-high` aliases are synthesized for GPT.
Use upstream-supported `reasoning_effort` / `reasoning.effort` values; an
unsupported capability is returned as an upstream rejection. Gemini native
requests targeting GPT explicitly return `501 unsupported_api`.

## Failure behavior and boundaries

- An invalid **Mixer** key returns HTTP 401 before contacting any upstream.
- Rejected upstream credentials or permissions return HTTP 502 with
  `upstream_authentication_failed`; replacing the Mixer key is not the fix.
- Upstream rate limiting returns HTTP 429 `upstream_rate_limited`. Mixer does
  not retry that request on a different provider, account group or model.
- Timeouts return HTTP 504. Unknown or unavailable models return HTTP 404.
  Errors after SSE headers are emitted as error/`response.failed` events, not
  a fabricated successful ending. Client cancellation closes the upstream.
- Failure of OpenAI discovery leaves the existing Gemini/Claude catalog usable.
- Upstream raw errors, secrets and response headers are not copied into error
  output. Only the dedicated upstream key is sent on upstream requests.
- Connections use fixed operator configuration, TLS verification, no ambient
  proxy settings, no redirects, a wall deadline and bounded request/response/
  event sizes. Requests marked as a Mixer upstream hop cannot reenter Mixer,
  preventing a direct configuration loop.
- The quota dashboard continues to report the existing Antigravity observations;
  this change does not invent a GPT remaining-quota or reset-time API.

Offline tests use mock upstreams and cover authentication isolation, combined
catalog discovery, payload preservation, both SSE protocols, cancellation,
truncation, status handling, timeouts and existing-provider compatibility.
