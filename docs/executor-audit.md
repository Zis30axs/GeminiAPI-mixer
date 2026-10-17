# Catiecli Build Executor 与 Playground UserScript 静态审计

审计日期：2026-10-17（UTC+8）。审计对象是用户提供的 ZIP、引用会话可取回的 Playground 脚本片段，以及同日从该脚本声明的提供方 URL 取得的同版本完整副本。未启动执行器、未安装依赖、未连接第三方 WebSocket、未调用 Google、未读取或输出账号密钥。完整副本仅供静态审计，没有纳入生产。

## 结论

Build ZIP 的实际生成路径是公开 Gemini Developer API：`https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`，流式为 `:streamGenerateContent?alt=sse`，通过 `x-goog-api-key` 认证。它提供的是“浏览器执行器 + 第三方 WebSocket 调度”，不包含一个独立的公开 Build 推理 API，也不构成免费、长期或不限额服务的证明。可复用公开 API 请求与原始 Gemini SSE 的接口思路，建议在自有服务端重新实现，密钥保留在服务端。

原包不能直接作为私有数据通道部署：第三方固定 WebSocket 服务控制请求、接收原始响应和错误体。Playground 可见部分还依赖页面内部 MakerSuite RPC 与 WAA 服务捕获；该路线不能当作已授权公开 API，不能通过移植保护机制来完成接入。

## 证据与范围

- ZIP：`C:\Users\Administrator\Downloads\catiecli---build-executor.zip`。
- SHA-256：`b33a1b594a59559367d14b2d2efd90c1d31d34e6bc773bc7d8d89beaa9c28705`，与提供值一致。
- ZIP 共 17 个条目、69,529 字节。未发现加密条目、符号链接、绝对路径、父目录跳转或二进制文件。通过 Python `zipfile` 直接读取文本，没有解压或运行其中代码。
- 逐项检查：`constants.js`、`executor.js`、`websocket.js`、`storage.js`、`state.js`、`vite.config.js`、`package.json`、`metadata.json`、`README.md`、`app.js`、`activity.js`、`dom.js`，并检查保活模块、HTML 与全包网络/执行/DOM 敏感调用。
- 原始 Playground 消息来自会话 `6ad34d74-1974-83ee-806d-45e42a636919` 的 `read_thread`。该用户消息 `truncated: true`，最多可见前 20,000 字符，末尾停在请求头捕获逻辑，无法取回原消息全文。
- 父任务于 2026-10-17 只读取得脚本声明的 `https://catiecli.sukaka.top/api/pg/script.user.js` 副本，保存在本工作区 `work/playground-current.user.js`，共 42,791 字节、1,173 行，标头 `@version 1.2.0`。SHA-256：`117d29f7db5ec5ea2379faaa661224d17544caa91779d6cf58fa4a45b2eb207b`。已完成此副本的全文静态检查；由于原消息截断，不能证明它与用户粘贴的原始完整脚本逐字一致。下载未执行，不作为可信更新安装。
- 已应用 `code-security` 及 secrets、insecure-transport、code-injection、XSS、prototype-pollution、SSRF 规则。已检查 Semgrep 工作流，但本环境没有 Semgrep 工具、命令或 Python 模块；未声称完成 Semgrep 扫描或依赖漏洞扫描。

下文文件与行号均指 ZIP 内源文件，不指重新生成或格式化后的文件。

## Build 数据路径与认证

1. `constants.js:4` 将 WebSocket 地址固定为 `wss://catiecli.sukaka.top/api/build/ws`。
2. `websocket.js:158–166` 连接后发送 `{type: "register", clientVersion, apiKey: userAPIKey}`。这是 Catiecli 站点凭证，不是 Google key。
3. `websocket.js:329–370` 接受 `registered`、`error`、`cancel` 与 `request` 消息；收到 `request` 后执行 Google 请求。
4. `constants.js:5,8–12`、`executor.js:111–122` 将 Gemini JSON 请求提交到固定 Google HTTPS 域名，用 `BUILD_API_KEY` 放入 `x-goog-api-key`。模型名受 1–160 字符的字母数字、点、下划线、连字符正则限制，并经过 URL 编码，未见任意目标 URL 或本地文件读取路径。
5. `executor.js:71–108,216–223` 经 WebSocket 返回原始 Gemini JSON、SSE 数据、状态及完整原始错误体。因此该第三方服务位于提示词和输出的数据链路中。TLS 只能保护传输，不会让服务运营方看不到明文业务数据。

## 发现

| 级别 | 发现 | 证据与影响 | 接入要求 |
|---|---|---|---|
| 高 | 本地构建会把实际 Google key 编入浏览器代码 | `vite.config.js:4–5,16–18` 使用 `define` 将环境变量替换为字面量；`executor.js:118` 在浏览器请求中使用它。环境变量本身不等于服务端保密。 | 自有服务端读取环境变量；不把实际 key 编入网页。AI Studio 托管平台是否另有 key 替换/代理机制，无法仅从此 ZIP 证实。 |
| 高 | 固定第三方 WS 是业务数据与调度信任边界 | `constants.js:4`；注册发送站点 key，第三方下发原始请求，接收全部生成结果。 | 默认不连接；自有 Sidecar 使用自有地址、独立认证和权限边界。禁止把 Google cookie、WAA proof 或站点凭证转发给外部调度服务。 |
| 中 | 缺少执行总时限、读空闲时限和资源上限 | `websocket.js:140–156` 仅有 WS 握手前 6 秒超时；`executor.js` 未给 fetch 或 reader 设置独立 deadline；无并发、请求字节、响应字节或 SSE 单事件大小上限。 | 设置连接、总请求和流空闲超时；限制并发及输入/输出字节；超限返回明确错误并取消上游。 |
| 中 | 断线缓冲可静默截断流 | `websocket.js:56–61` 在超过 200 条时删除最旧消息；`:76–84` 丢弃超过 10 分钟的消息。剩余 `stream-end` 仍可能被发送。 | 不能宣称无损续传。采用有序序号与确认机制，或断线/溢出时终止整个请求并发出明确流错误。 |
| 中 | 现有背压函数未用于结果发送 | `websocket.js:39–50` 定义缓冲控制；实际 `deliverResult` 在 `:56–57` 调用直接 `socket.send`，`flushOutbox` 也连续直接发送。 | 统一使用有字节上限和取消支持的发送队列；缓冲溢出明确失败。 |
| 中 | 浏览器保存站点 key | `storage.js:46–63` 默认 sessionStorage，可选 localStorage；同源脚本和页面脚本漏洞可访问它。 | 私有服务使用服务端环境变量或安全凭证存储；默认不持久化浏览器凭证。 |
| 中 | 错误内容无脱敏原样回传 | `executor.js:182–187,216–223` 把 Google 原始错误体发送给第三方；`activity.js:99–120` 可显示原始错误。UI 截断不限制上传的原始体。 | 对外返回结构化错误与请求 ID；日志/返回元信息严格剔除 key、cookie、proof、敏感请求头。 |
| 低 | 无可复现依赖版本证据 | `package.json:12` 为 `vite: ^6.2.0`，包中无 lock；`.gitignore:15–16` 忽略两类 lock 文件。 | 新实现提交锁文件并审计实际解析版本；此报告没有把版本范围推断为已验证的安全版本。 |
| 信息 | 未见明显动态代码执行或动态 HTML 注入 | 包内未见 `eval`/`new Function`；业务日志、错误和状态使用 `textContent`，`innerHTML` 只用于固定空态或清空。 | 该有限静态观察不能替代运行时、依赖和后端审计。 |

另有注册阶段限制不足：结果发送只检查 WebSocket OPEN，不检查注册状态；注册成功没有单独等待超时。若自建调度通道，需在协议层规定注册、请求、取消和结果各状态的合法顺序，防止重连期间越序交付。

## Build 能力核对

| 能力 | 原包实际提供 | 尚需完成 |
|---|---|---|
| Gemini 原生文本/多模态 JSON | 非流式与流式原样透传。只检查 JSON 对象中存在数组 `contents`（`executor.js:230–232`）。 | 请求 schema、模型支持和大小校验。 |
| 图片 | 未专门转换、下载、上传或校验图片。合法 `inlineData` 等字段可能随 JSON 透传。 | 按模型核验输入/输出图片；超限和不支持应拒绝；不要把“可透传”表述为端到端已验证。 |
| 工具调用 | 没有执行工具；Gemini `tools`、`functionCall`、`functionResponse` 仅可能作为 JSON 数据透传。 | 验证工具 schema 和多轮顺序，保留调用 ID 与思考签名；不支持模式明确报错。 |
| SSE | 支持 LF/CRLF 事件分隔、多 `data:` 行、UTF-8 增量解码、JSON 解析；上游嵌入 `error` 会抛错（`executor.js:125–170`）。 | 端到端背压、超时、断线、取消、溢出和不完整事件测试。 |
| 取消 | `websocket.js:360–363` 中止已进入 active map 的请求；主动断开/卸载会中止所有请求。 | 排队和派发前的取消、客户端断开传递、已完成/重复 ID、重连恢复语义；这些未经过运行测试。 |
| 异常 | 保留 HTTP 状态、Google 错误状态与原始体；网络错误归为 502；取消归为 499 且已取消时不再回传错误。 | 公共接口统一错误结构、脱敏、重试策略和流开始后的失败信号。 |
| 思考等级 | 无 `low`/`medium`/`high` 解析或模型支持表；任何 `generationConfig` 都由请求 JSON 决定。 | 从当前官方模型能力核实后映射；不能仅改模型名或忽略不支持等级。 |
| 来源/模型目录 | 没有 `build-`/`ais-` 别名、`/v1/models` 或来源元信息实现。 | Provider 路由层返回真实来源、上游模型与实际 thinking 设置。 |
| Chat Completions / Responses | 未实现。 | 独立协议适配及有意义的契约测试。 |
| 私有 Sidecar / Compose | ZIP 无后端、无 WS 服务实现、无 Docker/Compose。 | 独立实现自有服务端；不把第三方 WS 改名当作已审计 Sidecar。 |

## Playground 全文副本补审

以下行号指 2026-10-17 下载的完整副本；与原消息可见前缀一致的关键行为包括：

- UserScript 标头版本是 `1.2.0`，内部 `CLIENT_VERSION` 是 `1.1.0`；自动更新和下载 URL 均指向第三方站点。本次只读下载该地址供审计，没有执行、安装或启用自动更新。
- `GENERATE_URL` 是 `https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GenerateContent`，属于页面内部命名的 MakerSuite RPC。
- `WS_URL` 固定为与 Build 相同的第三方 WebSocket；配置只允许设置站点 API key。
- 运行于 `aistudio.google.com/*`，使用 `unsafeWindow` 和 GM 存储。通过扫描 `default_MakerSuite` 函数源码特征包装 snapshot 函数，捕获页面 WAA service；说明要求用户手动发一条消息引导，并捕获 GenerateContent 显式请求头。
- 暴露在页面全局的状态包括 WAA service 和捕获头；在广域匹配和页面内 hook 下，凭证与保护材料的可见性、生命周期与失效行为必须单独审计。
- 全文确认使用串行队列、AbortController、取消 ID 与 outbox 200 条/10 分钟，但并未因此提供完整的超时、限额或可靠交付保障。

补审的新发现：

| 级别 | 发现 | 全文副本证据与影响 |
|---|---|---|
| 高 | 页面保护材料出现在页面全局，且异常可能把部分材料送至第三方 | `:519,565` 保存完整显式请求头；`:468` 附近保存 WAA service。`:623–625` 在 proof 不符合前缀预期时将其前 40 字符写进错误消息；`:911–916,994–1002` 将此消息发送第三方 WS 并记录到页面日志。正常成功路径没有发现直接将 proof/cookie 对象单独回传 WS 的代码，但不能据此保证上游原始错误体从不含敏感值。 |
| 高 | 私有 RPC 请求直接接受第三方下发的 wire 内容 | `:809–835` 接受 `wire` 数组和 `binding`，除基础类型外没有模型、结构、功能、大小或权限白名单；在登录页面上下文请求固定 Google RPC，`credentials: "include"`。第三方控制的消息因此可以消耗当前登录会话能力。该路线不应作为公开授权 API 接入。 |
| 中 | stream 标志没有提供真实增量流 | `:847` 先 `await resp.text()` 收完全文；`:892–900` 以一条 `stream-chunk` 加 `stream-end` 回传。不能标为 token 级 SSE，也没有图片或工具的协议转换。 |
| 中 | 队列、取消集合与单次结果没有字节上限 | `:860–862` 的 Promise 队列无长度上限；`:1082` 可为任意 requestId 加入取消集合，未执行的无关 ID 不会自动清理；`:847` 整体缓冲原始响应。缺少执行总时限、WS 连接/注册超时、流空闲超时与去重限制。 |
| 中 | 取消无法终止全部前置步骤，断连也不中止执行 | fetch 有 AbortSignal，但页面输入同步和 snapshot/proof 调用没有取消信号；`:1011–1024,1101–1117` 断连和重连过程没有遍历中止 active controllers。队列中的取消任务可能直接返回而无统一终态。 |
| 中 | 请求头捕获只比对路径，且无明确头部白名单 | `:501,562` 比对 pathname，不核验捕获请求的目标 origin；`:828` 拷贝所有捕获显式头并复用。需要把认证材料限制在其原始授权用途内，而非复制进通用桥接协议。 |
| 中 | 结果交付仍非可靠续传 | `:934–940` 直接 send，无背压、确认或字节限制；`:957–958,977–984` 按条数和 TTL 静默删除数据。`sendError` 对 stream 请求也固定使用 `type: "error"`，后端如何映射未知。 |
| 信息 | 未发现其他执行型远程加载或动态代码求值 | 全文未见 `eval`、`new Function`、动态 script 注入、任意远程 fetch 目标或额外 WebSocket 目标。脚本管理器根据标头实施的第三方自动更新仍是独立供应链边界，本文不把未来更新视为已审计内容。 |

此副本没有 low/medium/high 的模型能力表、来源模型目录、OpenAI Chat/Responses 转换或自有 Sidecar 后端。不能把其通过网页保护生成请求的能力解释为 Google 已公开授权的 API，也不能把单条全文 chunk 包装解释为真正流式。

## 可复用边界与接入建议

1. 保留原 Gemini Web Provider。将来源路由、协议转换、能力校验、日志脱敏与上游传输分层，公开 API adapter 在服务端直接调用 Google。
2. `build-` 只有在真实接入用户控制的 Build 部署/执行器时才表明 Build 来源；若只是直接调用 Gemini Developer API，应明确标记实际 transport，不能以别名制造 Build 页面来源。`ais-` 不能在回退到另一个 Provider 后继续宣称 Playground 来源。
3. 默认不启用任何第三方 WS、页面私有 RPC 或 WAA hook。Playground Provider 可保留明确不可用/需授权实现的状态，并返回可操作的错误；公开 API 可以作为单独、真实标注来源的 Provider。
4. low/medium/high 必须绑定模型特定的已核验能力，转换到明确的原生参数，且在模型目录、日志和返回元信息中保留实际应用值。不支持时返回 400/422，不能静默丢弃、自动近似或伪造能力。
5. 此次完整下载副本已经完成网络目标、自动更新、凭证处理、序列化与协议状态的静态补审；不要安装即运行，也不要使用用户账号去验证保护绕过。若获取用户原始完整附件，应另外核对哈希或差异后再将审计结论用于该附件。

## 未覆盖与验收限制

未覆盖：用户原消息截断后的原始逐字内容（已审计同版本下载副本）、Catiecli 后端源码与部署、第三方站点实际运行行为、AI Studio 托管内部代理、任何用户 Google 账号、真实 key/cookie/proof、Google 当前模型和额度、第三方依赖解析版本/CVE、实际流式/图片/工具/思考请求、负载测试与运行时网络抓包。没有合法凭证和用户控制部署时，这些能力不能宣称已联调或已上线。
