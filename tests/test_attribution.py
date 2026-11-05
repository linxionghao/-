"""批次 1c 归因审批测试：状态机 + 退回必填 + 8D 导出三条路径。

覆盖重点：
- 状态机只允许 submitted → approved/rejected，重复审批报 40906；
- 退回必须写意见（40001），否则工程师无从修改；
- 8D 导出：有缓存直读、无缓存调 LLM 并缓存、LLM 不可用报 50002 且不产生半截文件。

全部离线：LLM 用 monkeypatch 替身，不发起任何网络请求。
"""

from __future__ import annotations

import pytest

from quality_trace.core.llm.client import LLMUnavailable
from quality_trace.core.repo import db as repo
from quality_trace.core.services import attribution as attr_service
from quality_trace.core.services.errors import ApiError, ErrorCode

EVIDENCE = (
    '[{"layer": "L1", "rule": "刀具寿命超限", "detail": "切刀使用 1820 件",'
    ' "source": "equipment_log"}]'
)


@pytest.fixture()
def conn(tmp_path):
    c = repo.connect(str(tmp_path / "attr.db"))
    repo.init_schema(c)
    yield c
    c.close()


def _order(conn, *, status: str = "submitted", draft: str = "") -> dict:
    """造一张归因单（含其所属工单与缺陷单，保持外键关系完整）。"""
    repo.insert_work_order(conn, "WO-001", "螺纹紧固件 M8", "line-01", 5000)
    repo.insert_defect(
        conn, "D-001", "WO-001", "line-01", "螺纹外径偏小", op_code="OP30",
        qty=12, severity="严重", defect_code="DC-012", status="pending",
    )
    row = repo.insert_attribution(conn, "D-001", EVIDENCE,
                                  "疑似切刀磨损（待验证）", submitted_by="qe-001",
                                  draft_8d_md=draft)
    if status != "submitted":
        conn.execute("UPDATE attribution_order SET status=? WHERE attribution_id=?",
                     (status, row["attribution_id"]))
        conn.commit()
        row = repo.get_attribution(conn, row["attribution_id"])
    return row


class TestDecision:
    def test_approve(self, conn):
        """批准：状态转 approved，记录审批人与时间。"""
        order = _order(conn)
        result = attr_service.decide(conn, order["attribution_id"], "approved",
                                     "manager-01", "证据链完整，同意")
        assert result["status"] == "approved"
        assert result["decided_by"] == "manager-01"
        assert result["decision_note"] == "证据链完整，同意"
        assert result["decided_at"]

    def test_reject_requires_note(self, conn):
        """退回不写意见 → 40001，状态保持不变。"""
        order = _order(conn)
        with pytest.raises(ApiError) as ei:
            attr_service.decide(conn, order["attribution_id"], "rejected",
                                "manager-01", "   ")
        assert ei.value.code == ErrorCode.BAD_REQUEST
        assert repo.get_attribution(conn, order["attribution_id"])["status"] == "submitted"

    def test_reject_with_note(self, conn):
        order = _order(conn)
        result = attr_service.decide(conn, order["attribution_id"], "rejected",
                                     "manager-01", "请补充设备点检记录")
        assert result["status"] == "rejected"
        assert result["decision_note"] == "请补充设备点检记录"

    def test_double_decide_conflict(self, conn):
        """已审批的单子再批 → 40906（状态机不允许回退）。"""
        order = _order(conn)
        attr_service.decide(conn, order["attribution_id"], "approved", "manager-01", "")
        with pytest.raises(ApiError) as ei:
            attr_service.decide(conn, order["attribution_id"], "rejected",
                                "manager-01", "反悔")
        assert ei.value.code == ErrorCode.ATTR_STATE_INVALID

    def test_missing_order(self, conn):
        with pytest.raises(ApiError) as ei:
            attr_service.decide(conn, "attr-nope", "approved", "manager-01", "")
        assert ei.value.code == ErrorCode.ATTR_NOT_FOUND


# ---------------------------------------------------------------------------
# 8D 导出
# ---------------------------------------------------------------------------
LLM_CFG = {"model": "test-model", "base_url": "http://127.0.0.1:1/v1"}


class TestExport8D:
    def test_cached_draft_no_llm_call(self, conn, monkeypatch):
        """库里已有草稿 → 直接返回，绝不重复调 LLM（省钱也保证内容稳定）。"""
        order = _order(conn, draft="# 8D 报告草稿\n\n历史生成内容")

        def _boom(*a, **kw):
            raise AssertionError("有缓存时不应调用 LLM")

        monkeypatch.setattr(attr_service, "call_llm", _boom)
        result = attr_service.export_8d(conn, order["attribution_id"], LLM_CFG, "k")
        assert result["filename"] == "8D-D-001.md"
        assert "历史生成内容" in result["markdown"]

    def test_generate_and_cache(self, conn, monkeypatch):
        """无缓存 → 调 LLM 生成，加红线头并回写缓存，第二次导出不再调用。"""
        order = _order(conn)
        calls = []

        def _fake(messages, cfg, key, **kw):
            calls.append(messages)
            return "## D2 问题描述\n螺纹外径 19.95mm，超下限。"

        monkeypatch.setattr(attr_service, "call_llm", _fake)
        first = attr_service.export_8d(conn, order["attribution_id"], LLM_CFG, "k")
        assert attr_service.DRAFT_BANNER in first["markdown"]
        assert first["markdown"].startswith("# 8D 报告草稿（D-001）")
        assert len(calls) == 1

        # 提示词必须包含缺陷信息与证据链，D4 表述受系统约束
        system_prompt = calls[0][0]["content"]
        assert "根因已确定" in system_prompt          # 作为禁止项出现
        assert "疑似根因（待验证）" in system_prompt   # D4 唯一允许的口径
        assert EVIDENCE in calls[0][1]["content"]

        second = attr_service.export_8d(conn, order["attribution_id"], LLM_CFG, "k")
        assert len(calls) == 1                        # 第二次走缓存
        assert second["markdown"] == first["markdown"]

    def test_llm_unavailable(self, conn, monkeypatch):
        """LLM 不可用 → 50002，且库里不落任何草稿（不留半成品）。"""
        order = _order(conn)

        def _down(*a, **kw):
            raise LLMUnavailable("连接超时")

        monkeypatch.setattr(attr_service, "call_llm", _down)
        with pytest.raises(ApiError) as ei:
            attr_service.export_8d(conn, order["attribution_id"], LLM_CFG, "k")
        assert ei.value.code == ErrorCode.LLM_DEGRADED
        assert "连接超时" in ei.value.message
        assert repo.get_attribution(conn, order["attribution_id"])["draft_8d_md"] in (None, "")

    def test_llm_empty_output(self, conn, monkeypatch):
        """LLM 返回空白 → 50003，不把空文件当成功交付。"""
        order = _order(conn)
        monkeypatch.setattr(attr_service, "call_llm", lambda *a, **kw: "   \n")
        with pytest.raises(ApiError) as ei:
            attr_service.export_8d(conn, order["attribution_id"], LLM_CFG, "k")
        assert ei.value.code == ErrorCode.INTERNAL_ERROR

    def test_missing_order(self, conn):
        with pytest.raises(ApiError) as ei:
            attr_service.export_8d(conn, "attr-nope", LLM_CFG, "k")
        assert ei.value.code == ErrorCode.ATTR_NOT_FOUND

    def test_pretty_evidence(self):
        """详情页展示用：坏 JSON 容错为空列表，正常 JSON 原样返回。"""
        assert attr_service.pretty_evidence(EVIDENCE)[0]["rule"] == "刀具寿命超限"
        assert attr_service.pretty_evidence("{oops") == []
        assert attr_service.pretty_evidence("") == []
        assert attr_service.pretty_evidence('{"a": 1}') == []   # 非数组也容错
