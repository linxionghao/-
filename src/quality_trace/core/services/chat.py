"""问答服务：会话校验 → 历史上下文 → 混合检索 → 流式生成 → 落库。

【核心模块】架构 L2 服务层。产出"事件流"（dict 列表），由入口层格式化为 SSE：
    {"event": "status",  "data": {"stage": "retrieving", "message": "正在检索依据…"}}
    {"event": "sources", "data": {"sources": [...]}}
    {"event": "delta",   "data": {"text": "…"}}
    {"event": "done",    "data": {"message_id": ..., "degraded": {...}, ...}}
    {"event": "error",   "data": {"code": 50003, "message": "…"}}

关键约定：
- sources 早于正文发送：断流/超时时引用仍完整落库并可渲染（架构 4.4 可审计）；
- 降级不等于失败：50001/50002/50401 仍产出回答或引用，只是标注 degraded；
- assistant 消息先写 partial（断流不丢），流结束再 UPDATE 补齐（决策 D①）；
- 全模块不 print、不读环境变量、不 import argparse（core 约束）。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from quality_trace.core.knowledge import ingest as kb_ingest
from quality_trace.core.knowledge import retrieval as kb_retrieval
from quality_trace.core.knowledge.prompts import build_chat_messages
from quality_trace.core.llm.client import (
    EmbeddingUnavailable,
    LLMUnavailable,
    embed_texts,
    stream_chat,
)
from quality_trace.core.repo import db as repo
from quality_trace.core.services.errors import ApiError, ErrorCode

# 引用片段展示长度
SNIPPET_LEN = 120


class ChatService:
    """一次问答的全部编排逻辑。按请求构造，线程不安全（每请求一个实例）。"""

    def __init__(
        self,
        conn,
        cfg: Dict,
        *,
        llm_api_key: str = "",
        emb_api_key: str = "",
        index_dir: Optional[Path] = None,
    ):
        self.conn = conn
        self.chat_cfg = cfg.get("chat") or {}
        self.ret_cfg = cfg.get("retrieval") or {}
        self.emb_cfg = cfg.get("embedding") or {}
        self.llm_cfg = cfg.get("llm") or {}
        self.llm_api_key = llm_api_key          # 由入口层从环境变量读出后传入
        self.emb_api_key = emb_api_key
        self.index_dir = Path(index_dir or self.chat_cfg.get("kb_index_dir", "data/phase1_index"))
        self._chunks: Optional[List[Dict]] = None   # 懒加载并缓存，避免每请求重建索引

    # ------------------------------------------------------------ 校验
    def validate(self, question: str, session_id: Optional[str],
                 user_id: str = "") -> Dict:
        """流开始前的校验：失败抛 ApiError，由入口层转成 HTTP JSON 错误。

        user_id 用于会话归属校验（权限矩阵 #3「本人会话」）：
        传了 user_id 且会话不属于该用户时，按「不存在」返回 40401，
        不向他人暴露会话是否存在。
        """
        if not question or not question.strip():
            raise ApiError(ErrorCode.BAD_REQUEST)
        max_len = int(self.chat_cfg.get("question_max_chars", 1000))
        if len(question.strip()) > max_len:
            raise ApiError(ErrorCode.QUESTION_TOO_LONG)

        # 限流：单会话每分钟消息数
        per_min = int(self.chat_cfg.get("rate_limit_per_min", 20))
        if session_id and repo.count_recent_messages(self.conn, session_id, 60) >= per_min:
            raise ApiError(ErrorCode.RATE_LIMITED)

        session = self.resolve_session(session_id, user_id)
        # 并发：上一条 assistant 还在生成（partial）则拒绝
        for msg in reversed(repo.list_messages(self.conn, session["session_id"])):
            if msg["role"] == "assistant" and msg["status"] == "partial":
                raise ApiError(ErrorCode.STREAM_BUSY)
            break
        return session

    def resolve_session(self, session_id: Optional[str], user_id: str = "",
                        role: str = "") -> Dict:
        """取会话；传了 id 就校验存在性 / 归属 / 归档状态，没传就新建。

        归属校验（权限矩阵 #3）：传了 user_id 且会话 user_id 不一致 →
        按不存在返回（40401），防止质量工程师之间互看互写会话。
        user_id 为空串时不校验（留给既有单测/内部调用），路由层必须传。
        """
        if session_id:
            session = repo.get_session(self.conn, session_id)
            if not session:
                raise ApiError(ErrorCode.SESSION_NOT_FOUND)
            if user_id and session["user_id"] != user_id:
                raise ApiError(ErrorCode.SESSION_NOT_FOUND)
            if session["status"] == "archived":
                raise ApiError(ErrorCode.SESSION_ARCHIVED)
            return session
        return repo.create_session(self.conn, user_id, role)

    # ------------------------------------------------------------ 历史
    def build_history(self, session_id: str) -> List[Dict]:
        """取最近 N 轮历史（默认 6 轮 = 12 条），按时间正序，超预算成对丢弃。"""
        turns = int(self.chat_cfg.get("history_turns", 6))
        max_chars = int(self.chat_cfg.get("history_max_chars", 6000))
        rows = repo.list_messages(self.conn, session_id)
        recent = rows[-(turns * 2):] if turns > 0 else []

        # 最后一条若是 user（上轮异常中断），丢弃以保证 user/assistant 成对
        if recent and recent[-1]["role"] == "user":
            recent = recent[:-1]

        # 字符预算兜底：从最旧一侧成对丢弃
        while len(recent) > 2 and sum(len(m["content"]) for m in recent) > max_chars:
            recent = recent[2:]

        # 只带正文：历史 assistant 不重复拼接引用材料
        return [
            {"role": m["role"], "content": m["content"]}
            for m in recent
            if m["status"] != "failed"      # 失败轮次不进上下文
        ]

    # ------------------------------------------------------------ 检索
    def _load_index(self) -> Tuple[List[Dict], kb_retrieval.BM25Index,
                                   Optional[kb_retrieval.VectorStore]]:
        """加载切片与索引（懒加载 + 缓存）。索引缺失抛 40402。"""
        if self._chunks is None:
            try:
                self._chunks = kb_ingest.load_chunks(self.index_dir)
            except FileNotFoundError as exc:
                raise ApiError(ErrorCode.INDEX_NOT_FOUND) from exc
        chunks = self._chunks
        bm25 = kb_retrieval.BM25Index([kb_retrieval.tokenize(c["content"]) for c in chunks])

        vectors = None
        if bool(self.emb_cfg.get("enabled", False)):
            f_path, n_path = self.index_dir / "vectors.faiss", self.index_dir / "vectors.npy"
            if f_path.exists() or n_path.exists():
                try:
                    vectors = kb_retrieval.VectorStore.load(self.index_dir)
                except Exception as exc:  # noqa: BLE001 —— 索引损坏 → 50301
                    raise ApiError(ErrorCode.INDEX_UNAVAILABLE) from exc
        return chunks, bm25, vectors

    def _make_embedder(self) -> Optional[kb_retrieval.Embedder]:
        """按配置装配向量化函数；未开启返回 None → 检索层走 BM25 单通道。"""
        if not bool(self.emb_cfg.get("enabled", False)):
            return None

        def _embed(text: str):
            return embed_texts([text], self.emb_cfg, self.emb_api_key)[0]

        return _embed

    def retrieve(self, question: str) -> Tuple[List[Dict], Dict]:
        """混合检索，返回 (命中项列表, 运行元信息)。"""
        chunks, bm25, vectors = self._load_index()
        ranked, meta = kb_retrieval.hybrid_search(
            question, bm25, vectors, self._make_embedder(), self.ret_cfg
        )
        return (
            [
                {"rank": pos, "rrf": score, "chunk": chunks[idx]}
                for pos, (idx, score) in enumerate(ranked, start=1)
            ],
            meta,
        )

    @staticmethod
    def to_citations(retrieved: List[Dict]) -> List[Dict]:
        """命中项 → 引用结构（文档名 + 条款号 + 版本号，前端展示与落库同构）。"""
        citations = []
        for item in retrieved:
            c = item["chunk"]
            meta = c.get("doc_meta") or {}
            citations.append(
                {
                    "seq": item["rank"],
                    "source_file": c.get("source_file", ""),
                    "doc_id": meta.get("doc_id"),
                    "clause": c.get("heading") or "",
                    "version": meta.get("version"),
                    "doc_type": meta.get("doc_type"),
                    "chunk_id": str(c.get("seq_no")),
                    "score": round(float(item["rrf"]), 6),
                    "snippet": c.get("content", "")[:SNIPPET_LEN],
                }
            )
        return citations

    # ------------------------------------------------------------ 主流程
    def stream_answer(
        self, question: str, session_id: Optional[str], user_id: str, role: str
    ) -> Iterator[Dict]:
        """流式问答：产出事件流（生成器）。入口层负责格式化为 SSE。

        注意：校验已在 validate() 中前置完成，这里只处理"流中"的异常，
        统一转成 error 事件——此时 HTTP 状态码已发出，无法再改。
        """
        started = time.time()
        session = self.resolve_session(session_id, user_id, role)
        sid = session["session_id"]

        # 0) 历史快照必须先于用户消息落库——否则当前问题会重复进上下文，
        #    且会把刚写入的 partial assistant 空行当成历史。
        history = self.build_history(sid)

        # 1) 用户消息落库 + 会话计数
        repo.save_message(self.conn, sid, "user", question.strip())
        repo.touch_session(self.conn, sid)
        if not session.get("title"):
            title = question.strip()[: int(self.chat_cfg.get("title_max_len", 20))]
            self.conn.execute(
                "UPDATE chat_session SET title = ? WHERE session_id = ?", (title, sid)
            )
            self.conn.commit()

        # 2) 检索阶段状态
        yield {"event": "status",
               "data": {"stage": "retrieving", "message": "正在检索依据…"}}

        degraded: Optional[Dict] = None
        try:
            retrieved, meta = self.retrieve(question)
        except ApiError as exc:                      # 索引类错误：流中只能发 error 事件
            yield {"event": "error", "data": {"code": exc.code, "message": exc.message}}
            return
        except EmbeddingUnavailable as exc:           # 向量服务挂了：降级，不中断
            retrieved, meta = [], {"mode": "bm25_only", "degraded": True}
            degraded = {"code": ErrorCode.EMBED_DEGRADED, "message": str(exc)}

        citations = self.to_citations(retrieved)
        if meta.get("degraded") and degraded is None:
            reason = meta.get("degrade_reason", "")
            degraded = {
                "code": ErrorCode.EMBED_DEGRADED if "向量" in reason
                else ErrorCode.INTERNAL_ERROR,
                "message": reason,
            }

        # 3) 引用先于正文发送（断流也能拿到依据）
        yield {"event": "sources", "data": {"sources": citations}}

        # 4) assistant 先落 partial 行，断流不丢（决策 D①）
        assistant_id = repo.save_message(
            self.conn, sid, "assistant", "", status="partial",
            retrieved_json=json.dumps(meta, ensure_ascii=False),
            model=self.llm_cfg.get("model"),
        )

        # 5) 流式生成（history 已在开头取好快照）
        #    系统提示词**每次请求实时读 active 模板**（主管改完立即生效，
        #    不写死在代码里）；库里没有模板时退回内置种子。
        template = repo.get_active_template(self.conn)
        system_prompt = template["content"] if template else None
        messages = build_chat_messages(history, question, retrieved,
                                        system_prompt=system_prompt)
        prompt_hash = hashlib.sha1(
            json.dumps(messages, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]

        content_parts: List[str] = []
        try:
            timeout_s = float(self.chat_cfg.get("stream_timeout_s", 60))
            for text in stream_chat(messages, self.llm_cfg, self.llm_api_key):
                content_parts.append(text)
                yield {"event": "delta", "data": {"text": text}}
                if time.time() - started > timeout_s:   # 超时：截断而非失败
                    degraded = {
                        "code": ErrorCode.STREAM_TIMEOUT,
                        "message": "大模型响应超时，回答可能不完整",
                    }
                    break
        except LLMUnavailable as exc:
            # 降级：无正文，但引用区照常可用
            degraded = {"code": ErrorCode.LLM_DEGRADED, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 —— 兜底：标失败并落库
            repo.update_message(self.conn, assistant_id, status="failed",
                                latency_ms=int((time.time() - started) * 1000))
            repo.save_citations(self.conn, assistant_id, citations)
            yield {"event": "error",
                   "data": {"code": ErrorCode.INTERNAL_ERROR, "message": str(exc)}}
            return

        # 6) 收尾：补齐正文、写引用、更新状态
        content = "".join(content_parts)
        latency_ms = int((time.time() - started) * 1000)
        repo.update_message(
            self.conn, assistant_id, content=content, status="complete",
            degraded=bool(degraded),
            degraded_code=str(degraded["code"]) if degraded else None,
            degraded_reason=degraded["message"] if degraded else None,
            retrieved_json=json.dumps(meta, ensure_ascii=False),
            model=self.llm_cfg.get("model"), prompt_hash=prompt_hash,
            latency_ms=latency_ms,
        )
        repo.save_citations(self.conn, assistant_id, citations)
        repo.touch_session(self.conn, sid)

        # 6.1) 审计三规则同步打标（批次 1c：主管审计页的数据源。
        #      标记是「疑似」语义——降级消息（无正文有引用）不会被误报）
        from quality_trace.core.services import audit as audit_service
        audit_service.flag_message(self.conn, assistant_id)

        yield {
            "event": "done",
            "data": {
                "message_id": assistant_id,
                "session_id": sid,
                "degraded": degraded,
                "mode": meta.get("mode"),
                "latency_ms": latency_ms,
                "history_turns": len(history) // 2,
                "citations": len(citations),
            },
        }
