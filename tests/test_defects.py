"""批次 2 班组长录入端测试：归一化 / 建单 / 确认 / 越权 / 照片。

全部离线：LLM 用 monkeypatch 替身，照片用 1x1 JPEG 的最小字节序列，
临时库 + 临时上传目录，不触碰演示数据。
"""

from __future__ import annotations

import base64
import json

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services import defects as defect_service
from quality_trace.core.services import normalize as normalize_service
from quality_trace.core.services import photo as photo_service
from quality_trace.core.services.errors import ApiError, ErrorCode

CODES = [
    {"code": "D-THREAD", "name": "螺纹外径超差", "category": "尺寸缺陷",
     "keywords_json": json.dumps(["螺纹", "外径", "偏小", "通规"], ensure_ascii=False)},
    {"code": "D-SURF", "name": "表面划伤", "category": "外观缺陷",
     "keywords_json": json.dumps(["划伤", "划痕", "毛刺"], ensure_ascii=False)},
    {"code": "D-OTHER", "name": "其他", "category": "其他", "keywords_json": "[]"},
]

# 最小合法 JPEG（SOI + APP0 头即可通过魔数校验，无需真实可解码）
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 32


# ---------------------------------------------------------------------------
# 归一化：关键词回退
# ---------------------------------------------------------------------------
class TestKeywordCandidates:
    def test_hits_by_keyword(self):
        result = normalize_service.keyword_candidates("螺纹外径偏小，通规过不去", CODES)
        assert result[0]["code"] == "D-THREAD"
        assert result[0]["source"] == "keyword"
        assert 0.3 <= result[0]["confidence"] <= 1.0

    def test_zero_score_dropped(self):
        """完全不相关的描述不返回候选——宁缺勿滥，避免误导现场。"""
        assert normalize_service.keyword_candidates("今天天气不错", CODES) == []

    def test_empty_description(self):
        assert normalize_service.keyword_candidates("", CODES) == []
        assert normalize_service.keyword_candidates("   ", CODES) == []

    def test_broken_keywords_tolerated(self):
        """字典里 keywords_json 写坏了也不能让建单流程挂掉。"""
        bad = [{"code": "X", "name": "坏字典", "category": "其他",
                "keywords_json": "{not json"}]
        assert normalize_service.keyword_candidates("坏字典", bad) != []

    def test_top_n_limit(self):
        result = normalize_service.keyword_candidates(
            "螺纹外径偏小 划伤 毛刺 通规", CODES, top_n=1)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 归一化：LLM 通道与降级语义
# ---------------------------------------------------------------------------
class TestLLMNormalize:
    def test_parse_with_code_fence(self):
        """模型爱用 ```json 包裹，解析必须容错。"""
        raw = '```json\n[{"code":"D-THREAD","confidence":0.91,"reason":"外径"}]\n```'
        result = normalize_service._parse_candidates(raw, CODES)
        assert result[0]["code"] == "D-THREAD"
        assert result[0]["confidence"] == 0.91

    def test_fabricated_code_dropped(self):
        """模型自造码必须丢弃——否则会写进不存在的分类。"""
        raw = '[{"code":"D-FAKE","confidence":0.99},{"code":"D-SURF","confidence":0.4}]'
        result = normalize_service._parse_candidates(raw, CODES)
        assert [r["code"] for r in result] == ["D-SURF"]

    def test_garbage_returns_empty(self):
        for raw in ("", "抱歉我不知道", "{", "[1,2,3]", '{"code":"D-THREAD"}'):
            assert normalize_service._parse_candidates(raw, CODES) == []

    def test_normalize_uses_llm(self, monkeypatch):
        monkeypatch.setattr(
            normalize_service, "llm_candidates",
            lambda *a, **kw: [{"code": "D-THREAD", "name": "螺纹外径超差",
                               "category": "尺寸缺陷", "confidence": 0.9,
                               "source": "llm"}],
        )
        out = normalize_service.normalize("外径偏小", CODES, {"model": "x"}, "key")
        assert out["source"] == "llm" and out["degraded"] is False

    def test_normalize_falls_back_when_llm_down(self, monkeypatch):
        """配了 LLM 但调用失败 → degraded=True 且仍有候选（回退关键词）。"""
        def _down(*a, **kw):
            raise RuntimeError("连接超时")

        monkeypatch.setattr(normalize_service, "llm_candidates", _down)
        out = normalize_service.normalize("螺纹外径偏小", CODES, {"model": "x"}, "key")
        assert out["source"] == "keyword"
        assert out["degraded"] is True
        assert "连接超时" in out["degraded_reason"]
        assert out["suggested"][0]["code"] == "D-THREAD"

    def test_normalize_without_key_not_degraded(self):
        """根本没配 Key 属既定运行模式，不算降级事件（全项目统一口径）。"""
        out = normalize_service.normalize("螺纹外径偏小", CODES, {"model": "x"}, "")
        assert out["degraded"] is False
        assert out["source"] == "keyword"

    def test_normalize_empty_dictionary(self):
        out = normalize_service.normalize("螺纹外径偏小", [], None, "")
        assert out["suggested"] == [] and out["degraded"] is False


# ---------------------------------------------------------------------------
# 建单测试夹具
# ---------------------------------------------------------------------------
LEADER = {"user_id": "sl-001", "role": "shift_leader", "line_id": "line-01"}
COLLEAGUE = {"user_id": "sl-002", "role": "shift_leader", "line_id": "line-02"}
QE = {"user_id": "qe-001", "role": "quality_engineer", "line_id": None}


@pytest.fixture()
def env(tmp_path):
    """临时库 + 两个产线的工单 + 字典 + 上传目录。"""
    conn = repo.connect(str(tmp_path / "defects.db"))
    repo.init_schema(conn)
    repo.insert_work_order(conn, "WO-A", "M8 螺栓", "line-01", 1000)
    repo.insert_work_order(conn, "WO-B", "M10 螺栓", "line-02", 1000)
    for item in CODES:
        repo.upsert_defect_code(
            conn, item["code"], item["name"], item["category"],
            json.loads(item["keywords_json"]),
        )
    return {
        "conn": conn,
        "upload": tmp_path / "uploads",
        "tmp": tmp_path,
    }


def _create(env, **kw):
    params = dict(
        user=LEADER, work_order_id="WO-A",
        description="螺纹外径偏小，通规过不去",
        op_code="OP30", qty=3, severity="严重",
        llm_cfg=None, api_key="", upload_dir=str(env["upload"]),
    )
    params.update(kw)
    return defect_service.create_draft(env["conn"], **params)


class TestCreateDraft:
    def test_basic_flow(self, env):
        """建单返回 draft + 候选码，且缺陷码此时为空（人工确认前不落分类）。"""
        view = _create(env)
        assert view["status"] == "draft"
        assert view["defect_code"] is None
        assert view["suggested"][0]["code"] == "D-THREAD"
        assert view["is_backfill"] is False

    def test_dual_timestamps(self, env):
        """occurred_at 用客户端时刻，recorded_at 用服务端落库时刻，两者分离。"""
        view = _create(env, occurred_at="2026-11-05T08:00:00")
        assert view["occurred_at"].startswith("2026-11-05T08:00:00")
        assert view["recorded_at"] != view["occurred_at"]

    def test_backfill_auto_detected(self, env):
        """发生时刻比录入早 10 分钟 → 自动打补录标。"""
        view = _create(env, occurred_at="2026-11-05T06:00:00")
        assert view["is_backfill"] is True

    def test_backfill_declared(self, env):
        """客户端显式声明补录（离线暂存场景）也打标。"""
        assert _create(env, declared_backfill=True)["is_backfill"] is True

    def test_idempotent_by_client_draft_id(self, env):
        """同一 client_draft_id 重复提交返回原单，绝不落两条。"""
        first = _create(env, client_draft_id="dr-abc")
        again = _create(env, client_draft_id="dr-abc")
        assert again["defect_no"] == first["defect_no"]
        assert again["existing"] is True
        assert repo.count_defects(env["conn"]) == 1

    def test_invalid_inputs(self, env):
        cases = [
            {"work_order_id": "  "},
            {"description": "短"},
            {"qty": 0},
            {"qty": "abc"},
            {"severity": "致命"},
        ]
        for case in cases:
            with pytest.raises(ApiError) as ei:
                _create(env, **case)
            assert ei.value.code == ErrorCode.BAD_REQUEST

    def test_work_order_must_belong_to_line(self, env):
        """班组长录他线工单 → 40406（不暴露该工单是否存在）。"""
        with pytest.raises(ApiError) as ei:
            _create(env, work_order_id="WO-B")
        assert ei.value.code == ErrorCode.WORK_ORDER_NOT_FOUND

    def test_missing_work_order(self, env):
        with pytest.raises(ApiError) as ei:
            _create(env, work_order_id="WO-NOPE")
        assert ei.value.code == ErrorCode.WORK_ORDER_NOT_FOUND

    def test_qe_uses_work_order_line(self, env):
        """质量工程师无产线绑定，按工单所属产线落库（代录场景）。"""
        view = _create(env, user=QE, work_order_id="WO-B")
        row = repo.get_defect(env["conn"], view["defect_no"])
        assert row["line_id"] == "line-02"
