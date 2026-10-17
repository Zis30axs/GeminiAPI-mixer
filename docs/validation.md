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
实际额度与可用性会变化；验收记录不代表后续任何时刻仍有相同余量。
