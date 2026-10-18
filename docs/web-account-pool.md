# Gemini Web 多账号轮询

Web 账号池是可选功能。它在用户主动导入的自有账号之间轮询，保持每个账号的认证、
请求和限流状态独立。不配置 `GEMINI_WEB_ACCOUNTS_FILE` 时，继续使用原来的单账号 Web 配置。
Web 模型名保持不变；`ais-` 和 `build-` 仍由 AI Studio 通道处理，不使用这个 Web 账号池。

## 准备账号文件

在自己的浏览器中正常登录 Gemini Web，分别主动导出需要接入的账号。
导出的 JSON 使用现有 Web 会话格式，包括 `cookie`、`sapisid`、`auth_user`、
`xsrf_token` 和 `gemini_bl`。保存完整导出文件，不要从不同账号拼接字段。
Google 会话失效时，仍需正常登录并重新导出该账号；文件轮询不会延长会话有效期。

建立一个私有目录，例如仓库内已被 Git 忽略的 `auth/web-accounts/`：

```text
auth/web-accounts/
  manifest.json
  u0.json
  u4.json
  u5.json
```

其中 `manifest.json` 只描述文件与匿名账号标识的对应关系：

```json
{
  "accounts": [
    {"id": "web-u0", "auth_user": 0, "cookie_file": "u0.json"},
    {"id": "web-u4", "auth_user": 4, "cookie_file": "u4.json"},
    {"id": "web-u5", "auth_user": 5, "cookie_file": "u5.json"}
  ]
}
```

这是结构示例，不包含登录信息，也不表示这些账号已经可用。每条记录都必须提供
`auth_user`，它应当与该次浏览器登录会话中的账号索引一致，并与导出 JSON 中的字段相同。
导出的五个字段均需完整有效；`sapisid` 必须与 Cookie 中的 `SAPISID` 一致，账号池不继承
旧账号的页面令牌或构建标识。`id` 必须等于 `web-u` 加账号索引，不要使用邮箱、姓名或 Cookie 作为标识。

`cookie_file` 必须是清单所在目录内部的相对文件路径；绝对路径、URL、越界路径、
通过符号链接指向目录外部的文件均不接受。账号清单和导出文件在启动时严格校验；
配置损坏、文件缺失或账号索引不一致会使启动失败，不会悄悄退回旧账号。

目录应只允许本机服务账户和管理者访问。Docker 中 Mixer 以 UID/GID `10001:10001` 运行，
该用户需要能读取清单及导出文件；不要为解决权限问题开放所有用户的访问权限。
Cookie、令牌和完整导出文件不要提交 Git、发到聊天或写入日志。

## 启用与更新

在 `.env` 设置宿主机目录与冷却时长：

```dotenv
GEMINI_WEB_ACCOUNTS_DIR_HOST=./auth/web-accounts
GEMINI_WEB_RATE_LIMIT_COOLDOWN_SEC=300
```

在现有 Compose 文件组合的末尾追加 `-f compose.web-pool.yml`。例如，一个只部署 Mixer
和 Web 账号池的新实例可以执行：

```sh
docker compose -f compose.yml -f compose.web-pool.yml config --quiet
docker compose -f compose.yml -f compose.web-pool.yml up -d --build
```

已有本地端口、AI Studio、额度或证据 overlay 的部署，必须保留原来的文件组合、项目名称和
`.env`，只追加此 overlay，避免丢失其他通道配置。不要把完整展开后的 Compose 配置贴到日志或聊天。
该 overlay 将整个账号目录只读挂载到 `/run/secrets/web-accounts`，并将
`GEMINI_WEB_ACCOUNTS_FILE` 设置为 `/run/secrets/web-accounts/manifest.json`。
宿主机目录不存在时不会自动创建空目录。

直接运行 Python 时，设置 `GEMINI_WEB_ACCOUNTS_FILE` 为私有清单的实际路径；
`GEMINI_WEB_ACCOUNTS_DIR_HOST` 仅供 Compose 使用。

清单和账号文件在启动时加载。更新、补充或重新导出文件后，使用部署时完整的 Compose
文件组合重启 `mixer`。更新 `.env` 或挂载路径时，应使用同一文件组合重新执行 `up -d`，
使容器获得新环境。只读目录挂载不代表文件会被自动热加载。

## 调度、失败与流式

| 情况 | 行为 |
| --- | --- |
| 多个账号可用 | 按轮询顺序选择空闲、未禁用且不在冷却中的账号 |
| 请求已选择账号 | 本次生成、上传、工具处理和 SSE 全程使用该账号；结束或取消后释放 |
| 同一个账号已有请求 | 不再分配第二个活动请求，三个账号最多同时承载三个活动请求 |
| 全部账号忙或当前无可用账号 | 返回明确错误；全部忙时返回 HTTP 503 |
| 上游 HTTP 401 / 403 | 禁用本次选中的账号，直到更新有效会话并重启服务 |
| 上游 HTTP 429 | 冷却本次选中的账号，默认 300 秒；有效 `Retry-After` 会被采用，上限 86400 秒 |
| 本次请求失败 | 返回该次失败，不将同一个请求自动重试到其他账号 |

`GEMINI_WEB_RATE_LIMIT_COOLDOWN_SEC` 必须是 `1..86400` 的整数。
每账号并发限制覆盖 Chat Completions、Responses 和 Gemini 原生入口，并持续到 SSE
结束或客户端取消。客户端开始下一次独立请求时，调度器才会重新选择可用账号。
不能通过重复导入同一身份、不断重启或清除冷却来绕过上游限制。

模型权限与额度仍由 Google 按实际账号判定。账号池不会让不具备权限的账号获得模型，
也不会凭其他账号的成功记录宣称当前账号支持思考档位、图片或工具。
本次部署验收只使用用户指定的 `gemini-3.8-flash`，不主动探测 Argon。

## 核对轮询与额度

响应头 `X-Mixer-Web-Account` 和已有 Mixer 元信息中的 `web_account` 字段，记录选中的
匿名标识，例如 `web-u0`；不返回邮箱、Cookie 或令牌。模型名与 `/v1/models` 中的模型
标识不因账号池改变。

验收时先检查每个已导入账号的正常生成，再连续提交独立请求，核对匿名标识的轮转和
每次结果；同时验证 SSE 完整结束、取消后释放账号及其他通道仍可用。
自动化测试可验证调度和错误隔离，不能代替当前账号的真实授权及生成测试。

本地冷却时间只是调度器的限流处理，不是 Google 的真实剩余额度或重置时间。
没有上游提供并验证的额度数据时应显示未知，不根据成功次数、冷却结束或账号数量推算精确额度。
