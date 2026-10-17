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

SSE 成功要求：返回固定通道和锁定确认头、至少两个含可见文本的增量事件、真实 `STOP` 终态，并且首个文本事件与终态来自不同的网络读取批次。整个结果一次性缓冲后送达，或者短回答只产生一个事件，都会显示 `incremental_delivery_not_observed`。这表示本次未观察到增量，不能当作已验证流式；工具不会自动重试来消耗额度。

确认要生成 Mixer 的能力证据时，显式指定文件。若在主机验收，但 Mixer 在容器内使用另一个后端地址，另外指定容器访问地址：

```text
python tools/verify_aistudio_http.py --model <upstream-model-id> --evidence-output work/aistudio-http-evidence.json --evidence-base-url http://aistudio-http:2048
```

只有某个来源的非流式和 SSE **都成功**，才写入该来源与精确模型的 `text=true`、`streaming=true`。文件不包含思考等级、工具或图片的支持声明；这些能力仍需独立验证。将生成文件只读挂载给 Mixer，并设置 `AISTUDIO_HTTP_EVIDENCE_FILE`，文件中的后端地址必须与 Mixer 的 `AISTUDIO_HTTP_BASE_URL` 完全一致。

输出文件会原子替换，**只包含本次成功项**，不会合并或刷新以前其他模型的证据时间。若所有来源失败，原文件保持不变，摘要中的 `evidence_written` 为 `false`。有一个来源失败而另一个通过时，可以写入通过的来源，但工具仍返回非零退出码。

合成测试不会连接 Google 或读取任何真实会话：

```text
python -m unittest tests.test_verify_aistudio_http -q
```
