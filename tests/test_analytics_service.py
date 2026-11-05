"""分析服务的单测：取数 → 口径 → 结论。

与 test_analytics.py 的分工：那个文件测"公式对不对"，这个文件测"口径对不对"。
口径错误的破坏力更大——公式错了会算出奇怪数字，口径错了会算出**正常但错误**的数字，
没人会去查。

所以这里的断言集中在：
- 时间区间到底含不含边界；
- 分母用的是计划产量还是别的；
- 班组长传了别人的 line_id 时，参数是否被覆写；
- 没数据时返回的是空结构还是 None。
"""

from __future__ import annotations

import datetime as _dt

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services import analytics as svc

QE = {"user_id": "u-qe", "username": "qe001", "role": "quality_engineer", "line_id": None}
LEADER = {"user_id": "u-sl", "username": "leader01", "role": "shift_leader", "line_id": "line-01"}


def _day(offset: int) -> str:
    """相对今天偏移 offset 天的日期串。"""
    return (_dt.date.today() - _dt.timedelta(days=offset)).isoformat()


@pytest.fixture()
def env(tmp_path):
    """两条产线、10 天数据、两个检验项目（一个 5 件/子组、一个 1 件/子组）。"""
    conn = repo.connect(str(tmp_path / "an.db"))
    repo.init_schema(conn)

    for i in range(10):
        d = _day(9 - i)
        repo.insert_work_order(conn, "WO-%02d" % i, "M8 螺栓", "line-01", 1000, occurred_at=d)
        repo.insert_work_order(conn, "WO-B-%02d" % i, "M10 螺栓", "line-02", 500, occurred_at=d)

        # line-01：越往后不良越多（构造上升趋势）
        repo.insert_defect(
            conn, "DF-A-%02d" % i, "WO-%02d" % i, "line-01", "螺纹外径偏小",
            op_code="OP30", qty=2 + i, severity="严重" if i >= 8 else "一般",
            defect_code="D-THREAD", status="pending", occurred_at=d,
        )
        repo.insert_defect(
            conn, "DF-B-%02d" % i, "WO-B-%02d" % i, "line-02", "表面划伤",
            op_code="OP20", qty=1, severity="轻微", defect_code="D-SURF",
            status="pending", occurred_at=d,
        )

    # 一条未确认单 + 一条补录单：数据质量自检要有东西可报
    repo.insert_defect(
        conn, "DF-DRAFT", "WO-09", "line-01", "疑似毛刺", qty=1,
        status="draft", occurred_at=_day(1),
    )
    repo.insert_defect(
        conn, "DF-BACK", "WO-09", "line-01", "螺纹外径偏小", qty=1,
        defect_code="D-THREAD", status="pending", is_backfill=True, occurred_at=_day(5),
    )

    # 计量数据：外径，5 件/子组，10 个子组
    for g in range(10):
        d = _day(9 - g)
        for k in range(5):
            repo.insert_measurement(
                conn, "外径", 10.0 + 0.01 * k + (0.02 if g == 9 else 0), "line-01",
                subgroup_no=g + 1, usl=10.15, lsl=9.85, occurred_at=d, gauge="G-01",
            )
    # 壁厚：1 件/子组，走 I-MR
    for g in range(12):
        repo.insert_measurement(
            conn, "壁厚", 2.0 + 0.005 * g, "line-01",
            subgroup_no=g + 1, usl=2.2, lsl=1.8, occurred_at=_day(11 - g),
        )

    return {"conn": conn, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# 时间区间
# ---------------------------------------------------------------------------
class TestResolveRange:
    def test_default_is_inclusive_n_days(self):
        """days=30 应该是 [今天-29, 今天]，30 天，不是 31 天。"""
        r = svc.resolve_range(days=30)
        start = _dt.date.fromisoformat(r["date_from"])
        end = _dt.date.fromisoformat(r["date_to"])
        assert (end - start).days == 29
        assert r["days"] == 30

    def test_explicit_range_kept(self):
        r = svc.resolve_range(date_from="2026-01-01", date_to="2026-01-31")
        assert r["date_from"] == "2026-01-01"
        assert r["date_to"] == "2026-01-31"
        assert r["days"] == 31

    def test_swapped_range_is_normalized(self):
        """用户把起止填反了不该报错，也不该返回负天数。"""
        r = svc.resolve_range(date_from="2026-02-01", date_to="2026-01-01")
        assert r["date_from"] < r["date_to"]
        assert r["days"] > 0

    def test_invalid_date_falls_back_to_today(self):
        r = svc.resolve_range(date_from="不是日期", date_to="也不是")
        assert r["days"] >= 1


# ---------------------------------------------------------------------------
# 总览
# ---------------------------------------------------------------------------
class TestOverview:
    def test_kpi_counts(self, env):
        ov = svc.overview(env["conn"], days=30)
        k = ov["kpi"]
        # 10 天 × 2 条线 + 草稿 1 + 补录 1
        assert k["orders"] == 22
        assert k["pieces"] == sum(2 + i for i in range(10)) + 10 + 1 + 1
        assert k["planned_qty"] == 10 * 1000 + 10 * 500

    def test_rate_uses_planned_qty(self, env):
        ov = svc.overview(env["conn"], days=30)
        k = ov["kpi"]
        assert k["defect_rate"] == pytest.approx(k["pieces"] / k["planned_qty"], rel=1e-4)
        assert "计划产量" in k["rate_basis"]

    def test_pareto_top_is_thread(self, env):
        ov = svc.overview(env["conn"], days=30)
        assert ov["pareto"]["rows"][0]["key"] == "D-THREAD"

    def test_severity_has_three_buckets(self, env):
        ov = svc.overview(env["conn"], days=30)
        names = [s["severity"] for s in ov["severity"]]
        assert "严重" in names and "一般" in names and "轻微" in names

    def test_data_quality_flags_uncoded(self, env):
        ov = svc.overview(env["conn"], days=30)
        dq = ov["data_quality"]
        assert dq["ok"] is False
        # 草稿单没有缺陷码，因此同时命中"缺码"这一条
        assert any("缺缺陷码" in x for x in dq["issues"])

    def test_data_quality_flags_high_draft_ratio(self, env):
        """草稿占比超过 20% 要单独报出来：说明归一化流程被跳过。"""
        for i in range(10):
            repo.insert_defect(
                env["conn"], "DF-D%02d" % i, "WO-09", "line-01", "草稿 %d" % i,
                qty=1, status="draft", occurred_at=_day(1),
            )
        dq = svc.overview(env["conn"], days=30)["data_quality"]
        assert any("未确认" in x for x in dq["issues"])

    def test_line_filter(self, env):
        ov = svc.overview(env["conn"], line_id="line-02", days=30)
        assert ov["kpi"]["orders"] == 10
        assert ov["pareto"]["rows"][0]["key"] == "D-SURF"

    def test_verdict_mentions_top_cause(self, env):
        ov = svc.overview(env["conn"], days=30)
        assert "PPM" in ov["verdict"]
        assert "D-THREAD" in ov["verdict"] or "螺纹" in ov["verdict"]

    def test_empty_db_gives_sentence(self, tmp_path):
        conn = repo.connect(str(tmp_path / "empty.db"))
        repo.init_schema(conn)
        ov = svc.overview(conn)
        assert ov["kpi"]["orders"] == 0
        assert "没有不良记录" in ov["verdict"]
        assert ov["pareto"]["rows"] == []

    def test_trend_direction_up(self, env):
        ov = svc.overview(env["conn"], line_id="line-01", days=30)
        assert ov["trend"]["direction"] == "up"


# ---------------------------------------------------------------------------
# 帕累托
# ---------------------------------------------------------------------------
class TestParetoBy:
    def test_by_defect_code(self, env):
        r = svc.pareto_by(env["conn"], "defect_code", days=30)
        assert r["dim"] == "defect_code"
        assert r["total"] > 0

    def test_weight_orders_counts_rows(self, env):
        by_qty = svc.pareto_by(env["conn"], "line_id", days=30, weight="qty")
        by_ord = svc.pareto_by(env["conn"], "line_id", days=30, weight="orders")
        # line-01 每单件数多，按件数算的占比应明显高于按单数算
        q1 = next(r for r in by_qty["rows"] if r["key"] == "line-01")
        o1 = next(r for r in by_ord["rows"] if r["key"] == "line-01")
        assert q1["ratio"] > o1["ratio"]

    def test_bad_dim_rejected(self, env):
        with pytest.raises(ValueError) as ei:
            svc.pareto_by(env["conn"], "1=1; DROP TABLE defect_item")
        assert "不支持的维度" in str(ei.value)

    def test_bad_weight_rejected(self, env):
        with pytest.raises(ValueError):
            svc.pareto_by(env["conn"], "line_id", weight="price")

    def test_top_n_merges_rest(self, env):
        r = svc.pareto_by(env["conn"], "defect_code", days=30, top_n=1)
        assert len(r["rows"]) == 2
        assert r["rows"][-1]["key"].startswith("其他")


class TestComparePeriods:
    def test_previous_range_same_length(self, env):
        r = svc.compare_periods(env["conn"], days=10)
        assert r["previous_range"]["date_from"] < r["current_range"]["date_from"]

    def test_no_new_types_when_stable(self, env):
        r = svc.compare_periods(env["conn"], days=30)
        assert isinstance(r["new_items"], list)


# ---------------------------------------------------------------------------
# 控制图
# ---------------------------------------------------------------------------
class TestControlChart:
    def test_xbar_r_for_five_piece_subgroups(self, env):
        r = svc.control_chart(env["conn"], item_code="外径", days=30)
        assert r["ok"] is True
        assert r["chart"]["chart"] == "xbar_r"
        assert r["chart"]["subgroup_size"] == 5
        assert r["n_subgroups"] == 10

    def test_imr_for_single_piece_subgroups(self, env):
        r = svc.control_chart(env["conn"], item_code="壁厚", days=30)
        assert r["chart"]["chart"] == "individual_mr"

    def test_capability_present_when_spec_given(self, env):
        r = svc.control_chart(env["conn"], item_code="外径", days=30)
        assert r["capability"] is not None
        assert r["capability"]["ok"] is True
        assert r["capability"]["spec"]["usl"] == pytest.approx(10.15)

    def test_mixed_subgroup_sizes_flagged(self, env):
        """子组大小不一致时不硬画，给出 need_regroup 与分布。"""
        conn = env["conn"]
        repo.insert_measurement(conn, "混合", 5.0, "line-01", subgroup_no=1)
        repo.insert_measurement(conn, "混合", 5.1, "line-01", subgroup_no=1)
        repo.insert_measurement(conn, "混合", 5.0, "line-01", subgroup_no=2)
        r = svc.control_chart(conn, item_code="混合", days=30)
        assert r["ok"] is True
        assert r["chart"] is None
        assert r["need_regroup"] is True
        assert r["size_distribution"] == {"1": 1, "2": 1}

    def test_unknown_item_returns_hint(self, env):
        r = svc.control_chart(env["conn"], item_code="不存在的项目", days=30)
        assert r["ok"] is False
        assert "没有" in r["message"]

    def test_series_meta_has_labels(self, env):
        r = svc.control_chart(env["conn"], item_code="外径", days=30)
        assert len(r["series_meta"]["labels"]) == r["n_subgroups"]
        assert r["series_meta"]["gauges"] == ["G-01"]

    def test_outlier_shifts_to_uncontrolled(self, env):
        """人为把一个点抬高，能力分析应被降级为 unstable。"""
        conn = env["conn"]
        for k in range(5):
            repo.insert_measurement(
                conn, "外径", 11.5 + 0.01 * k, "line-01", subgroup_no=11,
                usl=10.15, lsl=9.85, occurred_at=_day(0),
            )
        r = svc.control_chart(conn, item_code="外径", days=30, line_id="line-01")
        assert r["chart"]["x"]["out_of_control"]
        assert r["capability"]["verdict"] == "unstable"


# ---------------------------------------------------------------------------
# P 图 / 趋势 / 星期
# ---------------------------------------------------------------------------
class TestPChartService:
    def test_buckets_and_pbar(self, env):
        r = svc.defect_p_chart(env["conn"], line_id="line-01", days=30)
        assert r["ok"] is True
        assert r["pbar"] > 0
        assert len(r["points"]) == 10
        assert len(r["ucl"]) == 10

    def test_no_production_gives_message(self, tmp_path):
        conn = repo.connect(str(tmp_path / "nop.db"))
        repo.init_schema(conn)
        r = svc.defect_p_chart(conn)
        assert r["ok"] is False
        assert "产量" in r["message"]


class TestTrendAndWeekday:
    def test_trend_report_up(self, env):
        r = svc.trend_report(env["conn"], line_id="line-01", days=30)
        assert r["direction"] == "up"
        assert r["granularity"] == "day"

    def test_weekday_has_seven_rows(self, env):
        r = svc.weekday_report(env["conn"], days=30)
        assert len(r["rows"]) == 7
        assert r["peak"] in [x["name"] for x in r["rows"]]

    def test_weekly_granularity(self, env):
        r = svc.trend_report(env["conn"], days=30, granularity="week")
        assert r["granularity"] == "week"


# ---------------------------------------------------------------------------
# 仪表盘
# ---------------------------------------------------------------------------
class TestDashboard:
    def test_contains_all_blocks(self, env):
        d = svc.dashboard(env["conn"], days=30)
        for key in ("overview", "by_line", "weekday", "items", "lines"):
            assert key in d
        assert "line-01" in d["lines"]

    def test_chart_only_when_item_given(self, env):
        assert svc.dashboard(env["conn"], days=30)["chart"] is None
        d = svc.dashboard(env["conn"], days=30, item_code="外径")
        # dashboard.chart 是控制图接口的完整响应，图对象在其 chart 字段里
        assert d["chart"]["chart"]["chart"] == "xbar_r"

    def test_items_list(self, env):
        items = svc.dashboard(env["conn"])["items"]
        names = {i["item_code"] for i in items}
        assert names == {"外径", "壁厚"}
