"""帕累托分析：把"问题很多"变成"先解决这三件"。

原理只有一句：绝大多数不良集中在少数几个原因上（80/20）。但落地时有三个坑，
本模块每个坑都做了处理：

1. **排序要按数量，但看板要按损失**。不良"个数"和"金额/工时损失"经常不是同一个排序：
   某个外观划伤一天 40 件，每件损失 2 元；某个尺寸超差一天 3 件，每件报废 800 元。
   按个数排，你会去抓划伤；按金额排，你该去抓尺寸。所以本模块的输入是"带权重的条目"，
   权重可以是数量、金额、工时，由调用方决定，并且必须显式传 `weight_label`。
2. **累计百分比要在"全部"上算，不能在 Top N 上算**。
   只取前 10 项算累计，第 10 项也才 60%，于是"没有找到 80% 的关键少数"——
   这是取样范围错误，不是数据问题。
3. **并列项不能用"序号断点"**。两个原因各 20 件并列第 3，累计到它刚好跨过 80%，
   如果按序号切，会把其中一个划进 A 类、另一个划进 B 类，看起来像"系统随机分组"。
   本模块按**累计比例断点**切，并列项一定同组。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence


def build(
    items: Sequence[Dict[str, object]],
    weight_label: str = "数量",
    key_field: str = "key",
    weight_field: str = "value",
    top_n: Optional[int] = None,
) -> Dict[str, object]:
    """构建帕累托表。

    items: [{"key": "尺寸超差", "value": 37, ...}, ...]，其余字段原样带回。
    top_n: 只展示前 N 项，但**累计比例仍按全量计算**（见坑 2），
           未进入 Top N 的部分合并为一条 "其他"，其占比照实给出。
    """
    rows: List[Dict[str, object]] = []
    for it in items or []:
        try:
            w = float(it.get(weight_field) or 0)
        except (TypeError, ValueError):
            w = 0.0
        if w <= 0:
            continue
        row = dict(it)
        row["_w"] = w
        rows.append(row)

    if not rows:
        return {
            "rows": [],
            "total": 0.0,
            "n_items": 0,
            "key_items": [],
            "weight_label": weight_label,
            "empty": True,
            "message": "没有可分析的数据",
        }

    rows.sort(key=lambda r: (-r["_w"], str(r.get(key_field))))
    total = sum(r["_w"] for r in rows)

    cum = 0.0
    out: List[Dict[str, object]] = []
    for idx, r in enumerate(rows, start=1):
        cum_before = cum
        cum += r["_w"]
        ratio = r["_w"] / total
        cum_ratio = cum / total
        out.append(
            {
                "rank": idx,
                "key": r.get(key_field),
                "value": round(r["_w"], 4),
                "ratio": round(ratio, 6),
                "cum_ratio": round(min(cum_ratio, 1.0), 6),
                "abc": _abc(cum_before / total),
                "raw": {k: v for k, v in r.items() if k != "_w"},
            }
        )

    # 并列项同组：扫一遍，把"累计刚好跨过断点"的那一项及其并列者统一归入前一类
    out = _fix_ties(out)

    key_items = [r for r in out if r["abc"] == "A"]
    shown = out
    if top_n and top_n > 0 and len(out) > top_n:
        head = out[:top_n]
        rest = out[top_n:]
        head.append(
            {
                "rank": None,
                "key": "其他（%d 项）" % len(rest),
                "value": round(sum(r["value"] for r in rest), 4),
                "ratio": round(sum(r["ratio"] for r in rest), 6),
                "cum_ratio": 1.0,
                "abc": "C",
                "raw": {},
            }
        )
        shown = head

    a_ratio = sum(r["ratio"] for r in key_items)
    return {
        "rows": shown,
        "total": round(total, 4),
        "total_items": len(out),
        "n_items": len(shown),
        "key_items": [{"key": r["key"], "value": r["value"], "ratio": r["ratio"]} for r in key_items],
        "key_count": len(key_items),
        "key_ratio": round(a_ratio, 6),
        "weight_label": weight_label,
        "empty": False,
        "message": _message(key_items, a_ratio, weight_label),
    }


def _abc(cum_before: float) -> str:
    """ABC 分类：看**累计之前**的位置，不是累计之后。

    这个细节决定了对错。假设只有一个原因，它占了 86%：
    - 错的口径（看累计之后）：86% > 80%，于是被划进 B 类——"关键少数"里一个都没有；
    - 对的口径（看累计之前）：累加前是 0%（< 80%），它是把累计推过 80% 的那一项，
      所以是 A 类。

    ABC 的本意是"哪些项目撑起了前 80%"，最后一个跨过 80% 线的项目**必然**属于 A，
    哪怕它一个就占了 99%。写成 `cum_after <= 0.8` 是最常见的实现错误，
    表现为"单因主导的场景下 A 类为空"，报表上无法解释。
    """
    if cum_before < 0.8 - 1e-9:
        return "A"
    if cum_before < 0.95 - 1e-9:
        return "B"
    return "C"


def _fix_ties(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """并列项归入同一 ABC 组。

    做法：找出每个断点处"跨过断点的那一项"，把它与它前面所有同值项统一成同一组。
    同值判定用 value 完全相等（帕累托的输入是计数，等值即并列，不需要容差）。
    """
    if not rows:
        return rows
    for i in range(1, len(rows)):
        if rows[i]["abc"] != rows[i - 1]["abc"] and rows[i]["value"] == rows[i - 1]["value"]:
            # 同值不同组 → 把从 i-1 往前所有同值的项，统一为"更严"的那一组
            target = rows[i - 1]["abc"]
            j = i
            while j < len(rows) and rows[j]["value"] == rows[i]["value"]:
                rows[j]["abc"] = target
                j += 1
    return rows


def _message(key_items: List[Dict[str, object]], a_ratio: float, label: str) -> str:
    if not key_items:
        return "没有达到 A 类的项目，问题分布较为均匀。"
    names = "、".join(str(r["key"]) for r in key_items[:3])
    more = "" if len(key_items) <= 3 else " 等 %d 项" % len(key_items)
    return "%d 个项目贡献了 %.1f%% 的%s：%s%s，应优先处理。" % (
        len(key_items),
        a_ratio * 100,
        label,
        names,
        more,
    )


def compare(
    current: Sequence[Dict[str, object]],
    previous: Sequence[Dict[str, object]],
    key_field: str = "key",
    weight_field: str = "value",
    top_n: int = 5,
) -> Dict[str, object]:
    """两期帕累托对比：谁变多了、谁变少了、结构有没有变化。

    为什么单独做这个：只看一期的帕累托，永远"这几项最大"；
    真正有价值的是"上个月排第 7 的这项，这个月升到第 2"——那是新问题的信号。
    """
    cur = _as_map(current, key_field, weight_field)
    prev = _as_map(previous, key_field, weight_field)
    keys = set(cur) | set(prev)

    rows = []
    for k in keys:
        c = cur.get(k, 0.0)
        p = prev.get(k, 0.0)
        delta = c - p
        pct = None if p == 0 else (delta / p)
        rows.append(
            {
                "key": k,
                "current": round(c, 4),
                "previous": round(p, 4),
                "delta": round(delta, 4),
                "pct": None if pct is None else round(pct, 4),
                "direction": "new" if p == 0 and c > 0 else ("gone" if c == 0 and p > 0 else ("up" if delta > 0 else ("down" if delta < 0 else "flat"))),
            }
        )

    rows.sort(key=lambda r: -abs(r["delta"]))
    worse = [r for r in rows if r["delta"] > 0][:top_n]
    better = [r for r in rows if r["delta"] < 0][:top_n]
    new_items = [r for r in rows if r["direction"] == "new"]

    return {
        "rows": rows,
        "worse": worse,
        "better": better,
        "new_items": new_items,
        "message": _compare_message(worse, new_items),
    }


def _as_map(items: Sequence[Dict[str, object]], key_field: str, weight_field: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for it in items or []:
        k = str(it.get(key_field) or "未分类")
        try:
            out[k] = out.get(k, 0.0) + float(it.get(weight_field) or 0)
        except (TypeError, ValueError):
            continue
    return out


def _compare_message(worse: List[Dict[str, object]], new_items: List[Dict[str, object]]) -> str:
    if new_items:
        return "出现 %d 个新问题类型（%s），优先确认是否为新增失效模式。" % (
            len(new_items),
            "、".join(str(r["key"]) for r in new_items[:3]),
        )
    if worse:
        return "增幅最大的是「%s」（+%g），建议追查该类型对应的工序与班次。" % (
            worse[0]["key"],
            worse[0]["delta"],
        )
    return "各类型数量均未上升，问题结构稳定。"
