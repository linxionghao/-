# -*- coding: utf-8 -*-
"""批次谱系接口：正向/反向追溯、供应商记分卡、谱系图。

    #43 GET  /genealogy/overview            质量工程师 / 主管
    #44 GET  /genealogy/lots                同上
    #45 POST /genealogy/lots                同上（登记物料批）
    #46 POST /genealogy/lots/{id}/consume   同上（登记领料，幂等）
    #47 GET  /genealogy/lots/{id}           同上（正查）
    #48 GET  /genealogy/lots/{id}/graph     同上（谱系图节点/边）
    #49 GET  /genealogy/defects/{no}/backward 同上（反查）
    #50 GET  /genealogy/suppliers/scorecard 同上

为什么班组长没有权限：
班组长关心的是"我这单该怎么填"，而不是"这批料该不该召回"。
让他看到"某供应商 PPM 很高"只会造成两个后果：现场拒收（越权），
或者跟供应商的人吵架（无意义）。职责边界要体现在接口上，不只是文档里。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from quality_trace.api.deps import get_db, require_roles
from quality_trace.core.repo import db as repo
from quality_trace.core.services import genealogy as svc
from quality_trace.core.services.errors import ApiError, ErrorCode

router = APIRouter(prefix="/api/v1/genealogy")

_engineer = require_roles("quality_engineer", "quality_manager")


@router.get("/overview")
async def overview(
    days: int = 30,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
):
    """追溯模块概览：批次数、覆盖率、供应商 Top3。"""
    return {"code": 0, "message": "成功", "data": svc.overview(conn, days=days)}


@router.get("/lots")
async def list_lots(
    supplier: Optional[str] = None,
    material_code: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
):
    """物料批列表。"""
    rows = repo.list_material_lots(
        conn, supplier=supplier, material_code=material_code, status=status, limit=limit
    )
    return {"code": 0, "message": "成功", "data": {"lots": rows, "total": len(rows)}}


class LotIn(BaseModel):
    """登记物料批。"""

    lot_id: str = Field(min_length=1, max_length=64, description="内部批号")
    material_code: str = Field(min_length=1, max_length=64)
    material_name: str = Field(min_length=1, max_length=128)
    supplier: Optional[str] = None
    supplier_lot: Optional[str] = None
    qty: float = Field(default=0, ge=0, description="批量")
    unit: str = "kg"
    occurred_at: Optional[str] = None


@router.post("/lots")
async def create_lot(
    body: LotIn,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
):
    """登记一个物料批（幂等：同批号重复登记返回原记录）。"""
    row = repo.insert_material_lot(
        conn, body.lot_id, body.material_code, body.material_name,
        supplier=body.supplier, supplier_lot=body.supplier_lot,
        qty=body.qty, unit=body.unit, occurred_at=body.occurred_at,
    )
    return {"code": 0, "message": "成功", "data": {"lot": row}}


class ConsumeIn(BaseModel):
    """登记领料关系。"""

    work_order_id: str = Field(min_length=1, description="工单号")
    qty_used: float = Field(default=0, ge=0, description="本单用量")
    op_code: Optional[str] = None
    occurred_at: Optional[str] = None


@router.post("/lots/{lot_id}/consume")
async def consume(
    lot_id: str,
    body: ConsumeIn,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
):
    """把一批料挂到工单上。

    两道校验都不是多余的：
    1. 批次不存在 → 40407，而不是建一条孤立记录（孤立记录会让正查结果为空，
       排查时以为是"没领用"，实际是"批号打错了"）；
    2. 工单不存在 → 40404，理由同上。
    """
    if repo.get_material_lot(conn, lot_id) is None:
        raise ApiError(ErrorCode.LOT_NOT_FOUND, "批次 %s 不存在" % lot_id)
    if repo.get_work_order(conn, body.work_order_id) is None:
        raise ApiError(ErrorCode.WORK_ORDER_NOT_FOUND, "工单 %s 不存在" % body.work_order_id)

    row = repo.link_lot_to_work_order(
        conn, lot_id, body.work_order_id,
        qty_used=body.qty_used, op_code=body.op_code, occurred_at=body.occurred_at,
    )
    return {
        "code": 0,
        "message": "成功" if row.get("inserted") else "已存在，用量已累加",
        "data": {"link": row},
    }


@router.get("/lots/{lot_id}")
async def forward(
    lot_id: str,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
):
    """正查：这批料 → 工单 → 不良 → 影响面判定。"""
    data = svc.forward_trace(conn, lot_id)
    if not data.get("ok"):
        raise ApiError(ErrorCode.LOT_NOT_FOUND, data.get("message") or "批次不存在")
    return {"code": 0, "message": "成功", "data": data}


@router.get("/lots/{lot_id}/graph")
async def graph(
    lot_id: str,
    max_orders: int = 20,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
):
    """谱系图：节点与边（前端直接渲染）。"""
    data = svc.build_graph(conn, lot_id, max_orders=max_orders)
    if not data.get("ok"):
        raise ApiError(ErrorCode.LOT_NOT_FOUND, data.get("message") or "批次不存在")
    return {"code": 0, "message": "成功", "data": data}


@router.get("/defects/{defect_no}/backward")
async def backward(
    defect_no: str,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
):
    """反查：不良单 → 工单 → 物料批 → 供应商 + 嫌疑排序。"""
    data = svc.backward_trace(conn, defect_no)
    if not data.get("ok"):
        raise ApiError(ErrorCode.DEFECT_NOT_FOUND, data.get("message") or "不良单不存在")
    return {"code": 0, "message": "成功", "data": data}


@router.get("/suppliers/scorecard")
async def scorecard(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
):
    """供应商记分卡（含口径说明，避免被当成索赔依据）。"""
    return {
        "code": 0, "message": "成功",
        "data": svc.supplier_scorecard(conn, date_from=date_from, date_to=date_to, days=days),
    }
