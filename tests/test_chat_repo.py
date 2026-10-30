"""core/repo/db（会话/消息/引用三表）的单元测试。

用临时 SQLite 库，全部离线。
"""

from __future__ import annotations

from quality_trace.core.repo import db as repo


def _conn(tmp_path):
    conn = repo.connect(str(tmp_path / "chat.db"))
    repo.init_schema(conn)
    return conn


class TestSession:
    def test_create_and_get(self, tmp_path):
        conn = _conn(tmp_path)
        session = repo.create_session(conn, "qe-001", "quality_engineer", "标题")
        loaded = repo.get_session(conn, session["session_id"])
        assert loaded["user_id"] == "qe-001"
        assert loaded["status"] == "active"
        assert loaded["message_count"] == 0
        # 双时间列：业务时间与入库时间都已写入
        assert loaded["occurred_at"] and loaded["recorded_at"]

    def test_get_missing_returns_none(self, tmp_path):
        conn = _conn(tmp_path)
        assert repo.get_session(conn, "sess-nope") is None

    def test_archive(self, tmp_path):
        conn = _conn(tmp_path)
        session = repo.create_session(conn, "qe-001", "quality_engineer")
        assert repo.archive_session(conn, session["session_id"]) is True
        assert repo.get_session(conn, session["session_id"])["status"] == "archived"
        # 已归档的再归档：不再命中
        assert repo.archive_session(conn, session["session_id"]) is False

    def test_init_schema_idempotent(self, tmp_path):
        conn = _conn(tmp_path)
        repo.init_schema(conn)          # 重复建表不抛错
        repo.create_session(conn, "u", "r")


class TestMessage:
    def test_save_update_partial_to_complete(self, tmp_path):
        """决策 D①：流式先写 partial，结束后 UPDATE 补齐。"""
        conn = _conn(tmp_path)
        sid = repo.create_session(conn, "qe-001", "quality_engineer")["session_id"]
        mid = repo.save_message(conn, sid, "assistant", "", status="partial")
        repo.update_message(conn, mid, content="完整回答", status="complete",
                            degraded=True, degraded_code="50002",
                            degraded_reason="大模型不可用", latency_ms=123)
        msg = repo.list_messages(conn, sid)[0]
        assert msg["content"] == "完整回答"
        assert msg["status"] == "complete"
        assert msg["degraded"] == 1
        assert msg["degraded_code"] == "50002"
        assert msg["latency_ms"] == 123

    def test_list_order_and_limit(self, tmp_path):
        conn = _conn(tmp_path)
        sid = repo.create_session(conn, "qe-001", "quality_engineer")["session_id"]
        for i in range(4):
            repo.save_message(conn, sid, "user", f"问题{i}")
        rows = repo.list_messages(conn, sid)
        assert [r["content"] for r in rows] == ["问题0", "问题1", "问题2", "问题3"]
        assert len(repo.list_messages(conn, sid, limit=2)) == 2

    def test_count_recent_for_rate_limit(self, tmp_path):
        conn = _conn(tmp_path)
        sid = repo.create_session(conn, "qe-001", "quality_engineer")["session_id"]
        repo.save_message(conn, sid, "user", "q")
        assert repo.count_recent_messages(conn, sid, 60) == 1     # 刚写入算最近
        assert repo.count_recent_messages(conn, sid, 0) == 1      # 边界：seconds=0

    def test_touch_session(self, tmp_path):
        conn = _conn(tmp_path)
        sid = repo.create_session(conn, "qe-001", "quality_engineer")["session_id"]
        repo.touch_session(conn, sid, delta=2)
        assert repo.get_session(conn, sid)["message_count"] == 2


class TestCitation:
    def test_save_and_get_ordered(self, tmp_path):
        conn = _conn(tmp_path)
        sid = repo.create_session(conn, "qe-001", "quality_engineer")["session_id"]
        mid = repo.save_message(conn, sid, "assistant", "回答")
        repo.save_citations(conn, mid, [
            {"seq": 2, "source_file": "b.md", "clause": "D4 根因", "version": None},
            {"seq": 1, "source_file": "a.md", "clause": "3. 抽样方案",
             "version": "v2", "doc_type": "standard", "score": 0.016,
             "snippet": "首件检验…"},
        ])
        cites = repo.get_citations(conn, mid)
        assert [c["seq"] for c in cites] == [1, 2]                 # 按 seq 排序
        assert cites[0]["source_file"] == "a.md"
        assert cites[0]["version"] == "v2"
        assert cites[1]["version"] is None                         # 无版本存 NULL
        assert cites[0]["snippet"].startswith("首件检验")
