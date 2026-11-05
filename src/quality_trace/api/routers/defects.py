# -*- coding: utf-8 -*-
"""班组长现场录入接口（docs/stage3-design.md 权限矩阵 #24–#29）。

设计要点：
- 建单与确认分两个接口：POST /defects 只造 draft 并给候选码，
  POST /defects/{no}/confirm 由班组长亲手点选后才转 pending；
- 所有响应统一 {code, message, data}；错误由全局处理器转中文文案；
- 班组长身份里的 line_id 来自 JWT，服务层据此做 SQL 级过滤，
  前端传什么都不影响数据范围。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from quality_trace.api.deps import (
    REPO_ROOT,
    get_config,
    get_db,
    get_llm_key,
    require_roles,
)
from quality_trace.core.services import defects as defect_service
from quality_trace.core.services import photo as photo_service

router = APIRouter(prefix="/api/v1")

# 三个角色都能读字典与列表；只有班组长能写
_ALL_ROLES = require_roles("shift_leader", "quality_engineer", "quality_manager")
_SHIFT_LEADER = require_roles("shift_leader")


def _ok(data: Any = None, message: str = "成功") -> Dict[str, Any]:
    return {"code": 0, "message": message, "data": data}


def _upload_dir(cfg: Dict[str, Any]) -> Path:
    """上传根目录：配置为相对路径时按仓库根解析，便于跨机器运行。"""
    raw = str(cfg.get("defect", {}).get("upload_dir", "data/uploads"))
    path = Path(raw)
    return path if path.is_absolute() else REPO_ROOT / path


# ================================================================ 请求模型
class DefectCreateRequest(BaseModel):
    """现场建单入参；photo 与 photo_token 二选一（都可缺省）。"""

    work_order_id: str = Field(..., description="工单号")
    description: str = Field(..., description="现场描述（自由文本）")
    op_code: str = Field("", description="工序码")
    qty: int = Field(1, description="不良数量")
    severity: str = Field("一般", description="轻微/一般/严重")
    occurred_at: Optional[str] = Field(None, description="现场发生时刻（ISO）")
    photo: Optional[str] = Field(None, description="base64 图片（内联）")
    photo_token: Optional[str] = Field(None, description="预上传返回的 token")
    client_draft_id: Optional[str] = Field(None, description="离线暂存幂等键")
    is_backfill: bool = Field(False, description="客户端显式声明补录")


class ConfirmRequest(BaseModel):
    defect_code: str = Field(..., description="班组长点选的缺陷码")


class PhotoUploadRequest(BaseModel):
    photo: str = Field(..., description="data:image/...;base64,...")


# ================================================================ 字典
@router.get("/defect-codes")
async def list_codes(
    user: Dict[str, Any] = Depends(_ALL_ROLES),   # noqa: ARG001
    conn=Depends(get_db),
):
    """缺陷码字典（按 category 分组，移动端渲染分组大按钮）。"""
    rows = conn.execute(
        "SELECT * FROM defect_code WHERE status='active'"
        " ORDER BY category, sort_no, code"
    ).fetchall()
    items = [dict(r) for r in rows]
    groups: Dict[str, list] = {}
    for item in items:
        groups.setdefault(item["category"], []).append({
            "code": item["code"], "name": item["name"],
        })
    return _ok({"groups": [{"category": k, "items": v} for k, v in groups.items()],
                "total": len(items)})


# ================================================================ 建单 / 确认
@router.post("/defects")
async def create_defect(
    body: DefectCreateRequest,
    user: Dict[str, Any] = Depends(_SHIFT_LEADER),
    conn=Depends(get_db),
    cfg: Dict[str, Any] = Depends(get_config),
    api_key: str = Depends(get_llm_key),
):
    """第一步：创建 draft + 返回候选缺陷码（不做任何判定）。"""
    defect_cfg = cfg.get("defect", {})
    view = defect_service.create_draft(
        conn, user=user,
        work_order_id=body.work_order_id,
        description=body.description,
        op_code=body.op_code,
        qty=body.qty,
        severity=body.severity,
        occurred_at=body.occurred_at,
        photo=body.photo,
        photo_token=body.photo_token,
        client_draft_id=body.client_draft_id,
        declared_backfill=body.is_backfill,
        llm_cfg=cfg.get("llm"),
        api_key=api_key,
        upload_dir=str(_upload_dir(cfg)),
    )
    return _ok(view, "已创建草稿，请确认缺陷码")


@router.post("/defects/{defect_no}/confirm")
async def confirm_defect(
    defect_no: str,
    body: ConfirmRequest,
    user: Dict[str, Any] = Depends(_SHIFT_LEADER),
    conn=Depends(get_db),
):
    """第二步：人工确认缺陷码 → 状态转 pending（判定权仍在质量工程师）。"""
    view = defect_service.confirm(
        conn, user=user, defect_no=defect_no, defect_code=body.defect_code,
    )
    return _ok(view, "已提交，等待质量工程师判定")


# ================================================================ 查询
@router.get("/defects")
async def list_defects(
    user: Dict[str, Any] = Depends(_ALL_ROLES),
    conn=Depends(get_db),
    cfg: Dict[str, Any] = Depends(get_config),
):
    """列表：班组长只看本产线，字段按白名单裁剪。"""
    limit = int(cfg.get("defect", {}).get("list_limit", 50))
    items = defect_service.list_for_user(conn, user, limit=limit)
    return _ok({"defects": items, "total": len(items)})


@router.get("/defects/{defect_no}")
async def get_defect(
    defect_no: str,
    user: Dict[str, Any] = Depends(_ALL_ROLES),
    conn=Depends(get_db),
):
    """详情：他线单据对班组长表现为 40404（不暴露存在性）。"""
    return _ok(defect_service.get_for_user(conn, user, defect_no))


# ================================================================ 照片预上传
@router.post("/uploads/photo")
async def upload_photo(
    body: PhotoUploadRequest,
    user: Dict[str, Any] = Depends(_SHIFT_LEADER),   # noqa: ARG001
    cfg: Dict[str, Any] = Depends(get_config),
):
    """校验并暂存照片，返回 token；建单时用 photo_token 引用。"""
    blob, ext = photo_service.decode_photo(body.photo)
    token = photo_service.save_temp(blob, ext, _upload_dir(cfg))
    return _ok({"photo_token": token, "bytes": len(blob), "ext": ext})
