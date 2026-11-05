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
# 二、产线数据能做什么：一致性检查（而不是硬算 GRR）
# ---------------------------------------------------------------------------
# 这一段是整个模块最需要解释的设计决定。
#
# 直觉上"库里有几十万条实测值，为什么不直接算个 GRR"是合理的，
# 但 GRR 要分离的是三块变差：**零件之间 / 人之间 / 同一个人重复测同一件**。
# 产线数据里第三块根本不存在——每件产品只测一次就流走了。
# 于是"重复性"只能拿"同一工单、不同时间、不同件"的差异去顶替，
# 而那里面装的是过程漂移、换刀、换料、班次温度……
# 实测结果：这样算出来的 %GRR 会到 90% 以上，然后给出"测量系统不可接受"的
# 结论——这个结论是**算法制造的**，不是产线的真实状态。
#
# 所以这里不硬算。产线数据能可靠回答的是另一个问题：
#   "同一条产线、同一张工单，不同班次的人读出来的水平一致吗？"
# 这就是一致性检查：比较各人对同一批工单的均值，用组内标准差判断差异是否显著。
# 它不能替代 GRR，但能抓出"某个人总是偏高 0.01mm"这类真实问题，
# 而且不需要额外试验。要 GRR？按 10 件 × 3 人 × 3 次重新做，见 design_advice。


def build_cells(
    conn,
    *,
    item_code: str,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_readings: int = 3,
    limit: int = 50000,
) -> Dict[str, Any]:
    """把实测值按 (测量人, 工单) 归组成单元格，供一致性检查用。

    `min_readings` 是必要的门槛：一个人在同一张工单上只测了 1 件时，
    他的"均值"就是那一个数，和别人的均值比较毫无统计意义。
    """
    records = repo.query_measurements(
        conn, item_code=item_code, line_id=line_id,
        date_from=date_from, date_to=date_to, limit=limit,
    )
    if not records:
        return {"ok": False, "message": "该条件下没有实测值记录"}

    cells: Dict[tuple, List[float]] = {}
    for r in records:
        operator = (r.get("operator") or "").strip() or "未记录"
        work_order = (r.get("work_order_id") or "").strip() or "未关联工单"
        cells.setdefault((operator, work_order), []).append(float(r["value"]))

    grouped: Dict[str, Dict[str, List[float]]] = {}
    for (operator, work_order), values in cells.items():
        if len(values) < min_readings:
            continue
        grouped.setdefault(work_order, {})[operator] = values

    usable = {wo: ops for wo, ops in grouped.items() if len(ops) >= 2}
    if not usable:
        return {
            "ok": False,
            "message": ("没有可用数据：一致性检查需要同一张工单被至少 2 个人测过、"
                        "且每人不少于 %d 条读数（当前 %d 张工单、%d 条记录）"
                        % (min_readings, len(grouped), len(records))),
            "work_orders": len(grouped),
            "records": len(records),
        }
    return {"ok": True, "groups": usable, "records": len(records),
            "work_orders": len(usable), "skipped": len(grouped) - len(usable)}


def consistency_check(
    conn,
    *,
    item_code: str,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_readings: int = 3,
    z_limit: float = 3.0,
) -> Dict[str, Any]:
    """人员读数一致性检查：同一工单上，各人的均值差是否超过噪声水平。

    判据用的是"均值差的标准误"：

        se = s_pooled × √(1/n1 + 1/n2)
        z  = |x̄1 − x̄2| / se

    s_pooled 取各单元格**组内**标准差的合并值（不是全局标准差）——
    用全局标准差会把工单之间的真实差异算进噪声里，导致永远判"一致"。
    这是这类检查最常见的错误，也是它给出"一切正常"这种假安全结论的原因。
    """
    built = build_cells(conn, item_code=item_code, line_id=line_id,
                        date_from=date_from, date_to=date_to,
                        min_readings=min_readings)
    if not built.get("ok"):
        return built

    groups: Dict[str, Dict[str, List[float]]] = built["groups"]

    # 合并组内标准差：自由度加权
    num = 0.0
    den = 0
    for ops in groups.values():
        for values in ops.values():
            n = len(values)
            if n < 2:
                continue
            mean = sum(values) / n
            num += sum((v - mean) ** 2 for v in values)
            den += n - 1
    s_pooled = (num / den) ** 0.5 if den > 0 else 0.0
    if s_pooled <= 0:
        return {"ok": False, "message": "组内标准差为 0，无法判断（读数可能被四舍五入过度）"}

    details: List[Dict[str, Any]] = []
    flagged: List[Dict[str, Any]] = []
    for work_order, ops in sorted(groups.items()):
        means = {op: (sum(v) / len(v), len(v)) for op, v in ops.items()}
        worst = None
        names = sorted(means)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                ma, na = means[a]
                mb, nb = means[b]
                se = s_pooled * (1.0 / na + 1.0 / nb) ** 0.5
                z = abs(ma - mb) / se if se > 0 else 0.0
                if worst is None or z > worst["z"]:
                    worst = {"a": a, "b": b, "diff": ma - mb, "z": z}
        row = {
            "work_order": work_order,
            "operators": [{"operator": op, "n": means[op][1],
                           "mean": round(means[op][0], 6)} for op in names],
            "max_diff": round(worst["diff"], 6) if worst else 0.0,
            "pair": "%s vs %s" % (worst["a"], worst["b"]) if worst else "",
            "z": round(worst["z"], 2) if worst else 0.0,
            "flagged": bool(worst and worst["z"] > z_limit),
        }
        details.append(row)
        if row["flagged"]:
            flagged.append(row)

    # 每个人的整体偏移：与当期总均值比
    grand_num = 0.0
    grand_cnt = 0
    per_operator: Dict[str, List[float]] = {}
    for ops in groups.values():
        for op, values in ops.items():
            per_operator.setdefault(op, []).extend(values)
            grand_num += sum(values)
            grand_cnt += len(values)
    grand = grand_num / grand_cnt if grand_cnt else 0.0
    operator_bias = []
    for op, values in sorted(per_operator.items()):
        mean = sum(values) / len(values)
        se = s_pooled / (len(values) ** 0.5) if values else 0.0
        z = abs(mean - grand) / se if se > 0 else 0.0
        operator_bias.append({
            "operator": op, "n": len(values), "mean": round(mean, 6),
            "delta": round(mean - grand, 6), "z": round(z, 2),
            "flagged": z > z_limit,
        })

    bad_orders = len(flagged)
    bad_ops = [o for o in operator_bias if o["flagged"]]
    if bad_orders == 0 and not bad_ops:
        level = "accept"
        text = ("%d 张工单、%d 人的读数一致（组内 σ=%.4f，最大均值差 %.4f 未超 3σ）"
                % (len(details), len(per_operator), s_pooled,
                   max((d["max_diff"] for d in details), default=0.0)))
    elif bad_ops and bad_orders == 0:
        level = "conditional"
        text = ("%s 的整体读数系统性偏移（z>%.0f），但逐单比较未超限："
                "建议核对量具夹持方式或统一读数习惯"
                % ("、".join(o["operator"] for o in bad_ops), z_limit))
    else:
        level = "reject"
        text = ("%d / %d 张工单上出现人员读数显著差异（%s），"
                "需要复核对齐测量方法后再采信这批数据"
                % (bad_orders, len(details), "、".join(
                    "%s:%s" % (d["work_order"], d["pair"]) for d in flagged[:3])))

    return {
        "ok": True,
        "item_code": item_code,
        "records": built["records"],
        "work_orders": built["work_orders"],
        "operators": len(per_operator),
        "sigma_pooled": round(s_pooled, 6),
        "z_limit": z_limit,
        "details": details,
        "operator_bias": operator_bias,
        "flagged_orders": bad_orders,
        "verdict": {"level": level, "text": text},
        "caveat": ("这不是 GR&R。产线数据里每件只测一次，重复性与零件变差无法分离；"
                   "要正式的 %GRR 结论，请按 design_advice 给出的方案重做试验。"),
    }


def design_advice(conn, *, item_code: Optional[str] = None) -> Dict[str, Any]:
    """给出"要做一次正式 GR&R 该怎么安排"的建议。

    依据是**公差带**：%GRR 的分母决定结论，而现场常常拿过程变差当分母，
    结果同一个量具在宽公差产品上"合格"、在紧公差产品上"不合格"。
    这里用公差带推荐抽样数，并明确写出判定阈值。
    """
    items = repo.list_measurement_items(conn)
    target = None
    if item_code:
        for it in items:
            if it["item_code"] == item_code:
                target = it
                break
    spec = None
    records = repo.query_measurements(
        conn, item_code=item_code, limit=2000) if item_code else []
    for r in records:
        if r.get("usl") is not None and r.get("lsl") is not None:
            spec = (float(r["usl"]), float(r["lsl"]))
            break

    tolerance = (spec[0] - spec[1]) if spec else None
    return {
        "item_code": item_code or (target or {}).get("item_code"),
        "has_measurements": bool(items),
        "tolerance": tolerance,
        "plan": {
            "parts": 10,
            "appraisers": 3,
            "trials": 3,
            "note": ("10 件要覆盖过程的正常波动范围（不要挑 10 件几乎一样的），"
                     "3 人要是真正会测这个特性的人，3 次之间要重新装夹"),
        },
        "thresholds": {
            "accept": "< 10%：可用于控制图与能力分析",
            "conditional": "10% ~ 30%：仅用于非关键特性",
            "reject": "> 30%：先改量具/夹具",
            "ndc_min": 5,
        },
        "tolerance_note": (
            "公差带 %.3f；若按 %%Tolerance 判定，客户通常要求 GRR < 10%% 公差" % tolerance
            if tolerance else
            "该项目没有记录规格上下限，建议先在实测值里补 USL/LSL，否则只能按 %Study Variation 判定"
        ),
        "sample_size_tip": ("%GRR 的分母是总变差：同一量具在宽公差产品上更容易合格。"
                            "对外报数用 %Tolerance，对内改工艺看 %Study Variation。"),
    }


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
