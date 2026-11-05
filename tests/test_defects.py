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


# ---------------------------------------------------------------------------
# 人工确认：draft → pending
# ---------------------------------------------------------------------------
class TestConfirm:
    def test_confirm_moves_to_pending(self, env):
        view = _create(env)
        result = defect_service.confirm(
            env["conn"], user=LEADER, defect_no=view["defect_no"],
            defect_code="D-THREAD")
        assert result["status"] == "pending"
        assert result["defect_code"] == "D-THREAD"
        assert "判定" in result["status_text"]      # 只承诺待判定，不出现结论
        assert "合格" not in result["status_text"]

    def test_confirm_twice_rejected(self, env):
        """已确认的单不可改码——避免同一单出现两个分类。"""
        view = _create(env)
        defect_service.confirm(env["conn"], user=LEADER,
                               defect_no=view["defect_no"], defect_code="D-THREAD")
        with pytest.raises(ApiError) as ei:
            defect_service.confirm(env["conn"], user=LEADER,
                                   defect_no=view["defect_no"], defect_code="D-SURF")
        assert ei.value.code == ErrorCode.DEFECT_STATE_INVALID

    def test_unknown_code_rejected(self, env):
        view = _create(env)
        with pytest.raises(ApiError) as ei:
            defect_service.confirm(env["conn"], user=LEADER,
                                   defect_no=view["defect_no"], defect_code="D-NOPE")
        assert ei.value.code == ErrorCode.BAD_REQUEST

    def test_disabled_code_rejected(self, env):
        """停用的缺陷码不允许再被确认（字典可运营下架）。"""
        env["conn"].execute("UPDATE defect_code SET status='disabled' WHERE code='D-SURF'")
        env["conn"].commit()
        view = _create(env)
        with pytest.raises(ApiError) as ei:
            defect_service.confirm(env["conn"], user=LEADER,
                                   defect_no=view["defect_no"], defect_code="D-SURF")
        assert ei.value.code == ErrorCode.BAD_REQUEST

    def test_empty_code_rejected(self, env):
        view = _create(env)
        with pytest.raises(ApiError) as ei:
            defect_service.confirm(env["conn"], user=LEADER,
                                   defect_no=view["defect_no"], defect_code="  ")
        assert ei.value.code == ErrorCode.BAD_REQUEST

    def test_other_line_cannot_confirm(self, env):
        """他线班组长确认本线单据 → 40404（不暴露单据存在性）。"""
        view = _create(env)
        with pytest.raises(ApiError) as ei:
            defect_service.confirm(env["conn"], user=COLLEAGUE,
                                   defect_no=view["defect_no"], defect_code="D-THREAD")
        assert ei.value.code == ErrorCode.DEFECT_NOT_FOUND


# ---------------------------------------------------------------------------
# 可见性裁剪：班组长看不到归因与判定字段
# ---------------------------------------------------------------------------
class TestVisibility:
    def test_sl_fields_whitelisted(self, env):
        view = _create(env)
        assert "suggest_json" not in view
        assert "photo_path" not in view
        assert "line_id" not in view
        assert "created_by" not in view

    def test_qe_sees_more(self, env):
        view = _create(env, user=QE)
        assert "suggest_json" in view and "line_id" in view

    def test_list_filtered_by_line(self, env):
        """班组长列表只出现本线单；QE 能看到全部。"""
        _create(env)                                   # line-01
        _create(env, user=QE, work_order_id="WO-B")    # line-02
        sl_rows = defect_service.list_for_user(env["conn"], LEADER)
        qe_rows = defect_service.list_for_user(env["conn"], QE)
        assert len(sl_rows) == 1
        assert len(qe_rows) == 2

    def test_get_other_line_404(self, env):
        other = _create(env, user=QE, work_order_id="WO-B")
        with pytest.raises(ApiError) as ei:
            defect_service.get_for_user(env["conn"], LEADER, other["defect_no"])
        assert ei.value.code == ErrorCode.DEFECT_NOT_FOUND

    def test_status_text_has_no_judgement(self, env):
        """状态文案不得出现任何合格性判定语汇（需求红线）。"""
        view = _create(env)
        for word in ("合格", "不合格", "判退", "报废"):
            assert word not in view["status_text"]


# ---------------------------------------------------------------------------
# 照片：校验 + 落盘 + token 通道
# ---------------------------------------------------------------------------
class TestPhoto:
    def _data_url(self, blob=JPEG_BYTES):
        return "data:image/jpeg;base64," + base64.b64encode(blob).decode()

    def test_inline_photo_saved(self, env):
        view = _create(env, photo=self._data_url())
        row = repo.get_defect(env["conn"], view["defect_no"])
        assert row["photo_path"].endswith(".jpg")
        assert (env["tmp"] / "uploads").exists()

    def test_invalid_bytes_rejected(self, env):
        """GIF 头不是允许格式 → 40003，且不留下单据。"""
        with pytest.raises(ApiError) as ei:
            _create(env, photo="data:image/png;base64," +
                    base64.b64encode(b"GIF89a" + b"0" * 20).decode())
        assert ei.value.code == ErrorCode.PHOTO_INVALID
        assert repo.count_defects(env["conn"]) == 0

    def test_oversize_rejected(self, env):
        too_big = JPEG_BYTES + b"\x00" * (2 * 1024 * 1024 + 1)
        with pytest.raises(ApiError) as ei:
            _create(env, photo=self._data_url(too_big))
        assert ei.value.code == ErrorCode.PHOTO_INVALID

    def test_empty_photo_is_optional(self, env):
        view = _create(env, photo="")
        assert view["has_photo"] is False

    def test_token_flow(self, env):
        """预上传拿 token → 建单引用 → 临时文件移动到正式目录。"""
        token = photo_service.save_temp(JPEG_BYTES, "jpg", env["upload"])
        view = _create(env, photo_token=token)
        row = repo.get_defect(env["conn"], view["defect_no"])
        assert row["photo_path"].endswith(f"{view['defect_no']}.jpg")
        assert photo_service.resolve_temp(token, env["upload"]) is None

    def test_bad_token_rejected(self, env):
        for token in ("../../etc/passwd", "not-a-token", "0" * 32):
            with pytest.raises(ApiError) as ei:
                _create(env, photo_token=token)
            assert ei.value.code == ErrorCode.PHOTO_INVALID

    def test_path_traversal_token_ignored(self, env):
        """token 严格 hex 校验：路径穿越字符串解析为空，不产生文件。"""
        assert photo_service.resolve_temp("../../secret", env["upload"]) is None


# ---------------------------------------------------------------------------
# 工单校验（移动端失焦调用）
# ---------------------------------------------------------------------------
class TestWorkOrderCheck:
    def test_ok_for_own_line(self, env):
        view = defect_service.check_work_order(env["conn"], LEADER, "WO-A")
        assert view["product_name"] == "M8 螺栓"
        assert view["line_id"] == "line-01"

    def test_other_line_404(self, env):
        with pytest.raises(ApiError) as ei:
            defect_service.check_work_order(env["conn"], LEADER, "WO-B")
        assert ei.value.code == ErrorCode.WORK_ORDER_NOT_FOUND

    def test_empty_rejected(self, env):
        with pytest.raises(ApiError) as ei:
            defect_service.check_work_order(env["conn"], LEADER, "  ")
        assert ei.value.code == ErrorCode.BAD_REQUEST

    def test_qe_can_check_any_line(self, env):
        assert defect_service.check_work_order(
            env["conn"], QE, "WO-B")["line_id"] == "line-02"


# ---------------------------------------------------------------------------
# 未绑定产线的班组长：默认拒绝，绝不退化成全量可见
# ---------------------------------------------------------------------------
UNBOUND_LEADER = {"user_id": "sl-099", "role": "shift_leader", "line_id": None}


class TestUnboundShiftLeader:
    """配置漏填 line_id 时，必须 40302 挡住，而不是当成「不限产线」。"""

    def test_list_denied(self, env):
        with pytest.raises(ApiError) as ei:
            defect_service.list_for_user(env["conn"], UNBOUND_LEADER)
        assert ei.value.code == ErrorCode.LINE_FORBIDDEN

    def test_get_denied(self, env):
        no = _create(env)["defect_no"]
        with pytest.raises(ApiError) as ei:
            defect_service.get_for_user(env["conn"], UNBOUND_LEADER, no)
        assert ei.value.code == ErrorCode.LINE_FORBIDDEN

    def test_create_denied(self, env):
        with pytest.raises(ApiError) as ei:
            _create(env, user=UNBOUND_LEADER)
        assert ei.value.code == ErrorCode.LINE_FORBIDDEN

    def test_confirm_denied(self, env):
        no = _create(env)["defect_no"]
        with pytest.raises(ApiError) as ei:
            defect_service.confirm(
                env["conn"], user=UNBOUND_LEADER, defect_no=no,
                defect_code="D-THREAD")
        assert ei.value.code == ErrorCode.LINE_FORBIDDEN

    def test_work_order_check_denied(self, env):
        with pytest.raises(ApiError) as ei:
            defect_service.check_work_order(env["conn"], UNBOUND_LEADER, "WO-A")
        assert ei.value.code == ErrorCode.LINE_FORBIDDEN

    def test_empty_string_line_treated_as_unbound(self, env):
        """空串与 None 等价——不能因为写了个 "" 就绕过。"""
        user = {"user_id": "sl-098", "role": "shift_leader", "line_id": "   "}
        with pytest.raises(ApiError) as ei:
            defect_service.list_for_user(env["conn"], user)
        assert ei.value.code == ErrorCode.LINE_FORBIDDEN

    def test_other_roles_unaffected(self, env):
        """QE / QM 不受影响：仍是全量视角。"""
        assert defect_service.line_scope(QE) is None
        assert defect_service.line_scope({"role": "quality_manager"}) is None
        assert defect_service.line_scope(LEADER) == "line-01"
