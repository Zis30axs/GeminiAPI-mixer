# 通过现有 OpenFRP 入口访问 Mixer

同一个入口可同时访问原来的 Sub2API 和 Mixer：

| 用途 | 地址 |
|---|---|
| 原 Sub2API | `http(s)://<入口>/`，原路径保持不变 |
| Mixer 的 OpenAI Base URL | `http(s)://<入口>/gemini/v1` |
| Mixer 额度看板 | `http(s)://<入口>/gemini/quota` |
| Mixer 模型目录 | `http(s)://<入口>/gemini/v1/models` |
| Mixer 额度数据 | `http(s)://<入口>/gemini/v1/quota/antigravity` |

`<入口>` 是已有隧道的域名和端口。客户端继续使用 Mixer Key；Sub2API 的原有认证保持不变。
看板页面可直接打开，输入 Mixer Key 后才获取额度数据；Key 不放入 URL。

## 路由关系

```text
客户端 → 已有 OpenFRP TCP 入口 → 本机 Caddy :8080
                                     ├─ /gemini/* → 去掉 /gemini → Mixer 127.0.0.1:18083
                                     └─ 其余路径               → Sub2API 127.0.0.1:18081
```

仓库提供 [Caddyfile 示例](../examples/Caddyfile.openfrp)。它把 `/gemini` 以 308 跳转到
`/gemini/`，使用 `handle_path` 去掉前缀，再把普通请求和 SSE 原样转给 Mixer。
`flush_interval -1` 保持 SSE 及时输出，`response_header_timeout 600s` 为上游开始响应预留时间。
后端本身的总超时和限额仍然生效。无需添加或信任 `X-Forwarded-Prefix`，看板使用自身地址下的
相对路径请求数据。

示例约定 Mixer 已监听 `127.0.0.1:18083`。基础 `compose.yml` 默认发布到 `127.0.0.1:8081`；
按实际部署端口修改 Caddy 的 Mixer 目标，或在已有本机部署的 Compose 配置中使用 `18083:8081`。
避免额外直接公开后端端口。根路径的 Sub2API 目标保持实际部署地址。

现有隧道如果显示类型为 TCP，浏览器连接的是该 TCP 入口。OpenFRP 客户端到服务端采用
KCP 或 QUIC，是隧道内部传输协议的另一层设置；它不会把浏览器的入口 URL 变成 KCP/QUIC。
这里只调整本机 Caddy 路由，不需要修改 OpenFRP 控制台中的隧道、端口或底层传输设置。

## HTTP 入口的看板配置

看板默认只允许在 HTTPS 或本机回环地址提交 Key。已有公网 HTTP 入口需要由操作者在私有
`.env` 显式允许对应 origin，例如：

```dotenv
MIXER_DASHBOARD_HTTP_ORIGINS=http://gateway.example:60000
```

此处只是格式示例，请填写实际入口；多个 origin 用逗号分隔。origin 只包含协议、域名和端口，
不含 `/gemini`、其他路径或尾斜线。不使用通配符。基础 `compose.yml` 会把此变量传给 Mixer；
更新 `.env` 后用原部署命令重建 Mixer 容器，单纯重启不会更新容器环境变量。

HTTP 会以明文传输 Mixer Key 和请求内容；优先使用可用的 TLS 入口。显式允许 HTTP 只解锁
该 origin 的看板提交，认证要求保持不变，也不会改变隧道配置。

## 安全更新 Caddy

在已有配置中合并路由，保留日志、管理员端口和其他现有站点设置。不要用示例直接覆盖一份
包含其他业务路由的完整配置。以下 PowerShell 流程使用本机路径占位符：

```powershell
$gatewayDirectory = 'C:\path\to\api-gateway'
$caddyBinary = Join-Path $gatewayDirectory 'caddy.exe'
$activeConfig = Join-Path $gatewayDirectory 'Caddyfile'
$nextConfig = Join-Path $gatewayDirectory 'Caddyfile.next'
$backupConfig = Join-Path $gatewayDirectory ('Caddyfile.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))

Copy-Item -LiteralPath $activeConfig -Destination $backupConfig
Copy-Item -LiteralPath $activeConfig -Destination $nextConfig
# 编辑 Caddyfile.next，合并示例路由并核对两个后端端口。
& $caddyBinary validate --config $nextConfig --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw 'Caddy validation failed; active configuration is unchanged.' }

Copy-Item -LiteralPath $nextConfig -Destination $activeConfig
& $caddyBinary reload --config $activeConfig --adapter caddyfile --address 127.0.0.1:20191
if ($LASTEXITCODE -ne 0) { throw 'Caddy reload failed; inspect logs and restore the saved configuration if needed.' }
```

`--address` 应与现有 Caddy 管理地址一致，管理地址只监听回环。先检查本机
`http://127.0.0.1:8080/gemini/healthz` 和根路径，再从已有公网入口检查 `/gemini/quota`。
看板输入 Key 后应能显示模型、额度观察时间和重置时间；未认证访问模型或额度数据应返回 401。
最后做一次普通生成和 SSE 生成，确认 `/gemini/v1` 路由可用且流式内容及时到达。

回滚时把备份复制回 `Caddyfile`，再次执行 `validate`，通过后执行同样的 `reload`。
不要把实际 Key、管理员登录文件或完整私有 `.env` 写入文档、截图或版本库。
