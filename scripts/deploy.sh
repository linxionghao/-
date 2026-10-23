#!/usr/bin/env bash
# 打包并生成可交付目录 release/<时间戳>-<版本>/。
# 本项目是命令行工具，没有服务端，"部署"即产出可分发的一包文件；
# 若后续接 Web 服务，把最后的 cp 换成上传即可。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python}"

VERSION="$("$PYTHON" -c "import sys; sys.path.insert(0, 'src'); from quality_trace import __version__; print(__version__)")"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="release/${STAMP}-v${VERSION}"

echo "==> 版本: v${VERSION}"

if [ ! -d dist ] || [ -z "$(ls -A dist 2>/dev/null)" ]; then
    echo "==> dist/ 为空，先执行构建"
    ./scripts/build.sh
fi

echo "==> 生成交付目录: ${OUT}"
mkdir -p "$OUT"
cp -r dist "$OUT"/
cp config/default.yaml "$OUT"/
cp README.md LICENSE CHANGELOG.md "$OUT"/

echo "==> 完成，内容如下:"
find "$OUT" -type f | sort
