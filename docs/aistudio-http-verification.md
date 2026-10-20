> 历史验收工具说明：下述证据文件已不再作为 AIS/Build 运行门槛。无需执行这些步骤即可使用参数、工具、图片和流式。

# AI Studio HTTP 通道验收

`tools/verify_aistudio_http.py` 是手动验收工具，不会自动运行。它使用固定、无个人数据的短提示词，分别检查 Playground 和 Build 的一次原生生成与一次原生 SSE。请先在当前进程环境中安全设置 `AISTUDIO_HTTP_BASE_URL` 和 `AISTUDIO_HTTP_API_KEY`；普通 HTTP 还需要 `AISTUDIO_HTTP_ALLOW_HTTP=true`。密钥不要放入命令行参数或提交到仓库。

模型使用后端 `/v1/models` 中的精确 ID，不加 Mixer 的 `ais-` / `build-` 前缀：

```text
python tools/verify_aistudio_http.py --model <upstream-model-id>
```

默认最多四次生成请求，先 Playground 后 Build。单通道非流式失败便跳过该通道 SSE；不重试。遇到认证失败或额度限制（401、403、429），立即停止其余探测。每个请求默认最多 60 秒，可用 `--timeout 90` 修改，上限 180 秒。请求禁止跳转，响应与 SSE 事件均有大小限制。

只验证一个来源：

```text
python tools/verify_aistudio_http.py --model <upstream-model-id> --source ais
python tools/verify_aistudio_http.py --model <upstream-model-id> --source build
```

默认仅输出状态、耗时、文本长度和事件数量，不输出回答正文、上游错误原文、账号会话或密钥。非零退出码表示至少一个目标通道未完成验证。

验证一个明确的原生思考档位时，加上 `--thinking-level low`、`medium` 或 `high`：

```text
python tools/verify_aistudio_http.py --model <upstream-model-id> --source ais --thinking-level high --evidence-output work/aistudio-high-evidence.json
python tools/verify_aistudio_http.py --model <upstream-model-id> --source build --thinking-level low --evidence-output work/aistudio-build-low-evidence.json
```

每次普通生成和 SSE 之前，工具都会从同一后端地址重新读取 `/v1/models`，核对目录响应的来源锁定、精确模型 ID、`channels`、`capabilities.thinking_level=true` 以及非空 `capability_options.thinking_levels`。目录中的档位必须为规范小写值；允许目录声明 `minimal`，但这个工具只接受显式的 `low` / `medium` / `high`。缺少声明、档位不在列表、目录结构错误或来源不一致时，不发送该次生成请求。不会把其他档位或数值预算映射为所请求的档位。

发送时使用 `generationConfig.thinkingConfig.thinkingLevel`，值为对应大写 `LOW` / `MEDIUM` / `HIGH`。两次生成响应都必须包含原有来源锁定确认，以及完全匹配的 `X-Mixer-Thinking-Level` 和 `X-Mixer-Thinking-Validation: account-catalog`；后者由后端校验实际选中账号的模型目录和最终请求档位后提供。普通回显不足以通过。此验证记录目录支持、请求档位未被改写及实际调用成功，不测量或声称证明 Google 内部使用了多少计算量。

档位验证每个来源最多增加两次目录读取，每次最多 `min(--timeout, 15)` 秒；生成请求各自使用 `--timeout` 时限。因此一次“目录读取 + 生成”可能用到这两个时限之和。目录和生成均禁止跳转，响应上限为 1 MiB；401、403、429 会停止后续来源，无自动重试。

SSE 成功要求：返回固定通道和锁定确认头、至少两个含可见文本的增量事件、真实 `STOP` 终态，并且首个文本事件与终态来自不同的网络读取批次。整个结果一次性缓冲后送达，或者短回答只产生一个事件，都会显示 `incremental_delivery_not_observed`。这表示本次未观察到增量，不能当作已验证流式；工具不会自动重试来消耗额度。

确认要生成 Mixer 的能力证据时，显式指定文件。若在主机验收，但 Mixer 在容器内使用另一个后端地址，另外指定容器访问地址：

```text
python tools/verify_aistudio_http.py --model <upstream-model-id> --evidence-output work/aistudio-http-evidence.json --evidence-base-url http://aistudio-http:2048
```

只有某个来源的非流式和 SSE **都成功**，才写入该来源与精确模型的 `text=true`、`streaming=true`。默认运行的 `thinking` 保持为空；显式档位验证全部通过时，还写入本次档位，例如 `"thinking":{"high":{"thinkingLevel":"HIGH"}}`。工具与图片能力仍不作支持声明。将生成文件只读挂载给 Mixer，并设置 `AISTUDIO_HTTP_EVIDENCE_FILE`，文件中的后端地址必须与 Mixer 的 `AISTUDIO_HTTP_BASE_URL` 完全一致。

输出文件会原子替换，**只包含本次成功项和本次明确验证的档位**，不会合并或刷新以前其他模型、来源或档位的证据时间。逐档运行时请使用不同的输出文件；对同一文件连续运行不会累积 low / medium / high。若所有来源失败，原文件保持不变，摘要中的 `evidence_written` 为 `false`。有一个来源失败而另一个通过时，可以写入通过的来源，但工具仍返回非零退出码。

合成测试不会连接 Google 或读取任何真实会话：

```text
python -m unittest tests.test_verify_aistudio_http -q
```
