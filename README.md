# GeminiAPI Mixer

保留现有 Gemini Web Provider，按模型前缀路由到 AI Studio 公共 API 配置，
并可连接自托管 Sub2API 的 Antigravity 专属分组。提供 Chat Completions、
Responses、Gemini 原生 API、SSE 和带来源信息的模型列表。

## 来源与实现边界

| 模型名 | source / transport | 实际路径 |
|---|---|---|
| 原有 `gemini-*` | `web` / `gemini-web-private-rpc` | 保留原 Gemini Web 实现和本地修复，模型身份属于未验证网页预设 |
| `build-<model>[-low/medium/high]` | `build` / `google-gemini-public-api` | 在本服务端实现 Build ZIP 实际使用的公开 Gemini API，无第三方 WS |
| `ais-<model>[-low/medium/high]` | `ais` / `google-gemini-public-api` | Playground 的公开 API 替代配置；没有连接网页 Playground |
| `ag-<upstream-model>` | `antigravity` / `sub2api-openai` | 用户自托管 Sub2API；API key 必须绑定 Antigravity 专属分组 |

`build-`、`ais-` 是独立凭据/模型配置，**不是独立的网页额度池**。两者均使用
`generativelanguage.googleapis.com`。不接入 Catiecli 固定 WS，也不获取、生成或
转交 WAA proof。MakerSuite 私有 RPC/WAA Executor 没有启用。来源同时出现在
`/v1/models`、路由日志、`X-Mixer-Source` / `X-Mixer-Transport` 响应头和可用的
JSON/SSE 元信息中；所有响应带 `X-Request-ID`。

## 启动

```sh
cp .env.example .env
# 在 .env 设置 MIXER_API_KEYS；再填写所需上游的独立密钥。
docker compose up --build -d
```

默认仅发布 `127.0.0.1:8081`，容器非 root，文件系统只读，不挂载 Docker socket。
客户端使用 **MIXER_API_KEYS**，不会把它转发给上游。上游 key 只用于对应 Provider。
`.env`、Cookie、账号数据不进入 Git 或镜像。配置详情、Cookie 挂载和自托管服务地址见
[部署文档](docs/integration.md)。

```sh
curl http://127.0.0.1:8081/v1/models -H "Authorization: Bearer $MIXER_KEY"
curl http://127.0.0.1:8081/v1/chat/completions \
  -H "Authorization: Bearer $MIXER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"build-gemini-2.5-flash-low","messages":[{"role":"user","content":"你好"}],"stream":true}'
curl http://127.0.0.1:8081/v1/responses \
  -H "Authorization: Bearer $MIXER_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"ais-gemini-2.5-flash-high","input":"你好","store":false}'
curl http://127.0.0.1:8081/v1/quota/antigravity -H "Authorization: Bearer $MIXER_KEY"
```

`/v1/models` 只列出已配置、allowlist 允许、上游 models.list 实际可见的新增模型；
模型列表可见仍不保证生成权限或额度，真实上游拒绝会报错。未配置的 profile 不列出。
原 Web 名称保持兼容；新前缀错误不会悄悄回落到 Web。

## 思考与能力

| 上游模型 | 档位 | 实际参数 |
|---|---|---|
| `gemini-2.5-pro` / `flash` / `flash-lite` | low / medium / high | `thinkingBudget` 1024 / 8192 / 24576，采用 Google 官方 OpenAI 兼容映射 |
| `gemini-3-pro-preview` | low / high | `thinkingLevel`；medium 明确报错 |
| `gemini-3.1-pro-preview` / `gemini-3-flash-preview` | low / medium / high | `thinkingLevel` |
| 其他型号 | 无自动三档 | 仅 allowlist 原名；未验证的图像/工具/思考能力报错 |

别名后缀可用 `reasoning_effort`（Chat）或 `reasoning.effort`（Responses）代替；
相互冲突或同时指定原生预算/档位会报错。元信息报告实际预算或档位。
见 [Google 官方映射](https://ai.google.dev/gemini-api/docs/openai#thinking) 和
[thinking 参数](https://ai.google.dev/gemini-api/docs/thinking#controlling-thinking)。

公共 API profile 支持已核实模型的文本、内嵌图片输入、真实函数调用和 SSE。
图片仅接受 base64 data URL / Gemini `inlineData`，远程图片 URL 明确报错。
图片生成、音频、内置搜索工具、Responses 服务端状态/后台执行、严格函数 schema
等未实现能力明确拒绝。真实 `thoughtSignature` 会保留，绝不伪造。
Web 保留原有图片和工具提示词兼容行为，工具属于提示词模拟，不能宣称原生函数调用。
统一入口默认禁用旧 Web 的远程图片抓取，优先使用内嵌图片；保留兼容开关见部署文档。
Antigravity 透明转发 Chat/Responses，由选定上游校验能力；不支持 Gemini 原生入口。

## Antigravity 与额度

无需捆绑另一个登录/Token 提取器。设置 `ANTIGRAVITY_BASE_URL`、
`ANTIGRAVITY_API_KEY`、`ANTIGRAVITY_MODELS`，确认该 key 属于专属分组后设
`ANTIGRAVITY_GROUP_CONFIRMED=true`。未确认时保持禁用。

额度调用 Sub2API 实际 `GET /v1/usage`，返回
`scope=sub2api-key-wallet`。它是平台 API key/钱包或订阅限额，**不是 Google 账户
真实剩余额度**。未知值为 `null`，查询失败为 `unavailable`，无限制单独标注；
保留可用的周期重置时间，不以失败代替零余额。

各模型账户额度及 reset 可通过可选的隔离采集器提供，Mixer 仅读脱敏快照。
显式启用 `compose.quota.yml` 后默认每 60 秒采集，600 秒后显示过期；管理员密码/JWT
不进入 Mixer。上游报告已用 100% 时保留“耗尽或未知”的歧义，不伪造零余额。
配置与命令见 [模型额度说明](docs/antigravity-quota.md)。

## 验证与来源

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -q
node --test tests/test_cookie_extension.cjs
```

测试使用合成凭据和本地/Mock HTTP 上游，不消费真实额度。启动统一入口：
`python -m gemini_web2api`。旧独立 `gemini_web2api.py` 和账号池脚本仅作为保留的
兼容工具，不包含新的统一路由，不能用作对外的 Mixer 入口。

- [ZIP / UserScript 审计](docs/executor-audit.md)
- [上游接口与复用项目核实](docs/upstream-research.md)
- [导入修复的来源与许可证](UPSTREAM.md)
- [原 Gemini Web 文档](docs/upstream-gemini-web_CN.md)

本仓库保留 GPLv3 LICENSE，导入的 MIT 代码通知位于 `licenses/`。
