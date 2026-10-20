"""Small native desktop front end; all maintenance runs off the UI thread."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from session_manager import MaintenanceError, select_exports

LABELS = {"web": "Gemini Web", "ais": "AI Studio · AIS", "build": "AI Studio · Build", "studio": "AIS + Build"}
RESULTS = {
    "ok": "可用", "auth_required": "需要重新登录、导出 JSON", "rate_limited": "上游限流或额度不足",
    "timeout": "请求超时，请稍后测活", "web_accounts_unavailable": "账号忙碌、冷却中或认证失效",
    "connection_failed": "连接失败，请检查本机服务和网络", "model_unavailable": "Gemini 3.8 暂不可用",
    "upstream_error": "上游拒绝请求，请更新登录态后再测", "invalid_response": "返回异常，未判为可用",
    "invalid_generation": "没有有效回复，未判为可用", "incomplete_stream": "流式未完整结束",
    "mixer_key_rejected": "本机 Mixer Key 不匹配，请检查部署配置",
}
ERRORS = {
    "unchanged_exports": "这些文件与已导入内容相同，未重启服务。若仍掉线，请重新登录并导出新文件。",
    "duplicate_account_in_selection": "同一账号选了多份文件。请仅保留该账号最新的一份。",
    "expired_or_invalid_cookie": "文件中的登录 Cookie 已过期，请重新登录并导出。",
    "selection_expired": "所选文件已等待超过 15 分钟，请重新选择。",
    "import_rolled_back": "导入失败，原登录文件已恢复。请先确认 Docker 运行，再重试。",
    "rollback_needs_attention": "导入或恢复未完整完成。请检查 Docker 和 Backups 中的恢复记录，再运行 D 盘“启动服务”。",
    "docker_operation_failed": "Docker 操作失败，请先运行 D 盘“启动服务”。",
    "docker_missing": "未找到 Docker。请先启动 D 盘服务。",
    "unexpected_container": "现有容器与部署配置不符，已停止操作。",
    "operation_in_progress": "已有维护操作在运行，请等待完成。",
    "batch_size_limit": "一次最多选择 32 个账号文件。",
    "too_many_downloads_select_files": "下载目录中的旧导出文件过多，请用“选择 JSON”挑选最新文件。",
}


def open_window(manager):
    root = tk.Tk()
    root.title("Gemini 快捷入口")
    root.geometry("1020x790")
    root.minsize(940, 760)
    root.configure(bg="#f2f5f4")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background="#f2f5f4")
    style.configure("Card.TFrame", background="white")
    style.configure("TLabel", background="#f2f5f4", foreground="#1b302b", font=("Microsoft YaHei UI", 10))
    style.configure("Muted.TLabel", foreground="#596c65")
    style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
    style.configure("Sub.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
    style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(14, 9))
    style.configure("Accent.TButton", background="#18674e", foreground="white")
    style.map("Accent.TButton", background=[("active", "#235b4a"), ("disabled", "#9aa9a4")])
    style.configure("TCheckbutton", background="#f2f5f4", font=("Microsoft YaHei UI", 10))
    style.configure("Treeview", font=("Microsoft YaHei UI", 10), rowheight=28, background="white", fieldbackground="white")
    style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"), padding=(7, 7))

    events = queue.Queue()
    state = {"busy": False, "exports": [], "results": {}, "snapshot": {}}
    buttons = []
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Gemini 快捷入口", style="Heading.TLabel").pack(anchor="w")
    ttk.Label(frame, text="导入登录文件，快速确认 Web / AIS / Build 是否可用。", style="Muted.TLabel").pack(anchor="w", pady=(5, 12))

    def action_button(parent, text, command, primary=False):
        button = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if primary else "TButton")
        button.pack(side="left", padx=(0, 10))
        buttons.append(button)
        return button

    status_var = tk.StringVar(value="正在读取本机状态…")
    auto = tk.BooleanVar(value=False)
    streaming = tk.BooleanVar(value=False)
    channels = {s: tk.BooleanVar(value=True) for s in ("web", "ais", "build")}

    def progress(text):
        events.put(("progress", text))

    def begin(function, completed):
        if state["busy"]:
            return
        state["busy"] = True
        for button in buttons:
            button.configure(state="disabled")
        bar.start(12)
        def work():
            try:
                result = function()
                events.put(("done", (completed, result)))
            except Exception as error:
                code = str(error) if isinstance(error, (MaintenanceError, ValueError)) else "local_operation_failed"
                events.put(("error", ERRORS.get(code, "文件或本机操作未通过检查。请使用扩展重新导出的 JSON；原始错误与凭据不会显示。")))
        threading.Thread(target=work, daemon=True).start()

    def show_exports(items):
        state["exports"] = items
        preview.delete(*preview.get_children())
        for item in items:
            row = item.summary()
            preview.insert("", "end", values=(LABELS[row["kind"]], "u" + row["index"], row["account"], row["filename"]))
        import_button.configure(state="normal" if items else "disabled")
        status_var.set(f"已校验 {len(items)} 个文件。核对列表后点“导入并应用”。")

    def choose():
        paths = filedialog.askopenfilenames(parent=root, title="选择最新导出的 Gemini JSON（可多选）", initialdir=str(Path.home() / "Downloads"), filetypes=[("JSON 登录文件", "*.json")])
        if paths:
            begin(lambda: select_exports([Path(path) for path in paths]), show_exports)

    def downloads():
        def finished(value):
            items, rejected = value
            show_exports(items)
            if not items:
                status_var.set("下载目录没有可导入的文件。请先在 Edge 正常登录并导出，或手动选择 JSON。")
            elif rejected:
                status_var.set(f"已选出 {len(items)} 个现有账号的最新可解析文件；跳过 {rejected} 个不匹配或无效文件。认证以测活为准。")
        begin(lambda: manager.downloads(Path.home() / "Downloads"), finished)

    def refresh_rows(snapshot):
        state["snapshot"] = snapshot
        rows.delete(*rows.get_children())
        studio = snapshot.get("studio", {})
        counts = studio.get("counts", {})
        web_count = len(snapshot.get("web_accounts", []))
        for source in ("web", "ais", "build"):
            imported = str(web_count if source == "web" else counts.get("total", "?")) + " 个账号"
            if source == "web":
                readiness = "已导入，需测活确认" if web_count else "尚未配置"
            elif counts.get("total", 0) and counts.get("auth_required") == counts.get("total"):
                readiness = "全部需要重新认证"
            elif studio.get("ready"):
                readiness = f"后端就绪 {counts.get('ready', 0)} / {counts.get('total', '?')}"
            else:
                readiness = "后端未就绪 / 无法读取状态"
            result = state["results"].get(source)
            detail = "未测活"
            if result:
                detail = RESULTS.get(result.get("result"), "检查未通过")
                if result.get("stream"):
                    detail += " · 流式"
                if result.get("seconds") is not None:
                    detail += f" · {result['seconds']}s"
                if result.get("account"):
                    detail += " · " + result["account"]
            rows.insert("", "end", values=(LABELS[source], imported, readiness, detail), tags=("ok" if result and result.get("ok") else "normal",))
        mixer = "正常" if snapshot.get("mixer_reachable") else "无法连接"
        service_var.set("Mixer：" + mixer + "    ·    只检查本机    ·    测活使用 Gemini 3.8")
        tools = snapshot.get("tools", {})
        tools_var.set("Gemini 3.8 工具目录声明：" + " / ".join(s.upper() + (" 支持" if tools.get(s) is True else " 未声明支持" if tools.get(s) is False else " 状态未知") for s in ("ais", "build")))

    def refreshed(value):
        refresh_rows(value)
        status_var.set("状态已刷新。后端就绪不代表生成成功，请点“一键测活”。")

    def show_report(report):
        for row in report.get("results", []):
            state["results"][row["source"]] = row
        refresh_rows(state["snapshot"])
        passed = sum(row.get("ok", False) for row in report.get("results", []))
        status_var.set(f"测活完成：{passed} / {len(report.get('results', []))} 项通过。失效登录需重新导出，重启无法续期。")

    def test():
        selected = [s for s in channels if channels[s].get()]
        check_stream = streaming.get()
        if not selected:
            status_var.set("请至少选择一个通道。")
            return
        begin(lambda: (manager.test(selected, check_stream, progress), manager.snapshot()),
              lambda value: (refresh_rows(value[1]), show_report(value[0])))

    def apply():
        items = list(state["exports"])
        auto_test, check_stream = auto.get(), streaming.get()
        def work():
            result = manager.import_exports(items, progress)
            studio = any(item.kind == "studio" for item in items)
            sources = [s for s in ("web", "ais", "build") if any(i.kind == ("web" if s == "web" else "studio") for i in items)]
            if result["imported"]:
                manager.wait_after_import(studio, progress)
            report = manager.test(sources, check_stream, progress) if auto_test else None
            return result, report, manager.snapshot()
        def finished(value):
            result, report, snapshot = value
            state["results"] = {k: v for k, v in state["results"].items() if not result["imported"] or not any(i.kind == ("web" if k == "web" else "studio") for i in items)}
            refresh_rows(snapshot)
            if report:
                show_report(report)
            if not result["imported"]:
                status_var.set(ERRORS["unchanged_exports"])
            else:
                status_var.set(f"已导入 {result['imported']} 份，跳过 {result['unchanged']} 份相同文件。" + ("测活结果见上表。" if report else "请点击一键测活。"))
            state["exports"] = []
            preview.delete(*preview.get_children())
        begin(work, finished)

    toolbar = ttk.Frame(frame)
    toolbar.pack(fill="x", pady=(0, 10))
    action_button(toolbar, "选择 JSON（可多选）", choose)
    action_button(toolbar, "读取下载目录最新文件", downloads)
    import_button = action_button(toolbar, "导入并应用", apply, True)
    import_button.configure(state="disabled")
    ttk.Checkbutton(frame, text="导入后自动测活（仅测试涉及的通道）", variable=auto).pack(anchor="w", pady=(0, 10))
    preview_box = ttk.Frame(frame)
    preview_box.pack(fill="x")
    preview = ttk.Treeview(preview_box, columns=("type", "index", "account", "filename"), show="headings", height=3)
    for name, title, width in (("type", "导入通道", 130), ("index", "账号", 60), ("account", "身份", 210), ("filename", "文件名", 450)):
        preview.heading(name, text=title); preview.column(name, width=width, minwidth=50)
    preview.pack(side="left", fill="x", expand=True)
    scroll = ttk.Scrollbar(preview_box, command=preview.yview)
    scroll.pack(side="right", fill="y")
    preview.configure(yscrollcommand=scroll.set)
    ttk.Label(frame, text="AIS 文件同时用于 AIS / Build；Web 按 u 编号导入。自动备份；导入会短暂重启对应服务。", style="Muted.TLabel").pack(anchor="w", pady=(7, 12))
    ttk.Label(frame, text="通道状态", style="Sub.TLabel").pack(anchor="w")
    service_var = tk.StringVar(value="正在读取…")
    ttk.Label(frame, textvariable=service_var, style="Muted.TLabel").pack(anchor="w", pady=(5, 8))
    rows = ttk.Treeview(frame, columns=("source", "accounts", "ready", "result"), show="headings", height=3)
    for name, title, width in (("source", "通道", 160), ("accounts", "已导入", 100), ("ready", "登录 / 服务状态", 250), ("result", "本次测活结果", 370)):
        rows.heading(name, text=title); rows.column(name, width=width, minwidth=60)
    rows.tag_configure("ok", foreground="#176247")
    rows.pack(fill="x")
    tests = ttk.Frame(frame); tests.pack(fill="x", pady=(14, 8))
    for source in channels:
        ttk.Checkbutton(tests, text=source.upper(), variable=channels[source]).pack(side="left", padx=(0, 16))
    ttk.Checkbutton(tests, text="同时检查流式（更耗时）", variable=streaming).pack(side="left")
    tools = ttk.Frame(frame); tools.pack(fill="x", pady=(0, 12))
    action_button(tools, "一键测活", test, True)
    action_button(tools, "刷新状态", lambda: begin(manager.snapshot, refreshed))
    bar = ttk.Progressbar(frame, mode="indeterminate"); bar.pack(fill="x", pady=(3, 10))
    ttk.Label(frame, textvariable=status_var, wraplength=970).pack(anchor="w")
    tools_var = tk.StringVar(value="正在读取工具状态…")
    ttk.Label(frame, textvariable=tools_var, style="Muted.TLabel").pack(anchor="w", pady=(8, 0))
    ttk.Label(frame, text="参数、工具、图片和流式由上游处理，不需要人工验证记录。测活仅在你主动选择时运行。\n登录过期需在 Edge 重新导出。关闭窗口即退出。", style="Muted.TLabel").pack(anchor="w", pady=(5, 0))

    def drain():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "progress":
                    status_var.set(value)
                    continue
                state["busy"] = False
                bar.stop()
                for button in buttons:
                    button.configure(state="normal")
                if kind == "done":
                    callback, result = value
                    callback(result)
                else:
                    status_var.set(value)
                import_button.configure(state="normal" if state["exports"] else "disabled")
        except queue.Empty:
            pass
        root.after(100, drain)

    def close():
        if state["busy"]:
            messagebox.showinfo("正在处理", "请等待当前导入或测活结束后再关闭，避免中断文件恢复。", parent=root)
            return
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, drain)
    root.after(150, lambda: begin(manager.snapshot, refreshed))
    root.mainloop()
