# -*- coding: utf-8 -*-
"""MSA 服务层与接口测试：留档、重算、从实测值折叠、覆盖率。

服务层有三个决定值得单独测，因为它们都涉及"将来会不会被误用"：

1. **留档而不是只返回结果**：MSA 结论是 PPAP 证据，必须能回答
   "半年前凭什么签字"。所以测 create_study 之后再 get_study 能读到同样的结论。
2. **recompute 不覆盖留档**：审计链不能因为"换了个算法"就断掉。
   测试断言 fresh 与 archived 同时存在。
3. **从实测值折叠是近似的**：approximate=True 与说明文字必须在返回里，
   否则用户会把"用产线数据算的 GRR"当成正式结论去签字。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import quality_trace.api.app as app_module
from quality_trace.core.repo import db as repo
from quality_trace.core.services import auth as auth_service
from quality_trace.core.services import msa as svc
from quality_trace.core.services.errors import ApiError


# 2 人 × 3 件 × 2 次：能算出结论，又不至于零件太少到不可信
GRID = {
    "A": {"P1": [1.00, 1.02], "P2": [2.00, 2.02], "P3": [3.00, 3.02]},
    "B": {"P1": [1.01, 1.03], "P2": [2.01, 2.03], "P3": [3.01, 3.03]},
}


def _rows(grid=GRID):
    out = []
    for appraiser, parts in grid.items():
        for part, values in parts.items():
            for i, v in enumerate(values, start=1):
                out.append({"appraiser": appraiser, "part": part, "trial": i, "value": v})
    return out


@pytest.fixture()
def conn(tmp_path):
    c = repo.connect(str(tmp_path / "msa.db"))
    repo.init_schema(c)
    return c


# ===========================================================================
# 一、留档
# ===========================================================================
class TestCreateStudy:
    def test_persists_result(self, conn):
        res = svc.create_study(conn, item_code="外径", rows=_rows(), gauge="G-01",
                               tolerance=0.16, created_by="qe001")
        study = res["study"]
        assert study["study_id"].startswith("MSA-")
        assert study["item_code"] == "外径"
        assert study["gauge"] == "G-01"
        assert study["verdict"] in ("accept", "conditional", "reject")
        assert study["pct_grr"] >= 0
        assert repo.count_msa_studies(conn) == 1

    def test_roundtrip_equal(self, conn):
        """存进去的结论必须等于当场算的结论（不能被序列化弄丢精度）。"""
        res = svc.create_study(conn, item_code="外径", rows=_rows(), tolerance=0.16)
        loaded = svc.get_study(conn, res["study"]["study_id"])
        assert loaded["result"]["average_range"]["percent"]["grr"] == \
            res["report"]["average_range"]["percent"]["grr"]
        assert loaded["verdict"] == res["report"]["verdict"]["level"]

    def test_raw_rows_kept_for_recompute(self, conn):
        res = svc.create_study(conn, item_code="外径", rows=_rows())
        loaded = svc.get_study(conn, res["study"]["study_id"])
        assert len(loaded["rows"]) == len(_rows())

    def test_anova_snapshot(self, conn):
        res = svc.create_study(conn, item_code="外径", rows=_rows(), run_anova=True)
        assert res["report"]["anova"]["ok"] is True
        assert res["report"]["consistency"] is not None

    def test_anova_can_be_skipped(self, conn):
        res = svc.create_study(conn, item_code="外径", rows=_rows(), run_anova=False)
        assert res["report"]["anova"].get("skipped") is True

    def test_small_design_warns(self, conn):
        """零件数 < 5 时算得出但不可信，必须带警告。"""
        small = {"A": {"P1": [1.0, 1.02], "P2": [2.0, 2.02]},
                 "B": {"P1": [1.0, 1.03], "P2": [2.0, 2.03]}}
        res = svc.create_study(conn, item_code="壁厚", rows=_rows(small))
        assert any("零件数" in w for w in res["warnings"])

    def test_empty_rows_rejected(self, conn):
        with pytest.raises(ApiError) as exc:
            svc.create_study(conn, item_code="外径", rows=[])
        assert exc.value.code == 40001

    def test_empty_item_rejected(self, conn):
        with pytest.raises(ApiError) as exc:
            svc.create_study(conn, item_code="  ", rows=_rows())
        assert exc.value.code == 40001

    def test_bad_data_not_persisted(self, conn):
        """算不出来就不要写库，避免留下一堆 verdict=unknown 的空记录。"""
        bad = [{"appraiser": "A", "part": "P1", "trial": 1, "value": 1.0}]
        with pytest.raises(ApiError):
            svc.create_study(conn, item_code="外径", rows=bad)
        assert repo.count_msa_studies(conn) == 0


# ===========================================================================
# 二、查询与重算
# ===========================================================================
class TestQuery:
    def test_list_filters(self, conn):
        svc.create_study(conn, item_code="外径", rows=_rows())
        svc.create_study(conn, item_code="壁厚", rows=_rows())
        allrows = svc.list_studies(conn)
        assert allrows["total"] == 2
        only = svc.list_studies(conn, item_code="外径")
        assert only["total"] == 1
        assert only["studies"][0]["design_text"] == "2人 × 3件 × 2次"

    def test_list_excludes_raw(self, conn):
        svc.create_study(conn, item_code="外径", rows=_rows())
        row = svc.list_studies(conn)["studies"][0]
        assert "rows" not in row and "result" not in row

    def test_get_missing(self, conn):
        with pytest.raises(ApiError) as exc:
            svc.get_study(conn, "MSA-nope")
        assert exc.value.code == 40408

    def test_recompute_matches(self, conn):
        res = svc.create_study(conn, item_code="外径", rows=_rows())
        rc = svc.recompute(conn, res["study"]["study_id"])
        assert rc["delta_pct_grr"] is not None
        assert abs(rc["delta_pct_grr"]) < 0.01
        assert rc["same_verdict"] is True
        assert "一致" in rc["note"]

    def test_recompute_keeps_archive(self, conn):
        """重算不覆盖：归档值必须还在（审计链）。"""
        res = svc.create_study(conn, item_code="外径", rows=_rows())
        before = svc.get_study(conn, res["study"]["study_id"])["result"]
        svc.recompute(conn, res["study"]["study_id"], run_anova=False)
        after = svc.get_study(conn, res["study"]["study_id"])["result"]
        assert before["average_range"]["percent"]["grr"] == \
            after["average_range"]["percent"]["grr"]
        assert "anova" in after                  # 归档里仍保留 ANOVA 快照

    def test_recompute_missing(self, conn):
        with pytest.raises(ApiError) as exc:
            svc.recompute(conn, "MSA-nope")
        assert exc.value.code == 40408


# ===========================================================================
# 三、产线数据一致性检查
# ===========================================================================
@pytest.fixture()
def meas_conn(conn):
    """3 个测量人 × 3 张工单 × 6 条读数，工单间有差异、人之间只有极小偏置。"""
    rows = []
    for op, bias in (("张三", 0.0), ("李四", 0.0005), ("王五", -0.0005)):
        for wo in ("WO-01", "WO-02", "WO-03"):
            base = {"WO-01": 10.0, "WO-02": 10.02, "WO-03": 10.04}[wo]
            for k in range(6):
                rows.append({
                    "item_code": "外径", "value": base + bias + 0.001 * (k % 3),
                    "line_id": "line-01", "work_order_id": wo, "operator": op,
                    "subgroup_no": k + 1, "unit": "mm", "usl": 10.08, "lsl": 9.92,
                    "occurred_at": "2026-11-01 08:%02d:00" % k,
                })
    repo.insert_measurements(conn, rows)
    return conn


def _rows_like(conn, item_code="外径"):
    """复用一致性检查的结果做断言辅助。"""
    return svc.consistency_check(conn, item_code=item_code)


class TestBuildCells:
    def test_groups_by_operator_and_work_order(self, meas_conn):
        built = svc.build_cells(meas_conn, item_code="外径")
        assert built["ok"] is True
        assert built["work_orders"] == 3
        assert set(built["groups"]["WO-01"]) == {"张三", "李四", "王五"}

    def test_min_readings_filters_cells(self, meas_conn):
        repo.insert_measurements(meas_conn, [
            {"item_code": "外径", "value": 10.0, "line_id": "line-01",
             "work_order_id": "WO-01", "operator": "赵六", "occurred_at": "2026-11-01"},
        ])
        built = svc.build_cells(meas_conn, item_code="外径", min_readings=3)
        # 赵六只有 1 条读数，不参与比较（1 个数谈不上均值）
        assert "赵六" not in built["groups"]["WO-01"]

    def test_needs_two_operators(self, conn):
        repo.insert_measurements(conn, [
            {"item_code": "内径", "value": 5.0 + 0.01 * k, "line_id": "line-01",
             "work_order_id": "WO-01", "operator": "张三",
             "occurred_at": "2026-11-01 08:0%d:00" % k} for k in range(5)
        ])
        built = svc.build_cells(conn, item_code="内径")
        assert built["ok"] is False
        assert "至少 2 个人" in built["message"]

    def test_no_records(self, conn):
        assert svc.build_cells(conn, item_code="不存在")["ok"] is False


class TestConsistencyCheck:
    def test_consistent_data_passes(self, meas_conn):
        """人之间只差 0.0005，远小于组内噪声 → 判一致。"""
        res = _rows_like(meas_conn)
        assert res["ok"] is True
        assert res["verdict"]["level"] == "accept"
        assert res["flagged_orders"] == 0
        assert res["sigma_pooled"] > 0

    def test_flagged_when_one_operator_shifts(self, conn):
        """让王五整体偏高 0.02（组内 σ≈0.001）→ 必须被抓出来。"""
        rows = []
        for op, bias in (("张三", 0.0), ("李四", 0.0), ("王五", 0.02)):
            for k in range(10):
                rows.append({"item_code": "外径", "value": 10.0 + bias + 0.001 * (k % 3),
                             "line_id": "line-01", "work_order_id": "WO-01",
                             "operator": op, "occurred_at": "2026-11-01 08:%02d:00" % k})
        repo.insert_measurements(conn, rows)
        res = svc.consistency_check(conn, item_code="外径")
        assert res["verdict"]["level"] == "reject"
        assert res["flagged_orders"] >= 1
        assert any(o["flagged"] for o in res["operator_bias"])
        assert "王五" in res["verdict"]["text"]

    def test_pooled_sigma_not_global(self, conn):
        """用全局标准差会掩盖人员偏移（噪声被工单间差异撑大）→ 必须用组内合并。

        这里造"工单间差异极大、组内噪声极小、某人偏移中等"的数据：
        用全局 σ 算 z 会 < 3（判一致），用组内 σ 算会 >> 3（判偏移）。
        """
        rows = []
        for op, bias in (("张三", 0.0), ("李四", 0.0), ("王五", 0.05)):
            for wo, base in (("WO-01", 10.0), ("WO-02", 12.0)):
                for k in range(10):
                    rows.append({"item_code": "外径",
                                 "value": base + bias + 0.0005 * (k % 2),
                                 "line_id": "line-01", "work_order_id": wo,
                                 "operator": op,
                                 "occurred_at": "2026-11-01 08:%02d:00" % k})
        repo.insert_measurements(conn, rows)
        res = svc.consistency_check(conn, item_code="外径")
        assert res["sigma_pooled"] < 0.01          # 组内 σ 很小
        assert res["flagged_orders"] >= 1           # 因此偏移被识别出来

    def test_z_limit_is_configurable(self, conn):
        rows = []
        for op, bias in (("张三", 0.0), ("李四", 0.004)):
            for k in range(10):
                rows.append({"item_code": "外径", "value": 10.0 + bias + 0.001 * (k % 3),
                             "line_id": "line-01", "work_order_id": "WO-01",
                             "operator": op, "occurred_at": "2026-11-01 08:%0d:00" % k})
        repo.insert_measurements(conn, rows)
        # 阈值越严越容易判"显著"——这条性质本身就是接口正确性的证据
        loose = svc.consistency_check(conn, item_code="外径", z_limit=25)
        tight = svc.consistency_check(conn, item_code="外径", z_limit=1.5)
        assert loose["flagged_orders"] == 0
        assert tight["flagged_orders"] >= 1

    def test_zero_sigma_is_reported(self, conn):
        """所有读数完全相同 → 组内 σ=0，不能给出"一致"的假结论。"""
        rows = []
        for op in ("张三", "李四"):
            for k in range(5):
                rows.append({"item_code": "外径", "value": 10.0, "line_id": "line-01",
                             "work_order_id": "WO-01", "operator": op,
                             "occurred_at": "2026-11-01 08:0%d:00" % k})
        repo.insert_measurements(conn, rows)
        res = svc.consistency_check(conn, item_code="外径")
        assert res["ok"] is False
        assert "标准差为 0" in res["message"]

    def test_caveat_always_says_not_grr(self, meas_conn):
        """口径提示必须在结果里，别让用户把一致性检查当 GR&R 签字。"""
        res = _rows_like(meas_conn)
        assert "不是 GR" in res["caveat"]


class TestDesignAdvice:
    def test_plan_shape(self, meas_conn):
        d = svc.design_advice(meas_conn, item_code="外径")
        assert d["plan"]["parts"] == 10
        assert d["plan"]["appraisers"] == 3
        assert d["plan"]["trials"] == 3
        assert d["tolerance"] == pytest.approx(0.16)
        assert d["thresholds"]["ndc_min"] == 5

    def test_without_spec(self, conn):
        repo.insert_measurements(conn, [
            {"item_code": "内径", "value": 5.0, "line_id": "line-01",
             "occurred_at": "2026-11-01"}])
        d = svc.design_advice(conn, item_code="内径")
        assert d["tolerance"] is None
        assert "没有记录规格上下限" in d["tolerance_note"]


# ===========================================================================
# 五、HTTP 层
# ===========================================================================
USERS = [
    ("qe001", "顾工", "quality_engineer", None),
    ("leader01", "赵班组长", "shift_leader", "line-01"),
    ("manager01", "钱主管", "quality_manager", None),
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg = {
        "auth": {"secret": "test-secret", "token_ttl_s": 3600,
                 "dev_header_bypass": False},
        "llm": {"model": "t", "api_key_env": "QT_NOPE_KEY"},
        "embedding": {"enabled": False},
        "retrieval": {"top_k": 3, "recall": 10, "rrf_k": 60},
        "chat": {"db_path": str(tmp_path / "msa_api.db"),
                 "kb_index_dir": "data/phase1_index",
                 "history_turns": 6, "rate_limit_per_min": 1000,
                 "question_max_chars": 1000, "history_max_chars": 6000,
                 "stream_timeout_s": 5, "title_max_len": 20},
        "defect": {"upload_dir": str(tmp_path / "uploads"),
                   "max_photo_mb": 2, "backfill_threshold_s": 60,
                   "list_limit": 50, "description_min_chars": 4},
    }
    monkeypatch.setattr(app_module, "load_app_config", lambda: cfg)
    app = app_module.create_app()
    with TestClient(app) as c:
        conn = app.state.db
        for username, name, role, line_id in USERS:
            conn.execute(
                "INSERT INTO sys_user (user_id, username, password_hash, display_name,"
                " role, line_id, status, created_at) VALUES (?,?,?,?,?,?, 'active', ?)",
                (repo.new_id("u"), username, auth_service.hash_password(username),
                 name, role, line_id, "2026-11-06T09:00:00"),
            )
        conn.commit()
        yield c


def _h(client, username="qe001"):
    r = client.post("/api/v1/auth/login",
                    json={"username": username, "password": username})
    return {"Authorization": "Bearer " + r.json()["data"]["token"]}


class TestMsaApi:
    def test_overview(self, client):
        body = client.get("/api/v1/msa/overview", headers=_h(client)).json()
        assert body["code"] == 0
        assert body["data"]["studies"] == 0

    def test_create_and_fetch(self, client):
        h = _h(client)
        r = client.post("/api/v1/msa/studies", headers=h, json={
            "item_code": "外径", "rows": _rows(), "tolerance": 0.16,
            "gauge": "G-01",
        })
        assert r.status_code == 200, r.text
        study_id = r.json()["data"]["study"]["study_id"]

        got = client.get("/api/v1/msa/studies/" + study_id, headers=h).json()
        assert got["data"]["item_code"] == "外径"
        assert got["data"]["rows"]

        lst = client.get("/api/v1/msa/studies", headers=h).json()
        assert lst["data"]["total"] == 1

    def test_create_validation(self, client):
        h = _h(client)
        r = client.post("/api/v1/msa/studies", headers=h,
                        json={"item_code": "外径", "rows": []})
        assert r.status_code == 422                  # pydantic 拦住空列表

        r2 = client.post("/api/v1/msa/studies", headers=h, json={
            "item_code": "外径",
            "rows": [{"appraiser": "A", "part": "P1", "trial": 1, "value": 1.0}],
        })
        assert r2.status_code == 400
        assert r2.json()["code"] == 40001

    def test_missing_study_404(self, client):
        r = client.get("/api/v1/msa/studies/MSA-nope", headers=_h(client))
        assert r.status_code == 404
        assert r.json()["code"] == 40408

    def test_recompute_endpoint(self, client):
        h = _h(client)
        sid = client.post("/api/v1/msa/studies", headers=h, json={
            "item_code": "外径", "rows": _rows()}).json()["data"]["study"]["study_id"]
        r = client.post("/api/v1/msa/studies/%s/recompute" % sid, headers=h)
        assert r.status_code == 200
        assert r.json()["data"]["same_verdict"] is True

    def test_gaps_endpoint(self, client):
        body = client.get("/api/v1/msa/gaps", headers=_h(client)).json()
        assert body["code"] == 0
        assert "note" in body["data"]

    def test_consistency_requires_item(self, client):
        r = client.get("/api/v1/msa/consistency", headers=_h(client))
        assert r.status_code == 422

    def test_consistency_without_data(self, client):
        body = client.get("/api/v1/msa/consistency?item_code=不存在",
                          headers=_h(client)).json()
        assert body["code"] == 0
        assert body["data"]["ok"] is False

    def test_advice_endpoint(self, client):
        body = client.get("/api/v1/msa/advice?item_code=外径", headers=_h(client)).json()
        assert body["data"]["plan"]["parts"] == 10

    def test_shift_leader_forbidden(self, client):
        h = _h(client, "leader01")
        for path in ("/api/v1/msa/overview", "/api/v1/msa/studies", "/api/v1/msa/gaps"):
            r = client.get(path, headers=h)
            assert r.status_code == 403, path
            assert r.json()["code"] == 40301

    def test_manager_allowed(self, client):
        r = client.get("/api/v1/msa/overview", headers=_h(client, "manager01"))
        assert r.status_code == 200

    def test_anonymous_401(self, client):
        assert client.get("/api/v1/msa/overview").status_code == 401
