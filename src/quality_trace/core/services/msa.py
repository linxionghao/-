"""MSA 服务层：把"一次测量系统研究"变成可留档、可复核、可追踪的东西。

【为什么要有服务层，而不是让路由直接调 analytics.msa】

纯计算层回答"这组数据的 %GRR 是多少"；服务层回答三个计算层管不了的问题：

1. **这次的结论要不要留档**。MSA 是 PPAP 的必备证据，结论必须带时点。
   服务层把原始读数与结果快照同时落库，半年后能解释"当时为什么签字"。
2. **数据从哪来**。现场不会按 10 零件 × 3 人 × 3 次去测，他们会问
   "我库里已经有 500 条实测值，能不能直接算"。`from_measurements`
   就是干这个的——它按 测量人/工单/子组 折叠成 MSA 的三层结构。
3. **哪些项目还没做 MSA**。这个问题计算层永远不会被问到，
   但它比任何一个 %GRR 数字都重要：没做过 MSA 的控制图，等于没校准的秤。

【data sufficiency 的判断放在服务层】

零件数不足 5 个时，AIAG 的 K3 表还能给出系数，算出来的 %GRR 也"看着正常"，
但它对零件间变差的估计极不可靠。所以服务层在零件数 < 5 时加一条 warning，
而不是拦下来——课程演示数据本来就少，拦掉就什么都看不到了，
但必须让人知道这个数的可信度。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Sequence

from quality_trace.core.analytics import msa as calc
from quality_trace.core.repo import db as repo
from quality_trace.core.services.errors import ApiError, ErrorCode

# 少于这个零件数时提示（AIAG 经典方案是 10 件）
MIN_PARTS_RECOMMENDED = 5


def _new_study_id() -> str:
    return "MSA-%s" % uuid.uuid4().hex[:12]


def _loads(text: Optional[str], default: Any) -> Any:
    """宽松解析 JSON 列：历史数据里可能是空串或半截 JSON。"""
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# 一、提交一次研究（手工录入的读数）
# ---------------------------------------------------------------------------
def create_study(
    conn,
    *,
    item_code: str,
    rows: Sequence[Dict[str, Any]],
    gauge: Optional[str] = None,
    line_id: Optional[str] = None,
    tolerance: Optional[float] = None,
    run_anova: bool = True,
    created_by: str = "",
    occurred_at: Optional[str] = None,
    source: str = "manual",
) -> Dict[str, Any]:
    """计算并保存一次 MSA 研究，返回 {study, report}。

    校验顺序刻意是"先算、再判、后存"：
    计算失败（读数不合法）时不写库，避免留下一堆 verdict=unknown 的空记录。
    """
    if not item_code or not str(item_code).strip():
        raise ApiError(ErrorCode.BAD_REQUEST, "检验项目不能为空")
    if not rows:
        raise ApiError(ErrorCode.BAD_REQUEST, "没有读数：MSA 至少需要 2 人 × 2 件 × 2 次")

    report = calc.report(rows, tolerance=tolerance, run_anova=run_anova)
    if not report.get("ok"):
        raise ApiError(ErrorCode.BAD_REQUEST,
                       report.get("message") or "数据不足，无法计算 GRR")

    # 样本量提示：算得出不等于可信
    design = (report.get("average_range") or {}).get("design") or {}
    warnings: List[str] = list((report.get("average_range") or {}).get("warnings") or [])
    if design.get("parts", 0) < MIN_PARTS_RECOMMENDED:
        warnings.append("零件数 %d 少于建议值 %d：零件间变差的估计不可靠，"
                        "%%GRR 仅供参考" % (design.get("parts", 0), MIN_PARTS_RECOMMENDED))

    verdict = report.get("verdict") or {}
    study_id = _new_study_id()
    record = repo.insert_msa_study(
        conn, study_id, str(item_code).strip(),
        json.dumps(design, ensure_ascii=False),
        json.dumps(list(rows), ensure_ascii=False),
        json.dumps(report, ensure_ascii=False),
        gauge=gauge, line_id=line_id, tolerance=tolerance,
        verdict=verdict.get("level", "unknown"),
        pct_grr=(report.get("average_range") or {}).get("percent", {}).get("grr"),
        ndc=(report.get("average_range") or {}) .get("ndc"),
        source=source, created_by=created_by, occurred_at=occurred_at,
    )
    return {"study": record, "report": report, "warnings": warnings}


# ---------------------------------------------------------------------------
# 二、从既有实测值折叠出 MSA 数据
# ---------------------------------------------------------------------------
def build_rows_from_measurements(
    conn,
    *,
    item_code: str,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    max_parts: int = 10,
    max_trials: int = 3,
) -> Dict[str, Any]:
    """把 meas_record 折叠成 MSA 的三层结构。

    折叠规则（有意为之，见下方注释）：

        评价人 = operator（测量人）——再现性就是"人之间"的差异，没有别人
        零件   = work_order_id —— 同一张工单的同一个项目视为"同一个零件"，
                 因为工艺条件一致；跨工单才有零件间变差可谈
        试测   = 该 (人, 工单) 下的第 1..n 条读数（按子组号、时间排序）

    **这套折叠是近似的**，必须让调用方知道：
    真实 MSA 要求"同一零件反复测"，而产线数据是"不同零件各测一次"。
    用产线数据算 GRR，量到的是"过程变差 + 测量变差"的混合体，
    通常会**高估** GRR。所以返回值里带 `approximate=True` 与一段说明，
    页面上也要原样显示——把近似当精确是这类功能最常见的误用。
    """
    records = repo.query_measurements(
        conn, item_code=item_code, line_id=line_id,
        date_from=date_from, date_to=date_to, limit=50000,
    )
    if not records:
        return {"ok": False, "message": "该条件下没有实测值记录"}

    # 按 (评价人, 零件) 分组，保持时间顺序
    cells: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for r in records:
        appraiser = (r.get("operator") or "未记录").strip() or "未记录"
        part = (r.get("work_order_id") or r.get("item_code") or "未关联")
        cells.setdefault(appraiser, {}).setdefault(str(part), []).append(r)

    appraisers = sorted(cells)
    parts = sorted({p for a in cells.values() for p in a})

    if len(appraisers) < 2:
        # 一个人测不出再现性：AV 会恒为 0，"人之间是否一致"这个最重要的问题没答案。
        # 这种情况必须明确拒绝，而不是给一个 %GRR 看着还行的假结论。
        return {"ok": False,
                "message": "只有 1 个测量人（%s）：无法评估再现性，请至少安排 2 人"
                           % "、".join(appraisers)}

    # 只保留"所有人都测过"的零件，否则数据不平衡，算法直接算不出
    common = [p for p in parts
              if all(p in cells.get(a, {}) for a in appraisers)]
    dropped = len(parts) - len(common)
    common = common[:max_parts]

    rows: List[Dict[str, Any]] = []
    for appraiser in appraisers:
        for part in common:
            items = sorted(cells[appraiser][part],
                           key=lambda x: (str(x.get("occurred_at") or ""),
                                          int(x.get("subgroup_no") or 0)))
            for trial, rec in enumerate(items[:max_trials], start=1):
                rows.append({"appraiser": appraiser, "part": part,
                             "trial": trial, "value": rec.get("value")})

    # 试测次数必须一致：取各单元格的最小条数，宁可少用数据也要平衡
    from collections import Counter

    per_cell = Counter((r["appraiser"], r["part"]) for r in rows)
    if per_cell:
        n = min(per_cell.values())
        rows = [r for r in rows if r["trial"] <= n]

    if not rows:
        return {"ok": False,
                "message": "无法折叠成平衡数据（每个测量人需要对同一批工单都有读数）"}

    notes = ["该数据来自产线实测值，不是规范的 MSA 试验设计：",
             "· 零件按工单归并，读到的是「过程变差 + 测量变差」的混合，"
             "通常会高估 %GRR；",
             "· 如需正式结论，请按 10 零件 × 3 人 × 3 次重新试验。"]
    if dropped:
        notes.append("· 有 %d 个零件不是所有人都测过，已剔除" % dropped)

    return {
        "ok": True,
        "approximate": True,
        "item_code": item_code,
        "rows": rows,
        "design": {"appraisers": len(appraisers), "parts": len(common),
                   "trials": min(per_cell.values()) if per_cell else 0},
        "appraisers": appraisers,
        "parts": common,
        "source_records": len(records),
        "notes": notes,
    }


def study_from_measurements(conn, *, item_code: str, created_by: str = "",
                            **kwargs) -> Dict[str, Any]:
    """一键：折叠 → 计算 → 留档。source 记为 measurements，便于区分证据强度。"""
    # tolerance 是"计算 GRR 时的分母"，不参与折叠，必须在这里摘出来，
    # 否则会被透传给 build_rows_from_measurements 并抛 TypeError
    tolerance = kwargs.pop("tolerance", None)
    built = build_rows_from_measurements(conn, item_code=item_code, **kwargs)
    if not built.get("ok"):
        raise ApiError(ErrorCode.BAD_REQUEST, built.get("message") or "无法生成数据")
    result = create_study(
        conn, item_code=item_code, rows=built["rows"],
        gauge=None, line_id=kwargs.get("line_id"),
        tolerance=tolerance, created_by=created_by,
        source="measurements",
    )
    result["derived_from"] = {
        "source_records": built["source_records"],
        "approximate": built["approximate"],
        "notes": built["notes"],
        "design": built["design"],
    }
    result["warnings"] = list(result.get("warnings") or []) + built["notes"]
    return result


# ---------------------------------------------------------------------------
# 三、查询
# ---------------------------------------------------------------------------
def list_studies(conn, *, item_code: Optional[str] = None,
                 verdict: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """研究记录列表：把 design_json 展开成可读字段，页面不用自己解析。"""
    rows = repo.list_msa_studies(conn, item_code=item_code, verdict=verdict, limit=limit)
    out = []
    for r in rows:
        design = _loads(r.get("design_json"), {})
        out.append({
            "study_id": r["study_id"],
            "item_code": r["item_code"],
            "gauge": r.get("gauge"),
            "line_id": r.get("line_id"),
            "tolerance": r.get("tolerance"),
            "verdict": r.get("verdict"),
            "pct_grr": r.get("pct_grr"),
            "ndc": r.get("ndc"),
            "source": r.get("source"),
            "created_by": r.get("created_by"),
            "occurred_at": r.get("occurred_at"),
            "design": design,
            "design_text": "%s人 × %s件 × %s次" % (
                design.get("appraisers", "?"), design.get("parts", "?"),
                design.get("trials", "?")),
        })
    return {"studies": out, "total": len(out)}


def get_study(conn, study_id: str, *, with_raw: bool = True) -> Dict[str, Any]:
    """单条研究详情：结果快照 + 可选原始读数（重算用）。"""
    row = repo.get_msa_study(conn, study_id)
    if not row:
        raise ApiError(ErrorCode.MSA_NOT_FOUND, "MSA 记录 %s 不存在" % study_id)
    out = {
        "study_id": row["study_id"],
        "item_code": row["item_code"],
        "gauge": row.get("gauge"),
        "line_id": row.get("line_id"),
        "tolerance": row.get("tolerance"),
        "verdict": row.get("verdict"),
        "pct_grr": row.get("pct_grr"),
        "ndc": row.get("ndc"),
        "source": row.get("source"),
        "created_by": row.get("created_by"),
        "occurred_at": row.get("occurred_at"),
        "recorded_at": row.get("recorded_at"),
        "design": _loads(row.get("design_json"), {}),
        "result": _loads(row.get("result_json"), {}),
    }
    if with_raw:
        out["rows"] = _loads(row.get("raw_json"), [])
    return out


def recompute(conn, study_id: str, *, run_anova: bool = True) -> Dict[str, Any]:
    """用留档的原始读数重算（换算法、或修了 bug 之后复核）。

    不覆盖留档结果：返回"新算的"与"当初记的"两份，让人自己看差异。
    直接覆盖会让审计链断掉——"为什么这个数变了"没人答得上来。
    """
    row = repo.get_msa_study(conn, study_id)
    if not row:
        raise ApiError(ErrorCode.MSA_NOT_FOUND, "MSA 记录 %s 不存在" % study_id)
    rows = _loads(row.get("raw_json"), [])
    if not rows:
        raise ApiError(ErrorCode.BAD_REQUEST, "该记录没有留档的原始读数，无法重算")
    fresh = calc.report(rows, tolerance=row.get("tolerance"), run_anova=run_anova)
    archived = _loads(row.get("result_json"), {})
    old_pct = ((archived.get("average_range") or {}).get("percent") or {}).get("grr")
    new_pct = ((fresh.get("average_range") or {}).get("percent") or {}).get("grr")
    delta = None if (old_pct is None or new_pct is None) else round(new_pct - old_pct, 2)
    return {
        "study_id": study_id,
        "fresh": fresh,
        "archived": archived,
        "delta_pct_grr": delta,
        "same_verdict": (fresh.get("verdict") or {}).get("level") == row.get("verdict"),
        "note": ("重算结果与留档一致" if delta is not None and abs(delta) < 0.01
                 else "重算与留档存在差异，请核对是否更换了算法或修过口径"),
    }


# ---------------------------------------------------------------------------
# 四、看板
# ---------------------------------------------------------------------------
def overview(conn, *, limit: int = 10) -> Dict[str, Any]:
    """MSA 概览：做了几次、结论分布、哪些项目还在"裸奔"。

    `coverage` 是最重要的一个数：有实测值的检验项目里，
    做过 MSA 的占比。它低的时候，看板上的 Cp/Cpk 全都不能当真——
    这句话在页面上必须显式出现（见 verdict_text）。
    """
    studies = repo.list_msa_studies(conn, limit=200)
    by_verdict: Dict[str, int] = {"accept": 0, "conditional": 0, "reject": 0,
                                  "unknown": 0}
    for s in studies:
        v = s.get("verdict") or "unknown"
        by_verdict[v] = by_verdict.get(v, 0) + 1

    items = repo.list_measurement_items(conn)
    item_codes = sorted({i["item_code"] for i in items})
    covered = [c for c in item_codes if repo.latest_msa_of_item(conn, c)]
    gaps = repo.items_without_msa(conn, limit=limit)
    coverage = (len(covered) / len(item_codes)) if item_codes else 0.0

    stale: List[Dict[str, Any]] = []
    for code in covered:
        last = repo.latest_msa_of_item(conn, code)
        if last and last.get("verdict") != "accept":
            stale.append({"item_code": code, "verdict": last.get("verdict"),
                          "pct_grr": last.get("pct_grr"),
                          "occurred_at": last.get("occurred_at")})

    if not studies:
        verdict_text = ("还没有任何 MSA 记录：当前控制图与过程能力结论都不具备统计依据。"
                        "建议先对关键特性做一次 GR&R。")
    elif coverage < 1.0:
        verdict_text = ("%d / %d 个检验项目做过 MSA。未做 MSA 的项目，"
                        "其 Cp/Cpk 只能当参考值看。" % (len(covered), len(item_codes)))
    elif stale:
        verdict_text = "全部项目都做过 MSA，但有 %d 个结论不是「可接受」，建议复评。" % len(stale)
    else:
        verdict_text = "全部检验项目的 MSA 结论均可接受（注意：MSA 建议每 6 个月复评）"

    return {
        "studies": len(studies),
        "by_verdict": by_verdict,
        "items": len(item_codes),
        "covered_items": len(covered),
        "coverage": round(coverage, 3),
        "gaps": gaps,
        "stale": stale,
        "recent": [
            {"study_id": s["study_id"], "item_code": s["item_code"],
             "verdict": s["verdict"], "pct_grr": s["pct_grr"], "ndc": s["ndc"],
             "occurred_at": s["occurred_at"]}
            for s in studies[:5]
        ],
        "verdict_text": verdict_text,
        "caveat": ("%GRR 衡量的是「尺子多准」，不衡量「产品多好」。"
                   "两者混谈会让结论都不成立——详见 docs/adr/0008。"),
    }
