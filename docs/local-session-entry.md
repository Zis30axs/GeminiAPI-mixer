# Gemini 本机快捷入口

`tools/session_manager.py` 提供一个原生 Windows 窗口，只维护 Gemini Web、
AI Studio Playground（AIS）和 Build。无需新容器、浏览器窗口或管理端 HTTP 端口，
关闭窗口即退出。沿用现有部署及登录态导出扩展。

## 打开

需要 Python 3.10+（含 tkinter）、httpx、Docker CLI，以及已有 `.env`：

```powershell
powershell -NoProfile -File tools/Open-Gemini-Sessions.ps1
# 或明确指定部署目录：
python tools/session_manager.py --deployment-root D:\AI-Services\GeminiAPI-mixer
# 只读状态，不发起生成：
python tools/session_manager.py --deployment-root D:\AI-Services\GeminiAPI-mixer --check
```

本机默认沿用端口 18083（Mixer）和 18084（AIS 后端），容器名及 Compose 标签必须
匹配现有部署。配置使用 `.env` 中的 `AISTUDIO_DATA_DIR`、
`GEMINI_WEB_ACCOUNTS_DIR_HOST`、`MIXER_API_KEYS`、`AISTUDIO_ADMIN_USERNAME`、
`AISTUDIO_ADMIN_PASSWORD`；窗口不要求手输 Key，也不显示这些值。
部署目录的父目录需有 `Backups` 文件夹，现有 Web manifest 和 AI Studio 私有账号目录
由先前导入工具管理。此入口用于维护既有部署，不创建新数据库或重建容器。

## 日常使用

1. 在 Edge 正常登录 Gemini / AI Studio，确认页面能生成，然后用已有扩展导出 JSON。
2. 在窗口点“选择 JSON”多选文件，或“读取下载目录最新文件”。下载目录按钮只读取
   `gemini-auth*.json` / `aistudio-session-*.json`，只匹配已配置账号并取最新可解析文件，
   **尚不导入**。新增账号请用手动选择，防止下载目录的旧文件意外加入账号池。
3. 核对通道和 u 编号，点“导入并应用”。Web 使用其文件中的 u 编号；一份 AI Studio
   JSON 同时提供 AIS 和 Build 的账号登录态。账号权限、模型和额度仍依各通道实际情况。
4. 默认不自动测活；需要时勾选通道后“一键测活”，或自行勾选导入后测活。普通测活仅发一条
   Gemini 3.8 短请求；可额外勾选流式，普通生成失败时不会继续尝试流式或自动重试。

每次测活检查轮询中的一个账号，结果显示匿名 Web 账号标识。不是全账号逐个认证报告。
后端“就绪”、文件校验成功、HTTP 200 均不等于生成成功；只有所选通道返回有效文本，
流式同时完整结束，才显示通过。已知 AIS 全账号需要认证时直接提示重新导出，避免无用生成。
默认不测 Argon，也不涉及 GPT 或 Antigravity。

AIS/Build 参数、工具、图片和流式请求直接交给上游处理，不再需要人工验证记录。
“验证工具”按钮已移除，旧证据文件不再控制运行能力。目录中的能力字段表示上游声明，
不表示 Mixer 重新生成过测试结果。认证、实际账号权限、额度、协议格式与安全限制仍生效。

## 导入与恢复

- 导入前整批校验：大小、字段、账号身份、Cookie 域/过期时间、路径及重复账号。
  同批任何文件不合法都不开始写入。选择超过 15 分钟需重新选取。
- 相同内容跳过，不通过重启重新尝试失效认证。新导出替换前写私密备份。
- Web 更新会短暂停止并启动 Mixer，使进程读取新账号池；AIS 更新只停止并启动现有
  AIS 后端。批量混合导入会更新这两者；其他容器、配置、Key 保持原样。
- 保留账号配置、运行文件和冷却，不以续期为由清除额度或调度限制。
- 写入或容器启动失败会恢复原登录文件、manifest，再尝试启动原容器；恢复失败会
  提示人工检查。不递归删除账号目录，不执行来源文件提供的命令或 URL。
- Windows 文件使用当前用户与 SYSTEM 的私密 ACL。备份位于部署父目录的
  `Backups/session-entry-*`；成功测活的安全摘要在 `work/runtime/session-manager-check.json`。
  备份包含登录态，应像其他私有账号文件一样保管。源下载文件不被改动或自动删除。
- 同机多个窗口由跨进程文件锁串行执行导入和测活。导入途中窗口会要求等操作完成再关闭。

此工具无法延长 Google 会话寿命。认证过期仍需正常登录并重新导出，原有 JSON 即使仍在
Cookie 有效期内也可能被 Google 撤销。网络故障、冷却和上游拒绝不会伪装成导入成功。

## 验证

合成测试覆盖混合导入、原配置与冷却保留、写入/启动失败回滚、同内容跳过、路径与
Cookie 注入、跨进程锁、重复账号、流式 HTTP 200 内嵌错误以及安全错误摘要。
测试不使用真实登录态、Docker 或上游生成。

```powershell
python -m unittest discover -s tests -p test_session_manager.py -v
```
