# -*- coding: utf-8 -*-
"""灌入演示数据：三角色账号 + 简化工单 + 缺陷码字典。

用途：主管后台 / 班组长端 / 登录演示的前置数据。
幂等：重复执行不会产生重复行（用户按 username 去重，其余表按主键去重由调用方保证）。

用法（仓库根目录）：
    python scripts/seed_demo_data.py [--db data/trace.db]
"""

from __future__ import annotations

import argparse
import sys
import datetime as _dt
import random as _random
from pathlib import Path

# 让脚本能直接运行：把 src 加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 仓库根目录（定位 data/ 用）
REPO_ROOT = Path(__file__).resolve().parents[1]

from quality_trace.core.repo import db as repo          # noqa: E402
from quality_trace.core.services import auth as auth_svc  # noqa: E402

# ---- 三角色演示账号（密码即登录名，方便记忆；生产环境严禁如此）----
DEMO_USERS = [
    # username, 密码, 显示名, 角色, 产线
    ("qe001",        "qe001",        "顾工（质量工程师）", "quality_engineer", None),
    ("leader01",     "leader01",     "赵班组长（一线）",   "shift_leader",     "line-01"),
    ("manager01",    "manager01",    "钱主管（质量主管）", "quality_manager",  None),
]

# ---- 简化工单 / 缺陷码字典 ----
# 批次 2 起由 seed_defects() 灌入：工单供班组长建单校验，字典供归一化匹配。
DEMO_WORK_ORDERS = [
    # work_order_id, 产品, 产线, 计划数量
    ("WO-20261030-001", "六角螺栓 M10x40", "line-01", 5000),
    ("WO-20261030-002", "六角螺栓 M12x50", "line-02", 3000),
]

# ---- 缺陷码字典（分组 + 关键词回退匹配用，docs/stage3-design.md 5.3 节）----
DEMO_DEFECT_CODES = [
    # code, 名称, 分组, 关键词, 默认严重度, 排序
    ("D-THREAD",  "螺纹外径超差",   "尺寸缺陷", ["螺纹", "外径", "偏小", "偏大", "中径"], "一般", 10),
    ("D-LENGTH",  "长度超差",       "尺寸缺陷", ["长度", "偏长", "偏短"],               "一般", 11),
    ("D-SURF",    "表面划伤",       "外观缺陷", ["划伤", "划痕", "拉毛", "毛刺"],       "轻微", 20),
    ("D-CRACK",   "裂纹",           "结构缺陷", ["裂纹", "开裂", "崩牙", "断裂"],       "严重", 30),
    ("D-RUST",    "锈蚀",           "外观缺陷", ["锈", "锈蚀", "氧化", "发黑"],         "一般", 21),
    ("D-BENT",    "弯曲变形",       "结构缺陷", ["弯", "变形", "弯曲"],                 "一般", 31),
    ("D-OTHER",   "其他",           "其他",     [],                                     "一般", 99),
]


def seed_kb(conn) -> None:
    """把 data/knowledge/ 的示例语料登记进 kb_doc/kb_chunk（幂等：按名称+版本跳过）。

    版本元数据取自文件 front-matter（阶段 1 语料已带 version / effective_from 等），
    让主管后台列表一开就有数据；同时初始化默认提示词模板（仅当库中没有任何模板）。
    """
    from quality_trace.core.knowledge import ingest as kb_ingest
    from quality_trace.core.knowledge.prompts import SYSTEM_PROMPT
    from quality_trace.core.services import knowledge_admin as kb_admin

    knowledge_dir = REPO_ROOT / "data" / "knowledge"
    ret_cfg = {"retrieval": {"chunk_size": 500, "chunk_overlap": 80}}
    for path in sorted(knowledge_dir.glob("*.md")):
        meta, body = kb_ingest.parse_front_matter(
            path.read_text(encoding="utf-8"))
        version = str(meta.get("version") or "v1")
        doc_type = str(meta.get("doc_type") or "standard")
        eff_from = str(kb_ingest._jsonable(meta.get("effective_from") or "2026-01-01"))
        eff_to = str(kb_ingest._jsonable(meta.get("effective_to") or "2030-12-31"))
        # 幂等：同名同版本已登记则跳过（insert_doc 冲突时跳过）
        try:
            result = kb_admin.upload_document(
                conn, ret_cfg,
                filename=path.name, data=path.read_bytes(),
                doc_type=doc_type, version=version,
                effective_from=eff_from, effective_to=eff_to,
                uploaded_by="seed", index_dir=REPO_ROOT / "data" / "phase1_index",
                save_dir=REPO_ROOT / "data" / "uploads" / "kb",
                embedder=None,
            )
            print(f"[知识库] {path.name} {version}：{result['chunk_count']} 片已登记")
        except Exception as exc:  # noqa: BLE001 —— 重复登记跳过即可
            print(f"[知识库] {path.name} {version} 跳过（{getattr(exc, 'message', exc)}）")

    # 默认提示词模板：仅当库中没有任何 active 模板时灌入（不覆盖主管的修改）
    from quality_trace.core.repo import db as repo_mod
    if repo_mod.get_active_template(conn) is None:
        repo_mod.upsert_template(conn, "默认系统提示词", SYSTEM_PROMPT, "seed")
        print("[提示词] 已初始化默认系统提示词模板")


def seed_audit_demo(conn) -> None:
    """灌入批次 1c 演示数据：工单/缺陷/归因单 + 三类越界回答样本（幂等）。

    越界样本用真实消息+引用构造（三条规则各命中一条），主管审计页开箱即见；
    归因单给两张：一张待审（无 8D 缓存，演示 LLM 不可用时的导出报错路径），
    一张待审但已带 8D 缓存（演示导出直接下载路径）。
    """
    from quality_trace.core.services import audit as audit_service

    # 幂等闸门：归因单 0001 已存在 → 本节已灌过，只补扫不重建样本
    if repo.get_attribution(conn, "attr-20261030-0001") is not None:
        result = audit_service.rescan_all(conn)
        print(f"[审计样本] 已存在，仅补扫：{result['scanned']} 条，命中 {result['flagged']} 条")
        return

    qe = conn.execute(
        "SELECT * FROM sys_user WHERE username='qe001'").fetchone()
    qe_id = dict(qe)["user_id"] if qe else "qe-001"

    # ---- 工单 + 缺陷单（幂等：insert_work_order/insert_defect 自带去重）----
    repo.insert_work_order(conn, "WO-20261030-001", "六角螺栓 M10x40", "line-01", 5000)
    repo.insert_work_order(conn, "WO-20261030-002", "六角螺栓 M12x50", "line-02", 3000)
    repo.insert_defect(
        conn, "DF-20261030-001", "WO-20261030-001", "line-01",
        "螺纹外径偏小，通规不过", op_code="OP-30", qty=120,
        severity="一般", defect_code="D-THREAD",
        suggest_json='[{"code":"D-THREAD","name":"螺纹外径超差","confidence":0.9,"source":"keyword"}]',
        created_by=qe_id,
    )

    # ---- 归因单 1：待审、无 8D 缓存（导出走现场生成/降级报错路径）----
    repo.insert_attribution(
        conn, "DF-20261030-001",
        evidence_json=(
            '[{"type":"规则命中","text":"缺陷码 D-THREAD 命中历史案例「切刀磨损」规则",'
            ' "source":"rule:tool-wear-thread"},'
            '{"type":"统计线索","text":"OP-30 近 7 天该缺陷码占比 62%（基线 8%）",'
            ' "source":"stat:spc-daily"},'
            '{"type":"知识引用","text":"历史 8D 案例：切刀磨损导致螺纹外径偏小",'
            ' "source":"8D案例-切刀磨损螺纹外径偏小.md v1"}]'
        ),
        hypothesis_text=(
            "疑似根因（待验证）：OP-30 切刀达到磨损周期未及时更换，"
            "导致螺纹外径系统性偏小。建议核对 10 月 23 日刀具更换记录并抽检在制品。"
        ),
        submitted_by=qe_id, attribution_id="attr-20261030-0001",
    )
    # ---- 归因单 2：待审、带 8D 缓存（导出直接下载路径）----
    repo.insert_attribution(
        conn, "DF-20261030-001",
        evidence_json='[{"type":"规则命中","text":"同批次连续三日出现外观划伤",'
                      ' "source":"rule:batch-scratch"}]',
        hypothesis_text="疑似根因（待验证）：输送导轨毛刺划伤，待现场确认。",
        submitted_by=qe_id, attribution_id="attr-20261030-0002",
        draft_8d_md=(
            "# 8D 报告草稿（DF-20261030-001）\n\n"
            "> ⚠️ 本报告为系统草稿，需人工复核后方可进入正式 8D 流程。\n\n"
            "## D1 团队\n质量工程师、班组长、工艺员\n\n"
            "## D2 问题描述\n螺纹外径偏小（通规不过），批次 WO-20261030-001。\n\n"
            "## D3 临时围堵\n该批次全检隔离，可疑在制品复测。\n\n"
            "## D4 根因分析\n疑似根因（待验证）：切刀磨损。\n\n"
            "## D5 纠正措施\n更换切刀并重设补偿参数（待批准后执行）。\n\n"
            "## D6 措施验证\n换刀后首件 + 每 2 小时巡检。\n\n"
            "## D7 预防再发\n将刀具寿命写入 SOP 并接入预警。\n\n"
            "## D8 祝贺团队\n（人工补充）\n"
        ),
    )

    # ---- 三类越界回答样本（直接构造消息 + 引用，再跑审计标记）----
    def _demo_session(batch_no: str) -> str:
        """为样本消息造会话（带批次号，审计筛选可演示）。"""
        now = repo._now()
        sid = repo.new_id("sess")
        conn.execute(
            "INSERT INTO chat_session"
            " (session_id, user_id, role, title, status, message_count,"
            "  occurred_at, recorded_at, last_message_at, batch_no)"
            " VALUES (?,?,?,?, 'active', 0, ?,?,?, ?)",
            (sid, qe_id, "quality_engineer", "审计演示会话", now, now, now, batch_no),
        )
        conn.commit()
        return sid

    def _assistant(sid: str, content: str) -> str:
        mid = repo.save_message(conn, sid, "assistant", content)
        return mid

    # 样本 1：疑似定根因（规则一命中）
    sid1 = _demo_session("WO-20261030-001")
    mid1 = _assistant(sid1, "经分析，根因已确定为切刀磨损，请立即换刀。")
    # 样本 2：无引用作答（规则二命中：complete、非空、0 引用）
    sid2 = _demo_session("WO-20261030-002")
    _assistant(sid2, "这个批次建议直接判不合格，返工处理即可。")
    # 样本 3：引用废止版（先登记 v1 标准再下架，引用它）
    try:
        v1 = repo.insert_doc(
            conn, "检验标准-螺纹外径检验规范", "standard", "v0-已废止",
            "2024-01-01", "2025-12-31", uploaded_by="seed",
        )
        repo.set_doc_status(conn, v1["doc_id"], "archived", "seed")
    except repo.DuplicateDocVersion:
        pass
    sid3 = _demo_session("WO-20261030-001")
    mid3 = _assistant(sid3, "依据旧版标准，抽样方案为每批抽 8 件。【C1】")
    repo.save_citations(conn, mid3, [{
        "seq": 1, "source_file": "检验标准-螺纹外径检验规范",
        "doc_id": None, "clause": "3. 抽样", "version": "v0-已废止",
        "doc_type": "standard", "chunk_id": "3", "score": 0.9,
        "snippet": "每批抽 8 件……",
    }])

    # 跑一遍补扫给三条样本打标（幂等：重复执行结果一致）
    result = audit_service.rescan_all(conn)
    print(f"[审计样本] 补扫 {result['scanned']} 条回答，命中越界 {result['flagged']} 条")
    print("[归因单] attr-20261030-0001（无 8D 缓存）/ attr-20261030-0002（含 8D 缓存）均已就绪")


def seed_defects(conn) -> None:
    """灌入工单与缺陷码字典（幂等：主键冲突即更新描述字段）。

    班组长端一打开就需要字典渲染分组按钮；工单决定建单时能否通过产线校验。
    """
    for work_order_id, product, line_id, planned in DEMO_WORK_ORDERS:
        repo.insert_work_order(conn, work_order_id, product, line_id, planned)
    print(f"[工单] {len(DEMO_WORK_ORDERS)} 张（line-01/line-02 各一张）")

    for code, name, category, keywords, _severity, sort_no in DEMO_DEFECT_CODES:
        repo.upsert_defect_code(conn, code, name, category, keywords, sort_no)
    print(f"[缺陷码] {len(DEMO_DEFECT_CODES)} 个（分组+关键词已就绪）")


def main() -> None:
    parser = argparse.ArgumentParser(description="灌入三角色演示账号等种子数据")
    parser.add_argument("--db", default="data/trace.db", help="SQLite 库路径")
    parser.add_argument("--kb", action="store_true",
                        help="同时登记 data/knowledge/ 示例语料并初始化默认提示词模板")
    parser.add_argument("--audit", action="store_true",
                        help="同时灌入审计越界样本与归因单演示数据（批次 1c）")
    args = parser.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    conn = repo.connect(args.db)
    repo.init_schema(conn)

    # ---- 用户（幂等：按 username 去重）----
    for username, password, name, role, line_id in DEMO_USERS:
        row = repo.create_user(
            conn, username, auth_svc.hash_password(password), name, role, line_id
        )
        print(f"[用户] {row['username']} / {row['role']} / line={row['line_id']}")

    if args.kb:
        seed_kb(conn)
    if args.audit:
        seed_audit_demo(conn)
    seed_defects(conn)          # 字典与工单是两条前端的地基，始终灌入（幂等）
    seed_measurements(conn)     # 看板演示：计量型实测值（幂等）
    seed_defect_records(conn)   # 看板演示：跨日缺陷单（幂等）
    seed_lots(conn)             # 追溯演示：物料批与领料关系（幂等）

    print("种子数据完成。演示账号：qe001 / leader01 / manager01（密码同登录名）")
    conn.close()




# ---------------------------------------------------------------------------
# 阶段 4：看板演示数据（实测值 + 跨日缺陷记录）
#
# 为什么需要造这些：看板的每个图都要求"有跨日、有分布、有异常点"才看得出效果。
# 只灌当天的几条数据，控制图只有一个点、趋势图只有一根柱子，
# 演示时会被问"这图是不是坏了"。这里用**固定随机种子**造数据，
# 每次跑出来的图完全一样——答辩时截图与现场演示能对上。
# ---------------------------------------------------------------------------
def seed_measurements(conn, days: int = 14) -> None:
    """造计量型实测值：外径（5 件/子组，稳定）+ 壁厚（1 件/子组，含漂移）。"""
    if repo.count_measurements(conn) > 0:
        print("[实测值] 已存在，跳过（删表或换库可重新生成）")
        return

    rnd = _random.Random(20261106)      # 固定种子：图长得一样，便于截图对照
    today = _dt.date.today()
    total = 0
    for d in range(days - 1, -1, -1):
        day = (today - _dt.timedelta(days=d)).isoformat()
        for g in range(6):               # 每天 6 个子组（约每小时一次抽检）
            sg = (days - d - 1) * 6 + g + 1
            # 外径：均值 10.00，正常波动；最后 6 个子组加一点系统性偏移
            # （模拟刀具磨损：中心缓慢上移，X̄ 图的规则 2/6 会报出来）
            drift = 0.022 if sg > (days * 6 - 6) else 0.0
            for k in range(5):
                repo.insert_measurement(
                    conn, "外径", round(10.0 + drift + rnd.gauss(0, 0.020), 4),
                    "line-01", subgroup_no=sg, usl=10.08, lsl=9.92,
                    gauge="G-01", operator="qe001",
                    occurred_at=f"{day}T{8 + g:02d}:00:00",
                )
                total += 1
            # 壁厚：单件/子组，缓慢漂移（I-MR 图能看出来）
            # 壁厚：单件/子组，整体稳定，最后 5 个点开始下探（I-MR 图报警）
            step = -0.03 if sg > (days * 6 - 5) else 0.0
            repo.insert_measurement(
                conn, "壁厚", round(2.0 + step + rnd.gauss(0, 0.010), 4),
                "line-01", subgroup_no=sg, usl=2.06, lsl=1.94,
                gauge="G-02", operator="qe001",
                occurred_at=f"{day}T{8 + g:02d}:30:00",
            )
            total += 1
    print(f"[实测值] {total} 条（外径 5 件/子组 {days} 天，壁厚 1 件/子组）")


def seed_defect_records(conn, days: int = 14) -> None:
    """造跨日的缺陷单，让趋势/帕累托/星期效应有形状。

    幂等靠**固定编号**（DF-DEMO-0001…），不靠"表里有数据就跳过"。
    最早写成"有数据就跳过"，结果演示库里有 14 条当天录入的真实单，
    整个趋势图只有一根柱子，看板上写着"数据点不足 2 个"——幂等是对的，
    但把演示效果杀死了。判据应该是"我要的那批数据在不在"，不是"表空不空"。
    """

    rnd = _random.Random(20261107)
    today = _dt.date.today()
    plan = [
        ("D-THREAD", 0.45, "螺纹外径偏小"), ("D-SURF", 0.25, "表面划伤"),
        ("D-BURR", 0.15, "毛刺未清"), ("D-DIM", 0.10, "长度超差"),
        ("D-OTHER", 0.05, "其他外观缺陷"),
    ]
    n = 0
    for d in range(days - 1, -1, -1):
        day = (today - _dt.timedelta(days=d)).isoformat()
        # 每天一张工单：不良率的分母必须"每天都有"。
        # 只灌一张总工单的话，rate_series 只有一天有分母，趋势图上会出现
        # 一个孤零零的点，其余全是 None——这正是第一版踩的坑。
        repo.insert_work_order(
            conn, f"WO-DEMO-{days - d:02d}", "六角螺栓 M10x40", "line-01",
            rnd.choice([800, 1000, 1200, 1500]), occurred_at=day,
        )
        for i in range(rnd.randint(2, 6)):
            r = rnd.random()
            acc = 0.0
            code, desc = "D-OTHER", "其他外观缺陷"
            for c, w, text in plan:
                acc += w
                if r <= acc:
                    code, desc = c, text
                    break
            n += 1
            repo.insert_defect(
                conn, f"DF-DEMO-{n:04d}", f"WO-DEMO-{days - d:02d}", "line-01",
                description=desc, op_code=rnd.choice(["OP20", "OP30", "OP40"]),
                qty=rnd.randint(1, 4),
                severity=rnd.choice(["轻微", "一般", "一般", "严重"]),
                defect_code=code, status="pending", created_by="u-demo-sl",
                occurred_at=f"{day}T{rnd.randint(8, 17):02d}:{rnd.randint(0, 59):02d}:00",
            )
    print(f"[缺陷单] {n} 条（{days} 天，五种缺陷码，含严重单）")




def seed_lots(conn, days: int = 14) -> None:
    """造批次谱系：3 家供应商 × 每天 1 批钢料，挂到当天的工单上。

    固定批号（LOT-DEMO-xx）保证幂等；供应商刻意做成"一家明显更差"，
    这样记分卡上能看出排序，而不是三行一模一样的数字。
    """
    suppliers = [
        ("SUP-华东特钢", 0.55, 1.0),
        ("SUP-鑫达金属", 0.30, 2.2),      # 不良率明显偏高，用来演示"最差供应商"
        ("SUP-南方线材", 0.15, 0.6),
    ]
    rnd = _random.Random(20261108)
    today = _dt.date.today()
    n = 0
    for d in range(days - 1, -1, -1):
        day = (today - _dt.timedelta(days=d)).isoformat()
        wo = f"WO-DEMO-{days - d:02d}"
        r = rnd.random()
        acc = 0.0
        supplier, weight, _badness = suppliers[0]
        for name, w, badness in suppliers:
            acc += w
            if r <= acc:
                supplier = name
                break
        lot_id = f"LOT-DEMO-{days - d:02d}"
        repo.insert_material_lot(
            conn, lot_id, "MAT-45#", "45# 圆钢 Φ20",
            supplier=supplier, supplier_lot=f"S{rnd.randint(10000, 99999)}",
            qty=rnd.choice([2000, 3000, 5000]), unit="kg", occurred_at=day,
        )
        # 一批料通常喂不止一张工单（一炉钢够两个班用），所以往后多挂一天。
        # 只挂一张单会让"嫌疑排序"失去意义：每批料的样本都只有 1 单，
        # 不良率就是那张单的不良率，排出来的顺序纯属噪声。
        for offset in (0, 1):
            target = f"WO-DEMO-{days - d + offset:02d}"
            if repo.get_work_order(conn, target):
                repo.link_lot_to_work_order(
                    conn, lot_id, target, qty_used=rnd.choice([800, 1200, 1600]),
                    op_code="OP10", occurred_at=day,
                )
                n += 1
    print(f"[批次] {days} 批（3 家供应商，{n} 条领料关系）")


if __name__ == "__main__":
    main()
