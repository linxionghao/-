"""MSA 测量系统分析：先证明尺子可靠，再谈产品合不合格。

【为什么这个模块必须有】

控制图上出现一个超出控制限的点时，车间第一反应是"工艺变了"。
但在 AIAG MSA 手册的立场上，还有第二种可能且更常见：
**量具本身就在抖**。一个 %GRR = 45% 的测量系统，画出来的控制图
有一半的"异常"是量具制造的噪声——按它调机，只会把好好的工艺调坏。

所以 docs/architecture.md 里把 SPC 与 MSA 放在同一层：
没有 MSA 的 SPC，是把噪声当信号；没有 SPC 的 MSA，是测了不用。

【两种算法都实现，因为它们的用途不同】

1. **均值极差法（X̄-R）**：AIAG 推荐现场用。只要 2 个评价人 × 10 个零件
   × 2~3 次试测就能算，手算都行。缺点是分不清"评价人之间不一致"
   与"评价人与零件有交互"（比如老王测大件准、测小件飘）。
2. **ANOVA 法**：能拆出交互项 σ²_pa，是判断"要不要给某人单独培训"的依据。
   缺点是要平方和，手算不现实——但这正是代码该干的事。

两种算法的 GRR 会有几个百分点的差异，这是正常的：它们对交互项的假设不同。
本模块在两个结果都给出时不取平均、不择优，而是**都报**，并在 verdict 里
注明依据的是哪一个——把差异藏起来比暴露差异危险得多。

【判定标准（AIAG MSA 4th ed.）】

    %GRR < 10%      合格，测量系统可用
    10% ≤ %GRR ≤ 30% 有条件接受，取决于零件重要性与返工成本
    %GRR > 30%      不合格，必须改进量具或测量方法

    ndc ≥ 5         可分辨的类别数，小于 5 时控制图分不出真实波动

`ndc` 常被忽略但很致命：%GRR = 25% 时 ndc 往往只有 4，
意味着这套量具只能分出 4 档，而控制图要判 8 条判异准则——分辨率不够。
本模块把 ndc 作为独立的否决项，而不是塞进 %GRR 里一起看。

【单位与口径】

所有输出都用**与被测值相同的单位**（mm、N·m…），百分比另算。
把 σ 换算成"占公差的比例"只在 capability 里做，MSA 里不做——
MSA 回答"尺子多准"，不回答"产品多好"，混在一起会让两个结论都不成立。
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# AIAG 常数表
# ---------------------------------------------------------------------------
# K1：试测次数 n 对应的系数（= 1/d2*，d2 为极差分布的均值系数）
K1_TABLE = {2: 0.8862, 3: 0.5908, 4: 0.4857, 5: 0.4298}

# K2：评价人数 r 对应的系数
K2_TABLE = {2: 0.7071, 3: 0.5231, 4: 0.4467, 5: 0.4030}

# K3：零件数 k 对应的系数
K3_TABLE = {
    2: 0.7071, 3: 0.5231, 4: 0.4467, 5: 0.4030, 6: 0.3742,
    7: 0.3534, 8: 0.3375, 9: 0.3249, 10: 0.3146,
}

# 判定阈值（百分比）
ACCEPT_LIMIT = 10.0
CONDITIONAL_LIMIT = 30.0

# 可分辨类别数下限（AIAG：1.41 × PV/GRR ≥ 5 才算够用）
NDC_MIN = 5


def _k(table: Dict[int, float], key: int, label: str) -> float:
    """取系数，超表时报错而不是线性外推。

    外推看着"智能"，实际会给出一个没有理论依据的数：
    n=7 次试测时 K1 不在表里，自己插值出来的 GRR 谁也不敢签字。
    """
    if key in table:
        return table[key]
    raise ValueError(
        "%s=%d 超出 AIAG 常数表范围（%d~%d），请按标准方案重新设计"
        % (label, key, min(table), max(table))
    )


# ---------------------------------------------------------------------------
# 数据整形
# ---------------------------------------------------------------------------
def organize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """把扁平行整理成 评价人 → 零件 → [试测值] 的三层结构。

    输入行形如 {"appraiser": "张三", "part": "P01", "trial": 1, "value": 10.02}。

    这里做的不只是分组，还有**平衡性检查**：AIAG 方法要求
    每个评价人对每个零件测同样次数。少测一个零件的后果是
    R̄ 变成加权平均，权重取决于谁少测了——算出来的 GRR 无法复现。
    """
    cells: Dict[str, Dict[str, Dict[int, float]]] = {}
    for row in rows:
        appraiser = str(row.get("appraiser") or "").strip()
        part = str(row.get("part") or "").strip()
        if not appraiser or not part:
            continue
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            continue
        trial = int(row.get("trial") or 1)
        cells.setdefault(appraiser, {}).setdefault(part, {})[trial] = value

    appraisers = sorted(cells)
    parts = sorted({p for a in cells.values() for p in a})
    if not appraisers or not parts:
        return {"appraisers": [], "parts": [], "data": {}, "balanced": False,
                "message": "没有有效数据行"}

    # 期望的 (评价人, 零件) 组合必须全有，且每个组合的试测次数一致
    counts: List[int] = []
    missing: List[str] = []
    for a in appraisers:
        for p in parts:
            trials = cells.get(a, {}).get(p) or {}
            if not trials:
                missing.append("%s/%s" % (a, p))
            else:
                counts.append(len(trials))
    balanced = not missing and len(set(counts)) == 1

    data = {
        a: {p: [cells[a][p][t] for t in sorted(cells[a][p])] for p in parts if p in cells[a]}
        for a in appraisers
    }
    out: Dict[str, Any] = {
        "appraisers": appraisers,
        "parts": parts,
        "data": data,
        "balanced": balanced,
        "n_trials": counts[0] if counts and len(set(counts)) == 1 else 0,
        "message": "",
    }
    if missing:
        out["message"] = "数据不平衡，缺少 %d 个单元格（如 %s）" % (
            len(missing), "、".join(missing[:3]))
    elif len(set(counts)) > 1:
        out["message"] = "各单元格试测次数不一致（%s），GRR 结果不可复现" % (
            "、".join(str(c) for c in sorted(set(counts))))
    return out


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _range(values: Sequence[float]) -> float:
    return (max(values) - min(values)) if values else 0.0


# ---------------------------------------------------------------------------
# 一、均值极差法（现场首选）
# ---------------------------------------------------------------------------
def average_range_grr(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """均值极差法 GR&R（AIAG 第 4 版，10 零件 × 3 人 × 3 次为经典方案）。

    每一步都写清了公式来源，因为这个结果会被写进 PPAP 文件，
    出了问题要能倒推"这个 12.3% 是怎么来的"。
    """
    org = organize(rows)
    if not org["appraisers"] or not org["parts"]:
        return {"ok": False, "message": org["message"] or "没有有效数据"}

    appraisers, parts, data = org["appraisers"], org["parts"], org["data"]
    r, k = len(appraisers), len(parts)
    n = org["n_trials"] or min(len(v) for a in data.values() for v in a.values())

    warnings: List[str] = []
    if not org["balanced"]:
        warnings.append(org["message"] + "；结果为近似值，建议补齐后重算")
    if r < 2 or n < 2:
        return {"ok": False, "message": "至少需要 2 个评价人、每格 2 次试测"}
    if k < 2:
        return {"ok": False, "message": "至少需要 2 个零件才能估计零件间变差"}

    # ---- 每个 (评价人, 零件) 的均值与极差 ----
    cell_mean = {a: {p: _mean(v) for p, v in data[a].items()} for a in appraisers}
    cell_range = {a: {p: _range(v) for p, v in data[a].items()} for a in appraisers}

    # R̄：所有单元格极差的平均（等权，不按试测次数加权 —— 平衡设计下两者相同）
    all_ranges = [cell_range[a][p] for a in appraisers for p in cell_range[a]]
    r_bar = _mean(all_ranges)

    # 每位评价人的总体均值，用来看"人之间"的偏移
    appr_mean = {a: _mean([cell_mean[a][p] for p in cell_mean[a]]) for a in appraisers}
    x_diff = max(appr_mean.values()) - min(appr_mean.values())

    # 零件均值与极差（Rp 用零件均值算，不是原始值）
    part_mean = {p: _mean([cell_mean[a][p] for a in appraisers if p in cell_mean[a]])
                 for p in parts}
    r_p = max(part_mean.values()) - min(part_mean.values())

    # ---- 变差分量 ----
    ev = r_bar * _k(K1_TABLE, n, "试测次数")                     # 重复性（设备变差）
    av_sq = (x_diff * _k(K2_TABLE, r, "评价人数")) ** 2 - (ev ** 2 / (n * r))
    av = math.sqrt(av_sq) if av_sq > 0 else 0.0                  # 再现性（评价人变差）
    if av_sq <= 0:
        # 负的方差在数学上无意义，AIAG 规定截断为 0，但必须留痕
        warnings.append("再现性方差为负（AV 截断为 0），说明评价人之间的差异"
                        "小于重复性噪声，属正常现象，不做调整")

    grr = math.sqrt(ev ** 2 + av ** 2)
    pv = r_p * _k(K3_TABLE, k, "零件数")
    tv = math.sqrt(grr ** 2 + pv ** 2)

    pct = _pct_block(ev, av, grr, pv, tv)
    ndc = 1.41 * (pv / grr) if grr > 0 else float("inf")

    return {
        "ok": True,
        "method": "average_range",
        "method_label": "均值极差法",
        "design": {"appraisers": r, "parts": k, "trials": n,
                   "cells": r * k, "balanced": org["balanced"]},
        # σ 保留 6 位：量具分辨率做到 0.001mm 时，4 位小数会把 0.0001 舍成 0
        "sigma": {"ev": _r(ev, 6), "av": _r(av, 6), "grr": _r(grr, 6),
                  "pv": _r(pv, 6), "tv": _r(tv, 6)},
        "percent": pct,
        "ndc": _r(ndc) if ndc != float("inf") else None,
        "stats": {
            "r_bar": _r(r_bar, 6),
            "x_diff": _r(x_diff, 6),
            "rp": _r(r_p, 6),
            "appraiser_mean": {a: _r(v) for a, v in appr_mean.items()},
            "part_mean": {p: _r(v) for p, v in part_mean.items()},
        },
        "warnings": warnings,
        "verdict": _verdict(grr, tv, ndc),
    }


def _pct_block(ev: float, av: float, grr: float, pv: float, tv: float) -> Dict[str, float]:
    """把各分量换算成占 TV 的百分比（AIAG 的 %Study Variation）。"""
    if tv <= 1e-12:
        # 所有读数完全相同：TV=0，百分比无定义。返回 0 而不是抛 ZeroDivisionError，
        # 因为"量具读数是常数"本身就是一种需要看到的结论。
        return {"ev": 0.0, "av": 0.0, "grr": 0.0, "pv": 0.0}
    return {
        "ev": _r(ev / tv * 100),
        "av": _r(av / tv * 100),
        "grr": _r(grr / tv * 100),
        "pv": _r(pv / tv * 100),
    }


def _r(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _verdict(grr: float, tv: float, ndc: float) -> Dict[str, Any]:
    """判定：先看 %GRR，再看 ndc，两个都不过就是不合格。

    为什么 ndc 是独立否决项：%GRR=28%（"有条件接受"）时 ndc 可能只有 4，
    这时候控制图分不出 8 条判异准则要求的档位——用它判异等于抛硬币。
    """
    pct = (grr / tv * 100) if tv > 1e-12 else 0.0
    ndc_ok = ndc >= NDC_MIN
    if pct < ACCEPT_LIMIT and ndc_ok:
        level, text = "accept", "测量系统可接受，可直接用于控制图与能力分析"
    elif pct <= CONDITIONAL_LIMIT and ndc_ok:
        level, text = "conditional", "有条件接受：仅用于非关键特性，或配合更严的判异规则"
    elif pct < ACCEPT_LIMIT and not ndc_ok:
        level = "conditional"
        text = "变差占比达标但分辨率不足（ndc=%.1f < %d），控制图仍不可信" % (ndc, NDC_MIN)
    elif pct <= CONDITIONAL_LIMIT:
        level = "conditional"
        text = ("变差占比 %.1f%% 有条件接受，但 ndc=%.1f < %d，"
                "分辨率不足，建议先换量具" % (pct, ndc, NDC_MIN))
    else:
        level, text = "reject", "测量系统不可接受：先改进量具/夹具/测量方法，再谈工艺"
    return {"level": level, "text": text, "pct_grr": _r(pct, 2), "ndc_ok": ndc_ok}


# ---------------------------------------------------------------------------
# 二、ANOVA 法（能拆交互项）
# ---------------------------------------------------------------------------
def anova_grr(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """双因子随机效应 ANOVA 法 GR&R。

    模型：x_ijk = μ + 零件_i + 评价人_j + (零件×评价人)_ij + 误差_ijk

    与均值极差法的差别就在交互项：如果老王测大件很准、测小件总偏，
    交互项会显著，GRR 里就多出一块本属于"人-件配合"的变差。
    均值极差法把这块并进 AV 或 EV，所以它给出的 AV 常常偏大。
    """
    org = organize(rows)
    if not org["appraisers"] or not org["parts"]:
        return {"ok": False, "message": org["message"] or "没有有效数据"}
    if not org["balanced"]:
        # ANOVA 对不平衡设计需要一般线性模型（Type III SS），不在这里硬做
        return {"ok": False,
                "message": "ANOVA 法要求平衡设计：" + (org["message"] or "数据不平衡")}

    appraisers, parts, data = org["appraisers"], org["parts"], org["data"]
    r, k = len(appraisers), len(parts)
    n = org["n_trials"]
    if r < 2 or k < 2 or n < 2:
        return {"ok": False, "message": "至少 2 人 × 2 件 × 2 次"}

    all_values = [v for a in data.values() for vs in a.values() for v in vs]
    grand = _mean(all_values)
    total_n = len(all_values)

    # ---- 平方和 ----
    ss_total = sum((v - grand) ** 2 for v in all_values)
    ss_part = r * n * sum((_mean([_mean(data[a][p]) for a in appraisers]) - grand) ** 2
                          for p in parts)
    ss_appr = k * n * sum((_mean([_mean(data[a][p]) for p in parts]) - grand) ** 2
                          for a in appraisers)
    ss_cells = n * sum(
        (_mean(data[a][p]) - _mean([_mean(data[a][q]) for q in parts])
         - _mean([_mean(data[x][p]) for x in appraisers]) + grand) ** 2
        for a in appraisers for p in parts
    )
    ss_equip = ss_total - ss_part - ss_appr - ss_cells

    # ---- 自由度与均方 ----
    df_part, df_appr = k - 1, r - 1
    df_cells, df_equip = (k - 1) * (r - 1), k * r * (n - 1)
    ms_part = ss_part / df_part if df_part else 0.0
    ms_appr = ss_appr / df_appr if df_appr else 0.0
    ms_cells = ss_cells / df_cells if df_cells else 0.0
    ms_equip = ss_equip / df_equip if df_equip else 0.0

    # ---- 方差分量（负值截断为 0，与 MINITAB 一致）----
    var_equip = max(ms_equip, 0.0)
    var_int = max((ms_cells - ms_equip) / n, 0.0)
    var_appr = max((ms_appr - ms_cells) / (k * n), 0.0)
    var_part = max((ms_part - ms_cells) / (r * n), 0.0)

    ev = math.sqrt(var_equip)
    av = math.sqrt(var_appr + var_int) if (var_appr + var_int) > 0 else 0.0
    grr = math.sqrt(var_equip + var_appr + var_int)
    pv = math.sqrt(var_part)
    tv = math.sqrt(grr ** 2 + pv ** 2)

    pct = _pct_block(ev, av, grr, pv, tv)
    ndc = 1.41 * (pv / grr) if grr > 0 else float("inf")

    # F 检验：交互项显著与否决定"要不要按人分开评估"
    f_int = (ms_cells / ms_equip) if ms_equip > 0 else None
    f_appr = (ms_appr / ms_cells) if ms_cells > 0 else None

    warnings: List[str] = []
    if var_int <= 0:
        warnings.append("交互项方差为 0：评价人与零件无显著交互，"
                        "此时 ANOVA 与均值极差法结果接近")
    if f_appr is not None and ms_cells > 0 and f_appr < 1:
        warnings.append("评价人效应不显著，再现性主要来自交互项")

    return {
        "ok": True,
        "method": "anova",
        "method_label": "ANOVA 法",
        "design": {"appraisers": r, "parts": k, "trials": n,
                   "cells": r * k, "balanced": True},
        "anova": {
            "part": {"df": df_part, "ss": _r(ss_part), "ms": _r(ms_part),
                     "f": _r(ms_part / ms_cells) if ms_cells > 0 else None},
            "appraiser": {"df": df_appr, "ss": _r(ss_appr), "ms": _r(ms_appr),
                          "f": _r(f_appr) if f_appr is not None else None},
            "interaction": {"df": df_cells, "ss": _r(ss_cells), "ms": _r(ms_cells),
                            "f": _r(f_int) if f_int is not None else None},
            "repeatability": {"df": df_equip, "ss": _r(ss_equip), "ms": _r(ms_equip)},
            "total": {"df": total_n - 1, "ss": _r(ss_total)},
        },
        "var_components": {
            "repeatability": _r(var_equip),
            "interaction": _r(var_int),
            "appraiser": _r(var_appr),
            "part": _r(var_part),
            "total": _r(var_equip + var_int + var_appr + var_part),
        },
        "sigma": {"ev": _r(ev, 6), "av": _r(av, 6), "grr": _r(grr, 6),
                  "pv": _r(pv, 6), "tv": _r(tv, 6)},
        "percent": pct,
        "ndc": _r(ndc) if ndc != float("inf") else None,
        "warnings": warnings,
        "verdict": _verdict(grr, tv, ndc),
    }


# ---------------------------------------------------------------------------
# 三、偏倚（Bias）
# ---------------------------------------------------------------------------
def bias(measurements: Sequence[float], reference: float,
         sigma: Optional[float] = None) -> Dict[str, Any]:
    """偏倚：同一批件反复测，平均值与真值（标准件）的差。

    `sigma` 应传**重复性标准差**（同一次 MSA 里算出的 EV）。
    不传时用样本标准差代替，但要在 warnings 里说明——
    用样本标准差算偏倚的置信区间会把它自己的一部分变差算进去，
    这正是"偏倚显著但其实是量具太糙"这种误判的来源。

    显著性用单样本 t 检验：t = |bias| / (σ/√n)，自由度 n-1。
    """
    values = [float(v) for v in measurements if v is not None]
    if len(values) < 2:
        return {"ok": False, "message": "至少需要 2 次测量"}

    n = len(values)
    mean = _mean(values)
    dev = mean - float(reference)
    warnings: List[str] = []
    if sigma is None:
        sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
        warnings.append("未提供重复性标准差，已用样本标准差代替（偏倚的显著性会被低估）")
    else:
        sd = float(sigma)

    se = sd / math.sqrt(n) if sd > 0 else 0.0
    if se > 0:
        t_stat = abs(dev) / se
        p_approx = _t_two_sided_p(t_stat, n - 1)
    else:
        t_stat = float("inf") if abs(dev) > 1e-12 else 0.0
        p_approx = 0.0 if abs(dev) > 1e-12 else 1.0

    # 95% 置信区间（大样本用 1.96，小样本用 t 近似值表）
    t_crit = _t_critical(n - 1)
    ci = (dev - t_crit * se, dev + t_crit * se)

    significant = p_approx < 0.05
    return {
        "ok": True,
        "reference": _r(reference),
        "n": n,
        "mean": _r(mean),
        "bias": _r(dev),
        "bias_pct": _r(dev / reference * 100, 2) if reference else None,
        "sigma": _r(sd),
        "t": _r(t_stat) if t_stat != float("inf") else None,
        "p": _r(p_approx, 4),
        "ci95": [_r(ci[0]), _r(ci[1])],
        "significant": significant,
        "warnings": warnings,
        "verdict": {
            "level": "reject" if significant else "accept",
            "text": ("偏倚显著（p=%.3f）：需要校准量具或修正测量基准" % p_approx
                     if significant else
                     "偏倚不显著（p=%.3f）：无证据表明存在系统性偏移" % p_approx),
        },
    }


def _t_critical(df: int) -> float:
    """双侧 95% 的 t 临界值（小样本近似表，够 MSA 用）。

    查表而不是调 scipy：本项目零第三方统计依赖（见 ADR-0003 的取舍），
    df > 30 时 t 已逼近 1.96，用分段近似误差 < 1%。
    """
    table = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
        16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        25: 2.060, 30: 2.042,
    }
    if df in table:
        return table[df]
    if df < 1:
        return 12.706
    if df > 30:
        return 1.96
    # 21~29：取最近的表项（误差 < 0.5%）
    nearest = min((k for k in table if k <= df), key=lambda k: abs(k - df))
    return table[nearest]


def _t_two_sided_p(t: float, df: int) -> float:
    """双侧 p 值的粗估（不追求精确，只求判定方向正确）。

    用正态近似 + 小样本修正：p ≈ 2 * (1 - Φ(t * c))，c = df/(df-2) 的平方根修正。
    MSA 里真正重要的是"显著/不显著"这个二分，不是 p 值小数点后第三位。
    """
    if t <= 0:
        return 1.0
    scale = math.sqrt(df / (df - 2)) if df > 2 else 1.0
    z = t * scale
    p = 2 * (1 - _normal_cdf(z))
    return max(0.0, min(1.0, p))


def _normal_cdf(z: float) -> float:
    """标准正态 CDF（Abramowitz-Stegun 7.1.26，误差 < 7.5e-8）。"""
    sign = -1.0 if z < 0 else 1.0
    x = abs(z) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * math.exp(-x * x)
    return 0.5 * (1.0 + sign * y)


# ---------------------------------------------------------------------------
# 四、线性（Linearity）
# ---------------------------------------------------------------------------
def linearity(points: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """线性：偏倚随被测量大小变化而变化的程度。

    输入每个标准件的 {"reference": 名义值, "measurements": [读数…]}。
    对 (reference, bias) 做最小二乘，斜率显著非零即"线性不良"——
    表现为"小件测得准、大件偏大"，翻过来就是量具的放大倍率有问题。

    为什么单独做：偏倚只回答"平均差多少"，线性回答"差得是否随尺寸变"。
    后者决定能不能用一把卡尺量整个尺寸段，前者不能。
    """
    pts: List[Tuple[float, float, int, float]] = []
    for p in points:
        values = [float(v) for v in (p.get("measurements") or [])]
        if len(values) < 2:
            continue
        ref = float(p["reference"])
        pts.append((ref, _mean(values) - ref, len(values),
                    _stdev(values)))
    if len(pts) < 3:
        return {"ok": False, "message": "线性评估至少需要 3 个不同尺寸的标准件"}

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(xs)
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return {"ok": False, "message": "标准件的尺寸全都一样，无法评估线性"}
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0

    # 斜率的标准误：se = sqrt(SSres/(n-2) / Sxx)
    if n > 2 and sxx > 0:
        se_slope = math.sqrt(max(ss_res, 0.0) / (n - 2) / sxx)
        t_slope = abs(slope) / se_slope if se_slope > 0 else float("inf")
        p_slope = _t_two_sided_p(t_slope, n - 2) if se_slope > 0 else 0.0
    else:
        se_slope, t_slope, p_slope = 0.0, None, 1.0

    span = max(xs) - min(xs)
    warnings: List[str] = []
    if span <= 0:
        warnings.append("标准件尺寸跨度太小，线性结论不可靠")

    significant = p_slope < 0.05
    return {
        "ok": True,
        "points": [{"reference": _r(p[0]), "bias": _r(p[1]), "n": p[2],
                    "sd": _r(p[3])} for p in pts],
        "slope": _r(slope),
        "intercept": _r(intercept),
        "r2": _r(r2, 4),
        "span": _r(span),
        # 线性度：整个量程内偏倚的变化量（占量程的百分比）
        "linearity": _r(abs(slope) * span),
        "linearity_pct": _r(abs(slope) * span / span * 100, 2) if span else None,
        "se_slope": _r(se_slope),
        "t": _r(t_slope) if t_slope not in (None, float("inf")) else None,
        "p": _r(p_slope, 4),
        "significant": significant,
        "warnings": warnings,
        "verdict": {
            "level": "reject" if significant else "accept",
            "text": ("偏倚随尺寸显著变化（斜率 p=%.3f）：该量具不能跨整个量程使用，"
                     "请分段校准或换量具" % p_slope if significant else
                     "无证据表明存在线性问题（斜率 p=%.3f）" % p_slope),
        },
    }


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


# ---------------------------------------------------------------------------
# 五、稳定性（Stability）
# ---------------------------------------------------------------------------
def stability(periods: Sequence[Dict[str, Any]],
              sigma_ref: Optional[float] = None) -> Dict[str, Any]:
    """稳定性：同一标准件在不同时间点测，看均值和变差有没有漂移。

    输入 [{"period": "2026-W44", "measurements": [...]}, …]（按时间先后）。

    判据用**漂移量 vs 重复性**：某期的均值偏离首期超过 3σ（σ 为重复性），
    就不是随机波动了。也要看各期内部的 σ 是否在变大——
    量具磨损的典型表现是"平均值还准，但读数越来越散"。
    """
    series: List[Dict[str, Any]] = []
    for p in periods:
        values = [float(v) for v in (p.get("measurements") or [])]
        if not values:
            continue
        series.append({
            "period": str(p.get("period") or ("T%d" % (len(series) + 1))),
            "n": len(values),
            "mean": _mean(values),
            "sd": _stdev(values),
            "range": _range(values),
        })
    if len(series) < 2:
        return {"ok": False, "message": "稳定性分析至少需要 2 个时间点"}

    base_mean = series[0]["mean"]
    pooled = ([s["sd"] for s in series if s["n"] > 1])
    sigma = float(sigma_ref) if sigma_ref else (_mean(pooled) if pooled else 0.0)
    warnings: List[str] = []
    if sigma <= 0:
        warnings.append("各期内部标准差为 0，无法判定漂移（数据可能被四舍五入过头）")

    shifts = []
    for s in series:
        delta = s["mean"] - base_mean
        z = abs(delta) / sigma if sigma > 0 else 0.0
        shifts.append({"period": s["period"], "delta": _r(delta),
                       "sigma_multiple": _r(z, 2), "drift": z > 3})

    drifted = [s for s in shifts if s["drift"]]
    # 变差是否在扩大：末期 sd 相对首期超出 50%
    widening = False
    if len(series) >= 2 and series[0]["sd"] > 0:
        widening = series[-1]["sd"] > series[0]["sd"] * 1.5

    level = "reject" if (drifted or widening) else "accept"
    if drifted:
        text = "第 %s 期起均值漂移超过 3σ，量具需重新校准" % drifted[0]["period"]
    elif widening:
        text = "均值稳定但读数变差在扩大，疑似量具磨损"
    else:
        text = "均值与变差均稳定，量具处于受控状态"

    return {
        "ok": True,
        "series": [{k: (_r(v) if isinstance(v, float) else v) for k, v in s.items()}
                   for s in series],
        "sigma": _r(sigma),
        "shifts": shifts,
        "widening": widening,
        "warnings": warnings,
        "verdict": {"level": level, "text": text},
    }


# ---------------------------------------------------------------------------
# 六、综合报告
# ---------------------------------------------------------------------------
def report(rows: Sequence[Dict[str, Any]],
           *,
           tolerance: Optional[float] = None,
           run_anova: bool = True) -> Dict[str, Any]:
    """一次给出两种算法的 GRR + 交叉校验，作为 MSA 的对外出口。

    `tolerance` 传公差带宽（USL-LSL）时，额外给出 %Tolerance：
    %Study Variation 说"相对于过程变差有多大"，%Tolerance 说"相对于公差有多大"。
    客户审核看的是后者，内部改工艺看的是前者——两个都要给，别替用户选。
    """
    xr = average_range_grr(rows)
    out: Dict[str, Any] = {"ok": xr.get("ok", False), "average_range": xr}
    if not xr.get("ok"):
        out["message"] = xr.get("message")
        return out

    out["anova"] = anova_grr(rows) if run_anova else {"ok": False, "skipped": True}

    # 两种算法的 %GRR 差异：超过 5 个百分点要提醒，通常是数据不平衡或异常值
    if out["anova"].get("ok"):
        a = xr["percent"]["grr"]
        b = out["anova"]["percent"]["grr"]
        out["consistency"] = {
            "delta_pct": _r(abs(a - b), 2),
            "consistent": abs(a - b) <= 5.0,
            "note": ("两种算法结果一致（差 %.1f 个百分点）" % abs(a - b)
                     if abs(a - b) <= 5.0 else
                     "两种算法相差 %.1f 个百分点，通常意味着存在交互效应或异常值，"
                     "建议看 ANOVA 的方差分量表" % abs(a - b)),
        }
    else:
        out["consistency"] = None

    if tolerance and tolerance > 0:
        grr = xr["sigma"]["grr"]
        out["pct_tolerance"] = {
            "grr": _r(grr / tolerance * 100, 2),
            "tolerance": _r(float(tolerance)),
            "note": "客户审核看这个数；过程改进看 %Study Variation",
        }
    else:
        out["pct_tolerance"] = None

    # 最终结论以两个算法中较差的一个为准：宽进严出
    if out["anova"].get("ok"):
        order = {"accept": 0, "conditional": 1, "reject": 2}
        worst = max((xr["verdict"], out["anova"]["verdict"]),
                    key=lambda v: order.get(v["level"], 0))
        out["verdict"] = dict(worst)
        out["verdict"]["basis"] = "取两种算法中较差的结论（宽进严出）"
    else:
        out["verdict"] = dict(xr["verdict"])
        out["verdict"]["basis"] = "仅均值极差法"

    out["recommendations"] = _recommend(out)
    return out


def _recommend(report_data: Dict[str, Any]) -> List[str]:
    """按结论给出可执行的下一步建议。

    这些建议刻意写得"具体到动作"：说"改进测量系统"没用，
    说"换分辨率 0.001 的量具/加装定位夹具"才是工程师能直接立项的东西。
    """
    out: List[str] = []
    verdict = report_data.get("verdict") or {}
    level = verdict.get("level")
    xr = report_data.get("average_range") or {}
    pct = (xr.get("percent") or {})
    ndc = xr.get("ndc")

    if level == "accept":
        out.append("测量系统可用于控制图与过程能力分析，建议每 6 个月复评一次")
    elif level == "conditional":
        out.append("仅用于非关键特性；用于关键特性时须加严判异规则并留存复评记录")
    else:
        out.append("暂停使用该测量系统做判定，先立项改进：优先换量具或加装定位夹具")

    if pct.get("av", 0) > pct.get("ev", 0):
        out.append("再现性(%AV)大于重复性(%EV)：问题在**人**——补作业指导书、统一夹持位置、"
                   "对偏差大的评价人单独培训")
    elif pct.get("ev", 0) > 0:
        out.append("重复性(%EV)是主要来源：问题在**量具与夹具**——检查磨损、清洁测量面、"
                   "固定夹持力，而不是反复培训操作者")

    if ndc is not None and ndc < NDC_MIN:
        out.append("可分辨类别数 ndc=%.1f 不足 %d：换分辨率更高的量具，"
                   "或放大被测特征（如改用比较仪）" % (ndc, NDC_MIN))

    if report_data.get("pct_tolerance") and report_data["pct_tolerance"]["grr"] > 30:
        out.append("%%Tolerance 已超过 30%%，即使过程能力 Cp 好看，"
                   "也会有批量误判风险，务必先解决量具问题")

    anova = report_data.get("anova") or {}
    if anova.get("ok"):
        inter = (anova.get("var_components") or {}).get("interaction", 0)
        total = (anova.get("var_components") or {}).get("total", 0)
        if total > 0 and inter / total > 0.2:
            out.append("交互项占总变差 %.0f%%：评价人与零件尺寸存在配合问题，"
                       "应按尺寸段分工测量，而不是让所有人测全部件" % (inter / total * 100))
    return out


def overview(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """轻量概览：只要一个 %GRR 和结论时用（页面首屏）。

    不做 ANOVA：页面上先出结论，点"详细"再算 ANOVA，
    避免用户等 2 秒才看见第一个数字。
    """
    xr = average_range_grr(rows)
    if not xr.get("ok"):
        return {"ok": False, "message": xr.get("message")}
    return {
        "ok": True,
        "pct_grr": xr["percent"]["grr"],
        "pct_ev": xr["percent"]["ev"],
        "pct_av": xr["percent"]["av"],
        "ndc": xr["ndc"],
        "verdict": xr["verdict"],
        "design": xr["design"],
    }
