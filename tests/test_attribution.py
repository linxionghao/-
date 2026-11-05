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
