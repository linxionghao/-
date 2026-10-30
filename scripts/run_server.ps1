# 启动问答服务（API + 聊天页面），在仓库根目录执行：
#   powershell -File scripts/run_server.ps1 [-Port 8000]
param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$Python = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "python3" }

# 检索索引不存在时自动建库（幂等）
& $Python examples/phase1_rag_demo.py --ingest
if (-not $?) { exit 1 }

Write-Host "启动 quality-trace 问答服务: http://127.0.0.1:$Port/"
$env:PYTHONPATH = "src"
& $Python -m uvicorn quality_trace.api.app:app --host 127.0.0.1 --port $Port
