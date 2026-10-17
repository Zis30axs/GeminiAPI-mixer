# GeminiWeb2API 上游能力与授权边界核实

核实日期：2026-10-17。本文依据官方文档及公开项目源码；没有使用用户密钥、Cookie、OAuth 材料或 WAA proof，没有运行第三方项目，也没有对真实账户发起付费推理。文档支持不代表用户账户实际可用。

## 1. 通道与来源标注

Google 的公开授权推理接口是 Gemini API：`https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`；SSE 使用 `:streamGenerateContent?alt=sse`。密钥使用 `x-goog-api-key` 请求头，避免 URL 查询参数进入日志。[官方接口](https://ai.google.dev/api/generate-content)

AI Studio Build 是生成、运行和部署应用的平台。官方部署流程使用服务端保存的 `GEMINI_API_KEY`，支持 Cloud Run 或下载后自行部署。未找到一个独立、公开且稳定的 Build 网页推理 API 契约。`build-` 应对应用户控制并授权的 Build 应用 endpoint；直接调用 Gemini API 时必须明确标注真实 transport，不能声称流量经过 Build 网页。[Build 官方文档](https://ai.google.dev/gemini-api/docs/aistudio-build-mode)

Playground 的 MakerSuite 私有 RPC、WAA 运行时和 proof 捕获不是公开 Gemini API。若使用官方 API 作为替代，来源元信息应明确 `transport=google-gemini-api`；若必须维持 `ais-` 命名，则同时说明它是 AI Studio 项目/API-key 路由，并非已验证的 Playground 网页通道。不可把固定第三方 WebSocket 地址视为私有数据通道。

## 2. 思考等级

模型别名只能枚举经文档和上游实际模型列表确认的能力。建议记录 `requested_effort`、`effective_thinking_level` 或 `effective_thinking_budget`，以及映射依据。

| 模型 | low / medium / high | 原生 GenerateContent 参数 |
| --- | --- | --- |
| `gemini-2.5-pro`、`gemini-2.5-flash`、`gemini-2.5-flash-lite` | 可按 Google 官方兼容映射支持三档 | `thinkingBudget`: 1024 / 8192 / 24576 |
| `gemini-3-pro-preview` | low、high；medium 应拒绝 | `thinkingLevel` |
| `gemini-3.1-pro-preview` | low、medium、high | `thinkingLevel` |
| `gemini-3-flash-preview` | low、medium、high | `thinkingLevel` |
| `gemini-3.8-flash` | low、medium、high；minimal 明确不支持 | `thinkingLevel` |
| 未核实型号、图片专用型号 | 不自动生成三档别名 | 按具体型号核实 |

Gemini 2.5 的 GenerateContent 不支持 `thinkingLevel`，必须使用 budget；表中三个 budget 是 Google 官方 OpenAI 兼容层公布的映射。`reasoning_effort` 和显式 thinking level/budget 不能同时使用。[官方映射](https://ai.google.dev/gemini-api/docs/openai#thinking)、[GenerateContent 思考参数](https://ai.google.dev/gemini-api/docs/generate-content/thinking)、[当前模型档位表](https://ai.google.dev/gemini-api/docs/thinking#controlling-thinking)

## 3. 协议、图片、工具与流式

Google 官方 OpenAI 兼容文档提供 Chat Completions、SSE、函数调用及图像输入契约；未提供 Responses 端点契约。因此网关的 `/v1/responses` 需要本地转换。存储、后台任务、远端文件等未实现语义应明确报错。[OpenAI 兼容文档](https://ai.google.dev/gemini-api/docs/openai)

常规 2.5 文本模型、3.8 Flash 支持图像输入和函数调用，输出为文本，不支持图片生成。`gemini-3.1-flash-image` 支持图片输出，但不支持 function calling 或 structured outputs。不能根据模型名包含 Gemini 就统一宣称全功能。[2.5 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash)、[2.5 Pro](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-pro)、[2.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite)、[3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)、[3.1 Flash Image](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-image)

Gemini 3 工具往返需要保存真实 thought signature；OpenAI 兼容形式为 `tool_calls[].extra_content.google.thought_signature`。丢失签名可能造成 400。流式空文本 part 也可能携带签名，必须解析至真实终止状态，不能只收集可见文本。[签名官方文档](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)

`/v1/models` 应取配置、已实现能力与账户真实模型列表的交集；不能把文档示例当作账户可用证明。[模型发现 API](https://ai.google.dev/api/models)

## 4. Gemini 相关公开项目

| 项目 | 核实结果 | 本次适用性 |
| --- | --- | --- |
| [google-gemini/proxy-to-gemini](https://github.com/google-gemini/proxy-to-gemini) | Apache-2.0；官方 API-key sidecar。README 已说明原生 OpenAI 兼容上线后无需该 sidecar；旧实现仅文本，工具尚未完成 | 可以参考接口适配，不能承担全部要求 |
| [WAcry/AIStudio2API](https://github.com/WAcry/AIStudio2API) | MIT；Playwright UI 自动化，主要为 chat/models | 不能直接假定支持 Responses、图片或真实工具调用 |
| [Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API) | MIT；[开发文档](https://github.com/Mag1cFall/AIStudio2API/blob/main/docs/development.md)明确依赖 MakerSuite 私有 RPC 与 WAA/fresh proof | 代码许可证不代表 Google 授权该访问方式；不引入其保护机制实现 |

## 5. Antigravity 官方边界

Google 现已公开预览 Antigravity agent：`POST https://generativelanguage.googleapis.com/v1beta/interactions`，使用 `x-goog-api-key`，agent 为 `antigravity-preview-09-2026`。它是托管 agent workflow，支持文本/图片输入、自定义函数和 SSE，并提供 `agent_config.max_total_tokens` 预算控制。这与把 IDE 订阅额度转换为 raw-model Chat API 的社区项目不同，接入时必须标明真实产品和计费来源。[官方 Antigravity agent](https://ai.google.dev/gemini-api/docs/antigravity-agent)

官方 Antigravity 产品提供 Settings、CLI `/usage`（别名 `/quota`）和 `/credits` 查看用量。公开计划文档说明配额和刷新周期可变，并由工作量决定；未找到稳定、公开的订阅额度 REST JSON 契约。产品订阅配额、AI credits 和 Gemini API 项目用量必须分别显示。[计划与额度](https://antigravity.google/docs/plans)、[CLI 配额](https://antigravity.google/docs/cli/commands/usage)、[Credits](https://www.antigravity.google/docs/cli-credits)

## 6. Sub2API 自托管服务契约

核实公开 main 提交：`b0a02a3304a0a78cdd003ddda1dd23ba02c03084`。本机 `D:\.sub2api\app` 只有部署目录；只看了目录和 Compose 镜像/端口声明，没有读取 `.env`、数据库或账户材料。本机部署的实际版本、API key 和连通性尚未验证。

### 推理和鉴权

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| GET | `/v1/models` | 当前 key 可见模型 |
| POST | `/v1/chat/completions` | 按 key 分组平台路由 |
| POST | `/v1/responses` | 按 key 分组平台路由 |
| GET | `/v1/usage` | 当前 key 的额度/钱包/订阅和统计 |
| GET | `/v1/sub2api/billing` | 仅计费倍率，不是余额 |

鉴权使用自托管 Sub2API 发行的 `Authorization: Bearer <API_KEY>`；也支持 `x-api-key`。若要声明来源 Antigravity，必须把 key 绑定到 Antigravity 专属分组；任意混合分组不能保证来源。专用 `/antigravity/v1` 路由目前注册 messages、models、usage，没有 chat/completions 或 responses。路由、字段和能力可能随部署版本变化，adapter 应接受明确配置，禁止静默降级或换模型。[路由源码](https://github.com/Wei-Shaw/sub2api/blob/b0a02a3304a0a78cdd003ddda1dd23ba02c03084/backend/internal/server/routes/gateway.go)、[鉴权源码](https://github.com/Wei-Shaw/sub2api/blob/b0a02a3304a0a78cdd003ddda1dd23ba02c03084/backend/internal/server/middleware/api_key_auth.go)

### `/v1/usage` 返回值

返回 JSON 没有 `data` envelope。精确字段来自[Usage handler](https://github.com/Wei-Shaw/sub2api/blob/b0a02a3304a0a78cdd003ddda1dd23ba02c03084/backend/internal/handler/gateway_handler.go)。

- `mode="quota_limited"`：`isValid`、`status`；可选 `quota={limit,used,remaining,unit:"USD"}`、顶层 `remaining`/`unit`、`rate_limits`。
- `rate_limits[]`：`window` 为 `5h`、`1d`、`7d`；含 `limit`、`used`、`remaining`、`window_start`，可能有 `reset_at`。
- `mode="unrestricted"` 的钱包模式：`isValid`、`planName`、`remaining`、`balance`、`unit="USD"`。
- 订阅模式：`remaining` 及可选 `subscription`，后者含 `daily_usage_usd`、`weekly_usage_usd`、`monthly_usage_usd`、相应 `*_limit_usd`、`weekly_window_start`、`expires_at`。没有配置任何周期限额时，计算结果 `remaining=-1` 表示无限制。
- 可选 `usage`、`daily_usage`、`model_stats` 为统计数据，缺失可能是 best-effort 查询失败，不表示零消耗。

以上数值是该 Sub2API 实例对 key、钱包或订阅的分配/计费额度，不等于 Google 账户真实剩余配额。字段缺失、HTTP 失败、无管理权限必须表示 unknown/unavailable；不能显示 0。已耗尽或过期 key 仍允许通过 `/v1/usage` 查询自身用量，基础身份和 IP 权限检查仍会执行。

面板 `/api/v1/user/profile`、`/api/v1/subscriptions/*` 属另一组 JWT 鉴权接口，不应把普通推理 key 当作管理员凭据去试探。[用户路由](https://github.com/Wei-Shaw/sub2api/blob/b0a02a3304a0a78cdd003ddda1dd23ba02c03084/backend/internal/server/routes/user.go)

## 7. 另一候选：antigravity2api-nodejs

对 [liuw1535/antigravity2api-nodejs](https://github.com/liuw1535/antigravity2api-nodejs) 的只读源码核对结果：`/v1/models` 和 `/v1/chat/completions` 已注册；未在 OpenAI 路由看到 `/v1/responses`。推理接口使用平台配置的 API key，`Authorization: Bearer` 或 `x-api-key`。[推理路由](https://github.com/liuw1535/antigravity2api-nodejs/blob/main/src/routes/openai.js)、[服务入口](https://github.com/liuw1535/antigravity2api-nodejs/blob/main/src/server/index.js)

管理额度接口当前为 `GET /admin/tokens/:tokenId/quotas`，使用管理 JWT Bearer 或 `authToken` Cookie，与推理 API key 不同。返回 `{success:true,data:{lastUpdated,models:{<model>:{remaining,resetTime,resetTimeRaw}},requestCounts,tokenMeta?}}`；`remaining` 是比例值。禁用账户且无缓存时可返回空 models、`lastUpdated=null`，不能解释成额度为零。[管理路由](https://github.com/liuw1535/antigravity2api-nodejs/blob/main/src/routes/admin.js)

旧 `QUOTA_FEATURE.md` 仍展示 refreshToken 作为 URL 参数，已与当前源码的 tokenId 路由不符；不能照旧文档把 Google refresh token 放进 URL。本次只核对自托管服务表面契约，不实现其 Google 私有协议或凭据管理逻辑。

## 8. 网关建议的统一额度元信息

建议至少包含：`source`、`transport`、`scope`（key/wallet/subscription/upstream）、`status`（available/unknown/unavailable/unlimited）、`observed_at`、`unit`、可空的 `remaining`、窗口和 `reset_at`、以及服务版本/契约类型。保留真实数字 0；缺值用 null；Sub2API 的 -1 应转成明确 unlimited 状态。不要把缓存时间当作上游刷新时间，也不要从请求次数自行推算 Google 官方额度。
