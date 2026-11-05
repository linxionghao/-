# -*- coding: utf-8 -*-
"""问答路由：POST /chat（SSE 流式）+ 会话管理 + 健康检查。

SSE 实现说明（决策 B①）：
- 服务层 stream_answer() 是同步生成器（内部用 requests 流式 + SQLite 同步写）；
- 这里用「后台线程生产 + asyncio.to_thread 消费」桥接到异步响应，
  不阻塞 FastAPI 事件循环，也不引入 httpx；
- 流一旦打开，HTTP 状态码无法再变，所以流中错误只能以 error 事件发出。
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from typing import Any, AsyncIterator, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from quality_trace.api.deps import (
    ChatRequest,
    CitationOut,
    MessageOut,
    get_config,
    get_current_user,
    get_db,
    get_emb_key,
    get_llm_key,
    require_roles,
)
from quality_trace.api.sse import HEARTBEAT_SECONDS, format_sse, heartbeat
from quality_trace.core.repo import db as repo
from quality_trace.core.services.chat import ChatService
from quality_trace.core.services.errors import ErrorCode

router = APIRouter(prefix="/api/v1")

# 问答功能仅质量工程师可用（权限矩阵 #3–#6，主管走 /admin/audit 审计视图）
_qe_only = require_roles("quality_engineer")


def _json(code: int, message: str, data: Any = None) -> JSONResponse:
    """统一响应封装：{code, message, data}，message 恒为中文。

    HTTP 状态码 = 错码前三位（40401→404），与全局 ApiError 处理器一致；
    code=0 成功 → 200。前端只判 body.code，但状态码对齐便于网关/日志定位。
    """
    status = int(str(code)[:3]) if code >= 10000 else 200
    return JSONResponse(
        {"code": code, "message": message, "data": data}, status_code=status
    )


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: Dict[str, str] = Depends(_qe_only),
    conn=Depends(get_db),
    cfg: Dict[str, Any] = Depends(get_config),
    llm_key: str = Depends(get_llm_key),
    emb_key: str = Depends(get_emb_key),
):
    """流式问答主入口（SSE）。

    错误处理分两段：
    - 流前校验失败 → 普通 JSON 错误（HTTP 4xx/5xx）；
    - 流中失败     → SSE error 事件（此时 200 已发出）。
    """
    service = ChatService(
        conn, cfg, llm_api_key=llm_key, emb_api_key=emb_key,
        index_dir=os.environ.get("QT_KB_INDEX_DIR")
        or cfg.get("chat", {}).get("kb_index_dir"),
    )
    question = body.question.strip()

    # ---- 流前校验（参数 / 限流 / 会话归属 / 并发），失败直接返回 JSON 错误 ----
    try:
        service.validate(question, body.session_id, user_id=user["user_id"],
                        as_of=body.as_of)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", ErrorCode.INTERNAL_ERROR)
        message = getattr(exc, "message", "服务内部错误，请稍后重试")
        return _json(code, message)

    # ---- 流式响应：线程生产事件 + 异步消费（见模块 docstring） ----
    def _producer(out: "queue.Queue") -> None:
        try:
            for event in service.stream_answer(
                question, body.session_id, user["user_id"], user["role"],
                as_of=body.as_of,
            ):
                out.put(format_sse(event["event"], event["data"]))
        except Exception as exc:  # noqa: BLE001 —— 兜底：流中任何异常都成 error 事件
            out.put(format_sse("error", {"code": ErrorCode.INTERNAL_ERROR,
                                          "message": str(exc)}))
        finally:
            out.put(None)     # 结束标记

    events: "queue.Queue" = queue.Queue()
    threading.Thread(target=_producer, args=(events,), daemon=True).start()

    async def _stream() -> AsyncIterator[str]:
        last_beat = time.time()
        while True:
            try:
                chunk = await asyncio.to_thread(events.get, timeout=1.0)
            except queue.Empty:
                # 空闲超 15 秒发心跳，防止代理掐断长连接
                if time.time() - last_beat >= HEARTBEAT_SECONDS:
                    last_beat = time.time()
                    yield heartbeat()
                continue
            if chunk is None:
                break
            last_beat = time.time()
            yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",     # 禁用 nginx 缓冲，保证逐块下发
            "Connection": "keep-alive",
        },
    )


@router.post("/sessions")
async def create_session(
    user: Dict[str, str] = Depends(_qe_only),
    conn=Depends(get_db),
):
    """显式新建会话（也可以不调，POST /chat 不传 session_id 会自动建）。"""
    session = repo.create_session(conn, user["user_id"], user["role"])
    return _json(0, "成功", {"session_id": session["session_id"]})


@router.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: str,
    user: Dict[str, str] = Depends(_qe_only),
    conn=Depends(get_db),
):
    """拉取历史消息（含引用），供前端刷新后恢复现场。

    权限矩阵 #5：仅质量工程师；且只能看**本人**会话——
    他人的 session_id 按「不存在」处理（40401），不泄露会话存在性。
    """
    session = repo.get_session(conn, session_id)
    if not session or session["user_id"] != user["user_id"]:
        return _json(ErrorCode.SESSION_NOT_FOUND, "会话不存在或已被删除")
    rows = repo.list_messages(conn, session_id)
    data = []
    for row in rows:
        row["created_at"] = row.get("recorded_at")    # 前端展示用录入时间
        row["citations"] = repo.get_citations(conn, row["message_id"])
        data.append(MessageOut(**row).model_dump())
    return _json(0, "成功", {"session_id": session_id, "messages": data})


@router.delete("/sessions/{session_id}")
async def archive_session(
    session_id: str,
    user: Dict[str, str] = Depends(_qe_only),
    conn=Depends(get_db),
):
    """归档会话（软删除：历史保留，不可再提问）。权限矩阵 #6：本人会话。"""
    session = repo.get_session(conn, session_id)
    if not session or session["user_id"] != user["user_id"]:
        return _json(ErrorCode.SESSION_NOT_FOUND, "会话不存在或已被删除")
    ok = repo.archive_session(conn, session_id)
    if not ok:
        return _json(ErrorCode.SESSION_NOT_FOUND, "会话不存在或已被删除")
    return _json(0, "成功", {"session_id": session_id, "status": "archived"})


@router.get("/health")
async def health(cfg: Dict[str, Any] = Depends(get_config)):
    """健康检查：LLM Key、向量开关、索引文件是否就绪，供前端状态灯。"""
    index_dir = cfg.get("chat", {}).get("kb_index_dir", "data/phase1_index")
    index_ok = os.path.exists(os.path.join(index_dir, "chunks.json"))
    return _json(
        0, "成功",
        {
            "llm_key_ready": bool(os.environ.get("QT_LLM_API_KEY")),
            "embedding_enabled": bool(cfg.get("embedding", {}).get("enabled")),
            "index_ready": index_ok,
            "model": cfg.get("llm", {}).get("model"),
        },
    )
