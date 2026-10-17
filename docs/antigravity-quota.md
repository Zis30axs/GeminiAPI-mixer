# Antigravity 模型额度、重置时间与隔离采集

打开 Mixer 的 `/quota` 看板，输入现有 Mixer Key 即可查看。密钥仅保留在页面内存，
不会写入 URL 或浏览器存储。每 60 秒刷新，可关闭。公开页面只是空白界面；数据接口仍需认证。
`GET /v1/models` 和生成返回的 `x_mixer` 也包含对应模型额度、来源、重置时间及验证记录。
`GET /v1/quota/antigravity` 的 `models` 包含不可用模型的诊断，平台钱包保留在顶层。

`/v1/usage` 提供 Sub2API 平台 API key 的钱包、订阅或限额。各个 Google / Claude
模型的账户额度来自另一条管理接口，不能用平台余额替代，也不能把百分比换算成剩余 token
或请求数。Mixer 可同时报告这两种口径，账户模型额度明确标注
`source=sub2api-account-quota`。

## 可选启用

基础部署不需要管理员权限。只有需要模型额度时才叠加此文件：

```sh
docker compose -f compose.yml -f compose.quota.yml config --quiet
docker compose -f compose.yml -f compose.quota.yml up --build -d
```

如已使用 Cookie 或本机网络配置，保留原来的 `-f` 参数并在其后加
`-f compose.quota.yml`。所有命令使用相同的 Compose project，避免创建第二份无关服务。
此 overlay 通过显式叠加启用，无需再设置 profile。不会新增对外端口。

在 `.env` 中填写：

| 变量 | 配置 |
|---|---|
| `SUB2API_QUOTA_BASE_URL` | 操作者指定的 Sub2API 服务根地址，使用 HTTPS，不含 `/api/v1` |
| `SUB2API_QUOTA_ALLOW_HTTP` | HTTP 必须显式设为 `true`；仅用于操作者信任的本机/内网链路 |
| `SUB2API_QUOTA_CREDENTIALS_HOST_FILE` | 专用管理员登录 JSON 的宿主机绝对路径；不得指向整个部署 `.env` |
| `SUB2API_QUOTA_GROUP_ID` | Antigravity 专属分组 ID，示例默认 `4`，必须核对本机实际 ID |
| `SUB2API_QUOTA_ACCOUNT_IDS` | 允许展示额度的账号 ID，逗号分隔，默认 `2`，最多 8 个 |
| `SUB2API_QUOTA_INTERVAL_SEC` | 默认 60 秒；允许 30–3600 秒 |
| `SUB2API_QUOTA_OUTPUT_FILE` | 容器共享卷内文件，默认 `/var/lib/mixer-quota/snapshot.json`；自定义仍须位于该目录内 |
| `ANTIGRAVITY_QUOTA_MAX_AGE_SEC` | overlay 默认 600 秒；超过该年龄标记 stale |

Docker Desktop 访问既有宿主机服务可用 `http://host.docker.internal:18081`，并显式开启
`SUB2API_QUOTA_ALLOW_HTTP`。Linux 根据网络部署另配 host-gateway。采集地址与推理用的
`ANTIGRAVITY_BASE_URL` 独立设置；不得误指向 Mixer 自身。

专用 JSON 仅包含两个字段，使用本机已有管理员的正规登录凭据：

```json
{"email":"<管理员邮箱>","password":"<管理员密码>"}
```

该文件只读挂载给 `quota-exporter`，不挂载给 Mixer，不写进 `.env`、镜像或 Git。
文件应只允许操作者和采集容器 UID `10001` 读取。Linux 可将所有者设为 `10001:10001`
并设为 `0400`；Windows 使用相应文件 ACL。文件不存在时 Compose 不会创建替代目录。
它是有管理员权限的敏感文件，不能把“专用文件”解释为低权限凭据。

## 权限与输出边界

同一镜像运行两个独立进程。镜像预建 `/var/lib/mixer-quota` 并交给 UID `10001`；
Exporter 和 Mixer 均非 root、根文件系统只读、移除全部 capabilities。
共享命名卷对 Exporter 可写、对 Mixer 只读；Exporter 无监听端口，并禁用镜像继承的
HTTP API 健康检查。管理员密码和 JWT 不进入公开 Mixer 容器。

每轮采集只做：

1. `POST /api/v1/auth/login` 正常登录。验证码或 2FA 要求会使采集返回不可用，必须由操作者处理。
2. `GET /api/v1/admin/groups/{id}`，核实平台为 Antigravity 且没有两类 fallback。
3. `GET /api/v1/admin/accounts?...&lite=true`，逐项核实账号平台、分组归属及显式 ID 白名单；不信任服务端过滤参数本身。
4. `GET /api/v1/admin/accounts/{id}/usage?source=active&force=false`，只提取安全额度字段。

不读取 OAuth、Cookie、数据库或账号详情凭据，不调用账号/分组修改、模型推理或 refresh-token
接口。登录返回的 access token 仅在当轮内存中存在；refresh token 和 Cookie 不复用。
Sub2API 的 Admin API Key 在已核实版本中是完整管理员权限，不能作为只读 key 交给 Mixer。
不会为了共享额度而打开 Sub2API 的全局渠道监控展示开关。

HTTPS 校验证书，不跟随重定向，不使用环境代理。默认连接 5 秒、读取 15 秒、单轮总计
45 秒，每个响应最多 2 MiB。日志仅含固定状态/错误代码。采集失败会原子写入新的
`unavailable` 快照；写盘失败保留原文件的旧时间，读端随后按年龄标记 stale。

## 如何理解数值

每个账号保留上游的 `updated_at` 为 `observed_at`。导出时间与上游观察时间分开计算，
反复读取缓存不会把旧观察假装成新额度。`remaining_percent` 和 `reset_at` 只使用对应
模型的原始条目；不会猜测名称后缀、跨模型借用余量或把未知换成零。

已核实 Sub2API revision `3040209f205472038c1ba745a1bedd2edd9053b1` 的
`remainingFraction` 是非指针数字：字段缺失也会变为 `0`，随后显示成已用 `100%`。
因此 `reported_used_percent=100` 只报告为 `reported_exhausted_or_unknown`，剩余为 `null`；
它不能证明额度耗尽。其他合法整数使用率可按上游整数精度展示剩余百分比。
缺失观察时间、未关联模型、无快照和过期快照会明确显示 unknown、unavailable 或 stale；
重置时间若明确给出，可独立保留。不会从当前时间猜测新的 reset。

多个账号的百分比不相加，也不平均成一个“全组余额”。结果保留每个账号的独立值，
只报告已知剩余百分比范围；聚合 reset 取明确给出的最早未来重置时间。
若本地能力证据把多个别名明确指向同一个额度模型 ID，它们会展示同一额度桶；
仅名称相似、档位后缀相同或响应模型相同都不足以推断共享额度。

## 思考档位与模型身份

`build-` / `ais-` 仅使用已核实的公开 Gemini 模型支持表，将 low / medium / high 映射
到原生预算或档位。Antigravity 使用不同账户和模型映射，不能复制公共 API 的三档表。
`ag-<model>` 必须来自该专属分组实际模型目录和本地 allowlist；只根据后缀名称无法证明
low / medium / high 已生效。可用档位需要对应上游参数或账户映射证据；未核实的档位应报错，
不能把 medium 自动降成 low。额度条目属于实际模型/共享池，不意味着每个档位都有独立额度。

本机验证记录保存在操作者提供的只读 JSON 文件，由 `ANTIGRAVITY_MODEL_EVIDENCE_FILE`
指定容器内路径。可用 `compose.evidence.yml` 挂载，并设置宿主机变量
`ANTIGRAVITY_MODEL_EVIDENCE_HOST_FILE`。该文件只包含安全验证结果，不是凭据文件。
不要将本机证据自动用于其他账号：模型列表和额度条目不能代替生成测试。

证据格式为 `schema_version: 1`、`source_revision` 和 `models` 对象。每个精确模型 ID
对应 `status`（verified / upstream_declared / unavailable）、`tested_at`、`http_status`、
`effective_upstream_model`、`quota_model_id`、`thinking_control`、`effort` 与固定 `reason`。
`upstream_response_model` 可记录实际响应 ID。verified 必须有成功 HTTP 状态和非未来时间。
文件缺失或无效时能力为未知；不会激活新的 effort 映射。

仅同时满足“本地 allowlist、生成实测成功、精确上游档位证据”的型号支持 body effort：
例如已核实 `ag-gemini-3.8-flash-low` 时，`ag-gemini-3.8-flash` 配合
`reasoning_effort: "low"` 或 Responses 的 `reasoning: {"effort":"low"}` 会路由到该精确 ID。
直接指定档位与 body 冲突返回 400；没有该精确档位时也返回 400，不降级。
路由后删除通用 effort 控制，防止 Sub2API 的中间格式转换覆盖选中的精确档位。
控制方式标为 `upstream-model-variant`，不声称已发送某个数值 thinkingBudget。

Claude 的 canonical / thinking 型号照常可路由，但当前没有经核实的三档映射，
请求 low / medium / high 明确返回 `unsupported_thinking_level`。暂时 429 的证据只标记
临时不可用，保留再次调用的能力；实测 404 的型号从普通模型列表剔除并保留在额度诊断中。
重置后可重新做有限验证并更新证据，不自动轮换账号或反复重试。

参考已核实版本的 [管理员鉴权](https://github.com/Wei-Shaw/sub2api/blob/3040209f205472038c1ba745a1bedd2edd9053b1/backend/internal/server/middleware/admin_auth.go)、
[额度字段及转换](https://github.com/Wei-Shaw/sub2api/blob/3040209f205472038c1ba745a1bedd2edd9053b1/backend/internal/service/antigravity_quota_fetcher.go) 和
[原始字段定义](https://github.com/Wei-Shaw/sub2api/blob/3040209f205472038c1ba745a1bedd2edd9053b1/backend/internal/pkg/antigravity/client.go)。
