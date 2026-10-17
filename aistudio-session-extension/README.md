# AI Studio 本机登录态导出（Edge / Chrome）

此扩展仅在你点击“导出本机登录文件”时工作，不需要浏览器常驻。没有后台服务、自动上传、第三方连接、Drive 授权或密码读取。它使用浏览器提供的 cookies API，导出当前 AI Studio 标签所属浏览器配置的 Google 会话。

## Edge 一次性操作

1. 打开 `edge://extensions`，开启“开发人员模式”。
2. 选择“加载解压缩的扩展”，选中本目录（目录中应直接包含 manifest.json）。
3. 在 Edge 打开并正常登录 [AI Studio](https://aistudio.google.com/prompts/new_chat)。先手动生成一条消息，确认当前账号可用。
4. 保持这个页面为当前标签，点 Edge 右上角扩展图标，选择 **Mixer AI Studio Session Export**。
5. 从 AI Studio 头像菜单核对当前邮箱，填入扩展。填写当前账号索引：独立且只登录一个 Google 账号的浏览器配置通常为 0；多账号时核对页面 URL 中的 authuser= 或 /u/ 后的数字。扩展发现网址索引不一致会拒绝导出；它不会自行确认填写邮箱的身份。
6. 勾选确认，点击“导出本机登录文件”，在保存对话框中保存 JSON。只需告诉操作人员本机文件路径，**不要把 JSON 内容贴入聊天或上传**。

Chrome 的操作相同，扩展管理地址为 `chrome://extensions`。

## 导入到 Mixer 的私有账号目录

在 Mixer 仓库中运行（Python 3.10+，不需要额外依赖）：

    python tools/import_aistudio_session.py --source "C:\你的目录\aistudio-session-0.json" --auth-dir "C:\Mixer私有目录\auth"

两个参数都必须是明确的绝对本机路径。目标的父目录应已存在；auth 必须是新建、空目录或由此工具管理的目录。成功只输出 imported 状态和 Cookie 数量，不输出邮箱或凭据。

每个账号生成以经过严格校验的 Google 邮箱命名的独立目录，与上游账号格式一致；目录名属于私有数据。可逐个导出并导入多个自有账号。重复账号默认拒绝覆盖；续期时先停止对应 Sidecar，再追加 `--replace`。续期保留原有账号配置、运行状态及额度冷却记录。首次账号配置中的 proxy 默认为空；需要代理的部署应在启动前按实际出口配置。

导入目录在 Windows 上只允许当前用户和 SYSTEM 访问；在其他系统上使用目录 0700、文件 0600。容器中的运行用户仍需具备该挂载目录的读取与状态写回权限。

完成导入后，可删除下载的 JSON 副本并停用扩展，浏览器无需保持打开。Google 登录失效时再正常登录、导出并替换。多账号浏览器的共享 Cookie 可能涵盖其他 Google 会话，推荐使用仅含目标账号的独立浏览器配置。

## 验证与范围

扩展按当前标签的 Cookie Store 读取，并且只导出适用于 AI Studio 的 Google Cookie；不读取浏览器数据库、不导出 localStorage、不保存密码。它不会证明所填邮箱与网页实际身份一致，后端应核对身份并在认证失败时停止该账号。

标准 API 依据：[Chrome cookies](https://developer.chrome.com/docs/extensions/reference/api/cookies)、[activeTab](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab)。这套 API 可用于 Edge 的 Chromium 扩展。
