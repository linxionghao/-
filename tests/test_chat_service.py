"""core/services/chat（问答编排 + 流式事件）的单元测试。

策略：全离线——
- 向量通道关（embedding.enabled=false）→ BM25 单通道，用仓库自带索引；
- LLM Key 为空 → 走降级（50002），事件序列仍然完整。
这恰好覆盖了"降级不是失败"的产品约定（docs/chat-api.md 第 1 节）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services.chat import ChatService
from quality_trace.core.services.errors import ApiError, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_DIR = REPO_ROOT / "data" / "phase1_index"       # 仓库自带（--ingest 产物）


def _cfg(tmp_path, **overrides):
    """最小可用配置：BM25-only + 空 LLM Key。"""
    cfg = {
        "chat": {
            "db_path": str(tmp_path / "chat.db"),
            "kb_index_dir": str(INDEX_DIR),
            "history_turns": 6,
            "history_max_chars": 6000,
            "question_max_chars": 1000,
            "stream_timeout_s": 60,
            "rate_limit_per_min": 20,
            "title_max_len": 20,
        },
        "retrieval": {"top_k": 3, "recall": 10, "rrf_k": 60},
        "embedding": {"enabled": False},
        "llm": {"base_url": "https://api.invalid.example/v1",
                "model": "demo", "timeout_s": 1},
    }
    cfg.update(overrides)
    return cfg


def _service(tmp_path, cfg=None) -> ChatService:
    conn = repo.connect(":memory:")
    repo.init_schema(conn)
    return ChatService(conn, cfg or _cfg(tmp_path), llm_api_key="", emb_api_key="")


def _collect(service: ChatService, question: str, session_id=None):
    """把事件生成器收成列表，方便断言事件序列。"""
    return [
        ev for ev in service.stream_answer(question, session_id, "qe-001",
                                            "quality_engineer")
    ]


# ------------------------------------------------------------------ 校验
class TestValidate:
    def test_empty_question(self, tmp_path):
        svc = _service(tmp_path)
        with pytest.raises(ApiError) as ei:
            svc.validate("   ", None)
        assert ei.value.code == ErrorCode.BAD_REQUEST

    def test_too_long(self, tmp_path):
        svc = _service(tmp_path)
        with pytest.raises(ApiError) as ei:
            svc.validate("问" * 1001, None)
        assert ei.value.code == ErrorCode.QUESTION_TOO_LONG

    def test_rate_limited(self, tmp_path):
        svc = _service(tmp_path)
        cfg = _cfg(tmp_path)
        cfg["chat"]["rate_limit_per_min"] = 2
        svc2 = _service(tmp_path, cfg)
        for _ in range(2):
            _collect(svc2, "螺纹检验抽样方案", None)
        with pytest.raises(ApiError) as ei:
            svc2.validate("再来一个问题", svc2.conn and
                          repo.list_messages(svc2.conn, _first_sid(svc2))[0]["session_id"])
        assert ei.value.code == ErrorCode.RATE_LIMITED

    def test_archived_session(self, tmp_path):
        svc = _service(tmp_path)
        events = _collect(svc, "螺纹检验抽样方案", None)
        sid = _sid_of(events)
        repo.archive_session(svc.conn, sid)
        with pytest.raises(ApiError) as ei:
            svc.validate("继续问", sid)
        assert ei.value.code == ErrorCode.SESSION_ARCHIVED

    def test_missing_session(self, tmp_path):
        svc = _service(tmp_path)
        with pytest.raises(ApiError) as ei:
            svc.validate("问题", "sess-not-exist")
        assert ei.value.code == ErrorCode.SESSION_NOT_FOUND

    def test_other_users_session_returns_40401(self, tmp_path):
        """权限矩阵 #3：会话不属于本人 → 按「不存在」返回，不泄露存在性。"""
        svc = _service(tmp_path)
        events = _collect(svc, "螺纹检验抽样方案", None)   # 会话属 qe-001
        sid = _sid_of(events)
        # 另一位质量工程师（qe-999）访问 → 40401 而非 403（不暴露会话存在）
        with pytest.raises(ApiError) as ei:
            svc.validate("偷看别人的会话", sid, user_id="qe-999")
        assert ei.value.code == ErrorCode.SESSION_NOT_FOUND
        # 本人才放行
        assert svc.validate("自己的会话", sid, user_id="qe-001")["session_id"] == sid

    def test_stream_answer_rejects_foreign_session(self, tmp_path):
        """流式主入口同样拦截他人会话（validate 放过了 stream 也不能进）。"""
        svc = _service(tmp_path)
        events = _collect(svc, "螺纹检验抽样方案", None)
        sid = _sid_of(events)
        with pytest.raises(ApiError) as ei:
            list(svc.stream_answer("继续问", sid, "qe-999", "quality_engineer"))
        assert ei.value.code == ErrorCode.SESSION_NOT_FOUND


# ------------------------------------------------------------------ 主流程
class TestStreamAnswer:
    def test_event_sequence_and_degradation(self, tmp_path):
        """离线全链路：status → sources → done（LLM 无 Key → 50002 降级）。"""
        svc = _service(tmp_path)
        events = _collect(svc, "螺纹检验的抽样方案是什么？", None)

        kinds = [e["event"] for e in events]
        assert kinds[0] == "status"                      # 先「正在检索依据…」
        assert kinds[1] == "sources"                    # 引用先于正文
        assert "sources" in kinds and "done" in kinds
        assert "error" not in kinds                     # 降级不是失败

        sources = events[1]["data"]["sources"]
        assert sources, "BM25 至少应命中一条"
        assert all("source_file" in s for s in sources)

        done = events[-1]["data"]
        assert done["session_id"]
        assert done["degraded"]["code"] == ErrorCode.LLM_DEGRADED   # 50002
        assert "message" in done["degraded"]                        # 中文降级说明

    def test_messages_and_citations_persisted(self, tmp_path):
        """用户消息、partial→complete 的 assistant 消息、引用均落库。"""
        svc = _service(tmp_path)
        events = _collect(svc, "螺纹检验的抽样方案是什么？", None)
        sid = _sid_of(events)
        done = events[-1]["data"]

        rows = repo.list_messages(svc.conn, sid)
        assert [m["role"] for m in rows] == ["user", "assistant"]
        assert rows[1]["status"] == "complete"          # 流结束已补齐
        assert rows[1]["degraded"] == 1                 # 降级标记入库

        cites = repo.get_citations(svc.conn, done["message_id"])
        assert cites, "引用必须结构化落库"
        assert cites[0]["clause"], "条款号/章节不能为空"

    def test_citation_fields(self, tmp_path):
        """引用三要素：文档名 + 条款号 + 版本号。

        注：知识库改为 DB 权威后，source_file 是文档名（kb_doc.doc_name，
        不带 .md 扩展名）——断言按新语义写。
        """
        svc = _service(tmp_path)
        events = _collect(svc, "螺纹检验的抽样方案是什么？", None)
        src = events[1]["data"]["sources"][0]
        assert src["source_file"] == "检验标准-螺纹外径检验规范v2"
        assert src["clause"]
        assert src["version"] in (None, "v1", "v2")     # 有版本就带，没有则 None

    def test_history_window_six_turns(self, tmp_path):
        """第 7 轮提问时上下文只带最近 6 轮（12 条）。"""
        cfg = _cfg(tmp_path)
        svc = _service(tmp_path, cfg)
        sid = None
        for i in range(7):
            events = _collect(svc, f"第{i}轮：螺纹抽样方案", sid)
            sid = _sid_of(events)
        history = svc.build_history(sid)
        assert len(history) == 12                        # 6 轮 × 2 条
        assert history[0]["content"] == "第1轮：螺纹抽样方案"   # 最旧一轮是第 1 轮
        assert history[-1]["role"] == "assistant"

    def test_history_char_budget(self, tmp_path):
        """字符预算兜底：从最旧一侧成对丢弃，至少保留 1 轮。"""
        cfg = _cfg(tmp_path)
        cfg["chat"]["history_max_chars"] = 100
        svc = _service(tmp_path, cfg)
        sid = None
        for i in range(3):
            events = _collect(svc, "长" * 80 + f"{i}", sid)
            sid = _sid_of(events)
        history = svc.build_history(sid)
        assert len(history) <= 2                        # 预算只够最后一轮
        assert len(history) >= 2 and history[0]["role"] == "user"

    def test_title_from_first_question(self, tmp_path):
        svc = _service(tmp_path)
        events = _collect(svc, "切刀磨损导致螺纹外径偏小怎么处理？", None)
        sid = _sid_of(events)
        title = repo.get_session(svc.conn, sid)["title"]
        assert title == "切刀磨损导致螺纹外径偏小怎么处理？"[:20]


# ------------------------------------------------------------------ 工具
def _sid_of(events):
    for e in events:
        if e["event"] == "done":
            return e["data"]["session_id"]
    raise AssertionError("事件流中没有 done，拿不到 session_id")


def _first_sid(svc):
    row = svc.conn.execute("SELECT session_id FROM chat_session").fetchone()
    return row["session_id"]
