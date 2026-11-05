# -*- coding: utf-8 -*-
"""MSA 测量系统分析单测：GR&R（两种算法）、偏倚、线性、稳定性。

这一组测试的思路是"用能手算的数据校验公式"：

- 2 人 × 2 件 × 2 次的极小方案，L̄、X̄diff、Rp 都能在纸上算出来，
  断言直接用纸上的数。公式写错（比如把 K1 和 K2 弄反）立刻暴露。
- 合成数据里"只放重复性噪声、不放人/交互效应"，ANOVA 就必须给出
  AV≈0、交互≈0；如果它给出别的东西，说明方差分量的期望均方写错了。

第二类错误特别值得防：GRR 算错不会报错，只会给出一个"看起来合理"的百分数
（比如 σ 少除一个 √n 就整体小 2.2 倍），然后被写进 PPAP 文件签字。
本项目 stage 4 已经在 σ 口径上栽过一次（见 TestSigmaCaliber），这里格外小心。
"""

from __future__ import annotations

import math
import random

import pytest

from quality_trace.core.analytics import msa


def _rows(grid):
    """把 {评价人: {零件: [试测…]}} 摊平成行。"""
    out = []
    for appraiser, parts in grid.items():
        for part, values in parts.items():
            for i, v in enumerate(values, start=1):
                out.append({"appraiser": appraiser, "part": part, "trial": i, "value": v})
    return out


# 2 人 × 2 件 × 2 次：纸算用例
TINY = {
    "A": {"P1": [1.0, 1.1], "P2": [2.0, 2.1]},
    "B": {"P1": [1.05, 1.15], "P2": [2.05, 2.15]},
}


# ===========================================================================
# 一、数据整形
# ===========================================================================
class TestOrganize:
    def test_shape(self):
        org = msa.organize(_rows(TINY))
        assert org["appraisers"] == ["A", "B"]
        assert org["parts"] == ["P1", "P2"]
        assert org["balanced"] is True
        assert org["n_trials"] == 2

    def test_unbalanced_detected(self):
        grid = {"A": {"P1": [1.0, 1.1], "P2": [2.0]}, "B": {"P1": [1.0, 1.1]}}
        org = msa.organize(_rows(grid))
        assert org["balanced"] is False
        assert "缺少" in org["message"]

    def test_varying_trial_counts_detected(self):
        grid = {"A": {"P1": [1.0, 1.1, 1.2]}, "B": {"P1": [1.0, 1.1]}}
        org = msa.organize(_rows(grid))
        assert org["balanced"] is False
        assert "试测次数" in org["message"]

    def test_bad_rows_are_skipped(self):
        rows = _rows(TINY) + [
            {"appraiser": "", "part": "P1", "value": 1.0},
            {"appraiser": "A", "part": "P3", "value": "abc"},
        ]
        org = msa.organize(rows)
        assert "P3" not in org["parts"]

    def test_empty(self):
        assert msa.organize([])["appraisers"] == []


# ===========================================================================
# 二、均值极差法（纸算校验）
# ===========================================================================
class TestAverageRange:
    def test_hand_computed_values(self):
        """R̄=0.1, X̄diff=0.05, Rp=1.0 → EV=0.08862, PV=0.7071, %GRR=12.4%。"""
        r = msa.average_range_grr(_rows(TINY))
        assert r["ok"] is True
        assert r["design"] == {"appraisers": 2, "parts": 2, "trials": 2,
                               "cells": 4, "balanced": True}
        # EV = R̄ × K1(n=2) = 0.1 × 0.8862
        assert r["sigma"]["ev"] == pytest.approx(0.08862, abs=1e-5)
        # PV = Rp × K3(k=2) = 1.0 × 0.7071
        assert r["sigma"]["pv"] == pytest.approx(0.7071, abs=1e-4)
        assert r["sigma"]["grr"] == pytest.approx(0.08862, abs=1e-5)
        assert r["percent"]["grr"] == pytest.approx(12.43, abs=0.1)
        # ndc = 1.41 × PV/GRR
        assert r["ndc"] == pytest.approx(11.25, abs=0.1)

    def test_negative_av_is_truncated_with_warning(self):
        """AV² < 0 时截断为 0，但必须留痕——否则用户以为再现性完美。"""
        r = msa.average_range_grr(_rows(TINY))
        assert r["sigma"]["av"] == 0
        assert any("再现性" in w for w in r["warnings"])

    def test_perfect_gauge(self):
        """读数完全重复、零件间差异巨大 → %GRR 接近 0、ndc 很大。"""
        grid = {"A": {"P1": [1.0, 1.0], "P2": [5.0, 5.0]},
                "B": {"P1": [1.0, 1.0], "P2": [5.0, 5.0]}}
        r = msa.average_range_grr(_rows(grid))
        assert r["sigma"]["grr"] == 0
        assert r["ndc"] is None                    # 除零 → 无定义，不给假数字
        assert r["verdict"]["level"] == "accept"

    def test_appraiser_bias_dominates(self):
        """同一零件，B 比 A 系统高 0.5 → AV 变大、%GRR 变差。"""
        grid = {"A": {"P1": [1.0, 1.02], "P2": [2.0, 2.02], "P3": [3.0, 3.02]},
                "B": {"P1": [1.5, 1.52], "P2": [2.5, 2.52], "P3": [3.5, 3.52]}}
        r = msa.average_range_grr(_rows(grid))
        assert r["sigma"]["av"] > 0
        assert r["percent"]["av"] > r["percent"]["ev"]      # 人比量具影响大
        assert r["verdict"]["level"] in ("conditional", "reject")

    def test_insufficient_design(self):
        assert msa.average_range_grr(_rows({"A": {"P1": [1.0, 1.1]}}))["ok"] is False
        assert msa.average_range_grr(
            _rows({"A": {"P1": [1.0, 1.1]}, "B": {"P1": [1.0, 1.1]}}))["ok"] is False

    def test_trial_count_out_of_table(self):
        """n=6 超出 AIAG 常数表 → 报错，而不是自己外推一个系数。"""
        grid = {"A": {"P1": [1.0] * 6, "P2": [2.0] * 6},
                "B": {"P1": [1.0] * 6, "P2": [2.0] * 6}}
        with pytest.raises(ValueError) as exc:
            msa.average_range_grr(_rows(grid))
        assert "常数表" in str(exc.value)

    def test_part_count_out_of_table(self):
        grid = {a: {"P%d" % i: [float(i)] * 2 for i in range(1, 12)} for a in "AB"}
        with pytest.raises(ValueError):
            msa.average_range_grr(_rows(grid))

    def test_constant_readings_do_not_crash(self):
        """所有读数相同：TV=0，百分比按 0 处理而不是抛 ZeroDivisionError。"""
        grid = {"A": {"P1": [1.0, 1.0], "P2": [1.0, 1.0]},
                "B": {"P1": [1.0, 1.0], "P2": [1.0, 1.0]}}
        r = msa.average_range_grr(_rows(grid))
        assert r["ok"] is True
        assert r["percent"]["grr"] == 0


# ===========================================================================
# 三、ANOVA 法
# ===========================================================================
def _synthetic(seed=20261106, n_parts=10, n_appr=3, n_trials=3,
               part_sd=1.0, appr_sd=0.0, inter_sd=0.0, repeat_sd=0.1):
    """合成数据：零件效应 + 评价人效应 + 交互效应 + 重复性噪声。

    用固定种子保证可复现：MSA 的结论会随样本波动，
    如果测试每次跑出不同的结论，它就没有资格当回归测试。
    """
    rng = random.Random(seed)
    part_effect = {("P%02d" % i): rng.gauss(0, part_sd) for i in range(1, n_parts + 1)}
    appr_name = ["A", "B", "C", "D", "E"][:n_appr]
    appr_effect = {a: rng.gauss(0, appr_sd) if appr_sd else 0.0 for a in appr_name}
    grid = {}
    for a in appr_name:
        grid[a] = {}
        for p, pe in part_effect.items():
            base = 10.0 + pe + appr_effect[a] + (rng.gauss(0, inter_sd) if inter_sd else 0.0)
            grid[a][p] = [base + rng.gauss(0, repeat_sd) for _ in range(n_trials)]
    return grid


class TestAnova:
    def test_pure_repeatability(self):
        """只放重复性噪声：AV 与交互项都应接近 0，GRR ≈ σ_e。"""
        r = msa.anova_grr(_rows(_synthetic(repeat_sd=0.1)))
        assert r["ok"] is True
        assert r["var_components"]["appraiser"] == 0
        assert r["var_components"]["interaction"] == 0
        # 重复性方差应接近 0.01（σ=0.1）
        assert r["var_components"]["repeatability"] == pytest.approx(0.01, abs=0.006)
        assert r["sigma"]["grr"] == pytest.approx(0.1, abs=0.04)

    def test_appraiser_effect_is_captured(self):
        """人为加 +0.5/+1.0 的评价人偏置 → AV 明显变大，评价人 MS 的 F 值大。

        这里用**确定性的偏置**而不是随机高斯：随机数偶尔会抽出一组偏置很小的
        样本，让"AV 应该很大"的断言随机失败——那样的测试没人敢留。
        """
        offsets = {"A": 0.0, "B": 0.5, "C": 1.0}
        grid = {}
        for a, off in offsets.items():
            grid[a] = {"P%d" % i: [float(i) + off, float(i) + off + 0.01]
                       for i in range(1, 6)}
        r = msa.anova_grr(_rows(grid))
        assert r["sigma"]["av"] > 0.2
        assert r["anova"]["appraiser"]["f"] > 1.5
        assert r["percent"]["av"] > r["percent"]["ev"]

    def test_interaction_component(self):
        """加交互效应 → 交互方差 > 0，且 ANOVA 的 GRR 大于纯重复性方案。"""
        r_base = msa.anova_grr(_rows(_synthetic(inter_sd=0.0)))
        r_inter = msa.anova_grr(_rows(_synthetic(inter_sd=0.6)))
        assert r_inter["var_components"]["interaction"] > 0
        assert r_inter["sigma"]["grr"] > r_base["sigma"]["grr"]

    def test_unbalanced_is_rejected(self):
        r = msa.anova_grr(_rows({"A": {"P1": [1.0, 1.1], "P2": [2.0]},
                                 "B": {"P1": [1.0, 1.1]}}))
        assert r["ok"] is False
        assert "平衡设计" in r["message"]

    def test_ss_decomposition_adds_up(self):
        """SS_total = SS_part + SS_appr + SS_int + SS_equip，这是 ANOVA 的自检。"""
        r = msa.anova_grr(_rows(_synthetic(inter_sd=0.3)))
        a = r["anova"]
        parts = (a["part"]["ss"] + a["appraiser"]["ss"] + a["interaction"]["ss"]
                 + a["repeatability"]["ss"])
        assert parts == pytest.approx(a["total"]["ss"], rel=1e-6)

    def test_df_adds_up(self):
        r = msa.anova_grr(_rows(_synthetic(n_parts=5, n_appr=3, n_trials=2)))
        a = r["anova"]
        assert a["part"]["df"] + a["appraiser"]["df"] + a["interaction"]["df"] \
            + a["repeatability"]["df"] == a["total"]["df"]

    def test_small_design_rejected(self):
        assert msa.anova_grr(_rows({"A": {"P1": [1.0, 1.1]},
                                    "B": {"P1": [1.0, 1.1]}}))["ok"] is False


# ===========================================================================
# 四、偏倚
# ===========================================================================
class TestBias:
    def test_significant_bias(self):
        values = [10.5 + 0.1 * (i % 3 - 1) for i in range(25)]
        r = msa.bias(values, 10.0, sigma=0.1)
        assert r["ok"] is True
        assert r["bias"] == pytest.approx(0.5, abs=0.02)
        assert r["significant"] is True
        assert r["verdict"]["level"] == "reject"
        assert r["ci95"][0] < r["bias"] < r["ci95"][1]

    def test_no_bias(self):
        values = [10.0, 10.02, 9.98, 10.01, 9.99, 10.0, 10.01, 9.99]
        r = msa.bias(values, 10.0, sigma=0.05)
        assert abs(r["bias"]) < 0.01
        assert r["significant"] is False

    def test_missing_sigma_warns(self):
        r = msa.bias([10.1, 10.2, 10.05, 10.15], 10.0)
        assert any("样本标准差" in w for w in r["warnings"])

    def test_too_few_measurements(self):
        assert msa.bias([10.0], 10.0)["ok"] is False

    def test_bias_pct(self):
        r = msa.bias([10.4, 10.4, 10.4], 10.0, sigma=0.01)
        assert r["bias_pct"] == pytest.approx(4.0, abs=0.1)

    def test_t_critical_table(self):
        assert msa._t_critical(1) == pytest.approx(12.706)
        assert msa._t_critical(10) == pytest.approx(2.228)
        assert msa._t_critical(100) == pytest.approx(1.96)
        # 21~29 取最近表项，不能抛 KeyError
        assert 2.0 < msa._t_critical(23) < 2.1

    def test_normal_cdf(self):
        assert msa._normal_cdf(0) == pytest.approx(0.5, abs=1e-7)
        assert msa._normal_cdf(1.96) == pytest.approx(0.975, abs=1e-3)
        assert msa._normal_cdf(-1.96) == pytest.approx(0.025, abs=1e-3)


# ===========================================================================
# 五、线性
# ===========================================================================
class TestLinearity:
    def test_proportional_bias_is_detected(self):
        """偏倚 = 0.1 × 名义值 → 斜率 0.1、显著、R²≈1。"""
        points = []
        for ref in (10, 20, 30, 40, 50):
            points.append({"reference": ref, "measurements": [ref * 1.1] * 3})
        r = msa.linearity(points)
        assert r["ok"] is True
        assert r["slope"] == pytest.approx(0.1, abs=1e-6)
        assert r["intercept"] == pytest.approx(0.0, abs=1e-6)
        assert r["r2"] == pytest.approx(1.0, abs=1e-6)
        assert r["significant"] is True
        assert r["verdict"]["level"] == "reject"

    def test_constant_bias_is_not_linearity_problem(self):
        """偏倚恒为 0.2（不随尺寸变）→ 斜率 0，这是偏倚问题不是线性问题。"""
        points = [{"reference": ref, "measurements": [ref + 0.2] * 3}
                  for ref in (10, 20, 30, 40)]
        r = msa.linearity(points)
        assert abs(r["slope"]) < 1e-9
        assert r["linearity"] == pytest.approx(0.0)
        # R² 在 SS_tot=0 时按 1 处理（完全拟合），但斜率说明没有线性问题
        assert r["significant"] is False

    def test_too_few_points(self):
        assert msa.linearity([{"reference": 10, "measurements": [10.1, 10.2]}])["ok"] is False

    def test_same_reference_rejected(self):
        points = [{"reference": 10, "measurements": [10.1, 10.2]}] * 4
        r = msa.linearity(points)
        assert r["ok"] is False
        assert "尺寸" in r["message"]

    def test_span_and_linearity_value(self):
        points = [{"reference": ref, "measurements": [ref * 1.05] * 2}
                  for ref in (10, 30, 50)]
        r = msa.linearity(points)
        assert r["span"] == pytest.approx(40)
        assert r["linearity"] == pytest.approx(2.0, abs=0.01)   # 0.05 × 40


# ===========================================================================
# 六、稳定性
# ===========================================================================
class TestStability:
    def test_stable(self):
        periods = [{"period": "W%02d" % i, "measurements": [10.0, 10.02, 9.98, 10.01]}
                   for i in range(1, 7)]
        r = msa.stability(periods)
        assert r["ok"] is True
        assert r["verdict"]["level"] == "accept"
        assert r["widening"] is False

    def test_drift_detected(self):
        """第 4 期起均值跳 0.5（σ=0.05 时是 10σ）→ 必须报漂移。"""
        periods = [{"period": "W01", "measurements": [10.0] * 4},
                   {"period": "W02", "measurements": [10.0] * 4},
                   {"period": "W03", "measurements": [10.5] * 4},
                   {"period": "W04", "measurements": [10.6] * 4}]
        r = msa.stability(periods, sigma_ref=0.05)
        assert r["verdict"]["level"] == "reject"
        assert any(s["drift"] for s in r["shifts"])
        assert "校准" in r["verdict"]["text"]

    def test_widening_detected(self):
        """均值不变、方差变大 → 疑似量具磨损。"""
        periods = [{"period": "W01", "measurements": [10.00, 10.01, 9.99, 10.00]},
                   {"period": "W02", "measurements": [10.00, 10.02, 9.98, 10.00]},
                   {"period": "W03", "measurements": [9.90, 10.10, 9.85, 10.15]}]
        r = msa.stability(periods)
        assert r["widening"] is True
        assert "磨损" in r["verdict"]["text"]

    def test_too_few_periods(self):
        assert msa.stability([{"period": "W01", "measurements": [1, 2, 3]}])["ok"] is False

    def test_empty_period_skipped(self):
        periods = [{"period": "W01", "measurements": [10.0, 10.1]},
                   {"period": "W02", "measurements": []}]
        assert msa.stability(periods)["ok"] is False       # 只剩 1 期


# ===========================================================================
# 七、综合报告
# ===========================================================================
class TestReport:
    def test_both_methods(self):
        rows = _rows(_synthetic(inter_sd=0.2))
        r = msa.report(rows)
        assert r["ok"] is True
        assert r["average_range"]["ok"] and r["anova"]["ok"]
        assert r["consistency"]["delta_pct"] >= 0
        assert r["verdict"]["level"] in ("accept", "conditional", "reject")
        assert r["recommendations"]

    def test_worst_verdict_wins(self):
        """出厂结论取两种算法中较差的一个（宽进严出）。"""
        rows = _rows(_synthetic(appr_sd=0.6, repeat_sd=0.3))
        r = msa.report(rows)
        order = {"accept": 0, "conditional": 1, "reject": 2}
        worst = max(order[r["average_range"]["verdict"]["level"]],
                    order[r["anova"]["verdict"]["level"]])
        assert order[r["verdict"]["level"]] == worst

    def test_pct_tolerance(self):
        rows = _rows(_synthetic())
        r = msa.report(rows, tolerance=2.0)
        assert r["pct_tolerance"]["tolerance"] == 2.0
        # %Tolerance = GRR / (USL-LSL) × 100
        assert r["pct_tolerance"]["grr"] == pytest.approx(
            r["average_range"]["sigma"]["grr"] / 2.0 * 100, abs=0.01)

    def test_tolerance_omitted(self):
        assert msa.report(_rows(_synthetic()))["pct_tolerance"] is None

    def test_anova_can_be_skipped(self):
        r = msa.report(_rows(_synthetic()), run_anova=False)
        assert r["anova"].get("skipped") is True
        assert r["consistency"] is None
        assert "均值极差法" in r["verdict"]["basis"]

    def test_reject_advice_mentions_gauge(self):
        """不合格时建议必须落到动作上（换量具/夹具），不能只说"改进测量系统"。"""
        grid = {a: {"P%d" % i: [float(i) + (0.9 if a == "B" else 0) + 0.05 * j
                               for j in range(2)] for i in range(1, 4)}
                for a in ("A", "B")}
        r = msa.report(_rows(grid))
        text = " ".join(r["recommendations"])
        assert "量具" in text or "夹具" in text

    def test_invalid_data(self):
        r = msa.report([])
        assert r["ok"] is False
        assert r["message"]


class TestOverview:
    def test_lightweight(self):
        r = msa.overview(_rows(_synthetic()))
        assert r["ok"] is True
        assert "pct_grr" in r and "verdict" in r
        assert "anova" not in r              # 概览不算 ANOVA，首屏要快

    def test_invalid(self):
        assert msa.overview([])["ok"] is False


class TestVerdictThresholds:
    @pytest.mark.parametrize("pct,ndc,level", [
        (5.0, 10, "accept"),
        (10.0, 10, "conditional"),      # 边界：10% 不算 accept（AIAG 是 <10%）
        (25.0, 10, "conditional"),
        (30.0, 10, "conditional"),
        (31.0, 10, "reject"),
        (5.0, 4, "conditional"),        # 变差达标但分辨率不足
    ])
    def test_levels(self, pct, ndc, level):
        r = msa._verdict(pct * 10, 1000.0, ndc)
        assert r["level"] == level

    def test_ndc_flag(self):
        assert msa._verdict(1, 100, 5)["ndc_ok"] is True
        assert msa._verdict(1, 100, 4.9)["ndc_ok"] is False


class TestKTable:
    def test_lookup(self):
        assert msa._k(msa.K1_TABLE, 2, "n") == 0.8862
        assert msa._k(msa.K3_TABLE, 10, "k") == 0.3146

    def test_out_of_range(self):
        with pytest.raises(ValueError) as exc:
            msa._k(msa.K1_TABLE, 8, "试测次数")
        assert "试测次数=8" in str(exc.value)

    def test_table_monotonic(self):
        """常数应随样本量增大而减小（1/d2* 的性质），写反了会系统性高估 GRR。"""
        vals = [msa.K1_TABLE[n] for n in sorted(msa.K1_TABLE)]
        assert vals == sorted(vals, reverse=True)
