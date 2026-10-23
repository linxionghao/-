# 构建分发包（wheel + sdist）。
# 用法: pwsh -File scripts/build.ps1
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = if ($env:PYTHON) { $env:PYTHON } else { 'python' }

Write-Host "==> 解释器: $(& $python --version 2>&1)"
& $python -c "import build" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error '缺少 build 模块，先执行: pip install -r requirements-dev.txt'
    exit 1
}

Write-Host '==> 清理旧产物'
Remove-Item -Recurse -Force dist, build, src\*.egg-info -ErrorAction SilentlyContinue

Write-Host '==> 开始构建'
& $python -m build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host '==> 构建完成，产物在 dist/'
Get-ChildItem dist | Select-Object Name, @{n = 'KB'; e = { [math]::Round($_.Length / 1KB, 1) } }
