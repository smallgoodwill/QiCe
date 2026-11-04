# 启动本地服务（默认 http://127.0.0.1:8765）
#   .\scripts\run.ps1            默认端口
#   .\scripts\run.ps1 -Port 9000 指定端口
#   .\scripts\run.ps1 -Verbose   输出调试日志
param(
    [int]$Port = 8765,
    [switch]$Verbose
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    throw '虚拟环境不存在。请先运行： .\scripts\install.ps1'
}
if (-not (Test-Path "$root\.env")) {
    Write-Host '提示：尚未创建 .env，AI 问答与 SimNow 将不可用。可从 .env.example 复制后填写。'
}

$argsList = @('-m', 'app.server', '--port', $Port)
if ($Verbose) { $argsList += '--verbose' }
& "$root\.venv\Scripts\python.exe" @argsList
