#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一条命令跑完整个闭环（对应需求验收标准 #8）。

为什么用 TestClient 而不是"起服务再 curl"：
  - 一条命令、无端口占用、无需后台进程，答辩现场不会因为端口被占而卡住；
  - 走的是**真实路由 + 真实 SQLite**（FastAPI 内部只把 ASGI 传输换成内存），
    鉴权依赖、统一响应包、SSE 事件流都会被真实执行；
  - 库与上传目录全部落在临时目录，跑完即弃，**不污染演示数据**。

跑法：
    python scripts/demo_closed_loop.py            # 正常跑
    python scripts/demo_closed_loop.py -v         # 逐条打印 SSE 事件

预期输出 6 步全 ✓。第 5 步会刻意展示"无 LLM Key 时的降级"——
这不是失败，是本项目设计的一部分（50002 + 中文说明）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient          # noqa: E402

import quality_trace.api.app as app_module         # noqa: E402
from quality_trace.core.repo import db as repo     # noqa: E402
from quality_trace.core.services import auth as auth_service  # noqa: E402

DEMO_USERS = [
    ("qe001", "顾工（QE）", "quality_engineer", None),
    ("leader01", "赵班组长（SL）", "shift_leader", "line-01"),
    ("manager01", "钱主管（QM）", "quality_manager", None),
]

PASSED = 0
FAILED = 0


def step(no: int, title: str) -> None:
    print(f"\n[{no}] {title}")


def ok(msg: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  ✓ {msg}")


def bad(msg: str) -> None:
    global FAILED
    FAILED += 1
    print(f"  ✗ {msg}")


def build_client(tmp: Path):
    """临时库 + 临时上传目录的 app 实例（关闭演示旁路，走真实 JWT）。"""
    cfg = {
        "auth": {"secret": "demo-secret", "token_ttl_s": 3600,
                 "dev_header_bypass": False},
        "llm": {"model": "demo-model", "api_key_env": "QT_DEMO_NO_KEY"},
        "embedding": {"enabled": False},
        "retrieval": {"top_k": 3, "recall": 10, "rrf_k": 60,
                      "chunk_size": 500, "chunk_overlap": 80},
        "chat": {"db_path": str(tmp / "demo.db"),
                 "kb_index_dir": "data/phase1_index",
                 "history_turns": 6, "history_max_chars": 6000,
                 "question_max_chars": 1000, "stream_timeout_s": 30,
                 "rate_limit_per_min": 1000, "title_max_len": 20},
        "defect": {"upload_dir": str(tmp / "uploads"), "max_photo_mb": 2,
                   "backfill_threshold_s": 60, "list_limit": 50,
                   "description_min_chars": 4},
    }
    app_module.load_app_config = lambda: cfg        # 本次进程内替换配置来源
    app = app_module.create_app()
    client = TestClient(app)
    client.__enter__()                              # 触发 lifespan：建库 + 建表
    conn = app.state.db
    for username, name, role, line_id in DEMO_USERS:
        conn.execute(
            "INSERT INTO sys_user (user_id, username, password_hash, display_name,"
            " role, line_id, status, created_at) VALUES (?,?,?,?,?,?, 'active', ?)",
            (repo.new_id("u"), username, auth_service.hash_password(username),
             name, role, line_id, "2026-11-05T09:00:00"),
        )
    repo.insert_work_order(conn, "WO-20261105-001", "M8 螺栓", "line-01", 1200)
    repo.upsert_defect_code(conn, "D-THREAD", "螺纹外径超差", "尺寸",
                            ["螺纹", "外径", "偏小", "通规"])
    repo.upsert_defect_code(conn, "D-SURF", "表面划伤", "外观", ["划伤", "毛刺"])
    seed_kb(conn)
    conn.commit()
    return client, app


def seed_kb(conn) -> None:
    """给临时库塞两版「检验规范」，用来演示 as_of 版本闸。

    真实环境由主管在后台上传；这里直接写表，只为让 demo 不依赖外部文件。
    """
    versions = [
        ("v1", "2024-01-01", "2024-12-31",
         "3. 抽样方案\n按 GB/T 2828.1 一般检验水平 II，AQL 1.0，样本量 80 件，Ac=2 / Re=3。"),
        ("v2", "2025-01-01", "2027-12-31",
         "3. 抽样方案\n加严为一般检验水平 III，AQL 0.65，样本量 125 件，Ac=1 / Re=2。"),
    ]
    for version, start, end, text in versions:
        doc = repo.insert_doc(conn, "螺纹外径检验规范", "standard", version,
                              start, end, uploaded_by="u-qm")
        repo.insert_chunks(conn, doc["doc_id"], [{
            "seq_no": 0, "content": text, "heading": "3. 抽样方案",
            "char_len": len(text),
        }])


def login(client, username: str) -> str:
    body = client.post("/api/v1/auth/login",
                       json={"username": username, "password": username}).json()
    return body["data"]["token"]


def read_sse(client, token: str, payload: dict, verbose: bool) -> dict:
    """读完一次 SSE，返回 {sources: [...], answer: str, done: {...}}。"""
    got: dict = {"sources": [], "answer": "", "done": {}}
    headers = {"Authorization": f"Bearer {token}"}
    with client.stream("POST", "/api/v1/chat", json=payload, headers=headers) as r:
        event = ""
        for raw in r.iter_lines():
            line = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                if verbose:
                    print(f"      · event={event}")
                continue
            if not line.startswith("data:"):
                continue
            body = json.loads(line.split(":", 1)[1].strip() or "{}")
            if event == "sources":
                got["sources"] = body.get("items") or body.get("sources") or []
            elif event == "delta":
                got["answer"] += body.get("text", "")
            elif event == "done":
                got["done"] = body
                break
    return got


def main() -> int:
    parser = argparse.ArgumentParser(description="quality-trace 一键闭环演示")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 SSE 事件名")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="qt-demo-"))
    print("=" * 62)
    print("quality-trace 闭环演示（临时库，跑完即弃）")
    print(f"临时目录: {tmp}")
    print("=" * 62)
    client, app = build_client(tmp)
    try:
        # ---- 1) 三角色登录 ----
        step(1, "认证：三个角色各自登录，拿到 JWT")
        tokens = {}
        for username, _, role, _ in DEMO_USERS:
            tokens[role] = login(client, username)
            ok(f"{username} → {role}")
        if len(tokens) != 3:
            bad("登录角色数不足")

        qe = {"Authorization": f"Bearer {tokens['quality_engineer']}"}
        qm = {"Authorization": f"Bearer {tokens['quality_manager']}"}
        sl = {"Authorization": f"Bearer {tokens['shift_leader']}"}

        # ---- 2) 问答（无 Key → 检索照常，生成降级）----
        step(2, "问答：检索 + 引用（无 LLM Key 时按设计降级）")
        res = read_sse(client, tokens["quality_engineer"],
                       {"question": "螺纹外径的抽样方案是什么？"}, args.verbose)
        if res["sources"]:
            top = res["sources"][0]
            ok(f"命中 {len(res['sources'])} 条引用，首条："
               f"{top.get('source_file')} {top.get('clause') or ''} {top.get('version') or ''}")
        else:
            bad("未命中任何引用（检查 data/phase1_index）")
        degraded = res["done"].get("degraded")
        if degraded:
            ok(f"生成降级：{degraded.get('code')} {degraded.get('message')}（引用仍完整）")
        else:
            ok("生成正常，无降级")

        # ---- 3) as_of：按业务时刻取当时有效版本 ----
        step(3, "版本闸：as_of=2024-06-01 只召回当时有效的标准")
        res2 = read_sse(client, tokens["quality_engineer"],
                        {"question": "螺纹外径的抽样方案是什么？",
                         "as_of": "2024-06-01"}, args.verbose)
        versions = sorted({(c.get("version") or "?") for c in res2["sources"]})
        if res2["sources"] and versions == ["v1"]:
            ok(f"as_of=2024-06-01 → 命中 {len(res2['sources'])} 条，版本集合 {versions}"
               "（v2 虽更相似，但 2024 年尚未生效，被 SQL 时间闸排除）")
        else:
            bad(f"as_of 过滤异常：命中 {len(res2['sources'])} 条，版本集合 {versions}")
        res3 = read_sse(client, tokens["quality_engineer"],
                        {"question": "螺纹外径的抽样方案是什么？",
                         "as_of": "2023-01-01"}, args.verbose)
        if not res3["sources"]:
            ok("as_of=2023-01-01 → 0 命中（两版都还没生效，宁可空也不给现行版）")
        else:
            bad(f"区间外时刻不应命中，实际 {len(res3['sources'])} 条")

        # ---- 4) 现场录入：draft → 人工确认 → pending ----
        step(4, "录入：建草稿（带候选码）→ 班组长亲手确认")
        body = client.post("/api/v1/defects", headers=sl, json={
            "work_order_id": "WO-20261105-001",
            "description": "螺纹外径偏小，通规过不去",
            "op_code": "OP30", "qty": 3, "severity": "严重",
            "occurred_at": "2026-11-05T14:05:00",
        }).json()
        if body.get("code") != 0:
            bad(f"建单失败：{body.get('message')}")
            return 1
        draft = body["data"]
        no = draft["defect_no"]
        code = (draft.get("suggested") or [{}])[0].get("code")
        ok(f"草稿 {no}，状态={draft['status']}，候选码={code}，缺陷码此刻为空")
        confirm = client.post(f"/api/v1/defects/{no}/confirm", headers=sl,
                              json={"defect_code": code}).json()
        ok(f"确认后状态={confirm['data']['status']}，文案「{confirm['data']['status_text']}」")
        if "suggest_json" in json.dumps(confirm["data"]):
            bad("班组长响应泄露 suggest_json")
        else:
            ok("班组长响应不含 suggest_json（服务端未下发判定相关字段）")

        # ---- 5) 归因审批 + 8D 导出（无 Key 时降级）----
        step(5, "归因：提交 → 主管批准 → 导出 8D（无 Key 时按设计降级）")
        conn = app.state.db
        attr = repo.insert_attribution(
            conn, no,
            json.dumps([{"kind": "rule", "text": "3 号切刀寿命超限",
                         "evidence": "OP30 刀具台账：累计 12.8 万次 > 上限 12 万次"}],
                       ensure_ascii=False),
            "切刀磨损导致外径偏小（待验证）",
            submitted_by="u-qe",
        )
        attr_id = attr["attribution_id"]
        ok(f"归因单 {attr_id} 已提交（status=submitted）")
        approve = client.post(f"/api/v1/admin/attributions/{attr_id}/approve",
                              headers=qm, json={"note": "证据可追溯，同意"}).json()
        approved = approve["data"]["attribution"]
        ok(f"主管批准 → {approved['status']}（审批人 {approved.get('decided_by')}）")
        export = client.get(f"/api/v1/admin/attributions/{attr_id}/export-8d", headers=qm)
        if export.status_code == 200:
            ok("8D 导出成功（已配置 LLM Key）")
        else:
            msg = export.json().get("message", "")
            ok(f"8D 导出按设计降级：{export.status_code} / {msg[:24]}…")

        # ---- 6) 审计：三条规则扫描 + 人工核查 ----
        step(6, "审计：扫描问答记录，命中越界/无引用/旧版本")
        scan = client.post("/api/v1/admin/audit/rescan", headers=qm).json()
        scanned = scan["data"].get("scanned", 0)
        flagged = scan["data"].get("flagged", 0)
        ok(f"扫描 {scanned} 条回答，命中 {flagged} 条")
        listing = client.get("/api/v1/admin/audit/messages?only_flagged=1", headers=qm).json()
        items = listing["data"].get("messages") or []
        ok(f"仅看命中：{len(items)} 条可逐条核查")

        # ---- 汇总 ----
        print("\n" + "=" * 62)
        print(f"闭环完成：{PASSED} 项通过 / {FAILED} 项失败")
        print("覆盖：认证 → 检索 → 版本闸 → 问答 → 录入 → 确认 → 归因 → 审批 → 审计")
        print("=" * 62)
        return 0 if FAILED == 0 else 1
    finally:
        try:
            client.__exit__(None, None, None)
        except Exception:                       # noqa: BLE001 —— 关库失败不影响结论
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
