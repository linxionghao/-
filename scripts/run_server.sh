#!/usr/bin/env bash
# 启动问答服务（API + 聊天页面），在仓库根目录执行：
#   bash scripts/run_server.sh [端口]
set -e
cd "$(dirname "$0")/.."

PORT="${1:-8000}"
PYTHON="${PYTHON:-python}"

# 检索索引不存在时自动建库（幂等）
"$PYTHON" examples/phase1_rag_demo.py --ingest

echo "启动 quality-trace 问答服务: http://127.0.0.1:${PORT}/"
PYTHONPATH=src "$PYTHON" -m uvicorn quality_trace.api.app:app --host 127.0.0.1 --port "$PORT"
