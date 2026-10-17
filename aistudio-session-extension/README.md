# AI Studio 本机登录态导出（Edge / Chrome）

此扩展打开时读取当前 AI Studio 页面的邮箱和账号索引，只有你确认并点击“导出本机登录文件”后才读取 Cookie。不需要浏览器常驻，没有后台服务、自动上传、第三方连接、Drive 授权或密码读取。它使用浏览器提供的 cookies API，导出当前 AI Studio 标签所属浏览器配置的 Google 会话。

## Edge 一次性操作

1. 打开 `edge://extensions`，开启“开发人员模式”。
2. 选择“加载解压缩的扩展”，选中本目录（目录中应直接包含 manifest.json）。
3. 在 Edge 打开并正常登录 [AI Studio](https://aistudio.google.com/prompts/new_chat)。先手动生成一条消息，确认当前账号可用。
4. 保持这个页面为当前标签，点 Edge 右上角扩展图标，选择 **Mixer AI Studio Session Export**。
5. 扩展会自动填入页面提供的邮箱和账号索引，支持 /u/1/、/u/2/、/u/3/ 等路径及 authuser= 参数。与 AI Studio 头像菜单核对后再勾选确认。若页面没有提供可靠字段，扩展会说明原因并留空供你手填，绝不会猜成账号 0。不同身份来源冲突时必须刷新页面再试。切换账号后点击“重新读取当前页面”。
6. 勾选确认，点击“导出本机登录文件”，在保存对话框中保存 JSON。只需告诉操作人员本机文件路径，**不要把 JSON 内容贴入聊天或上传**。

Chrome 的操作相同，扩展管理地址为 `chrome://extensions`。

从 1.0.0 升级时，在 Edge 扩展页重新加载扩展并接受新增的 scripting 权限，然后刷新 AI Studio。该权限只用于读取当前标签顶层页面的身份元信息和当前账号头像标签，不会运行后台脚本、扫描其他标签或遍历账号。

## 导入到 Mixer 的私有账号目录

在 Mixer 仓库中运行（Python 3.10+，不需要额外依赖）：

    python tools/import_aistudio_session.py --source "C:\你的目录\aistudio-session-0.json" --auth-dir "C:\Mixer私有目录\auth"

两个参数都必须是明确的绝对本机路径。目标的父目录应已存在；auth 必须是新建、空目录或由此工具管理的目录。成功只输出 imported 状态和 Cookie 数量，不输出邮箱或凭据。

每个账号生成以经过严格校验的 Google 邮箱命名的独立目录，与上游账号格式一致；目录名属于私有数据。可逐个导出并导入多个自有账号。重复账号默认拒绝覆盖；续期时先停止对应 Sidecar，再追加 `--replace`。续期保留原有账号配置、运行状态及额度冷却记录。首次账号配置中的 proxy 默认为空；需要代理的部署应在启动前按实际出口配置。

导入目录在 Windows 上只允许当前用户和 SYSTEM 访问；在其他系统上使用目录 0700、文件 0600。容器中的运行用户仍需具备该挂载目录的读取与状态写回权限。

完成导入后，可删除下载的 JSON 副本并停用扩展，浏览器无需保持打开。Google 登录失效时再正常登录、导出并替换。多账号浏览器的共享 Cookie 可能涵盖其他 Google 会话，推荐使用仅含目标账号的独立浏览器配置。

## 验证与范围

扩展按当前标签的 Cookie Store 读取，并且只导出适用于 AI Studio 的 Google Cookie；不读取浏览器数据库、不导出 localStorage、不保存密码。自动识别只使用当前页面的启动元信息（例如 WIZ_global_data.oPEP7c/currentpageaccount）、URL、身份 meta 或可见账号头像标签，不读取聊天正文、历史记录或账号列表。

同时支持新版页面放在 `script[type="application/json"]` 中的启动元信息，脚本随机 ID 不影响识别。只接受顶层对象同时具备字符串 `WIu0Nc` 标记和有效邮箱字符串 `oPEP7c` 的形状；仅提取邮箱，以及确实存在的 `authuser` / `currentpageaccount` 索引，不遍历嵌套账号列表。标记字段只做类型检查，其内容及其他启动字段不会返回给扩展界面、写入导出文件或日志。此处只解析 JSON，不执行脚本。

启动 JSON 最多检查 8 个；单个最多 65,536 个字符，合计最多 131,072 个字符。非匹配内容会忽略，超过任一上限则停止检查，避免基于不完整扫描绑定账号。多个匹配对象或其他身份来源给出不同邮箱、索引时仍停止导出。只有邮箱但没有明确索引时继续要求手动核对，绝不推测账号 0。

导出前后都会重新检查同一个页面 documentId、邮箱和账号索引；发现换页、同一网址换账号或输入与识别结果不一致时停止导出。缺少可靠信息时仍允许用户核对后手填，这不等同于后端身份验证，后端应继续检查认证结果。

标准 API 依据：[Chrome cookies](https://developer.chrome.com/docs/extensions/reference/api/cookies)、[activeTab](https://developer.chrome.com/docs/extensions/develop/concepts/activeTab)、[scripting](https://developer.chrome.com/docs/extensions/reference/api/scripting)。这套 API 可用于 Edge 的 Chromium 扩展。
