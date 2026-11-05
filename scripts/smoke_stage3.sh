#!/usr/bin/env bash
# 批次 1c 冒烟脚本：对话审计 + 归因审批 + 8D 导出
# 用法：先启动服务（scripts/run_server.sh），再执行 bash scripts/smoke_stage3.sh [端口]
# 退出码：0 全部通过；非 0 表示有断言失败（便于 CI 使用）
set -u

PORT="${1:-8000}"
BASE="http://127.0.0.1:${PORT}"
PY="${PYTHON:-python}"
PASS=0
FAIL=0

# 断言助手：check <说明> <期望片段> <实际输出>
check() {
  local desc="$1" expect="$2" actual="$3"
  if echo "$actual" | grep -q "$expect"; then
    echo "  ✓ $desc"
    PASS=$((PASS + 1))
  else
    echo "  ✗ $desc"
    echo "    期望包含：$expect"
    echo "    实际输出：$(echo "$actual" | head -c 200)"
    FAIL=$((FAIL + 1))
  fi
}

echo "== 0) 健康检查 =="
check "服务健康" '"code":0' "$(curl -s "${BASE}/api/v1/health")"

echo "== 1) 主管登录 =="
LOGIN=$(curl -s -X POST "${BASE}/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"manager01","password":"manager01"}')
check "登录成功" '"code":0' "$LOGIN"
TOKEN=$(echo "$LOGIN" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")

AUTH="Authorization: Bearer ${TOKEN}"

echo "== 2) 审计列表：仅越界 =="
AUDIT=$(curl -s "${BASE}/api/v1/admin/audit/messages?only_flagged=1" -H "$AUTH")
check "越界样本可查出" '"total":3' "$AUDIT"

echo "== 3) 审计列表：按批次筛选 =="
BY_BATCH=$(curl -s "${BASE}/api/v1/admin/audit/messages?batch_no=WO-20261030-001" -H "$AUTH")
check "批次 WO-20261030-001 命中 2 条" '"total":2' "$BY_BATCH"

echo "== 4) 审计列表：按角色筛选 =="
BY_ROLE=$(curl -s "${BASE}/api/v1/admin/audit/messages?role=shift_leader" -H "$AUTH")
check "班组长维度筛选可用" '"code":0' "$BY_ROLE"

echo "== 5) 核查一条越界回答 =="
MID=$(echo "$AUDIT" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['messages'][0]['message_id'])")
REVIEW=$(curl -s -X POST "${BASE}/api/v1/admin/audit/messages/${MID}/review" \
  -H "Content-Type: application/json" -H "$AUTH" \
  -d '{"note":"冒烟脚本：已核查"}')
check "核查落库" '"code":0' "$REVIEW"

echo "== 6) 补扫（规则升级后可重跑）=="
RESCAN=$(curl -s -X POST "${BASE}/api/v1/admin/audit/rescan" -H "$AUTH")
check "补扫返回统计" '"scanned"' "$RESCAN"
check "补扫命中数不回退" '"flagged":3' "$RESCAN"

echo "== 7) 归因审批状态机 =="
APPROVE=$(curl -s -X POST "${BASE}/api/v1/admin/attributions/attr-20261030-0001/approve" \
  -H "Content-Type: application/json" -H "$AUTH" -d '{"note":"证据链完整，同意"}')
if echo "$APPROVE" | grep -q '"status":"approved"'; then
  echo "  ✓ 首次批准成功"
  PASS=$((PASS + 1))
  AGAIN=$(curl -s -X POST "${BASE}/api/v1/admin/attributions/attr-20261030-0001/approve" \
    -H "Content-Type: application/json" -H "$AUTH" -d '{"note":"再批"}')
  check "重复审批被拒 40906" '40906' "$AGAIN"
elif echo "$APPROVE" | grep -q '40906'; then
  # 已审批过（重复执行脚本）——状态机同样得到验证
  echo "  ✓ 单据已审批，状态机拒绝重复操作"
  PASS=$((PASS + 1))
else
  echo "  ✗ 审批返回异常：$(echo "$APPROVE" | head -c 200)"
  FAIL=$((FAIL + 1))
fi

echo "== 8) 退回必填意见 =="
REJECT_EMPTY=$(curl -s -X POST "${BASE}/api/v1/admin/attributions/attr-20261030-0002/reject" \
  -H "Content-Type: application/json" -H "$AUTH" -d '{"note":"   "}')
check "空意见被拒 40001" '40001' "$REJECT_EMPTY"

echo "== 9) 8D 导出（有缓存直读）=="
DISP=$(curl -s -D - -o /tmp/smoke_8d.md \
  "${BASE}/api/v1/admin/attributions/attr-20261030-0002/export-8d" -H "$AUTH" \
  | grep -i "content-disposition")
check "下载头含文件名" '8D-' "$DISP"
check "草稿含复核红线" '本报告为系统草稿' "$(cat /tmp/smoke_8d.md)"

echo "== 10) 越权：班组长调主管接口 =="
SL=$(curl -s -X POST "${BASE}/api/v1/auth/login" -H "Content-Type: application/json" \
  -d '{"username":"leader01","password":"leader01"}' \
  | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
FORBID=$(curl -s -w "|%{http_code}" "${BASE}/api/v1/admin/audit/messages" \
  -H "Authorization: Bearer ${SL}")
check "返回 40301 + HTTP 403" '40301' "$FORBID"

echo "== 11) 匿名访问 =="
ANON=$(curl -s "${BASE}/api/v1/admin/audit/messages")
check "匿名 40101" '40101' "$ANON"

echo ""
echo "结果：${PASS} 通过 / ${FAIL} 失败"
[ "$FAIL" -eq 0 ] || exit 1
