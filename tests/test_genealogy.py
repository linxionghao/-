"""批次谱系追溯单测：正查、反查、嫌疑排序、影响面判定。

这一组测试的重点不是"函数能跑通"，而是**边界语义**：

- 没有批次登记时，反查必须返回"查不到 + 原因"，而不是空列表让人猜；
- 同一批料重复上报，影响面不能翻倍（召回范围翻倍是要花钱的）；
- 只有 1 张工单的批次，不能进入嫌疑排序（样本不足会误导定责）。

这三条任何一条错了，追溯功能就从"决策依据"退化成"装饰"。
"""

from __future__ import annotations

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services import genealogy as svc


@pytest.fixture()
def env(tmp_path):
    """两条产线、4 张工单、3 批料，其中一批专供"高不良"工单。"""
    conn = repo.connect(str(tmp_path / "ge.db"))
    repo.init_schema(conn)

    for i in range(1, 5):
        repo.insert_work_order(conn, "WO-%02d" % i, "六角螺栓", "line-01", 1000, occurred_at="2026-11-0%d" % i)

    # 三条批次，供应商不同
    repo.insert_material_lot(conn, "LOT-A", "MAT-45", "45# 圆钢", supplier="SUP-甲",
                             supplier_lot="S1", qty=3000, occurred_at="2026-11-01")
    repo.insert_material_lot(conn, "LOT-B", "MAT-45", "45# 圆钢", supplier="SUP-乙",
                             supplier_lot="S2", qty=3000, occurred_at="2026-11-02")
    repo.insert_material_lot(conn, "LOT-C", "MAT-70", "70# 高碳钢", supplier="SUP-丙",
                             supplier_lot="S3", qty=1000, occurred_at="2026-11-03")

    # LOT-A 用在 WO-01/02/03 —— 不良多；LOT-B 用在 WO-03/04 —— 不良少
    repo.link_lot_to_work_order(conn, "LOT-A", "WO-01", qty_used=500)
    repo.link_lot_to_work_order(conn, "LOT-A", "WO-02", qty_used=500)
    repo.link_lot_to_work_order(conn, "LOT-A", "WO-03", qty_used=500)
    repo.link_lot_to_work_order(conn, "LOT-B", "WO-03", qty_used=400)
    repo.link_lot_to_work_order(conn, "LOT-B", "WO-04", qty_used=400)
    repo.link_lot_to_work_order(conn, "LOT-C", "WO-04", qty_used=100)

    # 缺陷：WO-01/02/03 各有较多不良；WO-04 只有 1 件
    n = 0
    for wo, qty in (("WO-01", 40), ("WO-02", 30), ("WO-03", 20), ("WO-04", 1)):
        n += 1
        repo.insert_defect(
            conn, "DF-%02d" % n, wo, "line-01", "螺纹外径偏小",
            qty=qty, defect_code="D-THREAD", status="pending",
        )
    return {"conn": conn, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# 物料批与领料
# ---------------------------------------------------------------------------
class TestLotRepo:
    def test_insert_idempotent(self, env):
        again = repo.insert_material_lot(
            env["conn"], "LOT-A", "MAT-45", "另一个名字", supplier="别人"
        )
        assert again["material_name"] == "45# 圆钢"      # 已存在则原样返回
        assert again["supplier"] == "SUP-甲"

    def test_link_idempotent_accumulates_qty(self, env):
        """重复上报只能累加用量，不能多出一条关系（否则影响面翻倍）。"""
        row = repo.link_lot_to_work_order(env["conn"], "LOT-A", "WO-01", qty_used=100)
        assert row["inserted"] is False
        assert row["qty_used"] == pytest.approx(600.0)
        orders = repo.list_work_orders_of_lot(env["conn"], "LOT-A")
        assert len(orders) == 3

    def test_list_lots_of_work_order_returns_supplier(self, env):
        lots = repo.list_lots_of_work_order(env["conn"], "WO-03")
        assert {l["lot_id"] for l in lots} == {"LOT-A", "LOT-B"}
        assert {l["supplier"] for l in lots} == {"SUP-甲", "SUP-乙"}

    def test_filters(self, env):
        assert [l["lot_id"] for l in repo.list_material_lots(env["conn"], supplier="SUP-丙")] == ["LOT-C"]
        assert repo.count_lots(env["conn"]) == 3
        assert repo.list_suppliers(env["conn"]) == ["SUP-丙", "SUP-乙", "SUP-甲"]


# ---------------------------------------------------------------------------
# 正查
# ---------------------------------------------------------------------------
class TestForwardTrace:
    def test_orders_and_summary(self, env):
        r = svc.forward_trace(env["conn"], "LOT-A")
        assert r["ok"] is True
        assert r["summary"]["orders"] == 3
        assert r["summary"]["produced"] == 3000
        assert r["summary"]["defect_pieces"] == 90
        assert r["summary"]["defect_rate"] == pytest.approx(0.03)

    def test_impact_level_high(self, env):
        """3 张工单 / 3000 件 → 高（越线即升级）。"""
        r = svc.forward_trace(env["conn"], "LOT-A")
        assert r["impact"]["level"] == "高"
        assert r["impact"]["need_recall"] is True

    def test_impact_level_low(self, env):
        """1 张工单 / 100 件：两个维度都没越线，判"低"。"""
        repo.insert_work_order(env["conn"], "WO-05", "试制单", "line-01", 100)
        repo.insert_material_lot(env["conn"], "LOT-S", "MAT-1", "试制料", qty=50)
        repo.link_lot_to_work_order(env["conn"], "LOT-S", "WO-05", qty_used=50)
        r = svc.forward_trace(env["conn"], "LOT-S")
        assert r["impact"]["level"] == "低"
        assert r["impact"]["need_recall"] is False

    def test_impact_upgrades_on_quantity(self, env):
        """只有 1 张工单，但产量 1000 件 > 500 → 升到"中"（取更严的一档）。"""
        r = svc.forward_trace(env["conn"], "LOT-C")
        assert r["impact"]["level"] == "中"

    def test_conclusion_mentions_supplier(self, env):
        r = svc.forward_trace(env["conn"], "LOT-A")
        assert "SUP-甲" in r["conclusion"]
        assert "高" in r["conclusion"]

    def test_lot_not_found(self, env):
        r = svc.forward_trace(env["conn"], "LOT-X")
        assert r["ok"] is False
        assert "不存在" in r["message"]

    def test_lot_without_usage(self, env):
        repo.insert_material_lot(env["conn"], "LOT-Z", "MAT-1", "新料", qty=10)
        r = svc.forward_trace(env["conn"], "LOT-Z")
        assert r["summary"]["orders"] == 0
        assert "还没有领用记录" in r["conclusion"]


# ---------------------------------------------------------------------------
# 反查
# ---------------------------------------------------------------------------
class TestBackwardTrace:
    def test_finds_lots_and_suppliers(self, env):
        r = svc.backward_trace(env["conn"], "DF-01")
        assert r["ok"] is True
        assert [l["lot_id"] for l in r["lots"]] == ["LOT-A"]
        assert r["suppliers"] == ["SUP-甲"]
        assert r["coverage"]["ok"] is True

    def test_no_lot_registration_gives_reason(self, env):
        """工单没登记批次时，必须说清"为什么查不到"。"""
        repo.insert_work_order(env["conn"], "WO-99", "裸单", "line-01", 100)
        repo.insert_defect(env["conn"], "DF-99", "WO-99", "line-01", "划伤", qty=1)
        r = svc.backward_trace(env["conn"], "DF-99")
        assert r["lots"] == []
        assert r["coverage"]["ok"] is False
        assert "没有物料批登记" in r["coverage"]["reason"]
        assert "无法追溯" in r["conclusion"]

    def test_defect_not_found(self, env):
        r = svc.backward_trace(env["conn"], "DF-404")
        assert r["ok"] is False

    def test_caveat_always_present(self, env):
        """相关性 ≠ 因果，这句话必须跟着结论一起返回。"""
        r = svc.backward_trace(env["conn"], "DF-01")
        assert "不等于因果" in r["caveat"]


# ---------------------------------------------------------------------------
# 嫌疑排序
# ---------------------------------------------------------------------------
class TestSuspects:
    def test_high_defect_lot_ranked_first(self, env):
        rows = svc.suspect_lots(env["conn"], "WO-03")
        assert rows[0]["lot_id"] == "LOT-A"
        assert rows[0]["defect_rate"] > rows[1]["defect_rate"]

    def test_single_order_lot_excluded(self, env):
        """只有 1 张工单的批次不参与排序，放在 low_sample 里。"""
        rows = svc.suspect_lots(env["conn"], "WO-03")
        ranked = [r for r in rows if "rank" in r]
        low = [r for r in rows if "note" in r]
        assert {r["lot_id"] for r in ranked} == {"LOT-A", "LOT-B"}
        assert low == []

    def test_low_sample_bucket(self, env):
        """只出现在一张工单上的批次，进 low_sample 而不是被丢掉。"""
        repo.insert_material_lot(env["conn"], "LOT-D", "MAT-9", "单次料", qty=1)
        repo.link_lot_to_work_order(env["conn"], "LOT-D", "WO-04", qty_used=1)
        rows = svc.suspect_lots(env["conn"], "WO-04")
        low = [r for r in rows if "note" in r]
        # LOT-C 与 LOT-D 都只出现在这一张工单上，都应进低样本区
        assert {r["lot_id"] for r in low} == {"LOT-C", "LOT-D"}
        assert all("样本不足" in r["note"] for r in low)
        # 低样本不进排序区（排在第 1 位的仍是正常批次）
        assert rows[0]["lot_id"] not in {r["lot_id"] for r in low}


# ---------------------------------------------------------------------------
# 供应商记分卡
# ---------------------------------------------------------------------------
class TestScorecard:
    def test_rows_sorted_by_ppm(self, env):
        r = svc.supplier_scorecard(env["conn"], days=3650)
        assert r["empty"] is False
        ppm = [x["ppm"] or 0 for x in r["rows"]]
        assert ppm == sorted(ppm, reverse=True)

    def test_note_warns_about_attribution(self, env):
        r = svc.supplier_scorecard(env["conn"], days=3650)
        assert "不能直接作为索赔依据" in r["note"]

    def test_small_sample_flag(self, env):
        r = svc.supplier_scorecard(env["conn"], days=3650)
        flags = {x["supplier"]: x["small_sample"] for x in r["rows"]}
        # SUP-甲 有 3 张工单，不算小样本；SUP-丙 只有 1 张
        assert flags["SUP-甲"] is False
        assert flags["SUP-丙"] is True


# ---------------------------------------------------------------------------
# 谱系图与概览
# ---------------------------------------------------------------------------
class TestGraphAndOverview:
    def test_graph_edges_match_nodes(self, env):
        g = svc.build_graph(env["conn"], "LOT-A")
        ids = {n["id"] for n in g["nodes"]}
        for e in g["edges"]:
            assert e["source"] in ids and e["target"] in ids

    def test_graph_stats(self, env):
        g = svc.build_graph(env["conn"], "LOT-A")
        assert g["stats"]["lots"] == 1
        assert g["stats"]["orders"] == 3

    def test_graph_missing_lot(self, env):
        g = svc.build_graph(env["conn"], "LOT-X")
        assert g["ok"] is False
        assert g["nodes"] == []

    def test_overview_coverage(self, env):
        ov = svc.overview(env["conn"], days=3650)
        assert ov["lots"] == 3
        assert 0 < ov["coverage"] <= 1
        assert "覆盖率" in ov["coverage_note"]

    def test_overview_empty(self, tmp_path):
        conn = repo.connect(str(tmp_path / "x.db"))
        repo.init_schema(conn)
        ov = svc.overview(conn)
        assert ov["empty"] is True
        assert ov["coverage"] == 0.0
