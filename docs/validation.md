# 验证记录

日期：2026-10-17。所有密钥和账号标识均为合成测试材料。

- 原部署快照：116 项 Python 回归测试通过；16 项浏览器扩展测试通过。
- 统一版本：Linux Docker 容器内 215 项 Python 测试通过，网络关闭，只允许本地测试服务器。
- 新增测试覆盖来源隔离、模型发现交集、思考映射/冲突、图片、工具与真实签名、
  Responses 生命周期、SSE 中断/取消、超时、字节上限、认证和安全错误。
- Antigravity 测试覆盖模型过滤、专属组开关、透明协议、额度/重置/无限制/未知/失败。
- Compose 主文件和 Web Cookie overlay 配置检查通过。
- Docker 镜像构建通过；以 UID 10001、只读文件系统、无额外 capabilities、无网络运行，
  健康检查、401、模型来源、未配置 Provider 的 503 均通过。
- 检查提交文件中常见 Google/GitHub 密钥和私钥格式，没有发现真实凭据。

上述为初版合并前验证，当时未向实际 Google/Antigravity 账户发起推理或读取真实额度。
后续获得授权的本机接入验证见下文。Web 原部署未被停止或修改。
Build/ais 实现的是明确标注的公开 API profile，不声称完成网页 Executor 对接。

ZIP 与同版本 UserScript 的全文副本已静态审阅；原消息导出截断，因此无法证明下载副本
与原粘贴内容逐字相同。原始执行器没有被安装或运行，详见 executor-audit.md。

## 本机 Antigravity 扩展验收

2026-10-17，在已有本机 Sub2API、专属无 fallback 分组和专用平台 key 上核验：

- `gemini-3-flash` 的 Chat、Responses、Chat SSE 通过。
- `gemini-3.8-flash-low` / `medium` / `high` / `tiered` 的精确模型生成通过；
  用量记录中的 requested/upstream ID 与选定 ID 相同。三个档位的原始 response model
  为 `gemini-3.8-flash-n`，因此元信息单独保留响应 ID，不将其当作独立模型/配额证据。
- 分组完整 Gemini 目录也已逐项核验。无后缀的 `gemini-3.8-flash` 生成通过，
  用量记录确认实际转到 `gemini-3.8-flash-high`，作为别名展示并引用该精确额度桶。
  其他 `3.1-pro`、`3.6/3.7` 的无后缀和 tiered 及 `gemini-4-argon-high` 均 404。
- `gemini-3.6-flash` 与 `3.7-flash` 的三个档位，以及 `gemini-3.1-pro-low`、
  `gemini-pro-agent` 实测 404，普通模型目录排除，额度目录保留失败原因。
- `claude-sonnet-4-6` 映射补齐后实际调用 429；同账号 `claude-opus-4-6-thinking`
  仅有目录及额度声明，没有因前者 429 而继续探测同一受限模型族。
  Claude 三档不能由现有证据确认，明确拒绝；原型号保留重置后再次调用的能力。
- 正规账号额度管理接口返回源观察时间、模型整数使用率与重置时间。Claude 的
  已用 100% 存在上游零值歧义，剩余保留 null；不声称精确 token/请求数。
- 隔离导出器实际单轮采集成功，只将安全字段写入快照。凭据文件未进 Git/镜像，
  Mixer 只读额度卷，模型验证证据单独只读挂载。

本次新增离线测试涵盖账号/组校验、认证挑战、快照脱敏、过期、错误、精确额度映射、
档位冲突和不降级、Chat/Responses 及 SSE 元信息、钱包故障保留模型额度、看板安全。
Linux Docker 无网络、只读根目录运行的 Python 全套测试通过，浏览器扩展 16 项通过。
本机更新后 low Chat、medium Responses、high Chat SSE 的实际 Mixer 请求均通过，
返回选中的精确上游 ID、档位、模型配额与来源时间；匿名额度请求 401。
Mixer 和采集器均以 UID 10001、只读根目录运行，管理员登录文件只挂载在采集器。
实际额度与可用性会变化；验收记录不代表后续任何时刻仍有相同余量。

## OpenFRP 单入口验收

2026-10-17，经用户已有公网隧道地址实际访问本机 8080 网关：

- Caddy `/gemini/*` 去前缀后改为 Mixer `127.0.0.1:18083`，其余路径仍转到
  Sub2API `127.0.0.1:18081`。保留原隧道、端口、管理员回环地址与启动任务。
- 更新前备份磁盘与活动配置，候选配置校验通过后热重载。Sub2API 根 HTML 除每次
  生成的 CSP nonce 外与基线一致；原匿名 `/v1/models` 仍为 401。
- 公网前缀入口的 low Chat、medium Responses 普通生成，以及 high Chat SSE、
  low Responses SSE 均 200，模型与 effort 元信息正确，流有实际文本与成功终态。
  两种流首事件约 3.7 / 3.5 秒，均在完整响应结束前收到。
- `/gemini/quota` 返回带匹配 CSP 的看板，使用相对路径读取
  `/gemini/v1/quota/antigravity`；明确允许的 HTTP origin 来自私有环境配置，
  不能由 Host、转发头或用户输入授权。
- 公网模型额度接口返回 200，含 24 条诊断型号的来源、源观察时间及重置时间；
  Claude 未知余量继续保持 null。匿名模型和额度接口均 401。
- 本机 297 项 Python 测试通过，22 项 Node 测试通过。Docker 关闭网络、只读根目录下
  同一套 Python 测试通过，其中 1 项 Node 执行检查因镜像不带 Node 而跳过；
  CI 会运行该检查及实际页面脚本的前缀、来源、认证请求和渲染行为测试。

公网验收是从本机通过节点的公网地址进行的完整往返，不代表已从另一独立设备验收。
沿用的 HTTP 外网入口不提供端到端 TLS，显式 origin 配置不会改变这一性质。
