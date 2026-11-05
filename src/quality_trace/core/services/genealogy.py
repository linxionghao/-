"""批次谱系追溯：从"这单不良"找到"哪批料"，再算"这批料还害了谁"。

追溯在质量体系里是硬要求（IATF 16949 8.5.2）。它的价值不在"能查"，而在三点：

1. **缩小范围**。不良发生后，如果只按时间圈范围，往往要圈一周的产量；
   按批次圈，可能只有 3 张工单。召回成本差一个数量级。
2. **定位责任**。一批料出问题，是供应商的锅还是本厂工艺的锅，
   决定了这笔钱谁出。所以反查必须能一路走到 `supplier_lot`。
3. **闭环**。查出可疑批次后，要能立刻"隔离"并回写状态，
   否则查完就完了，下个月同样的料还在用。

三个必须讲清的限制（写在返回值里，不靠文档提醒）：

- **共现不等于因果**。某批料用过的工单不良率高，可能只是因为它恰好赶上了
  那台不稳定的设备。本模块给出的是"嫌疑排序"，`caveat` 字段里写明这一点，
  不给出"就是这批料"的结论。
- **只能追到登记过的关系**。没扫码、没登记的领料，系统里根本不存在这条边。
  所以 `coverage` 字段会给出"有批次信息的工单占比"，低于 60% 时
  追溯结论不可用于召回决策。
- **反向追溯不到客户**。本系统只管到出厂前的工单，发货去向不在范围内。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Sequence

from ..analytics import trend as tr
from ..repo import db as repo

# 影响面分级：按"受影响工单数"与"受影响数量"双维度取较高者
# 阈值不是拍脑袋：3 张工单/500 件 是多数工厂"班组长能自己处理"的上限，
# 超过就要走主管审批，这是制度和代码对齐的地方。
IMPACT_LEVELS = [
    (10, 2000, "高", "建议立即隔离库存并通知客户，按 8D 立项"),
    (3, 500, "中", "建议冻结在制品，24 小时内完成批次甄别"),
    (0, 0, "低", "可继续生产，但需加严该批料的进料检验"),
]

CAVEAT = (
    "嫌疑排序基于「同一批次在各工单上的不良率」，属相关性证据，不等于因果。"
    "定责前需排除设备、班次、工艺参数等混杂因素。"
)


# ---------------------------------------------------------------------------
# 反查：不良单 → 工单 → 批次 → 供应商
# ---------------------------------------------------------------------------
def backward_trace(conn, defect_no: str) -> Dict[str, Any]:
    """从一条不良单反查到物料批与供应商。

    返回结构刻意保留"查不到"的中间态：如果工单没有批次登记，
    `lots` 为空且 `coverage.reason` 说明原因，而不是抛异常。
    现场最常见的追问是"为什么查不到"，答案必须能被系统直接说出来。
    """
    defect = repo.get_defect(conn, defect_no)
    if defect is None:
        return {"ok": False, "message": "不良单 %s 不存在" % defect_no}

    work_order_id = defect.get("work_order_id")
    order = repo.get_work_order(conn, work_order_id) if work_order_id else None
    lots = repo.list_lots_of_work_order(conn, work_order_id) if work_order_id else []

    suspects = suspect_lots(conn, work_order_id) if work_order_id else []

    return {
        "ok": True,
        "defect": {
            "defect_no": defect.get("defect_no"),
            "description": defect.get("description"),
            "qty": defect.get("qty"),
            "severity": defect.get("severity"),
            "defect_code": defect.get("defect_code"),
            "occurred_at": defect.get("occurred_at"),
            "line_id": defect.get("line_id"),
        },
        "work_order": order or {},
        "lots": lots,
        "suppliers": sorted({l["supplier"] for l in lots if l.get("supplier")}),
        "suspects": suspects,
        "coverage": _coverage(lots),
        "conclusion": _backward_conclusion(defect, lots, suspects),
        "caveat": CAVEAT,
    }


def _coverage(lots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """批次登记覆盖度：没有覆盖度，追溯结论就是空中楼阁。"""
    if not lots:
        return {
            "ok": False,
            "count": 0,
            "reason": "该工单没有物料批登记（未扫码或未启用批次管理），"
                      "无法反查到批次与供应商；请补录领料记录后再试。",
        }
    return {"ok": True, "count": len(lots), "reason": ""}


def _backward_conclusion(
    defect: Dict[str, Any],
    lots: Sequence[Dict[str, Any]],
    suspects: Sequence[Dict[str, Any]],
) -> str:
    if not lots:
        return "该不良单的生产工单没有批次登记，无法追溯物料来源。"
    top = suspects[0] if suspects else None
    head = "涉及 %d 个物料批（供应商 %d 家）。" % (
        len(lots),
        len({l.get("supplier") for l in lots if l.get("supplier")}),
    )
    if top and top.get("defect_rate") is not None:
        return head + "最可疑的是 %s（%s，该批共用于 %d 张工单，不良率 %.1f%%）。%s" % (
            top["lot_id"], top.get("material_name") or "未命名",
            top["orders"], (top["defect_rate"] or 0) * 100, CAVEAT,
        )
    return head + "各批次的不良率差异不显著，暂无明确嫌疑批次。"


# ---------------------------------------------------------------------------
# 嫌疑排序：哪批料最像元凶
# ---------------------------------------------------------------------------
def suspect_lots(conn, work_order_id: str, min_orders: int = 2) -> List[Dict[str, Any]]:
    """对一张工单用到的各批次，算"这批料整体表现如何"并排序。

    算法说明（面试/评审最容易被问的一段）：

    对每一批料，取**所有**用过它的工单，统计这些工单上的不良件数 ÷ 计划产量，
    得到该批次的"关联不良率"。然后按不良率降序。

    为什么不用"本工单的不良数"直接排：一张工单只吃一批料时，
    所有批次的不良数都是同一个数，排序毫无意义；必须把样本扩大到
    "这批料参与过的所有工单"，才有区分度。

    为什么要 `min_orders`：只出现过 1 次的批次，样本太小，
    一个巧合的不良就能让它排第一。默认要求至少 2 张工单才参与排序，
    不足的放在 `low_sample` 里单独展示（不是丢弃，而是降权）。
    """
    lots = repo.list_lots_of_work_order(conn, work_order_id)
    rows: List[Dict[str, Any]] = []
    low_sample: List[Dict[str, Any]] = []

    for lot in lots:
        orders = repo.list_work_orders_of_lot(conn, lot["lot_id"])
        order_ids = [o["work_order_id"] for o in orders]
        if not order_ids:
            continue

        produced = 0
        pieces = 0
        defect_orders = 0
        for o in orders:
            planned = int(o.get("planned_qty") or 0)
            produced += planned
            # 按工单精确取。这里不用 query_defects：
            # 它按时间过滤，会把"一张跨月工单"拆成两段，分母就不对了。
            rows_d = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(qty,0)),0) AS pieces, COUNT(*) AS n"
                " FROM defect_item WHERE work_order_id = ?",
                (o["work_order_id"],),
            ).fetchone()
            pieces += int(rows_d["pieces"] or 0)
            if int(rows_d["n"] or 0) > 0:
                defect_orders += 1

        item = {
            "lot_id": lot["lot_id"],
            "material_name": lot.get("material_name"),
            "material_code": lot.get("material_code"),
            "supplier": lot.get("supplier"),
            "supplier_lot": lot.get("supplier_lot"),
            "status": lot.get("status"),
            "orders": len(order_ids),
            "order_ids": order_ids,
            "produced": produced,
            "defect_pieces": pieces,
            "defect_orders": defect_orders,
            "defect_rate": round(pieces / produced, 6) if produced else None,
        }
        if len(order_ids) >= min_orders:
            rows.append(item)
        else:
            item["note"] = "仅用于 %d 张工单，样本不足，不参与排序" % len(order_ids)
            low_sample.append(item)

    rows.sort(key=lambda r: (-(r["defect_rate"] or 0), -r["defect_pieces"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows + low_sample


# ---------------------------------------------------------------------------
# 正查：这批料 → 哪些工单 / 多少不良 / 要不要召回
# ---------------------------------------------------------------------------
def forward_trace(conn, lot_id: str) -> Dict[str, Any]:
    """从一批料正向追：它进了哪些工单、这些工单出了多少不良。"""
    lot = repo.get_material_lot(conn, lot_id)
    if lot is None:
        return {"ok": False, "message": "批次 %s 不存在" % lot_id}

    orders = repo.list_work_orders_of_lot(conn, lot_id)
    detail: List[Dict[str, Any]] = []
    total_produced = 0
    total_pieces = 0

    for o in orders:
        wo = o["work_order_id"]
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(qty,0)),0) AS pieces,"
            " COALESCE(SUM(CASE WHEN severity='严重' THEN 1 ELSE 0 END),0) AS severe"
            " FROM defect_item WHERE work_order_id = ?",
            (wo,),
        ).fetchone()
        planned = int(o.get("planned_qty") or 0)
        pieces = int(row["pieces"] or 0)
        total_produced += planned
        total_pieces += pieces
        detail.append(
            {
                "work_order_id": wo,
                "product_name": o.get("product_name"),
                "line_id": o.get("line_id"),
                "planned_qty": planned,
                "qty_used": o.get("qty_used"),
                "op_code": o.get("op_code"),
                "wo_time": o.get("wo_time"),
                "defect_orders": int(row["n"] or 0),
                "defect_pieces": pieces,
                "severe": int(row["severe"] or 0),
                "defect_rate": round(pieces / planned, 6) if planned else None,
            }
        )

    impact = _impact_level(len(detail), total_produced, total_pieces)
    return {
        "ok": True,
        "lot": lot,
        "orders": detail,
        "summary": {
            "orders": len(detail),
            "produced": total_produced,
            "defect_pieces": total_pieces,
            "defect_rate": round(total_pieces / total_produced, 6) if total_produced else None,
            "severe": sum(d["severe"] for d in detail),
        },
        "impact": impact,
        "conclusion": _forward_conclusion(lot, detail, impact),
        "caveat": CAVEAT,
    }


def _impact_level(orders: int, produced: int, pieces: int) -> Dict[str, Any]:
    """影响面分级：工单数或产量任一超标即升级（取更严的那一档）。"""
    level = "低"
    advice = IMPACT_LEVELS[-1][3]
    for min_orders, min_qty, name, text in IMPACT_LEVELS:
        if orders >= min_orders >= 1 or produced >= min_qty >= 1:
            level, advice = name, text
            break
    return {
        "level": level,
        "advice": advice,
        "orders": orders,
        "produced": produced,
        "defect_pieces": pieces,
        "need_recall": level == "高",
    }


def _forward_conclusion(lot: Dict[str, Any], detail: Sequence[Dict[str, Any]], impact: Dict[str, Any]) -> str:
    if not detail:
        return "这批料还没有领用记录：可能仍在库存，也可能领料未登记。建议先盘点实物再判断。"
    rate = impact["defect_pieces"] / impact["produced"] if impact["produced"] else 0
    return (
        "批次 %s（%s，供应商 %s）已用于 %d 张工单、%d 件产量，涉及不良 %d 件（%.2f%%）。"
        "影响面判定为「%s」：%s"
        % (
            lot["lot_id"], lot.get("material_name") or "未命名", lot.get("supplier") or "未知",
            impact["orders"], impact["produced"], impact["defect_pieces"], rate * 100,
            impact["level"], impact["advice"],
        )
    )


# ---------------------------------------------------------------------------
# 供应商记分卡
# ---------------------------------------------------------------------------
def supplier_scorecard(
    conn,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
) -> Dict[str, Any]:
    """按供应商统计：供了多少批、进了多少工单、带来多少不良。

    口径提醒：不良不能直接归给供应商。这里的 `ppm` 是"用了该供应商物料的工单
    上的不良率"，包含本厂工艺因素。所以输出里同时给 `lots` 与 `orders`，
    让看的人能判断样本量——1 批料 1 张工单的 20% 不良率没有统计意义。
    """
    today = _dt.date.today()
    end = tr.parse_date(date_to) or today
    start = tr.parse_date(date_from) or (end - _dt.timedelta(days=days - 1))

    rows = conn.execute(
        "SELECT l.supplier AS supplier, COUNT(DISTINCT l.lot_id) AS lots,"
        " COUNT(DISTINCT c.work_order_id) AS orders,"
        " COALESCE(SUM(COALESCE(w.planned_qty,0)),0) AS produced"
        " FROM material_lot l"
        " LEFT JOIN lot_consumption c ON c.lot_id = l.lot_id"
        " LEFT JOIN work_order w ON w.work_order_id = c.work_order_id"
        " WHERE l.occurred_at >= ? AND l.occurred_at <= ?"
        " GROUP BY l.supplier ORDER BY lots DESC",
        (start.isoformat(), end.isoformat() + "~"),
    ).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        supplier = r["supplier"] or "未标注"
        pieces_row = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(d.qty,0)),0) AS pieces FROM defect_item d"
            " WHERE d.work_order_id IN ("
            "   SELECT c.work_order_id FROM lot_consumption c"
            "   JOIN material_lot l ON l.lot_id = c.lot_id WHERE l.supplier = ?)",
            (r["supplier"],),
        ).fetchone()
        pieces = int(pieces_row["pieces"] or 0)
        produced = int(r["produced"] or 0)
        out.append(
            {
                "supplier": supplier,
                "lots": int(r["lots"] or 0),
                "orders": int(r["orders"] or 0),
                "produced": produced,
                "defect_pieces": pieces,
                "ppm": round(pieces / produced * 1_000_000, 1) if produced else None,
                "small_sample": int(r["orders"] or 0) < 3,
            }
        )

    out.sort(key=lambda x: (-(x["ppm"] or 0), -x["lots"]))
    return {
        "rows": out,
        "range": {"date_from": start.isoformat(), "date_to": end.isoformat()},
        "note": "PPM 为「使用该供应商物料的工单」上的不良率，含本厂工艺因素，不能直接作为索赔依据。",
        "empty": not out,
    }


# ---------------------------------------------------------------------------
# 谱系图（前端画图用）
# ---------------------------------------------------------------------------
def build_graph(conn, lot_id: str, max_orders: int = 20) -> Dict[str, Any]:
    """生成"批 → 工单 → 不良"的节点/边结构。

    为什么在后端拼图、而不是让前端自己跑三次查询：
    三次查询之间的过滤条件必须一致（同样取最近 N 张工单），
    放在前端就会出现"节点 5 个、边 7 条"的不闭合情况，
    而这种图看起来"挺像回事"，没人会发现它是错的。
    """
    lot = repo.get_material_lot(conn, lot_id)
    if lot is None:
        return {"ok": False, "nodes": [], "edges": [], "message": "批次不存在"}

    orders = repo.list_work_orders_of_lot(conn, lot_id)[:max_orders]
    nodes: List[Dict[str, Any]] = [
        {"id": lot_id, "type": "lot", "label": "%s\n%s" % (lot["lot_id"], lot.get("material_name") or ""),
         "meta": {"supplier": lot.get("supplier"), "qty": lot.get("qty"), "unit": lot.get("unit")}}
    ]
    edges: List[Dict[str, Any]] = []

    for o in orders:
        wo = o["work_order_id"]
        nodes.append(
            {"id": wo, "type": "order", "label": wo,
             "meta": {"product": o.get("product_name"), "line": o.get("line_id"),
                      "planned": o.get("planned_qty")}}
        )
        edges.append({"source": lot_id, "target": wo, "label": "投入 %s" % (o.get("qty_used") or 0)})

        defects = conn.execute(
            "SELECT defect_no, defect_code, qty, severity FROM defect_item"
            " WHERE work_order_id = ? ORDER BY qty DESC LIMIT 6",
            (wo,),
        ).fetchall()
        for d in defects:
            nodes.append(
                {"id": d["defect_no"], "type": "defect",
                 "label": d["defect_code"] or d["defect_no"],
                 "meta": {"qty": d["qty"], "severity": d["severity"]}}
            )
            edges.append({"source": wo, "target": d["defect_no"], "label": "%s 件" % d["qty"]})

    return {
        "ok": True,
        "nodes": nodes,
        "edges": edges,
        "stats": {"lots": 1, "orders": len(orders), "defects": len(nodes) - 1 - len(orders)},
    }


def overview(conn, *, days: int = 30) -> Dict[str, Any]:
    """追溯模块首页：批次数、覆盖度、供应商数、可疑批次 Top3。"""
    lots = repo.list_material_lots(conn, limit=500)
    scored = supplier_scorecard(conn, days=days)
    total_orders = conn.execute("SELECT COUNT(*) AS n FROM work_order").fetchone()
    linked_orders = conn.execute(
        "SELECT COUNT(DISTINCT work_order_id) AS n FROM lot_consumption"
    ).fetchone()
    n_orders = int(total_orders["n"] or 0) if total_orders else 0
    n_linked = int(linked_orders["n"] or 0) if linked_orders else 0

    return {
        "lots": len(lots),
        "suppliers": len(repo.list_suppliers(conn)),
        "orders": n_orders,
        "orders_with_lot": n_linked,
        "coverage": round(n_linked / n_orders, 4) if n_orders else 0.0,
        "coverage_note": (
            "批次覆盖率 = 有领料登记的工单 / 全部工单。低于 60% 时，"
            "追溯结论不能用于召回决策。"
        ),
        "top_suppliers": scored["rows"][:3],
        "recent_lots": lots[:8],
        "empty": not lots,
    }
