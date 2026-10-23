#!/usr/bin/env bash
# 构建分发包（wheel + sdist）。
# 用法: ./scripts/build.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"

echo "==> 解释器: $("$PYTHON" --version 2>&1)"

if ! "$PYTHON" -c "import build" >/dev/null 2>&1; then
    echo "[error] 缺少 build 模块，先执行: pip install -r requirements-dev.txt" >&2
    exit 1
fi

echo "==> 清理旧产物"
rm -rf dist build src/*.egg-info

echo "==> 开始构建"
"$PYTHON" -m build

echo "==> 构建完成，产物在 dist/"
ls -lh dist/
