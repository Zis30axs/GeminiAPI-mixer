# Native window; no web server or persistent background service.
$ErrorActionPreference = 'Stop'
try {
    $python = (Get-Command pythonw.exe -ErrorAction Stop).Source
    $entry = Join-Path $PSScriptRoot 'session_manager.py'
    $deployment = Split-Path -Parent $PSScriptRoot
    # Windows argument quoting. Paths originate from this installed script.
    function Quote-Argument([string]$value) {
        return '"' + ([regex]::Replace([regex]::Replace($value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1')) + '"'
    }
    $arguments = (Quote-Argument $entry) + ' --deployment-root ' + (Quote-Argument $deployment)
    Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $deployment
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('无法打开 Gemini 入口。需要已安装 Python（含 tkinter、httpx）和现有 Mixer 部署。', 'Gemini 快捷入口') | Out-Null
    exit 1
}
