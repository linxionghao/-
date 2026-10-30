# -*- coding: utf-8 -*-
"""主管后台 · JSON 接口：知识库管理 + 提示词模板 + 对话审计 + 归因审批。

对应 docs/stage3-design.md 权限矩阵：
    #8–#14  知识库/模板   仅主管
    #15–#18 对话审计      仅主管
    #19–#20 归因列表/详情 主管全量；质量工程师只读自己提交的（数据级过滤）
    #21–#23 审批/退回/导出 仅主管
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response

from quality_trace.api.deps import (
    get_config,
    get_db,
    get_emb_key,
    get_llm_key,
    require_roles,
)
from quality_trace.core.repo import db as repo
from quality_trace.core.services import knowledge_admin as kb_admin
from quality_trace.core.services import attribution as attr_service
from quality_trace.core.services import audit as audit_service
from quality_trace.core.services.errors import ApiError, ErrorCode

router = APIRouter(prefix="/api/v1/admin")

# 路由级白名单：主管专属
_qm_only = require_roles("quality_manager")


def _repo_root() -> Path:
    """仓库根目录（上传文件留档目录的基准）。"""
    return Path(__file__).resolve().parents[3]


def _index_dir(cfg: Dict[str, Any]) -> Path:
    return Path(cfg.get("chat", {}).get("kb_index_dir", "data/phase1_index"))


@router.post("/documents")
async def upload_document(
    file: UploadFile = File(..., description="markdown 或 pdf 文件"),
    doc_type: str = Form(..., description="standard / sop_manual / 8d_case"),
    version: str = Form(..., description="版本号，如 v2"),
    effective_from: str = Form(..., description="生效起，如 2026-03-01"),
    effective_to: str = Form(..., description="生效止，如 2027-03-01"),
    user: Dict[str, Any] = Depends(_qm_only),
    conn=Depends(get_db),
    cfg: Dict[str, Any] = Depends(get_config),
):
    """上传文档：必填版本号与生效区间；同名同版本冲突 40903。

    embedding 关闭或失败时文档照常入库（vector_status=pending，50004 降级语义），
    BM25 检索不受影响。
    """
    data = await file.read()
    filename = file.filename or ""

    # embedder：向量服务开启时注入（Key 由入口层从环境变量读出）
    embedder = None
    emb_cfg = cfg.get("embedding") or {}
    if emb_cfg.get("enabled") and get_emb_key():
        from quality_trace.core.llm.client import embed_texts

        def _embed(texts):
            return embed_texts(texts, emb_cfg, get_emb_key())

        embedder = _embed

    result = kb_admin.upload_document(
        conn, cfg,
        filename=filename, data=data,
        doc_type=doc_type, version=version,
        effective_from=effective_from, effective_to=effective_to,
        uploaded_by=user["user_id"],
        index_dir=_index_dir(cfg),
        save_dir=_repo_root() / "data" / "uploads" / "kb",
        embedder=embedder,
    )
    degraded = None
    if not result["vector_ok"]:
        degraded = {"code": ErrorCode.EMBED_PENDING,
                    "message": "向量服务不可用，文档已入库但仅支持关键词检索"}
    return {
        "code": 0, "message": "成功",
        "data": {"doc": result["doc"],
                 "chunk_count": result["chunk_count"],
                 "index_stats": result["index_stats"],
                 "degraded": degraded},
    }


@router.get("/documents")
async def list_documents(
    doc_type: Optional[str] = None,
    status: Optional[str] = None,
    user: Dict[str, Any] = Depends(_qm_only),   # noqa: ARG001 —— 鉴权占位
    conn=Depends(get_db),
):
    """文档列表（可按类型 / 状态筛选，管理页表格用）。"""
    docs = repo.list_docs(conn, doc_type=doc_type, status=status)
    return {"code": 0, "message": "成功",
            "data": {"documents": docs, "total": len(docs)}}


@router.get("/documents/{doc_id}/chunks")
async def list_doc_chunks(
    doc_id: str,
    user: Dict[str, Any] = Depends(_qm_only),   # noqa: ARG001
    conn=Depends(get_db),
):
    """查看某文档的切片（核对切得对不对）。"""
    doc = repo.get_doc(conn, doc_id)
    if not doc:
        raise ApiError(ErrorCode.DOC_NOT_FOUND)
    rows = conn.execute(
        "SELECT chunk_id, seq_no, clause, content, char_len"
        " FROM kb_chunk WHERE doc_id = ? ORDER BY seq_no",
        (doc_id,),
    ).fetchall()
    chunks = [dict(r) for r in rows]
    return {"code": 0, "message": "成功",
            "data": {"doc": doc, "chunks": chunks, "total": len(chunks)}}


@router.post("/documents/{doc_id}/archive")
async def archive_document(
    doc_id: str,
    user: Dict[str, Any] = Depends(_qm_only),
    conn=Depends(get_db),
    cfg: Dict[str, Any] = Depends(get_config),
):
    """下架（废止）：不物理删除，切片即刻退出检索并触发索引重建。"""
    result = kb_admin.archive_document(conn, doc_id, user["user_id"],
                                       _index_dir(cfg))
    return {"code": 0, "message": "成功",
            "data": {"doc": result["doc"], "index_stats": result["index_stats"]}}


@router.post("/documents/{doc_id}/restore")
async def restore_document(
    doc_id: str,
    user: Dict[str, Any] = Depends(_qm_only),   # noqa: ARG001
    conn=Depends(get_db),
    cfg: Dict[str, Any] = Depends(get_config),
):
    """恢复上架（误下架补救）。"""
    result = kb_admin.restore_document(conn, doc_id, _index_dir(cfg))
    return {"code": 0, "message": "成功",
            "data": {"doc": result["doc"], "index_stats": result["index_stats"]}}


# ================================================================ 提示词模板
@router.get("/prompt-templates")
async def get_template(
    user: Dict[str, Any] = Depends(_qm_only),   # noqa: ARG001
    conn=Depends(get_db),
):
    """当前生效的系统提示词 + 最近修改历史。"""
    template = repo.get_active_template(conn)
    history = repo.list_template_history(conn)
    return {"code": 0, "message": "成功",
            "data": {"template": template, "history": history}}


@router.post("/prompt-templates")
async def create_template(
    body: Dict[str, Any],
    user: Dict[str, Any] = Depends(_qm_only),
    conn=Depends(get_db),
):
    """创建/更新 active 模板（upsert 语义：库里没有就建，有就更新+写历史）。

    body: {"name": "...", "content": "..."}，content 非空。
    """
    content = str(body.get("content", "")).strip()
    if not content:
        raise ApiError(ErrorCode.BAD_REQUEST, "提示词内容不能为空")
    name = str(body.get("name") or "默认系统提示词").strip()
    template = repo.upsert_template(conn, name, content, user["user_id"])
    return {"code": 0, "message": "成功，下一条回答立即生效",
            "data": {"template": template}}


@router.put("/prompt-templates/{template_id}")
async def update_template(
    template_id: str,
    body: Dict[str, Any],
    user: Dict[str, Any] = Depends(_qm_only),
    conn=Depends(get_db),
):
    """修改系统提示词：UPDATE active 行 + 追加历史（修改人与时间入档）。

    下一次 /chat 请求即生效（服务层每请求实时读库，无缓存）。
    body: {"name": "...", "content": "..."}，content 非空。
    """
    current = repo.get_active_template(conn)
    if not current or current["template_id"] != template_id:
        raise ApiError(ErrorCode.BAD_REQUEST, "模板不存在或已不是当前生效版本")
    content = str(body.get("content", "")).strip()
    if not content:
        raise ApiError(ErrorCode.BAD_REQUEST, "提示词内容不能为空")
    name = str(body.get("name") or current["name"]).strip() or current["name"]

    # 删除旧历史前的说明：历史只增不删，保留完整审计链
    template = repo.upsert_template(conn, name, content, user["user_id"])
    return {"code": 0, "message": "成功，下一条回答立即生效",
            "data": {"template": template}}


# ================================================================ 对话审计（#15–#18）
@router.get("/audit/messages")
async def list_audit_messages(
    role: Optional[str] = None,          # 提问者角色
    batch_no: Optional[str] = None,      # 批次号（会话维度）
    date_from: Optional[str] = None,     # 业务时间起（YYYY-MM-DD）
    date_to: Optional[str] = None,       # 业务时间止
    only_flagged: bool = False,          # 只看命中越界规则的
    limit: int = 200,
    user: Dict[str, Any] = Depends(_qm_only),   # noqa: ARG001
    conn=Depends(get_db),
):
    """审计列表：按角色/批次/时间/越界筛选（全部为 assistant 回答）。"""
    rows = repo.list_audit_messages(
        conn, role=role, batch_no=batch_no,
        date_from=date_from, date_to=date_to,
        only_flagged=only_flagged, limit=limit,
    )
    return {"code": 0, "message": "成功",
            "data": {"messages": rows, "total": len(rows)}}


@router.get("/audit/messages/{message_id}")
async def get_audit_message(
    message_id: str,
    user: Dict[str, Any] = Depends(_qm_only),   # noqa: ARG001
    conn=Depends(get_db),
):
    """审计详情：消息 + 所属会话（批次）+ 全部引用切片 + 核查记录。"""
    message = repo.get_message(conn, message_id)
    if message is None:
        raise ApiError(ErrorCode.SESSION_NOT_FOUND, "消息不存在")
    session = repo.get_session(conn, message["session_id"]) or {}
    citations = repo.get_citations(conn, message_id)
    return {"code": 0, "message": "成功",
            "data": {"message": message, "session": session,
                     "citations": citations}}


@router.post("/audit/messages/{message_id}/review")
async def review_message(
    message_id: str,
    body: Dict[str, Any],
    user: Dict[str, Any] = Depends(_qm_only),
    conn=Depends(get_db),
):
    """主管标记已核查（可附备注）：机器标记 → 人工核查闭环。"""
    note = str(body.get("note", "")).strip()
    if not repo.mark_reviewed(conn, message_id, user["user_id"], note):
        raise ApiError(ErrorCode.SESSION_NOT_FOUND, "消息不存在")
    return {"code": 0, "message": "已标记核查",
            "data": {"message_id": message_id,
                     "reviewed_by": user["user_id"]}}


@router.post("/audit/rescan")
async def rescan_audit(
    user: Dict[str, Any] = Depends(_qm_only),   # noqa: ARG001
    conn=Depends(get_db),
):
    """补扫：对历史消息重跑三规则（规则升级后可反复执行；不覆盖人工核查结论）。"""
    result = audit_service.rescan_all(conn)
    return {"code": 0, "message": f"补扫完成：扫描 {result['scanned']} 条，"
                                   f"命中越界 {result['flagged']} 条",
            "data": result}


# ================================================================ 归因审批（#19–#23）
def _attr_scope(conn, user: Dict[str, Any]) -> Optional[str]:
    """归因单数据级过滤：主管看全部，质量工程师只看自己提交的（#19 ◐）。

    返回 submitted_by 过滤值；None 表示不过滤（主管）。
    非主管非工程师进不到这里（路由白名单已拦）。
    """
    if user["role"] == "quality_engineer":
        return user["user_id"]
    return None


@router.get("/attributions")
async def list_attributions(
    status: Optional[str] = None,
    user: Dict[str, Any] = Depends(
        require_roles("quality_manager", "quality_engineer")),
    conn=Depends(get_db),
):
    """归因单列表：主管全量；质量工程师仅自己提交（repo 层过滤，非前端隐藏）。"""
    rows = repo.list_attributions(
        conn, status=status, submitted_by=_attr_scope(conn, user)
    )
    return {"code": 0, "message": "成功",
            "data": {"attributions": rows, "total": len(rows)}}


@router.get("/attributions/{attribution_id}")
async def get_attribution(
    attribution_id: str,
    user: Dict[str, Any] = Depends(
        require_roles("quality_manager", "quality_engineer")),
    conn=Depends(get_db),
):
    """归因单详情（证据链 + 假设 + 审批记录）。

    工程师访问他人归因单按「不存在」返回（40405），不泄露存在性。
    """
    row = repo.get_attribution(conn, attribution_id)
    scope = _attr_scope(conn, user)
    if row is None or (scope and row["submitted_by"] != scope):
        raise ApiError(ErrorCode.ATTR_NOT_FOUND)
    defect = repo.get_defect(conn, row["defect_no"]) or {}
    return {"code": 0, "message": "成功",
            "data": {"attribution": row, "defect": defect}}


@router.post("/attributions/{attribution_id}/approve")
async def approve_attribution(
    attribution_id: str,
    body: Optional[Dict[str, Any]] = None,
    user: Dict[str, Any] = Depends(_qm_only),
    conn=Depends(get_db),
):
    """批准归因单（意见可附；重复审批 40906）。"""
    note = str((body or {}).get("note", "")).strip()
    row = attr_service.decide(conn, attribution_id, "approved",
                              user["user_id"], note)
    return {"code": 0, "message": "已批准", "data": {"attribution": row}}


@router.post("/attributions/{attribution_id}/reject")
async def reject_attribution(
    attribution_id: str,
    body: Dict[str, Any],
    user: Dict[str, Any] = Depends(_qm_only),
    conn=Depends(get_db),
):
    """退回归因单：**意见必填**（工程师需要修改方向），重复操作 40906。"""
    note = str(body.get("note", "")).strip()
    row = attr_service.decide(conn, attribution_id, "rejected",
                              user["user_id"], note)
    return {"code": 0, "message": "已退回", "data": {"attribution": row}}


@router.get("/attributions/{attribution_id}/export-8d")
async def export_8d(
    attribution_id: str,
    user: Dict[str, Any] = Depends(_qm_only),   # noqa: ARG001
    conn=Depends(get_db),
    cfg: Dict[str, Any] = Depends(get_config),
):
    """导出 8D 草稿（Markdown 下载）：有缓存直读，无缓存现场调 LLM 并缓存。

    LLM 不可用 → 50002 中文错误（此时尚未输出文件头，不会产生半截文件）。
    """
    result = attr_service.export_8d(
        conn, attribution_id, cfg.get("llm") or {}, get_llm_key()
    )
    return Response(
        content=result["markdown"].encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="{result["filename"]}"'},
    )
