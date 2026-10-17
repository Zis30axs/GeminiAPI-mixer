# 自托管 AI Studio 纯 Go 后端

此模式复用 [Mag1cFall/AIStudio2API](https://github.com/Mag1cFall/AIStudio2API/tree/2cbe310ce6ebab093bf83e5078de8296f03770a9)
提交 `2cbe310ce6ebab093bf83e5078de8296f03770a9`，运行时不安装浏览器、WebView2 或 Node。
首次登录或登录失效时，用户仍需通过正常 Google 登录取得自己的会话。
源码、补丁与第三方许可证说明在 `sidecars/aistudio2api/`。

这使用网页私有协议和上游 WAA 实现，不是 Google 对外承诺兼容的公共 API。
没有额外付费 API key 需求不代表账号一定有可用额度；服务器的权限拒绝、模型限制和额度限制仍生效。
不创建新身份、不自动加入其他账号、不把失败请求切到 Web 或公开 API。

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

多个用户明确导入的账号可以使用 `AISTUDIO_ROUTING_STRATEGY=round-robin` 轮询。
每个账号保留自己的身份、权益和冷却状态；这不会增加任一账号的实际授权额度。
不要通过删除冷却数据或重复导入同一身份绕过限制。

节约内存的默认值为预热 1、最多活动 Worker 1、每账号并发 1。
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
实际部署验证记录应分别列出账号准备、后端启动、AIS、Build、流式、内存各项结果，保留失败和未完成项。
