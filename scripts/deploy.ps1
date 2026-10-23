# 打包并生成可交付目录 release/<时间戳>-v<版本>/。
# 用法: pwsh -File scripts/deploy.ps1
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = if ($env:PYTHON) { $env:PYTHON } else { 'python' }

$version = & $python -c "import sys; sys.path.insert(0, 'src'); from quality_trace import __version__; print(__version__)"
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$out = Join-Path 'release' "$stamp-v$version"

Write-Host "==> 版本: v$version"

if (-not (Test-Path 'dist') -or -not (Get-ChildItem 'dist' -ErrorAction SilentlyContinue)) {
    Write-Host '==> dist/ 为空，先执行构建'
    & (Join-Path $PSScriptRoot 'build.ps1')
}

Write-Host "==> 生成交付目录: $out"
New-Item -ItemType Directory -Force -Path $out | Out-Null
Copy-Item 'dist' $out -Recurse
Copy-Item 'config\default.yaml' $out
Copy-Item 'README.md', 'LICENSE', 'CHANGELOG.md' $out

Write-Host '==> 完成，内容如下:'
Get-ChildItem $out -Recurse -File | ForEach-Object {
    $_.FullName.Substring((Resolve-Path $out).Path.Length + 1)
}
