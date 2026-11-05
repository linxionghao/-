#!/usr/bin/env bash
# 班组长现场录入端冒烟脚本（批次 2）
# 用法：先启动服务（scripts/run_server.sh），再执行 bash scripts/smoke_defects.sh [端口]
# 前置：已执行 python scripts/seed_demo_data.py（工单与缺陷码字典）
set -u

PORT="${1:-8000}"
BASE="http://127.0.0.1:${PORT}"
PY="${PYTHON:-python}"
PASS=0
FAIL=0

check() {
  local desc="$1" expect="$2" actual="$3"
  if echo "$actual" | grep -q "$expect"; then
    echo "  ✓ $desc"; PASS=$((PASS + 1))
  else
    echo "  ✗ $desc"; echo "    期望包含：$expect"; echo "    实际：$(echo "$actual" | head -c 200)"
    FAIL=$((FAIL + 1))
  fi
}

echo "== 1) 页面与登录 =="
check "移动端页面 200" "200" "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/m")"
LOGIN=$(curl -s -X POST "${BASE}/api/v1/auth/login" -H "Content-Type: application/json" \
  -d '{"username":"leader01","password":"leader01"}')
check "班组长登录" '"role":"shift_leader"' "$LOGIN"
SL=$(echo "$LOGIN" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
AUTH="Authorization: Bearer ${SL}"

echo "== 2) 缺陷码字典 =="
CODES=$(curl -s "${BASE}/api/v1/defect-codes" -H "$AUTH")
check "字典分组返回" '"groups"' "$CODES"
check "含螺纹类缺陷码" 'D-THREAD' "$CODES"

echo "== 3) 建单：本线工单 =="
CREATE=$(curl -s -X POST "${BASE}/api/v1/defects" -H "Content-Type: application/json" -H "$AUTH" \
  -d '{"work_order_id":"WO-20261030-001","op_code":"OP30","description":"冒烟：螺纹外径偏小，通规过不去","qty":2,"severity":"一般"}')
check "返回 draft" '"status":"draft"' "$CREATE"
check "给出候选码" 'D-THREAD' "$CREATE"
DNO=$(echo "$CREATE" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['defect_no'])")
echo "     单号：$DNO"

echo "== 4) 跨线工单被拒 =="
CROSS=$(curl -s -w "|%{http_code}" -X POST "${BASE}/api/v1/defects" \
  -H "Content-Type: application/json" -H "$AUTH" \
  -d '{"work_order_id":"WO-20261030-002","description":"冒烟：跨线工单"}')
check "40406 + HTTP 404" '40406' "$CROSS"

echo "== 5) 人工确认缺陷码 =="
CONFIRM=$(curl -s -X POST "${BASE}/api/v1/defects/${DNO}/confirm" \
  -H "Content-Type: application/json" -H "$AUTH" -d '{"defect_code":"D-THREAD"}')
check "状态转 pending" '"status":"pending"' "$CONFIRM"
check "文案不含判定结论" '待质量工程师判定' "$CONFIRM"

echo "== 6) 重复确认被拒 =="
AGAIN=$(curl -s -X POST "${BASE}/api/v1/defects/${DNO}/confirm" \
  -H "Content-Type: application/json" -H "$AUTH" -d '{"defect_code":"D-SURF"}')
check "40905" '40905' "$AGAIN"

echo "== 7) 幂等补传 =="
CID="smoke-$(date +%s)"
P='{"work_order_id":"WO-20261030-001","description":"冒烟：离线补传毛刺","is_backfill":true'
R1=$(curl -s -X POST "${BASE}/api/v1/defects" -H "Content-Type: application/json" -H "$AUTH" \
  -d "${P},\"client_draft_id\":\"${CID}\"}")
R2=$(curl -s -X POST "${BASE}/api/v1/defects" -H "Content-Type: application/json" -H "$AUTH" \
  -d "${P},\"client_draft_id\":\"${CID}\"}")
check "首次为补录单" '"is_backfill":true' "$R1"
check "重复提交返回原单" '"existing":true' "$R2"
N1=$(echo "$R1" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['defect_no'])")
N2=$(echo "$R2" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['defect_no'])")
check "两次单号一致" "$N1" "$N2"

echo "== 8) 班组长字段裁剪 =="
LIST=$(curl -s "${BASE}/api/v1/defects" -H "$AUTH")
if echo "$LIST" | grep -q "suggest_json"; then
  echo "  ✗ 班组长响应泄露 suggest_json"; FAIL=$((FAIL + 1))
else
  echo "  ✓ 班组长响应不含 suggest_json"; PASS=$((PASS + 1))
fi

echo "== 9) 照片预上传 =="
IMG=$("$PY" -c "import base64;print(base64.b64encode(b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'+b'0'*100).decode())")
UP=$(curl -s -X POST "${BASE}/api/v1/uploads/photo" -H "Content-Type: application/json" -H "$AUTH" \
  -d "{\"photo\":\"data:image/jpeg;base64,${IMG}\"}")
check "返回 photo_token" 'photo_token' "$UP"

echo "== 10) 非图片被拒 =="
BAD=$(curl -s -X POST "${BASE}/api/v1/uploads/photo" -H "Content-Type: application/json" -H "$AUTH" \
  -d '{"photo":"data:image/png;base64,R0lGODlh"}')
check "40003" '40003' "$BAD"

echo "== 11) 角色边界 =="
QE=$(curl -s -X POST "${BASE}/api/v1/auth/login" -H "Content-Type: application/json" \
  -d '{"username":"qe001","password":"qe001"}' \
  | "$PY" -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
QE_CREATE=$(curl -s -w "|%{http_code}" -X POST "${BASE}/api/v1/defects" \
  -H "Content-Type: application/json" -H "Authorization: Bearer ${QE}" \
  -d '{"work_order_id":"WO-20261030-001","description":"冒烟：QE 越权建单"}')
check "质量工程师建单 40301" '40301' "$QE_CREATE"
ANON=$(curl -s -w "|%{http_code}" "${BASE}/api/v1/defects")
check "匿名 40101" '40101' "$ANON"

echo ""
echo "结果：${PASS} 通过 / ${FAIL} 失败"
[ "$FAIL" -eq 0 ] || exit 1
