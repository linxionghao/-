"""过程能力分析：规格是客户给的，能力是自己有的，两者之差就是风险。

本模块回答一个问题：**这个过程的固有波动，塞得进规格上下限吗？**

四个指标的关系，先把话说清（评审最爱问的就是这四个数）：

    Cp  = (USL - LSL) / (6σ_within)        只看"宽窄"，不管中心偏不偏
    Cpk = min(USL-μ, μ-LSL) / (3σ_within)  同时看宽窄和偏心，常用判据
    Pp  = (USL - LSL) / (6σ_overall)       用总体标准差，含批间漂移
    Ppk = min(USL-μ, μ-LSL) / (3σ_overall) 同上，含偏心

- `Cp/Pp` 回答"如果能把中心调到正中，最好能做到多少"；
- `Cpk/Ppk` 回答"按现在这个偏心程度，实际能做到多少"；
- `Cp - Cpk` 的差值就是"偏心造成的损失"，调机可以改善；
- `Pp - Cp` 的差值就是"批间漂移"，调机改不了，得改工艺或换设备。

**σ 用哪个**：Cpk 用组内标准差（R̄/d2，只含普通原因），Ppk 用总体标准差（含批间）。
混用会导致"同一条产线两个系统给出两个 Cpk"，这是我们把它拆成 within / overall
两个字段的原因——不是冗余，是刻意让调用方必须选一个并且说清选的哪个。

最后一句实话：**过程不受控时，Cpk 没有意义**。Cpk 的前提是稳定过程，
拿一段正在漂移的数据算 Cpk，得到的只是一个好看的平均数。所以 `assess()`
会先要求调用方传入"是否受控"的结论（来自 spc 模块），不受控时把 verdict
降级为 `unstable`，而不是照样输出"能力充足"。
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, Optional, Sequence

from . import descriptive as _d

# 能力判级（行业惯例，AIAG SPC 手册 + 多数主机厂 CSR）
# 不做成"可配置"，因为这几个阈值是客户审核时直接对照的，配置化了反而不利于对齐口径。
GRADE_TABLE = [
    (1.67, "A+", "过程能力优秀，可考虑放宽检验频次或降低抽检比例"),
    (1.33, "A", "过程能力充足，维持现有控制方式"),
    (1.00, "B", "过程能力勉强，需加严监控并制定改善计划"),
    (0.67, "C", "过程能力不足，已产生不良，需立即改善"),
    (0.0, "D", "过程能力严重不足，应停线整改"),
]


def cp(usl: float, lsl: float, sigma: Optional[float]) -> Optional[float]:
    """潜在能力指数 Cp = (USL - LSL) / 6σ。σ 为 0 时返回 None。

    σ=0 理论上意味着"每件都一样"，实际只会出现在量具分辨率不足的情况
    （比如要求测到 0.001mm，但卡尺只能读到 0.02mm，量出来全是 19.98）。
    这时算出的 Cpk 会是无穷大，报表上显示 ∞ 比报错更糟——它会让主管以为"完美"，
    从而跳过"量具能力不足"这个真正的问题。所以这里返回 None，由上层提示换量具。
    """
    if sigma is None or sigma <= 0:
        return None
    if usl is None or lsl is None or usl <= lsl:
        return None
    return (usl - lsl) / (6.0 * sigma)


def cpk(usl: float, lsl: float, mu: float, sigma: Optional[float]) -> Optional[float]:
    """实际能力指数 Cpk = min(USL-μ, μ-LSL) / 3σ。"""
    if sigma is None or sigma <= 0:
        return None
    if usl is None or lsl is None:
        return None
    upper = (usl - mu) / (3.0 * sigma)
    lower = (mu - lsl) / (3.0 * sigma)
    return min(upper, lower)


def ca(usl: float, lsl: float, mu: float) -> Optional[float]:
    """制程准确度 Ca = |μ - M| / ((USL-LSL)/2)，M 为规格中心。

    Ca 衡量"偏了多少"，与 Cpk 互补：
    Ca 大、Cp 大 → 设备精度够，只是没调正（调机即可，成本低）；
    Ca 小、Cp 小 → 中心对得很准，但波动大（要改工艺，成本高）。
    现场据此决定"调机"还是"报设备改造"，这个判断直接影响维修预算花在哪。
    """
    if usl is None or lsl is None or usl <= lsl:
        return None
    m = (usl + lsl) / 2.0
    half = (usl - lsl) / 2.0
    if half == 0:
        return None
    return abs(mu - m) / half


def cr(usl: float, lsl: float, sigma: Optional[float]) -> Optional[float]:
    """制程精密度 Cr = 6σ / (USL - LSL) = 1 / Cp。"""
    c = cp(usl, lsl, sigma)
    if not c:
        return None
    return 1.0 / c


def ppm(usl: float, lsl: float, mu: float, sigma: Optional[float]) -> Optional[float]:
    """按正态假设估算超出规格的百万分之数（PPM）。

    用 Φ 的近似（Abramowitz-Stegun 7.1.26），误差 < 1.5e-7，够用且零依赖。
    注意：这是**假设正态**的估计值。实际质量数据常带厚尾，PPM 会偏乐观，
    所以本函数的结果在报表里一律标注为"理论估算"，不能当作实测不良率。
    """
    if sigma is None or sigma <= 0:
        return None
    z_upper = (usl - mu) / sigma
    z_lower = (lsl - mu) / sigma
    above = 1.0 - _normal_cdf(z_upper)
    below = _normal_cdf(z_lower)
    return round((above + below) * 1_000_000, 2)


def _normal_cdf(z: float) -> float:
    """标准正态 CDF 近似（Abramowitz & Stegun 7.1.26，|误差| < 1.5e-7）。"""
    if z < -8:
        return 0.0
    if z > 8:
        return 1.0
    sign = 1.0 if z >= 0 else -1.0
    x = abs(z) / math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * x)
    y = 1.0 - (
        ((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592
    ) * t * math.exp(-x * x)
    return 0.5 * (1.0 + sign * y)


def grade(value: Optional[float]) -> Dict[str, str]:
    """按 Cpk/Ppk 给等级与建议。"""
    if value is None:
        return {"grade": "—", "advice": "数据不足或波动为 0，无法评级"}
    for threshold, letter, advice in GRADE_TABLE:
        if value >= threshold:
            return {"grade": letter, "advice": advice}
    return {"grade": "D", "advice": "过程能力严重不足，应停线整改"}


def assess(
    values: Sequence[float],
    usl: float,
    lsl: float,
    sigma_within: Optional[float] = None,
    controlled: Optional[bool] = None,
    target: Optional[float] = None,
) -> Dict[str, object]:
    """一次算全：Cp/Cpk/Pp/Ppk/Ca/Cr/PPM + 判级 + 一句话结论。

    参数
    ----
    values        : 实测值
    usl / lsl     : 规格上下限
    sigma_within  : 组内标准差（来自 spc 的 sigma_within）。
                    不传则退化为用总体标准差算 Cpk，并在 warnings 里明确标注——
                    "用总体 σ 冒充组内 σ"会让 Cpk 偏低、看起来更保守，
                    但保守不等于正确，这类口径混淆在客户审核时是硬伤。
    controlled    : 过程是否受控（来自 spc）。None 表示未知，会提示先做控制图。
    target        : 目标值（默认规格中心）
    """
    data = _d.clean(values)
    warnings: list = []

    if not data:
        return {"ok": False, "message": "没有实测值", "warnings": ["实测值为空"]}
    if usl is None or lsl is None or usl <= lsl:
        return {"ok": False, "message": "规格限非法：USL 必须大于 LSL", "warnings": []}

    mu = sum(data) / len(data)
    sigma_overall = _d.stdev(data)
    if sigma_within is None:
        sigma_within = sigma_overall
        warnings.append("未提供组内标准差，Cpk 用总体标准差估算（会偏低，仅供粗判）")
    if sigma_overall is None or sigma_overall == 0:
        warnings.append("全部实测值相同：疑似量具分辨率不足，能力指数无意义")

    cp_v = cp(usl, lsl, sigma_within)
    cpk_v = cpk(usl, lsl, mu, sigma_within)
    pp_v = cp(usl, lsl, sigma_overall)
    ppk_v = cpk(usl, lsl, mu, sigma_overall)
    ca_v = ca(usl, lsl, mu)
    cr_v = cr(usl, lsl, sigma_within)
    ppm_v = ppm(usl, lsl, mu, sigma_overall)

    m = (usl + lsl) / 2.0
    off_center = mu - (target if target is not None else m)

    g = grade(cpk_v)

    # ---- 结论：先判受控，再判能力 ----
    if controlled is False:
        verdict = "unstable"
        conclusion = (
            "过程不受控，Cpk 仅供参考。请先用控制图找出特殊原因，"
            "待过程稳定后重新评估能力——不受控数据算出的能力指数没有工程意义。"
        )
    elif cpk_v is None:
        verdict = "unknown"
        conclusion = "数据不足或波动为 0，无法判断过程能力。"
    elif cpk_v >= 1.33:
        verdict = "capable"
        conclusion = "过程能力充足（Cpk=%.2f ≥ 1.33）。%s" % (cpk_v, g["advice"])
    elif cpk_v >= 1.0:
        verdict = "marginal"
        conclusion = "过程能力勉强（1.00 ≤ Cpk=%.2f < 1.33），存在产生不良的风险。%s" % (cpk_v, g["advice"])
    else:
        verdict = "incapable"
        conclusion = "过程能力不足（Cpk=%.2f < 1.00），已在产生不良。%s" % (cpk_v, g["advice"])

    # ---- 改善方向提示：偏心 vs 波动 ----
    hint = None
    if cp_v is not None and cpk_v is not None:
        if cp_v - cpk_v > 0.3:
            hint = "Cp 与 Cpk 相差 %.2f：能力损失主要来自中心偏移（Ca=%.2f），优先调机对中。" % (
                cp_v - cpk_v,
                ca_v or 0.0,
            )
        elif pp_v is not None and cp_v - pp_v > 0.3:
            hint = "Cp 与 Pp 相差 %.2f：存在批间漂移，单件调机无用，需查料/机/法的一致性。" % (cp_v - pp_v)
        else:
            hint = "Cp 与 Cpk 接近：中心已对正，能力主要受固有波动限制，需改工艺或换设备。"

    if controlled is None:
        warnings.append("未提供过程受控结论：建议先跑控制图，再解读 Cpk")

    return {
        "ok": True,
        "n": len(data),
        "mean": round(mu, 4),
        "target": round(target if target is not None else m, 4),
        "spec": {"usl": usl, "lsl": lsl, "width": round(usl - lsl, 6)},
        "sigma_within": round(sigma_within, 6) if sigma_within else None,
        "sigma_overall": round(sigma_overall, 6) if sigma_overall else None,
        "cp": _r(cp_v),
        "cpk": _r(cpk_v),
        "pp": _r(pp_v),
        "ppk": _r(ppk_v),
        "ca": _r(ca_v),
        "cr": _r(cr_v),
        "ppm_est": ppm_v,
        "offset": round(off_center, 4),
        "grade": g["grade"],
        "advice": g["advice"],
        "verdict": verdict,
        "conclusion": conclusion,
        "improve_hint": hint,
        "warnings": warnings,
    }


def _r(v: Optional[float], digits: int = 3) -> Optional[float]:
    return None if v is None else round(v, digits)


def capability_matrix(
    groups: Sequence[Dict[str, object]],
) -> Dict[str, object]:
    """多产线/多工序的能力汇总，用于"哪条线最该整改"的排序。

    groups 每项形如 {"key": "LINE-A", "values": [...], "usl": .., "lsl": .., "sigma_within": ..}
    排序按 Cpk 升序——最差的排最前，因为看板的第一屏应该放"要动手的事"，
    而不是"最好的那条线又拿了第一"。
    """
    rows = []
    for g in groups or []:
        res = assess(
            g.get("values") or [],
            g.get("usl"),
            g.get("lsl"),
            sigma_within=g.get("sigma_within"),
            controlled=g.get("controlled"),
        )
        rows.append(
            {
                "key": g.get("key"),
                "n": res.get("n", 0),
                "cpk": res.get("cpk"),
                "ppk": res.get("ppk"),
                "grade": res.get("grade"),
                "verdict": res.get("verdict"),
                "ppm_est": res.get("ppm_est"),
            }
        )
    rows.sort(key=lambda r: (r["cpk"] is None, r["cpk"] if r["cpk"] is not None else 0))
    worst = rows[0]["key"] if rows and rows[0]["cpk"] is not None else None
    return {
        "rows": rows,
        "worst": worst,
        "total": len(rows),
        "message": ("优先整改：%s（Cpk 最低）" % worst) if worst else "无有效数据",
    }
