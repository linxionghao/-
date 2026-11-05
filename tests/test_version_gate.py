"""版本失效硬闸测试（架构 4.3「三道闸」的第一道）。

场景：知识库里同时存在同一份检验标准的 v1（已废止）、v2（当年生效）、
v3（次年生效）。按 2026 年的业务时刻提问，只允许命中 v2——
**即使 v1 的措辞与问题更相似**，也不能被当成现行标准。

这正是需求里明确点名的陷阱，本文件用可执行的断言把它钉死。
"""

from __future__ import annotations

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services.chat import ChatService
from quality_trace.core.services.errors import ApiError, ErrorCode

DOC_NAME = "螺纹外径检验规范"


def _service(tmp_path):
    """临时库 + 三版标准（v1 已下架 / v2 本期 / v3 下期）。"""
    conn = repo.connect(str(tmp_path / "gate.db"))
    repo.init_schema(conn)

    v1 = repo.insert_doc(conn, DOC_NAME, "standard", "v1",
                         "2025-01-01", "2025-12-31")
    repo.set_doc_status(conn, v1["doc_id"], "archived", "seed")
    v2 = repo.insert_doc(conn, DOC_NAME, "standard", "v2",
                         "2026-01-01", "2026-12-31")
    v3 = repo.insert_doc(conn, DOC_NAME, "standard", "v3",
                         "2027-01-01", "2027-12-31")

    # v1 的措辞与问题高度重合（用来验证「相似度高也进不来」）
    repo.insert_chunks(conn, v1["doc_id"], [{
        "seq_no": 1, "content": "螺纹外径抽样方案：每批抽 8 件，全检通止规。",
        "heading": "3. 抽样", "char_len": 28,
    }])
    repo.insert_chunks(conn, v2["doc_id"], [{
        "seq_no": 1, "content": "螺纹外径抽样方案：首件三件全检，过程每两小时抽五件。",
        "heading": "3. 抽样", "char_len": 32,
    }])
    repo.insert_chunks(conn, v3["doc_id"], [{
        "seq_no": 1, "content": "螺纹外径抽样方案：按 GB/T 2828.1 一般检验水平 II。",
        "heading": "3. 抽样", "char_len": 32,
    }])

    cfg = {
        "chat": {"question_max_chars": 1000, "rate_limit_per_min": 20},
        "retrieval": {"top_k": 3, "recall": 10, "rrf_k": 60},
        "embedding": {"enabled": False},
        "llm": {},
    }
    return ChatService(conn, cfg), conn


class TestVersionGate:
    def test_as_of_hits_only_current_version(self, tmp_path):
        svc, _ = _service(tmp_path)
        hits, meta = svc.retrieve("螺纹外径抽样方案", as_of="2026-06-01")
        versions = {h["chunk"]["doc_meta"]["version"] for h in hits}
        assert versions == {"v2"}, f"应只命中 v2，实际 {versions}"
        assert meta["version_gate"] == "sql"

    def test_archived_version_never_leaks(self, tmp_path):
        """v1 已下架：即使用它的原话提问，也一条都检不到。"""
        svc, _ = _service(tmp_path)
        hits, _ = svc.retrieve("每批抽 8 件 全检通止规", as_of="2026-06-01")
        versions = {h["chunk"]["doc_meta"]["version"] for h in hits}
        assert "v1" not in versions

    def test_future_version_not_visible(self, tmp_path):
        """2026 年的业务时刻不能看到 2027 才生效的 v3。"""
        svc, _ = _service(tmp_path)
        hits, _ = svc.retrieve("GB/T 2828.1 抽样", as_of="2026-06-01")
        assert all(h["chunk"]["doc_meta"]["version"] != "v3" for h in hits)

    def test_historical_query_uses_then_effective(self, tmp_path):
        """按 2025 年时刻提问：v1 已下架仍不应出现（下架是全局动作）。"""
        svc, _ = _service(tmp_path)
        hits, _ = svc.retrieve("螺纹外径抽样方案", as_of="2025-06-01")
        assert hits == []          # 该时刻没有在架文档

    def test_no_as_of_uses_index_path(self, tmp_path):
        """不传 as_of 时行为不变：走索引文件（version_gate=index），不查库。

        注意：此时检索的是仓库自带索引，而非临时库里的三版标准——
        这正是需要 as_of 的原因：索引文件无法表达版本语义。
        """
        svc, _ = _service(tmp_path)
        hits, meta = svc.retrieve("螺纹外径抽样方案")
        assert meta["version_gate"] == "index"
        assert isinstance(hits, list)

    def test_empty_result_meta(self, tmp_path):
        """该时刻无有效文档：返回空命中 + hits=0，不抛异常。"""
        svc, _ = _service(tmp_path)
        hits, meta = svc.retrieve("螺纹外径抽样方案", as_of="2020-01-01")
        assert hits == [] and meta["hits"] == 0

    def test_invalid_as_of_rejected(self, tmp_path):
        """格式错误必须 40001——静默按「当前」检索会让追溯结论失真。"""
        svc, _ = _service(tmp_path)
        for bad in ("2026-13-01", "昨天", "2026/06/01", "26-6-1"):
            with pytest.raises(ApiError) as ei:
                svc.validate("问题", None, as_of=bad)
            assert ei.value.code == ErrorCode.BAD_REQUEST

    def test_valid_as_of_formats(self, tmp_path):
        svc, _ = _service(tmp_path)
        for good in ("2026-06-01", "2026-06-01T08:30:00", "2026-06-01T08:30:00Z"):
            svc.validate("问题", None, as_of=good)      # 不抛即通过

    def test_citation_carries_version(self, tmp_path):
        """引用必须带版本号，前端与审计才有据可查。"""
        svc, _ = _service(tmp_path)
        hits, _ = svc.retrieve("螺纹外径抽样方案", as_of="2026-06-01")
        citations = svc.to_citations(hits)
        assert citations[0]["version"] == "v2"
        assert citations[0]["source_file"] == DOC_NAME
