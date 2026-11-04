# 一键安装依赖（在项目根目录创建 .venv）
#   .\scripts\install.ps1            仅装运行必需依赖（python-dotenv）
#   .\scripts\install.ps1 -WithCtp   额外安装 SimNow/CTP 行情网关
param(
    [switch]$WithCtp
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host '创建虚拟环境 .venv ...'
    python -m venv "$root\.venv"
}
$py = "$root\.venv\Scripts\python.exe"

# 有些环境用 --without-pip 建的 venv 没有 pip，这里补一次
& $py -m pip --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '虚拟环境缺少 pip，正在补装 ...'
    & $py -m ensurepip --upgrade
}

& $py -m pip install --upgrade pip
& $py -m pip install -r "$root\requirements.txt"

if ($WithCtp) {
    Write-Host '安装 SimNow / CTP 网关（Windows x64 + Python 3.13 wheel）...'
    & $py -m pip install -r "$root\requirements-ctp.txt"
}

if (-not (Test-Path "$root\.env")) {
    Copy-Item "$root\.env.example" "$root\.env"
    Write-Host '已从 .env.example 生成 .env，请填入 API Key 与 SimNow 账号。'
}

Write-Host ''
Write-Host '依赖安装完成。下一步：'
Write-Host '  1) 编辑 .env 填入 DEEPSEEK_API_KEY / SIMNOW_*'
Write-Host '  2) .\scripts\run.ps1        启动服务'
Write-Host '  3) .\scripts\smoke_test.ps1 全链路自检'
