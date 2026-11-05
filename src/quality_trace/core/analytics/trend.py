"""趋势分析：把散落的记录按时间桶聚合，回答"在变好还是变坏"。

三条设计原则，都是为了"结论可复核"：

1. **空桶要补 0，不能跳过**。10 月 1 日到 10 月 7 日只有 5 天有不良记录，
   如果只画有数据的天，曲线是平的；补 0 之后才看得出"中间两天停了线"。
   但"补 0"只对**计数型**指标成立（不良数、投诉数），对**比率型**（不良率）
   不成立——那天没生产，不良率不是 0，是"无数据"。所以本模块用 `fill` 参数
   显式控制，并且对缺失给出 None 而不是 0。
2. **周起点固定为周一**。ISO 8601 规定周一为一周第一天，质检周报按周日的
   自然周算会把周末两天的数据切到两周，导致"周末不良率高"的假象。
3. **比较必须同期**。10 月（31 天）和 9 月（30 天）直接比总量，天然多 3%；
   本模块提供 `per_day` 归一化，并在返回结构里同时给出总量与日均，让看图的人自己选。
"""

from __future__ import annotations

import datetime as _dt
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import descriptive as _d

GRANULARITY = ("day", "week", "month")


def parse_date(value: object) -> Optional[_dt.date]:
    """尽量宽容地把各种时间表示解析成 date。

    现场数据的时间格式五花八门：`2026-11-05`、`2026-11-05 08:12:30`、
    `2026/11/05`、ISO 8601 带时区 `2026-11-05T08:12:30+08:00`。
    统一在这里吃掉，绝不让上层各自 `split("T")`——那必然有一处漏掉带时区的写法。
    """
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("/", "-").replace("T", " ")
    if "+" in s:
        s = s.split("+")[0].strip()
    if s.endswith("Z"):
        s = s[:-1].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def bucket_of(d: _dt.date, granularity: str = "day") -> str:
    """把日期归到桶标签上。"""
    g = granularity if granularity in GRANULARITY else "day"
    if g == "day":
        return d.isoformat()
    if g == "week":
        monday = d - _dt.timedelta(days=d.weekday())
        return monday.isoformat()
    return "%04d-%02d" % (d.year, d.month)


def series(
    records: Sequence[Dict[str, object]],
    granularity: str = "day",
    date_field: str = "occurred_at",
    value_field: str = "value",
    fill: bool = True,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Dict[str, object]:
    """按时间桶聚合。

    返回：
        buckets:     [{bucket, value, count, missing}]  按时间升序
        total / mean_per_bucket / max_bucket / min_bucket
        start / end / granularity
        fill:        是否补了空桶

    `missing=True` 的桶表示"该时间段没有任何记录"：
    计数型指标补 0，比率型指标上层应据此显示断点（见模块头注释第 1 条）。
    """
    g = granularity if granularity in GRANULARITY else "day"
    agg: Dict[str, Dict[str, float]] = {}

    for r in records or []:
        d = parse_date(r.get(date_field))
        if d is None:
            continue
        b = bucket_of(d, g)
        slot = agg.setdefault(b, {"value": 0.0, "count": 0.0})
        slot["count"] += 1
        try:
            slot["value"] += float(r.get(value_field) or 0)
        except (TypeError, ValueError):
            slot["value"] += 0.0

    if not agg:
        return {
            "granularity": g,
            "buckets": [],
            "total": 0.0,
            "count": 0,
            "mean_per_bucket": None,
            "max_bucket": None,
            "min_bucket": None,
            "start": date_from,
            "end": date_to,
            "fill": fill,
            "empty": True,
        }

    keys = sorted(agg)
    if fill:
        keys = _fill_keys(keys, g, date_from, date_to)

    buckets = []
    for k in keys:
        slot = agg.get(k)
        buckets.append(
            {
                "bucket": k,
                "value": round(slot["value"], 4) if slot else 0.0,
                "count": int(slot["count"]) if slot else 0,
                "missing": slot is None,
            }
        )

    values = [b["value"] for b in buckets]
    peak = max(buckets, key=lambda b: b["value"])
    low = min(buckets, key=lambda b: b["value"])
    return {
        "granularity": g,
        "buckets": buckets,
        "total": round(sum(values), 4),
        "count": sum(b["count"] for b in buckets),
        "mean_per_bucket": round(sum(values) / len(values), 4) if values else None,
        "max_bucket": {"bucket": peak["bucket"], "value": peak["value"]},
        "min_bucket": {"bucket": low["bucket"], "value": low["value"]},
        "start": buckets[0]["bucket"],
        "end": buckets[-1]["bucket"],
        "fill": fill,
        "empty": False,
    }


def _fill_keys(keys: List[str], g: str, date_from: Optional[str], date_to: Optional[str]) -> List[str]:
    """把桶序列补齐（按天/周/月递增），中间不跳号。"""
    start_s = date_from or keys[0]
    end_s = date_to or keys[-1]
    start = parse_date(start_s) or parse_date(keys[0])
    end = parse_date(end_s) or parse_date(keys[-1])
    if not start or not end or end < start:
        return keys

    out: List[str] = []
    cur = start
    guard = 0
    while cur <= end and guard < 4000:  # 防止误传 1900 年导致死循环
        out.append(bucket_of(cur, g))
        if g == "day":
            cur = cur + _dt.timedelta(days=1)
        elif g == "week":
            cur = cur + _dt.timedelta(days=7)
        else:
            cur = _add_month(cur)
        guard += 1
    # 合并实际存在的、但不落在区间内的桶（用户传的 date_from 可能早于数据）
    merged = sorted(set(out) | set(keys))
    return merged


def _add_month(d: _dt.date, months: int = 1) -> _dt.date:
    y, m = d.year, d.month + months
    while m > 12:
        y += 1
        m -= 12
    day = min(d.day, _days_in_month(y, m))
    return _dt.date(y, m, day)


def _days_in_month(y: int, m: int) -> int:
    if m == 12:
        return 31
    return (_dt.date(y, m + 1, 1) - _dt.timedelta(days=1)).day


def rate_series(
    defects: Sequence[Dict[str, object]],
    production: Sequence[Dict[str, object]],
    granularity: str = "day",
    date_field: str = "occurred_at",
    defect_qty_field: str = "qty",
    produced_field: str = "planned_qty",
) -> Dict[str, object]:
    """不良率序列（PPM = 不良数 / 产量 × 10⁶）。

    分母为 0 的桶给 None 而不是 0：那天没生产，不良率不存在。
    画图时 None 会断线，正好提示"这里没有数据"，而不是把曲线拉到 0 造成"质量突好"的错觉。
    """
    num: Dict[str, float] = {}
    den: Dict[str, float] = {}

    for d in defects or []:
        dt = parse_date(d.get(date_field))
        if not dt:
            continue
        k = bucket_of(dt, granularity)
        try:
            num[k] = num.get(k, 0.0) + float(d.get(defect_qty_field) or 0)
        except (TypeError, ValueError):
            continue

    for p in production or []:
        dt = parse_date(p.get(date_field))
        if not dt:
            continue
        k = bucket_of(dt, granularity)
        try:
            den[k] = den.get(k, 0.0) + float(p.get(produced_field) or 0)
        except (TypeError, ValueError):
            continue

    keys = sorted(set(num) | set(den))
    buckets = []
    for k in keys:
        n = num.get(k, 0.0)
        d_ = den.get(k, 0.0)
        buckets.append(
            {
                "bucket": k,
                "defects": round(n, 4),
                "produced": round(d_, 4),
                "rate": round(n / d_, 6) if d_ > 0 else None,
                "ppm": round(n / d_ * 1_000_000, 1) if d_ > 0 else None,
            }
        )

    valid = [b for b in buckets if b["rate"] is not None]
    return {
        "granularity": granularity,
        "buckets": buckets,
        "total_defects": round(sum(num.values()), 4),
        "total_produced": round(sum(den.values()), 4),
        "overall_rate": round(sum(num.values()) / sum(den.values()), 6) if sum(den.values()) > 0 else None,
        "overall_ppm": round(sum(num.values()) / sum(den.values()) * 1_000_000, 1) if sum(den.values()) > 0 else None,
        "mean_rate": round(sum(b["rate"] for b in valid) / len(valid), 6) if valid else None,
        "empty": not buckets,
    }


def analyze(
    records: Sequence[Dict[str, object]],
    granularity: str = "day",
    date_field: str = "occurred_at",
    value_field: str = "value",
) -> Dict[str, object]:
    """趋势体检：方向 + 斜率 + 变化率 + 是否值得关注。

    判"值得关注"的标准不用 p 值，用工程口径：
    斜率显著（|t|>2）**且** 首尾变化超过 20%——否则一天波动 1 件也算"上升趋势"，
    报警会多到没人看。
    """
    s = series(records, granularity=granularity, date_field=date_field, value_field=value_field)
    values = [b["value"] for b in s["buckets"]]
    if len(values) < 2:
        return {
            "series": s,
            "direction": "insufficient",
            "slope": None,
            "change": {"delta": None, "pct": None, "direction": "unknown"},
            "significant": False,
            "message": "数据点不足 2 个，无法判断趋势。",
        }

    fit = _d.linear_fit(values)
    first, last = values[0], values[-1]
    change = _d.describe_change(last, first)

    significant = False
    slope_sig = bool(fit.get("perfect_fit")) or (
        fit["t_stat"] is not None and abs(fit["t_stat"]) > 2
    )
    if slope_sig:
        if change["pct"] is not None and abs(change["pct"]) > 0.2:
            significant = True
        elif change["pct"] is None and last > 0:
            significant = True

    direction = "flat"
    if fit["slope"] is not None:
        if fit["slope"] > 0:
            direction = "up"
        elif fit["slope"] < 0:
            direction = "down"

    if not significant:
        msg = "未发现显著趋势（斜率 t=%s，首尾变化 %s）。" % (
            "完美线性" if fit.get("perfect_fit") else (fit["t_stat"] if fit["t_stat"] is not None else "—"),
            "—" if change["pct"] is None else "%.1f%%" % (change["pct"] * 100),
        )
    elif direction == "up":
        msg = "呈显著上升趋势（每%s +%.3g，累计 +%.1f%%），需要排查新增的特殊原因。" % (
            _unit(granularity),
            fit["slope"],
            (change["pct"] or 0) * 100,
        )
    else:
        msg = "呈显著下降趋势（每%s %.3g，累计 %.1f%%），改善措施可能已生效。" % (
            _unit(granularity),
            fit["slope"],
            (change["pct"] or 0) * 100,
        )

    return {
        "series": s,
        "direction": direction,
        "slope": fit["slope"],
        "r2": fit["r2"],
        "t_stat": fit["t_stat"],
        "change": change,
        "significant": significant,
        "moving_average": _d.moving_average(values, 3),
        "ewma": _d.ewma(values, 0.3),
        "message": msg,
    }


def _unit(granularity: str) -> str:
    return {"day": "天", "week": "周", "month": "月"}.get(granularity, "个时间桶")


def weekday_profile(records: Sequence[Dict[str, object]], date_field: str = "occurred_at",
                    value_field: str = "value") -> Dict[str, object]:
    """按星期几聚合：找出"周一总是最多"这类周期性规律。

    用途很具体：如果周一显著高于其他天，多半是"周末停机后首件未确认"，
    对策是加一条"周一开机首件检验"的制度，而不是去追责某个人。
    """
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    agg = [0.0] * 7
    cnt = [0] * 7
    for r in records or []:
        d = parse_date(r.get(date_field))
        if not d:
            continue
        try:
            v = float(r.get(value_field) or 0)
        except (TypeError, ValueError):
            v = 0.0
        agg[d.weekday()] += v
        cnt[d.weekday()] += 1

    total = sum(agg)
    rows = [
        {
            "weekday": i,
            "name": names[i],
            "value": round(agg[i], 4),
            "count": cnt[i],
            "ratio": round(agg[i] / total, 6) if total else 0.0,
        }
        for i in range(7)
    ]
    peak = max(rows, key=lambda r: r["value"]) if rows else None
    return {
        "rows": rows,
        "total": round(total, 4),
        "peak": peak["name"] if peak else None,
        "message": ("%s 占比最高（%.1f%%），建议核查该日的作业条件（首件确认、交接班、设备状态）。"
                    % (peak["name"], peak["ratio"] * 100)) if peak and total else "无数据",
    }
