# 配置、Compose 与 API 集成

## 最小配置

运行入口为 `python -m gemini_web2api`，Python 3.10+（容器/CI 使用 3.12）。
启动时必须设置 `MIXER_API_KEYS` 或旧 JSON 配置中的 `api_keys`。默认禁止匿名服务。
仅本地调试可设置 `MIXER_ALLOW_UNAUTHENTICATED=true`，且必须绑定 loopback。
环境变量覆盖可选旧 JSON；不生成或写回真实配置。

| 环境变量 | 用途 |
|---|---|
| `MIXER_API_KEYS` | 客户端 gateway key，逗号分隔，必填 |
| `MIXER_HOST` / `MIXER_PORT` | 本地默认 127.0.0.1:8081；容器内 0.0.0.0:8081 |
| `MIXER_DEFAULT_MODEL` | 未指定模型时保留 Web 默认；也可设已配置的新别名 |
| `MIXER_WEB_ENABLED` | 默认 true；false 时禁用 Web 路由和列表 |
| `MIXER_WEB_ALLOW_REMOTE_IMAGES` | 默认 false；显式恢复旧 Web 远程图片行为 |
| `BUILD_API_KEY` / `BUILD_MODELS` | Build 公共 API profile 的 key / 逗号模型 allowlist |
| `AIS_API_KEY` / `AIS_MODELS` | Playground 公共 API 替代 profile 的 key / allowlist |
| `GEMINI_WEB_COOKIE_FILE` | 旧 Web Cookie 文件路径；无需交给其他 Provider |
| `GEMINI_WEB_PROXY` | 仅旧 Web Provider 的代理；不传给公共 Provider |
| `ANTIGRAVITY_BASE_URL` | 用户自托管 Sub2API 的 HTTPS origin/base，不含 `/v1` |
| `ANTIGRAVITY_API_KEY` / `ANTIGRAVITY_MODELS` | 专属分组 key / allowlist，均必需 |
| `ANTIGRAVITY_GROUP_CONFIRMED` | 默认 false，确认 key 只走 Antigravity 分组后设 true |
| `ANTIGRAVITY_ALLOW_HTTP` | 非 loopback 内网 HTTP 必须显式 true；公网用 HTTPS |

没有配置 Google key 时，`build-` / `ais-` 请求返回 503；不是免费网页额度。
`build-` 复现 ZIP 中实际的公开 API 传输，`ais-` 是公开 API 替代，二者均不运行网页。
公开 Provider 固定 Google HTTPS host、校验证书、不跟随重定向、不使用环境代理、
不自动重试生成请求；不接受客户端设置上游 URL 或上游认证头。

## 可选纯 Go AI Studio 会话模式

`compose.aistudio.yml` 可把 `ais-` 和 `build-` 切到用户自托管、正常认证的网页会话后端。
该模式无需公开 Gemini API key，运行时不安装或启动浏览器。它使用固定版本私有协议实现，
必须单独验证账号权益和实际能力；操作和多账号设置见 [部署说明](aistudio-http.md)。
下文未特别标注的 build/ais 行为仍描述默认公开 API 模式。

## Compose

复制 `.env.example` 为 `.env` 并填写后执行 `docker compose up --build -d`。
`docker compose config --quiet` 可先验证；不要分享含真实环境变量展开值的配置输出。
`docker compose logs --tail 50` 查看来源、模型、档位、request ID 与错误代码。
健康检查 `/healthz` 不要求认证，也不公开模型或密钥。

需要 Web Cookie 时创建本地 `auth/cookie.json`，文件应可被 UID 10001 读取：

```sh
docker compose -f compose.yml -f compose.web.yml up --build -d
```

`compose.web.yml` 只读挂载 Cookie，源文件不存在时不会自动创建目录。
不要把 Cookie 放进镜像或提交。旧部署保持原样，未自动导出其真实配置/凭据。
需要非默认账号索引、旧 Web 构建标识等历史参数时，可以只读挂载旧 JSON 配置，
通过 `GEMINI_WEB2API_CONFIG` 指向它；`MIXER_API_KEYS` 等环境变量仍优先。

Sub2API 可复用已有部署。容器访问宿主机时可使用
`http://host.docker.internal:<port>`，同时显式 `ANTIGRAVITY_ALLOW_HTTP=true`；
Linux 根据部署需要添加 host-gateway。不要把自己的 gateway 地址配置成上游以免递归。
`ANTIGRAVITY_GROUP_CONFIRMED=true` 是操作者确认，不代表程序已审计远端分组设置。
本项目不接管 Sub2API 登录、OAuth、账号轮换或 Google 私有 RPC。

旧 Web 的远程图片下载器没有内网地址防护和响应大小上限，因此统一入口默认只接受
内嵌图片；只有可信本地客户端需要旧行为时才开启 `MIXER_WEB_ALLOW_REMOTE_IMAGES`。
该开关不会改变 build/ais 的内嵌图片限制。Web 错误正文和日志不回传原异常中的头值。

## 接口

| 入口 | 支持范围 |
|---|---|
| `GET /v1/models` | 来源、实际 transport、能力和可见的别名 |
| `GET /v1beta/models` | 同一来源目录，`ag-` 不广告 Gemini 生成能力 |
| `POST /v1/chat/completions` | 保留 Web；build/ais 转 Gemini；ag 转 Sub2API |
| `POST /v1/responses` | build/ais 为无状态转换；ag 透明转发 |
| `POST /v1beta/models/{alias}:generateContent` | Web/build/ais；ag 明确 501 |
| `POST /v1beta/models/{alias}:streamGenerateContent?alt=sse` | 同上，真实 SSE 或旧 Web 实际回退行为 |
| `GET /v1/quota` 或 `/v1/quota/antigravity` | 认证后的 Sub2API key/钱包/订阅额度 |

请求认证支持 Bearer、`x-api-key`、`x-goog-api-key`；原生客户端也可用 `?key=`，
但推荐请求头。HTTP 日志只记录去除 query 的路径，不记录请求正文、key、Cookie 或 proof。

build/ais 的 Responses 支持 `input`、`instructions`、函数调用/函数结果、图片输入、
`reasoning.effort` 与 SSE 事件；不存储会话，`previous_response_id`、后台任务、
自动截断等返回 400，应传入完整历史。SSE 提供输出项/内容/参数生命周期和真实 usage；
中途错误不发成功终态，Responses 使用 `response.failed`。生成前的上游拒绝保留 HTTP
错误状态。未知使用量保持未知，不估算公共 API token 数。

函数调用只返回函数名和参数，由客户端执行，服务端不会执行模型生成的代码。
真实 `thoughtSignature` 放在 OpenAI `extra_content.google.thought_signature` 或
原生 `thoughtSignature` 中；多轮必须传回，不可自行补造。没有文本的签名 part 也会保留。

quota 返回的 `scope=sub2api-key-wallet` 明确不是 Google 真实余量。周期重置、余额和
无限制标志仅来自上游已知字段。上游 429/权限不足会报错，不轮换身份规避限制。

需要 Antigravity 各模型的账户额度及 reset 时，可显式叠加 `compose.quota.yml`。
隔离采集器持有专用登录文件，Mixer 仅读共享卷中的脱敏快照；默认每 60 秒采集、
600 秒后标记过期，不重写上游观察时间。启用命令、变量、权限边界和已用 100% 的
歧义见 [模型额度部署说明](antigravity-quota.md)。

## 限制与时限

客户端请求体默认 20 MiB、读取超时 60 秒，可用 `MIXER_MAX_REQUEST_BYTES`、
`MIXER_CLIENT_TIMEOUT_SEC` 设置（Compose 可在 override 中补入 environment）。
Google Provider 默认连接 10 秒、读取 60 秒、总计 180 秒；
`PROVIDER_CONNECT_TIMEOUT_SEC`、`PROVIDER_READ_TIMEOUT_SEC`、`PROVIDER_TOTAL_TIMEOUT_SEC`
可覆盖。`PROVIDER_MAX_REQUEST_BYTES` / `PROVIDER_MAX_RESPONSE_BYTES` 默认 20 MiB，
`PROVIDER_MAX_SSE_LINE_BYTES` 默认 1 MiB，模型发现缓存 `PROVIDER_MODELS_CACHE_SEC` 默认 30 秒。
写入断线时关闭上游流，不在断线后排队补发私密结果。

没有用户提供的真实 Google/Antigravity key，不声称完成真实账户生成或真实额度验收。
公开 API 对应账户的模型可见性、区域、计费与额度需要配置后再验证。

## 参考契约

- [Gemini GenerateContent / SSE](https://ai.google.dev/api/generate-content)
- [Google OpenAI 兼容与 thinking 映射](https://ai.google.dev/gemini-api/docs/openai)
- [Google thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures)
- [AI Studio Build 的服务端 key](https://ai.google.dev/gemini-api/docs/aistudio-build-mode)
- [OpenAI Responses SSE](https://developers.openai.com/api/reference/resources/responses/streaming-events)
- [Sub2API 路由，运行版本](https://github.com/Wei-Shaw/sub2api/blob/3040209f205472038c1ba745a1bedd2edd9053b1/backend/internal/server/routes/gateway.go)
- [Sub2API usage schema，运行版本](https://github.com/Wei-Shaw/sub2api/blob/3040209f205472038c1ba745a1bedd2edd9053b1/backend/internal/handler/gateway_handler.go)
