# 自托管 AI Studio 纯 Go 后端

此模式复用 [Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API/tree/2cbe310ce6ebab093bf83e5078de8296f03770a9)
提交 `2cbe310ce6ebab093bf83e5078de8296f03770a9`，运行时不安装浏览器、WebView2 或 Node。
首次登录或登录失效时，用户仍需通过正常 Google 登录取得自己的会话。
源码、补丁与第三方许可证说明在 `sidecars/aistudio2api/`。

这使用网页私有协议和上游 WAA 实现，不是 Google 对外承诺兼容的公共 API。
没有额外付费 API key 需求不代表账号一定有可用额度；服务器的权限拒绝、模型限制和额度限制仍生效。
不创建新身份、不自动加入其他账号、不把失败请求切到 Web 或公开 API。

## 本机实测状态（2026-10-17）

两个自有账号已主动导入；分别使用所选会话读取 AI Studio 官方页面，页面邮箱与导出信息匹配。
关闭全部 Edge 进程后，`gemini-3.8-flash` 的 AIS、Build 两通道均完成以下验证：

| 入口 | AIS 文本 / SSE | Build 文本 / SSE |
| --- | --- | --- |
| Chat Completions，经现有 OpenFRP 外网入口 | 通过 / 通过 | 通过 / 通过 |
| Responses，本机入口 | 通过 / 通过 | 通过 / 通过 |
| Gemini 原生，本机入口 | 通过 / 通过 | 通过 / 通过 |

以上共 12 项。最初的验证辅助程序把 Responses 成功响应中的 `error: null` 误判为错误，
修正判定后重新执行对应两项并通过。外网入口测试由本机经现有公共地址发起，不代表第二台外部设备验证。
后端运行的是 Go 进程，没有浏览器进程；单预热 Worker 阶段 Docker 采样峰值为 **215.3 MiB**，
Mixer 约 **33–35 MiB**。双预热配置的轮询验证阶段后端采样峰值为 **247.6 MiB**，
合并 Argon 验证阶段后的最大采样值为 **248.1 MiB**。
这些是本次测试期间的采样值，不是完整负载峰值或长期内存保证。

AIS 的 `gemini-4-argon` 也已直接验证：文本 HTTP 200，用时 14.625 秒；
SSE HTTP 200，首段文本 15.657 秒、终态 16.188 秒，共 3 个内容事件。
`ais-gemini-4-argon` 已启用，外网 Chat SSE 也已通过：HTTP 200，首段文本 8.297 秒，
总耗时 8.875 秒，3 个内容事件、110 字符，收到终态及 `[DONE]`，内容分不同读取批次到达。
第一次外网尝试在客户端 90 秒总时限到期时出现 ReadError；后端账本稍后记录该请求约 90 秒完成、
状态 200，但这不表示客户端收到成功结果。随后仅重测此 AIS 外网请求，将客户端总时限设为 180 秒，
得到上述完整成功结果。
Build 的同模型返回 HTTP 429 后已停止探测，**未启用 `build-gemini-4-argon`**。
不能根据这些结果推算剩余额度，也没有验证或启用上述模型的 low / medium / high 档位。

修复官方规范账号路径跳转的补丁配合 `2 warm / 2 active` 配置，已通过真实轮询验证：
连续四次同一 AIS 来源、同一 Flash 模型的请求均为 HTTP 200，后端账本明确记录
`account-index-1 → account-index-0 → account-index-1 → account-index-0`，每次均无失败尝试。
本机与外网的 `/healthz`、`/quota`、认证后的 `/v1/models`、`/v1/quota` 均为 HTTP 200；
模型目录含 30 条，未授权的数据接口返回 401，Sub2API 原入口仍为 HTTP 200。
自动化检查为 Python **338 项**、Node **41 项**及 **4 个 Go 包测试套件**通过。
当前镜像、补丁校验值和分阶段记录见 [验证记录](../sidecars/aistudio2api/VALIDATION.md)。

## 来源锁定与凭据

Mixer 向后端发送独立服务 Bearer key，并通过补丁限定本次请求为 Playground 或 Build。
后端在调度及发送时均检查通道，冷却时不跨通道回退；Mixer 校验回传的通道确认。
`ais-` 对应 Playground，`build-` 对应 Build。来源出现在模型列表、响应元信息、响应头和日志中。
后端返回的错误原文不会透传到 Mixer 客户端，以免包含会话材料。

Google 会话只挂载给后端，Mixer 仅读能力证据文件。后端 API key 与管理密码独立，
管理端口只发布到 `127.0.0.1:18084`。不要把后端管理路由加入 OpenFRP 的公共反代。

## 认证准备

用户在自己控制的 Edge 或 Chrome 中登录 AI Studio，使用仓库中的 `aistudio-session-extension`
主动导出当前账号会话，然后通过 `tools/import_aistudio_session.py` 导入指定私有目录。
扩展无后台任务、不向网络发送 Cookie、不申请 Google Drive 授权。
导出与导入的具体步骤见扩展 README；不要将 Cookie、下载文件或 proof 发到聊天或提交到 Git。

扩展 v1.1.0 已支持 `/u/N/` 路径及当前页面 JSON 启动元信息中的邮箱，
实现已完成独立审查和合成测试。现有 Edge 安装需要用户在扩展页重新加载；
当前不能把源码验证或官方页面读取结果表述为扩展弹窗已经实测通过。

每个账号有独立 `account.json` 和 `storage-state.json`。会话扩展中的 `auth_user` 是
Google 当前登录账号索引，必须与用户选择的账号对应；`browser=mixer-import` 禁止后端隐式扫描个人 Chrome。
当登录失效时重新主动导出该账号。导出文件包含凭据，应保存在受保护目录并在成功导入后妥善清理。

## Compose

1. 设置原有 `MIXER_API_KEYS`，另生成不同的 `AISTUDIO_HTTP_API_KEY`、`AISTUDIO_ADMIN_PASSWORD`。
2. 将 `AISTUDIO_DATA_DIR` 指向上述私有目录，确保 UID 10001 可写其内容。
3. 设置精确的 `AIS_MODELS` 和 `BUILD_MODELS` 允许列表。不要仅凭名称宣称型号可用。
4. 在 `AISTUDIO_EVIDENCE_FILE` 路径创建初始 JSON `{"version":1,"models":{}}`。
5. 在已有 Compose 文件组合末尾追加 `-f compose.aistudio.yml`。

```sh
docker compose -f compose.yml -f compose.aistudio.yml config --quiet
docker compose -f compose.yml -f compose.aistudio.yml up -d --build
```

已有 Web、Antigravity 配额或本地端口 overlay 的部署，继续包含原有文件，避免丢失挂载或端口覆盖。
不要展示展开后的 Compose 配置；其中可能含有服务密钥。

该 overlay 明确选择 `AIS_TRANSPORT=aistudio-http`、`BUILD_TRANSPORT=aistudio-http`。
不使用 overlay 时默认仍为 `public-api`；直接运行 Python 时可分别配置两者。
后端地址使用服务根地址 `http://aistudio-http:2048`，不要加 `/v1`。

## 多账号与资源

多个用户明确导入的账号可以使用 `AISTUDIO_ROUTING_STRATEGY=round-robin` 在已预热的账号之间轮询。
每个账号保留自己的身份、权益和冷却状态；这不会增加任一账号的实际授权额度。
不要通过删除冷却数据或重复导入同一身份绕过限制。

节约内存的默认值为预热 1、最多活动 Worker 1、每账号并发 1。这会复用唯一可用的热账号，
不保证逐请求切换冷账号。两个账号持续轮询需同时设置 `AISTUDIO_WARM_WORKER_LIMIT=2`
和 `AISTUDIO_MAX_ACTIVE_WORKERS=2`，等两个 Worker 均预热完成后再核对实际请求账本。
可以增加 `AISTUDIO_MAX_ACTIVE_WORKERS`，但必须同时测量内存，不能把账号数量当成并发能力。
`AISTUDIO_GO_MEMORY_LIMIT=256MiB` 是 Go GC 的软目标，容器 `AISTUDIO_MEMORY_LIMIT=512m` 才是硬限制。
内存超限会使请求失败；没有测量前不能承诺固定 MB 或相对 WebView2 的节约比例。

## 能力证据与验证

`/v1/models` 是后端声明与本地允许列表的交集，不证明生成额度。
流式、思考档位、图像和工具还需要当前有效的实测记录。
记录分别使用 `ais/<model>`、`build/<model>`，不得跨通道套用。

```json
{
  "version": 1,
  "backend_base_url": "http://aistudio-http:2048",
  "verified_at": "2026-10-17T00:00:00Z",
  "models": {
    "ais/example-model": {
      "text": true,
      "streaming": true,
      "image_input": false,
      "tools": false,
      "parameters": ["maxOutputTokens"],
      "thinking": {"low": {"thinkingLevel": "LOW"}}
    }
  }
}
```

以上为格式示例，不能复制为真实通过记录。后端也必须声明相应能力。
证据默认 7 天过期；缺失或过期时只保留声明的基础文本，其他能力明确报错。
真实验证应依次完成各通道非流式、真正增量流式、取消/超时和来源核对，
再通过 Mixer 的 Chat、Responses、Gemini 原生入口验证。思考等级必须核对实际映射。
返回未知 token usage 时保持未知，不自行估算为真实用量。

目前后端模型目录缺少明确的图像输入声明，适配器会拒绝图像请求；不因原生协议可携带图片就宣布端到端支持。
后端的用量账本与冷却列表不等于 Google 各模型剩余额度。

自动化测试使用合成后端与本地 fixture，不能替代真实账号生成验证。
手动生成、SSE 验收和能力证据的生成步骤见 [通道验收](aistudio-http-verification.md)。
实际部署验证记录应分别列出账号准备、后端启动、AIS、Build、流式、内存各项结果，保留失败和未完成项。
