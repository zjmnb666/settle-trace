# SettleTrace 开发环境（Windows PowerShell）
# 用法： .\scripts\dev.ps1            启动服务
#        .\scripts\dev.ps1 check_db   执行其他子命令
# 若提示"禁止运行脚本"，先执行：
#        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".venv")) {
    Write-Host "[1/3] 创建虚拟环境 .venv"
    python -m venv .venv
}

Write-Host "[2/3] 安装依赖"
& ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[3/3] 已生成 .env，请填写 DATABASE_URL 后重新执行本脚本。"
    exit 0
}

Write-Host "[3/3] 启动"
if ($args.Count -eq 0) {
    & ".venv\Scripts\python.exe" run.py serve
} else {
    & ".venv\Scripts\python.exe" run.py @args
}
