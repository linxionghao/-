"""分析服务：把库里的事实，翻译成看板上的结论。

这一层只做三件事，且**只做这三件**：

    取数 → 选口径 → 装配结论

它不实现任何统计公式（都在 core/analytics 里），也不写 SQL（都在 repo 里），
更不负责渲染（在 web 里）。这样安排的好处是：口径可以单独评审——
"不良率的分母到底是工单计划数还是实际产出数"这种问题，答案只在这一个文件里，
不会在某个页面的 JS 里再出现第二份。

三个口径决定（写在这里，评审可以直接引用）：

1. **不良率分母用工单 `planned_qty`**。系统里没有"实际产出数"，
   用计划数会在超产时低估不良率。这是已知偏差，所以返回值里带
   `rate_basis` 字段，前端会把它显示在数字旁边（"按计划产量计"），
   而不是让看的人误以为是按实际产出算的。
2. **时间轴用 `occurred_at`（业务时间）**。补录的单据不会污染历史趋势——
   三个月前那次停线的不良，今天补录，仍归到三个月前那一天。
   想看"录入工作量"时改用 `recorded_at`，两个口径不混。
3. **空数据返回结构完整的空对象，不返回 None**。前端因此只有一套渲染逻辑，
   不会出现"某个字段是 null 导致整页白屏"。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional, Sequence

from ..analytics import capability as cap
from ..analytics import descriptive as desc
from ..analytics import pareto as par
from ..analytics import spc
from ..analytics import trend as tr
from ..repo import db as repo

DEFAULT_DAYS = 30

# 帕累托的可用维度（白名单）。为什么必须白名单：维度名会进 SQL 的分组逻辑，
# 如果直接拼字符串，一个 `dim=1;DROP` 就能把库带走。这里宁可多写一行校验。
PARETO_DIMS = {
    "defect_code": "缺陷类型",
    "line_id": "产线",
    "op_code": "工序",
    "severity": "严重度",
    "created_by": "录入人",
}

SEVERITY_ORDER = ["轻微", "一般", "严重"]


# ---------------------------------------------------------------------------
# 时间区间
# ---------------------------------------------------------------------------
def resolve_range(
    date_from: Optional[str] = None, date_to: Optional[str] = None, days: int = DEFAULT_DAYS
) -> Dict[str, str]:
    """把"最近 N 天"翻译成具体的起止日期。

    默认区间是 **含今天在内的 N 天**：days=30 时是 [今天-29, 今天]。
    常见错误是写成 [今天-30, 今天]，那样实际是 31 天，跨月报表会对不上。
    """
    today = _dt.date.today()
    if date_to:
        end = tr.parse_date(date_to) or today
    else:
        end = today
    if date_from:
        start = tr.parse_date(date_from) or (end - _dt.timedelta(days=days - 1))
    else:
        start = end - _dt.timedelta(days=days - 1)
    if start > end:
        start, end = end, start
    return {"date_from": start.isoformat(), "date_to": end.isoformat(), "days": (end - start).days + 1}


# ---------------------------------------------------------------------------
# 首页 KPI
# ---------------------------------------------------------------------------
def overview(
    conn,
    *,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = DEFAULT_DAYS,
) -> Dict[str, Any]:
    """质量总览：6 个 KPI + 趋势 + 帕累托 + 严重度分布。

    一次返回这么多东西，是为了让看板**只发一个请求**。分 6 个接口的话，
    首屏会出现"KPI 已经显示 120 件、下面图表还是空的"这种半成品状态，
    用户会以为系统卡了。
    """
    rng = resolve_range(date_from, date_to, days)
    kw = dict(line_id=line_id, date_from=rng["date_from"], date_to=rng["date_to"])

    kpi = repo.quality_kpi(conn, **kw)
    defects = repo.query_defects(conn, **kw)
    orders = repo.query_work_orders(conn, **kw)

    pieces = int(kpi.get("pieces") or 0)
    planned = int(kpi.get("planned_qty") or 0)

    rate_series = tr.rate_series(defects, orders, granularity="day")
    trend_report = tr.analyze(
        [{"occurred_at": d.get("occurred_at"), "value": d.get("qty") or 0} for d in defects],
        granularity="day",
    )

    by_code = _group(defects, "defect_code")
    pareto = par.build(
        [{"key": k or "未分类", "value": v} for k, v in by_code.items()],
        weight_label="不良件数",
        top_n=8,
    )

    severity = _severity_rows(defects)
    backfill = int(kpi.get("backfills") or 0)
    total_orders = int(kpi.get("orders") or 0)

    return {
        "range": rng,
        "line_id": line_id,
        "kpi": {
            "orders": total_orders,                       # 几单
            "pieces": pieces,                             # 几件（不良品数）
            "planned_qty": planned,                       # 计划产量（分母）
            "defect_rate": round(pieces / planned, 6) if planned else None,
            "ppm": round(pieces / planned * 1_000_000, 1) if planned else None,
            "rate_basis": "按工单计划产量计（系统未采集实际产出）",
            "backfill_ratio": round(backfill / total_orders, 4) if total_orders else 0.0,
            "severe": int(kpi.get("severe") or 0),
            "uncoded": int(kpi.get("uncoded") or 0),
            "drafts": int(kpi.get("drafts") or 0),
            "pendings": int(kpi.get("pendings") or 0),
        },
        "severity": severity,
        "pareto": pareto,
        "trend": {
            "daily": rate_series["buckets"],
            "overall_ppm": rate_series["overall_ppm"],
            "direction": trend_report["direction"],
            "significant": trend_report["significant"],
            "message": trend_report["message"],
        },
        "data_quality": _data_quality(kpi, defects),
        "verdict": _verdict(pieces, planned, severity, pareto),
    }


def pareto_by(
    conn,
    dim: str = "defect_code",
    *,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = DEFAULT_DAYS,
    top_n: int = 10,
    weight: str = "qty",
) -> Dict[str, Any]:
    """任意维度的帕累托。

    `weight=qty` 按件数、`weight=orders` 按单数。两者经常给出不同的排序：
    一单 200 件的外观划伤，按件数排第一，但按单数只算 1 单。
    处理优先级该看哪个，取决于你要解决的是"报废成本"还是"处理工作量"。
    """
    if dim not in PARETO_DIMS:
        raise ValueError("不支持的维度：%s（可选 %s）" % (dim, "、".join(PARETO_DIMS)))
    if weight not in ("qty", "orders"):
        raise ValueError("weight 只能是 qty 或 orders")

    rng = resolve_range(date_from, date_to, days)
    rows = repo.query_defects(
        conn, line_id=line_id, date_from=rng["date_from"], date_to=rng["date_to"]
    )

    agg: Dict[str, float] = {}
    for r in rows:
        key = r.get(dim) or "未分类"
        key = str(key)
        val = 1.0 if weight == "orders" else float(r.get("qty") or 0)
        agg[key] = agg.get(key, 0.0) + val

    label = PARETO_DIMS[dim] + ("（件数）" if weight == "qty" else "（单数）")
    result = par.build(
        [{"key": k, "value": v} for k, v in agg.items()], weight_label=label, top_n=top_n
    )
    result["dim"] = dim
    result["dim_label"] = PARETO_DIMS[dim]
    result["weight_field"] = weight
    result["range"] = rng
    return result


def compare_periods(
    conn,
    *,
    dim: str = "defect_code",
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = DEFAULT_DAYS,
) -> Dict[str, Any]:
    """本期 vs 上期（等长区间）的帕累托对比。

    上期区间是本期区间整体左移 `days` 天，保证两天数完全相等；
    用"上个月"做对比会在月份天数不等时产生 3% 的天然偏差，
    在趋势判断上正好是一个"看起来在变好"的假信号。
    """
    if dim not in PARETO_DIMS:
        raise ValueError("不支持的维度：%s" % dim)
    cur = resolve_range(date_from, date_to, days)
    end = tr.parse_date(cur["date_from"]) - _dt.timedelta(days=1)
    start = end - _dt.timedelta(days=cur["days"] - 1)

    rows_cur = repo.query_defects(
        conn, line_id=line_id, date_from=cur["date_from"], date_to=cur["date_to"]
    )
    rows_prev = repo.query_defects(
        conn, line_id=line_id, date_from=start.isoformat(), date_to=end.isoformat()
    )

    def to_items(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        agg: Dict[str, float] = {}
        for r in rows:
            k = str(r.get(dim) or "未分类")
            agg[k] = agg.get(k, 0.0) + float(r.get("qty") or 0)
        return [{"key": k, "value": v} for k, v in agg.items()]

    result = par.compare(to_items(rows_cur), to_items(rows_prev))
    result["dim"] = dim
    result["current_range"] = cur
    result["previous_range"] = {"date_from": start.isoformat(), "date_to": end.isoformat()}
    return result


# ---------------------------------------------------------------------------
# 控制图（计量型，来自 meas_record）
# ---------------------------------------------------------------------------
def control_chart(
    conn,
    *,
    item_code: str,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = DEFAULT_DAYS,
) -> Dict[str, Any]:
    """按检验项目出控制图。

    自动选图型：
    - 所有子组都只有 1 件 → I-MR 图（破坏性检验的常态）；
    - 子组大小一致且 ≥2 → X̄-R 图；
    - 子组大小不一致 → 不硬画，返回 `need_regroup` 提示，并给出各子组大小分布。
      这是因为 A2 系数随 n 变，混着画等于用同一把尺子量两种东西，
      算出来的控制限谁都不认。
    """
    rng = resolve_range(date_from, date_to, days)
    rows = repo.query_measurements(
        conn,
        item_code=item_code,
        line_id=line_id,
        date_from=rng["date_from"],
        date_to=rng["date_to"],
    )
    if not rows:
        return {
            "ok": False,
            "chart": None,
            "range": rng,
            "item_code": item_code,
            "message": "该区间没有「%s」的实测值，请先在录入端或导入接口补数据。" % item_code,
        }

    groups: Dict[int, List[float]] = {}
    for r in rows:
        groups.setdefault(int(r.get("subgroup_no") or 1), []).append(float(r["value"]))
    ordered = [groups[k] for k in sorted(groups)]
    sizes = sorted({len(g) for g in ordered})

    spec = _spec_of(rows)
    meta = {
        "ok": True,
        "range": rng,
        "item_code": item_code,
        "line_id": line_id,
        "unit": rows[0].get("unit") or "mm",
        "n_records": len(rows),
        "n_subgroups": len(ordered),
        "subgroup_sizes": sizes,
        "spec": spec,
    }

    if len(sizes) > 1:
        meta.update(
            {
                "chart": None,
                "need_regroup": True,
                "size_distribution": _size_distribution(ordered),
                "message": "子组大小不一致（%s），无法直接画一张图。请按子组大小分开统计，"
                           "或统一抽样方案后重新出图。" % "、".join(str(s) for s in sizes),
            }
        )
        return meta

    if sizes[0] == 1:
        chart = spc.individual_mr_chart([g[0] for g in ordered])
    elif 2 <= sizes[0] <= 10:
        chart = spc.xbar_r_chart(ordered)
    else:
        meta.update(
            {
                "chart": None,
                "message": "子组大小 %d 超出 X̄-R 图适用范围（2..10），建议改用 S 图或缩小样本。" % sizes[0],
            }
        )
        return meta

    meta.update(
        {
            "chart": chart,
            "capability": _capability_of(rows, chart, spec),
            "series_meta": _series_meta(rows, ordered),
        }
    )
    return meta


def _spec_of(rows: Sequence[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """取规格限。取**最新一条**的 usl/lsl，而不是 max/min。

    规格是会改的（工程变更 ECN），max(usl) 会把历史上宽松的旧规格带出来，
    让当前这批明明超差的数据看起来"还在规格内"。
    """
    latest = rows[-1]
    usl = latest.get("usl")
    lsl = latest.get("lsl")
    return {
        "usl": float(usl) if usl is not None else None,
        "lsl": float(lsl) if lsl is not None else None,
        "source": "最新一条实测值携带的规格（ECN 变更后应以新规格为准）",
    }


def _capability_of(
    rows: Sequence[Dict[str, Any]], chart: Dict[str, Any], spec: Dict[str, Optional[float]]
) -> Optional[Dict[str, Any]]:
    """有规格才做能力分析；没有就返回 None（而不是编一个 USL 出来）。"""
    usl, lsl = spec.get("usl"), spec.get("lsl")
    if usl is None or lsl is None:
        return None
    values = [float(r["value"]) for r in rows]
    controlled = not (chart.get("x", {}).get("out_of_control") or [])
    return cap.assess(
        values,
        usl=usl,
        lsl=lsl,
        sigma_within=chart.get("sigma_within"),
        controlled=controlled,
    )


def _series_meta(
    rows: Sequence[Dict[str, Any]], ordered: Sequence[Sequence[float]]
) -> Dict[str, Any]:
    """给图上每个点配一个"什么时候抽的"，让用户能把异常点和班次对上。"""
    labels = []
    for r in rows:
        labels.append(str(r.get("occurred_at") or "")[:16])
    # 每个子组取第一条的时间作为标签
    per_group: List[str] = []
    seen = set()
    for r in rows:
        g = int(r.get("subgroup_no") or 1)
        if g in seen:
            continue
        seen.add(g)
        per_group.append(str(r.get("occurred_at") or "")[:16])
    return {
        "labels": per_group,
        "first": labels[0] if labels else None,
        "last": labels[-1] if labels else None,
        "gauges": sorted({str(r.get("gauge")) for r in rows if r.get("gauge")}),
        "operators": sorted({str(r.get("operator")) for r in rows if r.get("operator")}),
    }


def _size_distribution(ordered: Sequence[Sequence[float]]) -> Dict[str, int]:
    dist: Dict[str, int] = {}
    for g in ordered:
        k = str(len(g))
        dist[k] = dist.get(k, 0) + 1
    return dist


def defect_p_chart(
    conn,
    *,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = DEFAULT_DAYS,
    granularity: str = "day",
) -> Dict[str, Any]:
    """不良率控制图（P 图）：按天/周的不良件数 ÷ 产量。

    这是"没有计量数据也能做 SPC"的路径：车间只报了不良数，也能判"不良率是否失控"。
    局限要说清：它只能发现"不良率整体抬升"，发现不了"尺寸在缓慢漂移"——
    后者必须有实测值。
    """
    rng = resolve_range(date_from, date_to, days)
    defects = repo.query_defects(
        conn, line_id=line_id, date_from=rng["date_from"], date_to=rng["date_to"]
    )
    orders = repo.query_work_orders(
        conn, line_id=line_id, date_from=rng["date_from"], date_to=rng["date_to"]
    )

    buckets: Dict[str, Dict[str, float]] = {}
    for d in defects:
        dt = tr.parse_date(d.get("occurred_at"))
        if not dt:
            continue
        k = tr.bucket_of(dt, granularity)
        buckets.setdefault(k, {"d": 0.0, "n": 0.0})
        buckets[k]["d"] += float(d.get("qty") or 0)
    for o in orders:
        dt = tr.parse_date(o.get("occurred_at"))
        if not dt:
            continue
        k = tr.bucket_of(dt, granularity)
        buckets.setdefault(k, {"d": 0.0, "n": 0.0})
        buckets[k]["n"] += float(o.get("planned_qty") or 0)

    keys = sorted(k for k in buckets if buckets[k]["n"] > 0)
    if not keys:
        return {"ok": False, "range": rng, "message": "该区间没有可用的产量数据，无法计算不良率。"}

    counts = [buckets[k]["d"] for k in keys]
    sizes = [buckets[k]["n"] for k in keys]
    chart = spc.p_chart(counts, sizes)
    chart["labels"] = keys
    chart["range"] = rng
    chart["ok"] = True
    return chart


# ---------------------------------------------------------------------------
# 趋势与星期效应
# ---------------------------------------------------------------------------
def trend_report(
    conn,
    *,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = DEFAULT_DAYS,
    granularity: str = "day",
) -> Dict[str, Any]:
    """不良件数的趋势体检：方向、斜率、显著性、移动平均。"""
    rng = resolve_range(date_from, date_to, days)
    rows = repo.query_defects(
        conn, line_id=line_id, date_from=rng["date_from"], date_to=rng["date_to"]
    )
    recs = [{"occurred_at": r.get("occurred_at"), "value": r.get("qty") or 0} for r in rows]
    result = tr.analyze(recs, granularity=granularity)
    result["range"] = rng
    result["granularity"] = granularity
    return result


def weekday_report(
    conn,
    *,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = DEFAULT_DAYS,
) -> Dict[str, Any]:
    """星期效应：哪一天最容易出问题。"""
    rng = resolve_range(date_from, date_to, days)
    rows = repo.query_defects(
        conn, line_id=line_id, date_from=rng["date_from"], date_to=rng["date_to"]
    )
    recs = [{"occurred_at": r.get("occurred_at"), "value": r.get("qty") or 0} for r in rows]
    result = tr.weekday_profile(recs)
    result["range"] = rng
    return result


# ---------------------------------------------------------------------------
# 仪表盘（一个接口给全页）
# ---------------------------------------------------------------------------
def dashboard(
    conn,
    *,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = DEFAULT_DAYS,
    item_code: Optional[str] = None,
) -> Dict[str, Any]:
    """看板首页数据：总览 + 产线帕累托 + 星期效应 +（可选）控制图。"""
    ov = overview(
        conn, line_id=line_id, date_from=date_from, date_to=date_to, days=days
    )
    by_line = pareto_by(
        conn, "line_id", date_from=date_from, date_to=date_to, days=days, top_n=6
    )
    weekday = weekday_report(
        conn, line_id=line_id, date_from=date_from, date_to=date_to, days=days
    )

    items = repo.list_measurement_items(conn)
    chart = None
    if item_code:
        chart = control_chart(
            conn,
            item_code=item_code,
            line_id=line_id,
            date_from=date_from,
            date_to=date_to,
            days=days,
        )

    return {
        "overview": ov,
        "by_line": by_line,
        "weekday": weekday,
        "items": items,
        "chart": chart,
        "lines": repo.distinct_lines(conn),
    }


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _group(rows: Sequence[Dict[str, Any]], field: str) -> Dict[str, float]:
    agg: Dict[str, float] = {}
    for r in rows:
        k = str(r.get(field) or "未分类")
        agg[k] = agg.get(k, 0.0) + float(r.get("qty") or 0)
    return agg


def _severity_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """严重度分布。固定输出三档（哪怕某档为 0），前端不用处理缺档。"""
    agg: Dict[str, int] = {s: 0 for s in SEVERITY_ORDER}
    for r in rows:
        s = str(r.get("severity") or "未标注")
        agg[s] = agg.get(s, 0) + 1
    total = sum(agg.values()) or 1
    return [
        {"severity": k, "count": v, "ratio": round(v / total, 4)}
        for k, v in sorted(agg.items(), key=lambda kv: -kv[1])
    ]


def _data_quality(kpi: Dict[str, Any], rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """数据质量自检。看板最有价值的一块：先看数据靠不靠谱，再看结论。

    三项指标：
    - 未确认单（draft）：还没选缺陷码，进不了任何分类分析；
    - 补录比例：过高说明现场在事后补单，时间戳的可信度下降；
    - 缺码单：已确认但 defect_code 为空（理论上不该出现，出现即 bug）。
    """
    orders = int(kpi.get("orders") or 0) or 1
    drafts = int(kpi.get("drafts") or 0)
    backfills = int(kpi.get("backfills") or 0)
    uncoded = int(kpi.get("uncoded") or 0)

    issues = []
    if drafts / orders > 0.2:
        issues.append("未确认单占比 %.0f%%，归一化流程可能被跳过" % (drafts / orders * 100))
    if backfills / orders > 0.3:
        issues.append("补录占比 %.0f%%，业务时间的可信度下降" % (backfills / orders * 100))
    if uncoded:
        issues.append("%d 单缺缺陷码，已确认却未分类" % uncoded)

    return {
        "draft_ratio": round(drafts / orders, 4),
        "backfill_ratio": round(backfills / orders, 4),
        "uncoded": uncoded,
        "issues": issues,
        "ok": not issues,
    }


def _verdict(
    pieces: int, planned: int, severity: Sequence[Dict[str, Any]], pareto: Dict[str, Any]
) -> str:
    """一句话结论。没有数据时也要说人话，不能返回空串让前端显示一片空白。"""
    if not pieces:
        return "所选区间没有不良记录。若产线确实在运行，请先确认录入端是否正常。"
    if not planned:
        return "有 %d 件不良，但缺少产量数据，无法计算不良率——请补录工单计划产量。" % pieces

    ppm = pieces / planned * 1_000_000
    level = "正常" if ppm < 5000 else ("偏高" if ppm < 20000 else "严重偏高")
    top = (pareto.get("key_items") or [{}])[0].get("key") if pareto.get("key_items") else None
    severe = next((s["count"] for s in severity if s["severity"] == "严重"), 0)

    tail = ""
    if top:
        tail = "主因是「%s」，建议先处理这一项。" % top
    if severe:
        tail += "其中 %d 单为严重不良，需按 8D 流程立项。" % severe
    return "不良率 %d PPM（%s）。%s" % (round(ppm), level, tail)
