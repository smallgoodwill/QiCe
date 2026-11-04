# 启动本地服务（默认 http://127.0.0.1:8765，启动后自动开浏览器）
#   .\scripts\run.ps1              默认端口
#   .\scripts\run.ps1 -Port 9000   指定端口
#   .\scripts\run.ps1 -NoOpen      不自动打开浏览器
#   .\scripts\run.ps1 -Verbose     输出调试日志
param(
    [int]$Port = 8765,
    [switch]$NoOpen,
    [switch]$Verbose
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    throw '虚拟环境不存在。请先运行： .\scripts\install.ps1'
}
if (-not (Test-Path (Join-Path $root '.env'))) {
    Write-Host '提示：尚未创建 .env，AI 问答与 SimNow 将不可用。可从 .env.example 复制后填写。'
}

$argsList = @('-m', 'app.server', '--port', $Port)
if ($Verbose) { $argsList += '--verbose' }
if (-not $NoOpen) { $argsList += '--open' }

Write-Host ''
Write-Host "  期策  http://127.0.0.1:$Port"
Write-Host '  按 Ctrl+C 停止服务'
Write-Host ''

& $py @argsList
$code = $LASTEXITCODE

# 退出码 3 = 端口上已有本服务在跑，这次只是复用，不算失败
if ($code -eq 3) {
    Write-Host '  已有实例在运行，浏览器已尝试打开。要重启请先停掉原来那个窗口。'
    exit 0
}
exit $code
