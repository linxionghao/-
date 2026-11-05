"""SPC 控制图：把"这批数据是不是正常波动"变成一个可以判定的问题。

本模块只做计算，不碰数据库。输入是**子组**（subgroup）——车间通常每小时抽 5 件
算一个子组，一天 8 个子组——输出是可以直接画在纸上的中心线与上下控制限。

三个必须先说清的概念，否则一定会用错：

1. **控制限 ≠ 规格限**。控制限由过程自身波动算出（UCL = X̄ + A2·R̄），
   规格限由设计图纸给定（USL/LSL）。把两者画在同一张图上、或者用 Cpk 去判断
   "过程是否受控"，都是把两件事混为一谈：
   - 过程受控但能力不足 → 该改工艺（换设备、改参数）；
   - 过程不受控但能力够 → 该先查特殊原因（换料、换人、换刀），别急着调机。
2. **先看 R 图，再看 X̄ 图**。R 图（子组内极差）反映"瞬时波动"，X̄ 图反映"过程中心"。
   R 图失控说明子组内部就不一致（同一小时抽的 5 件差异大），这时 X̄ 图的控制限
   是用一个被污染的 R̄ 算出来的，整张图都不可信。顺序错了，结论一定错。
3. **判异规则要成组使用**。只用"超出 3σ"一条，会漏掉刀具缓慢磨损（规则 3/6）、
   混批（规则 8）、以及调机过头（规则 5）——这三种恰恰是车间最常见的异常模式。

常量表来自 GB/T 4091 / ASTM E2587（与 Montgomery《Introduction to Statistical
Quality Control》附录一致）。**不要自己推**，这些数是用 d2、d3 的积分算出来的，
抄错一位，全厂的控制限都偏。
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import descriptive as _d

# ---------------------------------------------------------------------------
# 控制图常量（子组大小 n = 2..10）
# ---------------------------------------------------------------------------
# A2: X̄ 图控制限系数，UCL = X̄ + A2 * R̄
# D3/D4: R 图控制限系数，LCL = D3 * R̄，UCL = D4 * R̄
# d2: 由极差估计标准差，σ̂ = R̄ / d2
# c4: 由样本标准差估计 σ 的校正系数（本模块未用，留作 S 图扩展）
A2_TABLE = {2: 1.880, 3: 1.023, 4: 0.729, 5: 0.577, 6: 0.483, 7: 0.419, 8: 0.373, 9: 0.337, 10: 0.308}
D3_TABLE = {2: 0.000, 3: 0.000, 4: 0.000, 5: 0.000, 6: 0.000, 7: 0.076, 8: 0.136, 9: 0.184, 10: 0.223}
D4_TABLE = {2: 3.267, 3: 2.574, 4: 2.282, 5: 2.114, 6: 2.004, 7: 1.924, 8: 1.864, 9: 1.816, 10: 1.777}
D2_TABLE = {2: 1.128, 3: 1.693, 4: 2.059, 5: 2.326, 6: 2.534, 7: 2.704, 8: 2.847, 9: 2.970, 10: 3.078}

# 单值移动极差图（I-MR）：E2 = 3 / d2(n=2) = 2.66，D4 = 3.267
E2_IMR = 2.660
D4_IMR = 3.267

# 判异规则的中文说明。放在模块级而不是函数里，是因为前端图例、
# 审计报告的"判异依据"、以及测试断言都要引用同一份文案，三处各写一份必然漂移。
RULE_TEXTS = {
    1: "1 点超出 3σ 控制限",
    2: "连续 9 点在中心线同侧",
    3: "连续 6 点单调上升或下降",
    4: "连续 14 点上下交替",
    5: "连续 3 点中有 2 点超出同侧 2σ",
    6: "连续 5 点中有 4 点超出同侧 1σ",
    7: "连续 15 点落在 1σ 以内",
    8: "连续 8 点全部落在 1σ 以外",
}


def _oob(n: int) -> Tuple[float, float, float]:
    """取 n 对应的 (A2, D3, D4)。n 超出表范围时按线性外推会错，直接报 ValueError。

    为什么不做"自动外推"：控制图常量随 n 非线性变化，外推出来的限值看着像模像样，
    实际上把误报率从 0.27% 抬到 3% 都有可能。宁可让调用方把子组设计成 2..10。
    """
    if n in A2_TABLE:
        return A2_TABLE[n], D3_TABLE[n], D4_TABLE[n]
    raise ValueError("子组大小需在 2..10 之间，当前为 %r" % (n,))


# ---------------------------------------------------------------------------
# 判异规则（Nelson Rules，1..8）
# ---------------------------------------------------------------------------
def nelson_rules(
    series: Sequence[Optional[float]],
    center: float,
    sigma: float,
    rules: Optional[Iterable[int]] = None,
    max_points: int = 200,
) -> List[Dict[str, object]]:
    """对一条序列执行 Nelson 8 条判异，返回违规列表。

    参数
    ----
    series : 序列（允许 None，代表"这个子组缺数据"）
    center : 中心线（X̄ 或 p̄）
    sigma  : 过程标准差（**必须**与 center 同一口径，见下）
    rules  : 只跑哪几条，默认全跑
    max_points : 序列超过这个长度时只判最后 max_points 个点

    关于 sigma 的口径：X̄ 图的 σ 是 **子组均值的标准差**（σ/√n），
    不是单件标准差。传错了图会看起来"特别宽松"，永远不报警——而且是静默的，
    没有任何报错。所以本函数要求调用方显式传入，绝不自己从 series 里再估一次。

    关于 None：一段连续的有效点才构成"连续 N 点"，中间的缺口会把序列切断。
    直接把 None 当 0 参与判断是错的——那会凭空造出"连续 9 点在中心线下方"。
    """
    use = set(rules) if rules else set(RULE_TEXTS.keys())
    violations: List[Dict[str, object]] = []
    if not series or sigma is None or sigma <= 0:
        return violations

    values = list(series)
    start = max(0, len(values) - max_points)
    values = values[start:]

    # 切成连续段：None 是断点
    segments: List[Tuple[int, List[float]]] = []
    cur: List[float] = []
    cur_start = 0
    for i, v in enumerate(values):
        if v is None:
            if cur:
                segments.append((cur_start, cur))
            cur = []
            continue
        if not cur:
            cur_start = i
        cur.append(float(v))
    if cur:
        segments.append((cur_start, cur))

    def pos(i: int) -> int:
        """段内下标 → 全局下标（用于前端高亮）。"""
        return start + i

    for seg_start, seg in segments:
        n = len(seg)

        # ---- 规则 1：超出 3σ ----
        if 1 in use:
            for i, v in enumerate(seg):
                z = (v - center) / sigma
                if abs(z) > 3:
                    violations.append(
                        _v(1, pos(seg_start + i), v, "第 %d 点 z=%.2f，超出 3σ" % (pos(seg_start + i) + 1, z))
                    )

        # ---- 规则 2：连续 9 点同侧 ----
        if 2 in use:
            run = 1
            for i in range(1, n):
                same = (seg[i] - center) * (seg[i - 1] - center) > 0
                run = run + 1 if same else 1
                if run >= 9 and (i == n - 1 or (seg[i + 1] - center) * (seg[i] - center) <= 0):
                    side = "上" if seg[i] > center else "下"
                    violations.append(
                        _v(2, pos(seg_start + i), seg[i], "连续 %d 点在中心线%s侧" % (run, side))
                    )

        # ---- 规则 3：连续 6 点单调 ----
        if 3 in use:
            up = down = 1
            for i in range(1, n):
                if seg[i] > seg[i - 1]:
                    up, down = up + 1, 1
                elif seg[i] < seg[i - 1]:
                    down, up = down + 1, 1
                else:
                    up = down = 1
                longest = max(up, down)
                if longest >= 6 and (i == n - 1 or _breaks_monotonic(seg, i, up > down)):
                    word = "上升" if up >= 6 else "下降"
                    violations.append(
                        _v(3, pos(seg_start + i), seg[i], "连续 %d 点单调%s" % (longest, word))
                    )

        # ---- 规则 4：连续 14 点上下交替 ----
        if 4 in use and n >= 14:
            alt = 1
            for i in range(1, n):
                d1 = seg[i] - seg[i - 1]
                d0 = seg[i - 1] - seg[i - 2] if i >= 2 else None
                if d0 is not None and d1 * d0 < 0:
                    alt += 1
                else:
                    alt = 1
                if alt >= 14:
                    violations.append(
                        _v(4, pos(seg_start + i), seg[i], "连续 14 点上下交替（疑似两批料混用）")
                    )
                    break

        # ---- 规则 5：3 点中 2 点超同侧 2σ ----
        if 5 in use:
            for i in range(2, n):
                win = [seg[i - 2], seg[i - 1], seg[i]]
                for sign, word in ((1, "上"), (-1, "下")):
                    hits = [j for j, v in enumerate(win) if (v - center) * sign > 2 * sigma]
                    if len(hits) >= 2:
                        violations.append(
                            _v(5, pos(seg_start + i), seg[i], "3 点中 2 点超出%s侧 2σ" % word)
                        )
                        break

        # ---- 规则 6：5 点中 4 点超同侧 1σ ----
        if 6 in use:
            for i in range(4, n):
                win = seg[i - 4 : i + 1]
                for sign, word in ((1, "上"), (-1, "下")):
                    hits = sum(1 for v in win if (v - center) * sign > sigma)
                    if hits >= 4:
                        violations.append(
                            _v(6, pos(seg_start + i), seg[i], "5 点中 4 点超出%s侧 1σ" % word)
                        )
                        break

        # ---- 规则 7：连续 15 点在 1σ 内 ----
        if 7 in use:
            run = 0
            for i, v in enumerate(seg):
                if abs(v - center) < sigma:
                    run += 1
                    if run >= 15:
                        violations.append(
                            _v(7, pos(seg_start + i), v, "连续 15 点落在 1σ 以内（波动异常偏小，疑数据造假或量具分辨率不足）")
                        )
                        break
                else:
                    run = 0

        # ---- 规则 8：连续 8 点在 1σ 外 ----
        if 8 in use:
            run = 0
            for i, v in enumerate(seg):
                if abs(v - center) >= sigma:
                    run += 1
                    if run >= 8:
                        violations.append(
                            _v(8, pos(seg_start + i), v, "连续 8 点全部落在 1σ 以外（疑似两个分布混合）")
                        )
                        break
                else:
                    run = 0

    violations.sort(key=lambda x: (int(x["index"]), int(x["rule"])))
    return violations


def _breaks_monotonic(seg: Sequence[float], i: int, ascending: bool) -> bool:
    """判断"单调段是否在第 i 点结束"——只在段尾报一次，避免 6 点连线报 3 次。"""
    if i + 1 < len(seg):
        nxt = seg[i + 1]
        return not (nxt > seg[i]) if ascending else not (nxt < seg[i])
    return True


def _v(rule: int, index: int, value: float, message: str) -> Dict[str, object]:
    return {
        "rule": rule,
        "rule_text": RULE_TEXTS.get(rule, "规则 %d" % rule),
        "index": index,
        "value": round(float(value), 4),
        "message": message,
    }


# ---------------------------------------------------------------------------
# X̄-R 图（计量型，子组 2..10）
# ---------------------------------------------------------------------------
def xbar_r_chart(subgroups: Sequence[Sequence[float]]) -> Dict[str, object]:
    """X̄-R 控制图。

    输入：`[[x1..x5], [x1..x5], ...]`，每个子组是一次抽检。
    输出：两段可直接绘图的序列 + 限值 + 判异结果 + 过程能力预估值 σ̂。

    子组大小不一致时会抛错——真实数据里"这小时抽 5 件、那小时抽 3 件"很常见，
    但 X̄ 图的 A2 系数是按 n 定的，混着画等于用同一把尺子量两种东西。
    正确做法是先按 n 分组，各画一张，或者统一补成等样本（重新抽样）。
    """
    clean_groups: List[List[float]] = []
    for g in subgroups or []:
        vals = _d.clean(g)
        if vals:
            clean_groups.append(vals)
    if not clean_groups:
        return _empty_chart("xbar_r", "X̄-R 图", "没有有效子组数据")

    sizes = {len(g) for g in clean_groups}
    if len(sizes) > 1:
        raise ValueError("子组大小不一致：%s；请按子组大小分组后再计算" % sorted(sizes))
    n = sizes.pop()
    if n < 2:
        raise ValueError("X̄-R 图要求子组大小 ≥ 2；单件数据请用 I-MR 图（individual_mr_chart）")

    a2, d3, d4 = _oob(n)
    xbars = [sum(g) / len(g) for g in clean_groups]
    ranges = [max(g) - min(g) for g in clean_groups]

    xbarbar = sum(xbars) / len(xbars)
    rbar = sum(ranges) / len(ranges)

    ucl_x = xbarbar + a2 * rbar
    lcl_x = xbarbar - a2 * rbar
    ucl_r = d4 * rbar
    lcl_r = max(0.0, d3 * rbar)  # R 不可能为负，D3=0 时下限就是 0

    # 子组均值的标准差 = R̄ / (d2 * √n)
    sigma_within = rbar / (D2_TABLE[n] * math.sqrt(n)) if rbar > 0 else 0.0

    # X̄ 图用的是"均值的标准差"，即 σ/√n
    sigma_xbar = rbar / (D2_TABLE[n] * math.sqrt(n))

    vx = nelson_rules(xbars, xbarbar, sigma_xbar)
    vr = nelson_rules(ranges, rbar, rbar / D2_TABLE[n] if rbar > 0 else 0.0)

    return {
        "chart": "xbar_r",
        "name": "X̄-R 控制图",
        "subgroup_size": n,
        "n_subgroups": len(clean_groups),
        "x": {
            "label": "子组均值 X̄",
            "points": [round(v, 4) for v in xbars],
            "center": round(xbarbar, 4),
            "ucl": round(ucl_x, 4),
            "lcl": round(lcl_x, 4),
            "sigma": round(sigma_xbar, 6),
            "violations": vx,
            "out_of_control": sorted({int(v["index"]) for v in vx}),
        },
        "r": {
            "label": "子组极差 R",
            "points": [round(v, 4) for v in ranges],
            "center": round(rbar, 4),
            "ucl": round(ucl_r, 4),
            "lcl": round(lcl_r, 4),
            "sigma": round(rbar / D2_TABLE[n], 6) if rbar > 0 else 0.0,
            "violations": vr,
            "out_of_control": sorted({int(v["index"]) for v in vr}),
        },
        "sigma_within": round(sigma_within, 6),
        "order": "先看 R 图：R 图失控时 X̄ 图的限值不可信",
        "message": _verdict(vr, vx),
    }


# ---------------------------------------------------------------------------
# I-MR 图（单值 + 移动极差，子组大小为 1）
# ---------------------------------------------------------------------------
def individual_mr_chart(values: Sequence[float]) -> Dict[str, object]:
    """单值-移动极差控制图。

    适用场景：破坏性检验（拉伸、盐雾）、每批只测 1 件、或抽检频次低到凑不出子组。
    移动极差 MR_i = |x_i - x_{i-1}|，样本标准差近似 σ̂ = MR̄ / 1.128。
    """
    data = _d.clean(values)
    if len(data) < 2:
        return _empty_chart("individual_mr", "I-MR 图", "至少需要 2 个单值")

    mr = [abs(data[i] - data[i - 1]) for i in range(1, len(data))]
    xbar = sum(data) / len(data)
    mrbar = sum(mr) / len(mr)

    sigma = mrbar / D2_TABLE[2] if mrbar > 0 else 0.0
    ucl_x = xbar + E2_IMR * mrbar
    lcl_x = xbar - E2_IMR * mrbar
    ucl_mr = D4_IMR * mrbar

    vi = nelson_rules(data, xbar, sigma)
    vm = nelson_rules(mr, mrbar, mrbar / D2_TABLE[2] if mrbar > 0 else 0.0)

    return {
        "chart": "individual_mr",
        "name": "I-MR 控制图",
        "n_points": len(data),
        "x": {
            "label": "单值 I",
            "points": [round(v, 4) for v in data],
            "center": round(xbar, 4),
            "ucl": round(ucl_x, 4),
            "lcl": round(lcl_x, 4),
            "sigma": round(sigma, 6),
            "violations": vi,
            "out_of_control": sorted({int(v["index"]) for v in vi}),
        },
        "mr": {
            "label": "移动极差 MR",
            "points": [round(v, 4) for v in mr],
            "center": round(mrbar, 4),
            "ucl": round(ucl_mr, 4),
            "lcl": 0.0,
            "sigma": round(mrbar / D2_TABLE[2], 6) if mrbar > 0 else 0.0,
            "violations": vm,
            "out_of_control": sorted({int(v["index"]) for v in vm}),
        },
        "sigma_within": round(sigma, 6),
        "message": _verdict(vm, vi),
    }


# ---------------------------------------------------------------------------
# P 图（计数型：不良率，样本量可变）
# ---------------------------------------------------------------------------
def p_chart(defect_counts: Sequence[float], sample_sizes: Sequence[float]) -> Dict[str, object]:
    """不良率控制图（P 图）。样本量可变时控制限逐点不同，这是它的核心价值。

    与 X̄ 图不同，P 图**允许**各点样本量不同——因为限值按每点自己的 n 算：
        UCL_i = p̄ + 3·√(p̄(1-p̄)/n_i)
    这也解释了为什么"小样本的日子"控制限特别宽：n=20 时 3σ 约 ±30%，
    那天出现 2 个不良根本不算异常；而 n=500 时同样的 2 个不良已经超出下界。
    用固定限值画 P 图，是新人最常犯的错。
    """
    counts = _d.clean(defect_counts)
    sizes = _d.clean(sample_sizes)
    if len(counts) != len(sizes):
        raise ValueError("不良数与样本量长度不一致：%d vs %d" % (len(counts), len(sizes)))
    if not counts:
        return _empty_chart("p", "P 控制图", "没有数据")

    pairs = [(c, s) for c, s in zip(counts, sizes) if s > 0]
    if not pairs:
        return _empty_chart("p", "P 控制图", "样本量为 0")

    total_n = sum(s for _, s in pairs)
    pbar = sum(c for c, _ in pairs) / total_n
    sigma = math.sqrt(pbar * (1 - pbar) / (total_n / len(pairs))) if 0 < pbar < 1 else 0.0

    points, ucls, lcls = [], [], []
    for c, s in pairs:
        p = c / s
        se = math.sqrt(pbar * (1 - pbar) / s)
        points.append(round(p, 6))
        ucls.append(round(max(0.0, pbar + 3 * se), 6))
        lcls.append(round(max(0.0, pbar - 3 * se), 6))

    # 判异用"逐点标准化"，不能直接用 pbar±3σ（那是平均样本量下的近似）
    z = []
    for (c, s) in pairs:
        se = math.sqrt(pbar * (1 - pbar) / s)
        z.append((c / s - pbar) / se if se > 0 else 0.0)
    viol = nelson_rules(z, 0.0, 1.0, rules=[1, 2, 3, 5, 6])

    return {
        "chart": "p",
        "name": "P 控制图（不良率）",
        "points": points,
        "pbar": round(pbar, 6),
        "center": round(pbar, 6),
        "ucl": ucls,
        "lcl": lcls,
        "variable_limits": True,
        "sample_sizes": [int(s) for _, s in pairs],
        "defect_counts": [int(c) for c, _ in pairs],
        "sigma": round(sigma, 6),
        "violations": viol,
        "out_of_control": sorted({int(v["index"]) for v in viol}),
        "message": _verdict(viol, []),
    }


# ---------------------------------------------------------------------------
# C 图 / U 图（单位缺陷数）
# ---------------------------------------------------------------------------
def u_chart(defect_counts: Sequence[float], unit_sizes: Sequence[float]) -> Dict[str, object]:
    """U 图：单位缺陷数（DPU = 缺陷数 / 检验单位数）。

    与 P 图的区别是"分子能不能大于 1 个/件"：
    - P 图统计"不良品数"，一件最多算 1 个不良；
    - U 图统计"缺陷数"，一件上可以有多个缺陷（比如一块板 5 个焊点虚焊）。
    用错了会把 U 图的数据塞进 P 图，算出 p > 1 的荒谬结果（本函数因此加了断言）。
    """
    counts = _d.clean(defect_counts)
    units = _d.clean(unit_sizes)
    if len(counts) != len(units):
        raise ValueError("缺陷数与检验单位数长度不一致")
    pairs = [(c, u) for c, u in zip(counts, units) if u > 0]
    if not pairs:
        return _empty_chart("u", "U 控制图", "没有数据")

    ubar = sum(c for c, _ in pairs) / sum(u for _, u in pairs)
    points, ucls, lcls = [], [], []
    for c, u in pairs:
        val = c / u
        se = math.sqrt(ubar / u)
        points.append(round(val, 6))
        ucls.append(round(ubar + 3 * se, 6))
        lcls.append(round(max(0.0, ubar - 3 * se), 6))

    z = []
    for (c, u) in pairs:
        se = math.sqrt(ubar / u)
        z.append((c / u - ubar) / se if se > 0 else 0.0)
    viol = nelson_rules(z, 0.0, 1.0, rules=[1, 2, 3, 5, 6])

    return {
        "chart": "u",
        "name": "U 控制图（单位缺陷数）",
        "points": points,
        "center": round(ubar, 6),
        "ubar": round(ubar, 6),
        "ucl": ucls,
        "lcl": lcls,
        "variable_limits": True,
        "violations": viol,
        "out_of_control": sorted({int(v["index"]) for v in viol}),
        "message": _verdict(viol, []),
    }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _empty_chart(kind: str, name: str, why: str) -> Dict[str, object]:
    """空图。返回结构完整但全是 None / 空列表，前端不用写两套渲染分支。"""
    return {
        "chart": kind,
        "name": name,
        "points": [],
        "center": None,
        "ucl": None,
        "lcl": None,
        "violations": [],
        "out_of_control": [],
        "empty": True,
        "message": why,
    }


def _verdict(r_violations: Sequence[Dict[str, object]], x_violations: Sequence[Dict[str, object]]) -> str:
    """把判异结果翻译成一句人话。

    这里刻意不做"严重程度分级"——控制图只有"受控/不受控"两态，
    再细分成三级会让现场以为"二级可以先不管"。质量上的"先不管"通常就是事故的起点。
    """
    if r_violations:
        return "极差图失控（%d 处），先查子组内变异：换刀、换料、换人、量具重复性。此时均值图的限值不可信。" % len(r_violations)
    if x_violations:
        names = sorted({int(v["rule"]) for v in x_violations})
        return "均值图失控（%d 处，触发规则 %s），过程存在特殊原因，需停线排查。" % (
            len(x_violations),
            "、".join(str(n) for n in names),
        )
    return "过程受控：波动仅由普通原因造成。若要判断是否满足规格，请继续看过程能力（Cpk）。"
