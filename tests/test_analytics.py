"""统计分析包的单测。

这个文件的特点：**不建库、不起服务、不 mock**。全部是"一组数进去、一组数出来"，
所以跑得飞快（< 0.1s），也就没有理由不写全。

断言口径统一为"手算能得到"：要么用公式反推（如 UCL == X̄ + A2·R̄），
要么用可以口算的例子（如 [1..10] 的 P95、[10,12] 的 Cpk）。凡是只能"大概对"
的断言（比如 PPM 估算），都在注释里写清误差来源，避免以后有人把它当成精确值去改。
"""

from __future__ import annotations

import math

import pytest

from quality_trace.core.analytics import capability as cap
from quality_trace.core.analytics import descriptive as d
from quality_trace.core.analytics import pareto as par
from quality_trace.core.analytics import spc
from quality_trace.core.analytics import trend as tr


# ---------------------------------------------------------------------------
# 描述统计
# ---------------------------------------------------------------------------
class TestDescriptive:
    def test_clean_drops_none_nan_and_strings(self):
        assert d.clean([1, None, float("nan"), "3", "", 2.5]) == [1.0, 3.0, 2.5]

    def test_clean_empty(self):
        assert d.clean(None) == []
        assert d.clean([]) == []

    def test_mean_of_empty_is_none(self):
        """空数据必须是 None，不能是 0——0 是合法均值（当天零不良）。"""
        assert d.mean([]) is None
        assert d.mean([0, 0, 0]) == 0.0

    def test_median_odd_and_even(self):
        assert d.median([3, 1, 2]) == 2
        assert d.median([4, 1, 3, 2]) == 2.5

    def test_variance_sample_vs_population(self):
        data = [2, 4, 4, 4, 5, 5, 7, 9]
        # 总体方差 = 4，样本方差 = 32/7 ≈ 4.571
        assert d.variance(data, sample=False) == pytest.approx(4.0)
        assert d.variance(data, sample=True) == pytest.approx(32 / 7)

    def test_variance_single_point_sample_is_none(self):
        """n=1 时自由度不足，返回 None 而不是 0——0 会让控制限塌成一条线。"""
        assert d.variance([5.0], sample=True) is None
        assert d.variance([5.0], sample=False) == 0.0

    def test_quantile_matches_excel_inc(self):
        data = list(range(1, 11))  # 1..10
        assert d.quantile(data, 0.5) == pytest.approx(5.5)
        assert d.quantile(data, 0.25) == pytest.approx(3.25)
        assert d.quantile(data, 0.95) == pytest.approx(9.55)
        # 边界
        assert d.quantile(data, 0) == 1
        assert d.quantile(data, 1) == 10

    def test_quantile_single_value(self):
        assert d.quantile([7], 0.95) == 7

    def test_iqr(self):
        assert d.iqr([1, 2, 3, 4, 5]) == pytest.approx(2.0)

    def test_outliers_tukey(self):
        data = [10, 10, 11, 10, 11, 10, 10, 11, 100]
        assert 100 in d.outliers(data)
        assert 11 not in d.outliers(data)

    def test_zscores_keeps_length(self):
        z = d.zscores([1, 2, 3])
        assert len(z) == 3
        assert z[1] == pytest.approx(0.0)

    def test_zscores_constant_series(self):
        assert d.zscores([5, 5, 5]) == [None, None, None]

    def test_histogram_counts_sum_to_n(self):
        h = d.histogram([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], bins=5)
        assert sum(b["count"] for b in h["bins"]) == 10
        assert h["bin_width"] == pytest.approx(1.8)

    def test_histogram_all_same_value(self):
        h = d.histogram([3, 3, 3])
        assert h["bin_width"] == 0.0
        assert h["bins"][0]["count"] == 3

    def test_skewness_symmetric_is_zero(self):
        assert d.skewness([1, 2, 3, 4, 5]) == pytest.approx(0.0, abs=1e-9)

    def test_kurtosis_needs_four_points(self):
        assert d.kurtosis([1, 2, 3]) is None

    def test_moving_average_is_lagged(self):
        """前 window-1 个必须是 None：车间看的是"最近 3 天"，不能用未来数据。"""
        ma = d.moving_average([1, 2, 3, 4, 5], 3)
        assert ma[:2] == [None, None]
        assert ma[2] == pytest.approx(2.0)
        assert ma[4] == pytest.approx(4.0)

    def test_ewma_first_point_is_raw(self):
        e = d.ewma([10, 20], alpha=0.5)
        assert e[0] == 10
        assert e[1] == pytest.approx(15.0)

    def test_linear_fit_perfect_line(self):
        fit = d.linear_fit([2, 4, 6, 8])
        assert fit["slope"] == pytest.approx(2.0)
        assert fit["intercept"] == pytest.approx(2.0)
        assert fit["r2"] == pytest.approx(1.0)

    def test_linear_fit_flat_series(self):
        """全平的序列：斜率为 0，但 ss_tot=0 使 R² 无定义 → None（不是 1.0）。"""
        fit = d.linear_fit([5, 5, 5, 5])
        assert fit["slope"] == pytest.approx(0.0)
        assert fit["r2"] is None
        assert fit["perfect_fit"] is False

    def test_describe_change_zero_base(self):
        """上期为 0：百分比不存在，必须是 None，不能写 100%。"""
        r = d.describe_change(3, 0)
        assert r["pct"] is None
        assert r["direction"] == "up"

    def test_describe_change_both_zero(self):
        r = d.describe_change(0, 0)
        assert r["pct"] == 0.0
        assert r["direction"] == "flat"

    def test_summary_shape(self):
        s = d.summary([1, 2, 3])
        assert s["n"] == 3
        assert s["median"] == 2


# ---------------------------------------------------------------------------
# 控制图
# ---------------------------------------------------------------------------
class TestNelsonRules:
    def test_rule1_single_outlier(self):
        series = [10.0] * 10 + [20.0]
        viol = spc.nelson_rules(series, center=10.0, sigma=1.0, rules=[1])
        assert len(viol) == 1
        assert viol[0]["rule"] == 1
        assert viol[0]["index"] == 10

    def test_rule2_nine_same_side(self):
        series = [0.5] * 9 + [0.0]
        viol = spc.nelson_rules(series, center=0.0, sigma=1.0, rules=[2])
        assert any(v["rule"] == 2 for v in viol)

    def test_rule2_eight_is_not_enough(self):
        series = [0.5] * 8 + [0.0]
        assert not [v for v in spc.nelson_rules(series, 0.0, 1.0, rules=[2]) if v["rule"] == 2]

    def test_rule3_six_increasing(self):
        series = [1, 2, 3, 4, 5, 6, 0]
        viol = spc.nelson_rules(series, 3.0, 1.0, rules=[3])
        assert any(v["rule"] == 3 for v in viol)

    def test_rule3_reports_once_per_run(self):
        """单调段只报一次，不能 6 点连线报 3 次（否则前端会叠三个红圈）。"""
        series = [1, 2, 3, 4, 5, 6, 7, 8]
        viol = [v for v in spc.nelson_rules(series, 4.0, 5.0, rules=[3]) if v["rule"] == 3]
        assert len(viol) == 1

    def test_rule5_two_of_three_beyond_2sigma(self):
        series = [0, 2.5, 2.6, 0]
        viol = spc.nelson_rules(series, 0.0, 1.0, rules=[5])
        assert any(v["rule"] == 5 for v in viol)

    def test_rule6_four_of_five_beyond_1sigma(self):
        series = [1.5, 1.6, 1.4, 1.7, 0.0]
        viol = spc.nelson_rules(series, 0.0, 1.0, rules=[6])
        assert any(v["rule"] == 6 for v in viol)

    def test_rule7_fifteen_inside_one_sigma(self):
        series = [0.1, -0.1] * 8
        viol = spc.nelson_rules(series, 0.0, 1.0, rules=[7])
        assert any(v["rule"] == 7 for v in viol)

    def test_rule8_eight_outside_one_sigma(self):
        series = [2.0] * 8
        viol = spc.nelson_rules(series, 0.0, 1.0, rules=[8])
        assert any(v["rule"] == 8 for v in viol)

    def test_none_breaks_the_run(self):
        """None 必须切断连续段，不能当成 0 参与判断。"""
        series = [0.5] * 4 + [None] + [0.5] * 4
        viol = spc.nelson_rules(series, 0.0, 1.0, rules=[2])
        assert viol == []

    def test_zero_sigma_returns_empty(self):
        """σ=0 时不判异（而不是除零崩掉）。"""
        assert spc.nelson_rules([1, 2, 3], 2.0, 0.0) == []

    def test_rule_texts_cover_eight(self):
        assert sorted(spc.RULE_TEXTS) == [1, 2, 3, 4, 5, 6, 7, 8]


class TestXbarR:
    def _groups(self):
        return [
            [10.0, 10.2, 9.8, 10.1, 9.9],
            [10.1, 9.9, 10.0, 10.2, 9.8],
            [9.9, 10.0, 10.1, 9.9, 10.1],
            [10.0, 10.0, 10.0, 10.0, 10.0],
            [10.2, 10.1, 9.9, 10.0, 9.8],
        ]

    def test_limits_follow_constants(self):
        r = spc.xbar_r_chart(self._groups())
        a2 = spc.A2_TABLE[5]
        assert r["x"]["ucl"] == pytest.approx(r["x"]["center"] + a2 * r["r"]["center"], abs=1e-4)
        assert r["x"]["lcl"] == pytest.approx(r["x"]["center"] - a2 * r["r"]["center"], abs=1e-4)

    def test_r_chart_lower_limit_never_negative(self):
        r = spc.xbar_r_chart(self._groups())
        assert r["r"]["lcl"] >= 0

    def test_subgroup_size_recorded(self):
        assert spc.xbar_r_chart(self._groups())["subgroup_size"] == 5

    def test_mixed_subgroup_sizes_rejected(self):
        """子组大小混用是数据错误，必须报错而不是硬算。"""
        with pytest.raises(ValueError):
            spc.xbar_r_chart([[1, 2, 3], [1, 2, 3, 4]])

    def test_single_point_subgroup_points_to_imr(self):
        with pytest.raises(ValueError) as ei:
            spc.xbar_r_chart([[1], [2]])
        assert "I-MR" in str(ei.value)

    def test_empty_returns_empty_chart(self):
        r = spc.xbar_r_chart([])
        assert r["empty"] is True
        assert "没有有效子组" in r["message"]

    def test_verdict_mentions_r_first(self):
        groups = [[10, 10, 10, 10, 10]] * 5 + [[10, 20, 10, 10, 10]]
        r = spc.xbar_r_chart(groups)
        assert "极差图失控" in r["message"]

    def test_sigma_within_positive(self):
        assert spc.xbar_r_chart(self._groups())["sigma_within"] > 0


class TestIndividualMR:
    def test_basic_limits(self):
        r = spc.individual_mr_chart([10, 10.5, 9.8, 10.2, 10.1])
        assert r["x"]["ucl"] > r["x"]["center"] > r["x"]["lcl"]
        assert r["mr"]["lcl"] == 0.0

    def test_needs_two_points(self):
        r = spc.individual_mr_chart([10])
        assert r["empty"] is True

    def test_pair_outlier_detected(self):
        r = spc.individual_mr_chart([10] * 8 + [25])
        assert r["x"]["out_of_control"]


class TestPChart:
    def test_limits_vary_with_sample_size(self):
        """样本量不同的点，控制限必须不同——这是 P 图的核心。"""
        r = spc.p_chart([1, 5], [50, 500])
        assert r["ucl"][0] > r["ucl"][1]

    def test_length_mismatch_rejected(self):
        with pytest.raises(ValueError):
            spc.p_chart([1, 2], [100])

    def test_zero_sample_skipped(self):
        r = spc.p_chart([1, 2], [0, 100])
        assert len(r["points"]) == 1

    def test_pbar_is_total_based(self):
        r = spc.p_chart([5, 5], [100, 100])
        assert r["pbar"] == pytest.approx(0.05)


class TestUChart:
    def test_unit_defects(self):
        r = spc.u_chart([3, 4, 2], [10, 10, 10])
        assert r["ubar"] == pytest.approx(0.3)

    def test_length_mismatch(self):
        with pytest.raises(ValueError):
            spc.u_chart([1], [1, 2])


# ---------------------------------------------------------------------------
# 过程能力
# ---------------------------------------------------------------------------
class TestCapability:
    def test_cp_formula(self):
        assert cap.cp(12, 8, 0.5) == pytest.approx(4 / 3)

    def test_cpk_centered_equals_cp(self):
        assert cap.cpk(12, 8, 10, 0.5) == pytest.approx(cap.cp(12, 8, 0.5))

    def test_cpk_off_center_smaller(self):
        assert cap.cpk(12, 8, 11, 0.5) == pytest.approx(0.6667, abs=1e-3)

    def test_zero_sigma_returns_none(self):
        """σ=0 时 Cpk 会是无穷大；返回 None 并提示量具问题，比显示 ∞ 诚实。"""
        assert cap.cp(12, 8, 0.0) is None
        assert cap.cpk(12, 8, 10, 0.0) is None

    def test_ca_measures_offset(self):
        assert cap.ca(12, 8, 10) == pytest.approx(0.0)
        assert cap.ca(12, 8, 11) == pytest.approx(0.5)

    def test_cr_is_inverse_cp(self):
        assert cap.cr(12, 8, 0.5) == pytest.approx(1 / cap.cp(12, 8, 0.5))

    def test_ppm_at_center(self):
        """μ 居中、6σ 覆盖规格时，PPM 应远小于 1。"""
        assert cap.ppm(112, 88, 100, 2.0) < 1

    def test_ppm_one_sided_shift(self):
        """中心偏 3σ 时两侧各 1350 PPM，合计约 2700 PPM（正态假设）。"""
        v = cap.ppm(106, 94, 100, 2.0)
        assert 2600 < v < 2800

    def test_normal_cdf_bounds(self):
        assert cap._normal_cdf(-10) == 0.0
        assert cap._normal_cdf(10) == 1.0
        assert cap._normal_cdf(0) == pytest.approx(0.5, abs=1e-6)

    def test_assess_grade_a(self):
        values = [10.0, 10.1, 9.9, 10.05, 9.95, 10.0, 10.02, 9.98]
        r = cap.assess(values, usl=11, lsl=9, sigma_within=0.15, controlled=True)
        assert r["ok"] is True
        assert r["grade"] in ("A", "A+")
        assert r["verdict"] == "capable"

    def test_assess_unstable_downgrades(self):
        values = [10.0, 10.1, 9.9, 10.05, 9.95]
        r = cap.assess(values, usl=11, lsl=9, sigma_within=0.15, controlled=False)
        assert r["verdict"] == "unstable"
        assert "不受控" in r["conclusion"]

    def test_assess_missing_sigma_warns(self):
        r = cap.assess([10, 10.1, 9.9], 11, 9)
        assert any("组内标准差" in w for w in r["warnings"])

    def test_assess_bad_spec(self):
        r = cap.assess([10], usl=8, lsl=12)
        assert r["ok"] is False

    def test_assess_empty(self):
        assert cap.assess([], 11, 9)["ok"] is False

    def test_improve_hint_offset_vs_spread(self):
        """偏心导致的损失应提示"调机"，而不是"改工艺"。"""
        values = [10.9, 10.95, 10.85, 10.9, 11.0]
        r = cap.assess(values, usl=12, lsl=8, sigma_within=0.1, controlled=True)
        assert "调机" in (r["improve_hint"] or "")

    def test_grade_table_descending(self):
        assert cap.grade(2.0)["grade"] == "A+"
        assert cap.grade(1.4)["grade"] == "A"
        assert cap.grade(1.1)["grade"] == "B"
        assert cap.grade(0.8)["grade"] == "C"
        assert cap.grade(0.3)["grade"] == "D"
        assert cap.grade(None)["grade"] == "—"

    def test_capability_matrix_sorts_worst_first(self):
        groups = [
            {"key": "LINE-A", "values": [10, 10.01, 9.99, 10, 10], "usl": 11, "lsl": 9, "sigma_within": 0.05},
            {"key": "LINE-B", "values": [10, 10.5, 9.5, 10, 10], "usl": 11, "lsl": 9, "sigma_within": 0.4},
        ]
        m = cap.capability_matrix(groups)
        assert m["rows"][0]["key"] == "LINE-B"
        assert m["worst"] == "LINE-B"


# ---------------------------------------------------------------------------
# 帕累托
# ---------------------------------------------------------------------------
class TestPareto:
    def _items(self):
        return [
            {"key": "尺寸超差", "value": 50},
            {"key": "外观划伤", "value": 30},
            {"key": "装配干涉", "value": 10},
            {"key": "标识错误", "value": 5},
            {"key": "其他", "value": 5},
        ]

    def test_sorted_desc(self):
        r = par.build(self._items())
        assert [row["key"] for row in r["rows"]][:2] == ["尺寸超差", "外观划伤"]

    def test_cumulative_ratio_ends_at_one(self):
        r = par.build(self._items())
        assert r["rows"][-1]["cum_ratio"] == pytest.approx(1.0)

    def test_key_items_cover_80_percent(self):
        r = par.build(self._items())
        # 50% + 30% = 80%，前两项即 A 类
        assert r["key_count"] == 2
        assert r["key_ratio"] == pytest.approx(0.8)

    def test_top_n_keeps_cumulative_scope(self):
        """Top 2 展示时，累计比例仍按全量算，不能按展示的子集算。"""
        r = par.build(self._items(), top_n=2)
        assert r["rows"][1]["cum_ratio"] == pytest.approx(0.8)
        assert r["rows"][-1]["key"].startswith("其他（")

    def test_weight_label_passed_through(self):
        assert par.build(self._items(), weight_label="金额")["weight_label"] == "金额"

    def test_zero_and_negative_filtered(self):
        r = par.build([{"key": "a", "value": 0}, {"key": "b", "value": -1}])
        assert r["empty"] is True

    def test_empty(self):
        assert par.build([])["empty"] is True

    def test_ties_stay_in_same_class(self):
        """并列项不能被序号切开。"""
        items = [{"key": "a", "value": 40}, {"key": "b", "value": 40}, {"key": "c", "value": 20}]
        r = par.build(items)
        ab = [row["abc"] for row in r["rows"] if row["key"] in ("a", "b")]
        assert ab[0] == ab[1]

    def test_compare_detects_new_type(self):
        cur = [{"key": "a", "value": 10}, {"key": "new", "value": 5}]
        prev = [{"key": "a", "value": 10}]
        r = par.compare(cur, prev)
        assert r["new_items"][0]["key"] == "new"

    def test_compare_worse_sorted_by_delta(self):
        cur = [{"key": "a", "value": 30}, {"key": "b", "value": 12}]
        prev = [{"key": "a", "value": 10}, {"key": "b", "value": 11}]
        r = par.compare(cur, prev)
        assert r["worse"][0]["key"] == "a"


# ---------------------------------------------------------------------------
# 趋势
# ---------------------------------------------------------------------------
class TestTrend:
    def test_parse_date_variants(self):
        assert tr.parse_date("2026-11-05").isoformat() == "2026-11-05"
        assert tr.parse_date("2026/11/05 08:00:00").isoformat() == "2026-11-05"
        assert tr.parse_date("2026-11-05T08:12:30+08:00").isoformat() == "2026-11-05"
        assert tr.parse_date("") is None
        assert tr.parse_date("不是日期") is None

    def test_bucket_week_starts_monday(self):
        # 2026-11-05 是周四 → 归到 2026-11-02（周一）
        assert tr.bucket_of(tr.parse_date("2026-11-05"), "week") == "2026-11-02"

    def test_bucket_month(self):
        assert tr.bucket_of(tr.parse_date("2026-11-05"), "month") == "2026-11"

    def test_series_fills_missing_days(self):
        recs = [
            {"occurred_at": "2026-11-01", "value": 3},
            {"occurred_at": "2026-11-03", "value": 5},
        ]
        s = tr.series(recs, granularity="day")
        assert len(s["buckets"]) == 3
        assert s["buckets"][1]["missing"] is True
        assert s["buckets"][1]["value"] == 0

    def test_series_no_fill(self):
        recs = [{"occurred_at": "2026-11-01", "value": 3}, {"occurred_at": "2026-11-03", "value": 5}]
        s = tr.series(recs, granularity="day", fill=False)
        assert len(s["buckets"]) == 2

    def test_series_empty(self):
        assert tr.series([])["empty"] is True

    def test_rate_series_none_when_no_production(self):
        """没生产的那天不良率是 None，不是 0。"""
        defects = [{"occurred_at": "2026-11-01", "qty": 2}]
        prod = [{"occurred_at": "2026-11-01", "planned_qty": 100}]
        r = tr.rate_series(defects, prod)
        assert r["buckets"][0]["rate"] == pytest.approx(0.02)
        assert r["buckets"][0]["ppm"] == pytest.approx(20000.0)

    def test_rate_series_zero_denominator(self):
        r = tr.rate_series([{"occurred_at": "2026-11-02", "qty": 1}], [])
        assert r["buckets"][0]["rate"] is None

    def test_analyze_upward_trend(self):
        recs = [
            {"occurred_at": "2026-11-%02d" % i, "value": i * 10} for i in range(1, 11)
        ]
        a = tr.analyze(recs)
        assert a["direction"] == "up"
        assert a["significant"] is True
        assert "上升" in a["message"]

    def test_analyze_insufficient(self):
        assert tr.analyze([{"occurred_at": "2026-11-01", "value": 1}])["direction"] == "insufficient"

    def test_weekday_profile(self):
        recs = [
            {"occurred_at": "2026-11-02", "value": 10},  # 周一
            {"occurred_at": "2026-11-03", "value": 1},   # 周二
        ]
        p = tr.weekday_profile(recs)
        assert p["peak"] == "周一"
        assert p["rows"][0]["value"] == 10

    def test_add_month_clamps_day(self):
        """1 月 31 日加一个月应得到 2 月 28/29 日，而不是抛异常。"""
        assert tr._add_month(tr.parse_date("2026-01-31")).isoformat() == "2026-02-28"


class TestSigmaCaliber:
    """回归：σ 的口径（曾经多除一次 √n，Cpk 虚高 2.2 倍）。"""

    def test_sigma_within_is_piece_level(self):
        import random
        from statistics import pstdev

        rnd = random.Random(7)
        groups = [[10 + rnd.gauss(0, 0.02) for _ in range(5)] for _ in range(30)]
        chart = spc.xbar_r_chart(groups)
        sd = pstdev([v for g in groups for v in g])
        # R̄/d2 是 σ 的无偏估计，与总体标准差应当在 ±25% 内一致；
        # 若误写成 R̄/(d2·√n)，会掉到 sd 的 1/2.24，这里立刻失败
        assert 0.75 * sd < chart["sigma_within"] < 1.25 * sd

    def test_sigma_xbar_is_sigma_over_sqrt_n(self):
        groups = [[10, 10.1, 9.9, 10.05, 9.95]] * 6
        chart = spc.xbar_r_chart(groups)
        assert chart["x"]["sigma"] == pytest.approx(chart["sigma_within"] / math.sqrt(5), abs=1e-6)

    def test_cpk_uses_piece_sigma(self):
        """Cpk 的 σ 必须是单件 σ；用错口径时 Cpk 会正好差 √n 倍。"""
        import random
        from statistics import pstdev

        rnd = random.Random(11)
        groups = [[10 + rnd.gauss(0, 0.02) for _ in range(5)] for _ in range(40)]
        chart = spc.xbar_r_chart(groups)
        values = [v for g in groups for v in g]
        res = cap.assess(values, usl=10.1, lsl=9.9, sigma_within=chart["sigma_within"])
        expect = (10.1 - 9.9) / (6 * pstdev(values))
        assert res["cp"] == pytest.approx(expect, rel=0.3)
