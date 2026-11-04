# 全链路自检：起一个临时服务进程，把网页会走的接口逐条打通。
#   .\scripts\smoke_test.ps1              使用临时数据目录（不动真实账本）
#   .\scripts\smoke_test.ps1 -Keep        保留临时数据目录以便排查
#   .\scripts\smoke_test.ps1 -External http://127.0.0.1:8765   只测已在跑的服务
param(
    [switch]$Keep,
    [string]$External = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = 'python' }

$argsList = @("$root\scripts\smoke_test.py")
if ($Keep) { $argsList += '--keep' }
if ($External) { $argsList += @('--external', $External) }

& $py @argsList
exit $LASTEXITCODE
