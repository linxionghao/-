# -*- coding: utf-8 -*-
"""MSA 测量系统分析接口：先证明尺子准，再谈工艺稳。

    #57 GET  /msa/overview                  质量工程师 / 主管
    #58 GET  /msa/studies                   同上（研究记录列表）
    #59 POST /msa/studies                   同上（提交一次研究：计算 + 留档）
    #60 GET  /msa/studies/{id}              同上（详情：结果快照 + 原始读数）
    #61 POST /msa/studies/{id}/recompute    同上（用留档读数重算，不覆盖）
    #62 GET  /msa/consistency               同上（产线数据人员一致性检查，非 GR&R）
    #63 GET  /msa/advice                    同上（正式 GR&R 试验方案建议）
    #64 GET  /msa/gaps                      同上（有实测值但没做过 MSA 的项目）

为什么班组长没有权限：
他看到 "%GRR=32%，尺子不合格" 只能得出"那我今天别测了"。
这不是他要处理的问题，而是质量工程师要立项解决的问题。
权限边界在这里的意义是：**不让一线为二线的问题焦虑**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from quality_trace.api.deps import get_db, require_roles
from quality_trace.core.analytics import msa as calc
from quality_trace.core.repo import db as repo
from quality_trace.core.services import msa as svc

router = APIRouter(prefix="/api/v1/msa")

_engineer = require_roles("quality_engineer", "quality_manager")


# =========================================================== 概览与清单
@router.get("/overview")
async def overview(
    limit: int = 10,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """MSA 概览：次数、结论分布、覆盖率、"没做过 MSA 的项目"。"""
    return {"code": 0, "message": "成功", "data": svc.overview(conn, limit=limit)}


@router.get("/gaps")
async def gaps(
    limit: int = 50,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """有实测值但从未做过 MSA 的检验项目。

    这个接口的存在本身就是一种态度：把"没验证过尺子就画控制图"
    从隐性风险变成一张可以派活的清单。
    """
    rows = repo.items_without_msa(conn, limit=limit)
    return {
        "code": 0, "message": "成功",
        "data": {
            "items": rows,
            "total": len(rows),
            "note": "这些项目当前的 Cp/Cpk 只能当参考值，不能作为放行依据",
        },
    }


# =========================================================== 研究记录
@router.get("/studies")
async def list_studies(
    item_code: Optional[str] = None,
    verdict: Optional[str] = None,
    limit: int = 50,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """研究记录列表（不含原始读数，列表页不需要几万行）。"""
    return {"code": 0, "message": "成功",
            "data": svc.list_studies(conn, item_code=item_code,
                                     verdict=verdict, limit=limit)}


class ReadingIn(BaseModel):
    """单条读数。"""

    appraiser: str = Field(min_length=1, max_length=64, description="测量人")
    part: str = Field(min_length=1, max_length=64, description="零件/工单")
    trial: int = Field(default=1, ge=1, le=20, description="第几次试测")
    value: float = Field(description="读数")


class StudyIn(BaseModel):
    """提交一次 MSA 研究。"""

    item_code: str = Field(min_length=1, max_length=64, description="检验项目")
    rows: List[ReadingIn] = Field(min_length=1, description="读数行")
    gauge: Optional[str] = Field(default=None, description="量具编号")
    line_id: Optional[str] = None
    tolerance: Optional[float] = Field(
        default=None, gt=0,
        description="公差带宽（USL-LSL）；给了才计算 %Tolerance")
    run_anova: bool = Field(default=True, description="是否同时跑 ANOVA 法")
    occurred_at: Optional[str] = Field(default=None, description="研究时刻")


@router.post("/studies")
async def create_study(
    body: StudyIn,
    user: Dict[str, Any] = Depends(_engineer),
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """提交一次研究：现算 GRR、留档快照。

    留档而不是只返回结果，是因为 MSA 结论要能回答"半年前凭什么签字"。
    """
    result = svc.create_study(
        conn,
        item_code=body.item_code,
        rows=[r.model_dump() for r in body.rows],
        gauge=body.gauge, line_id=body.line_id, tolerance=body.tolerance,
        run_anova=body.run_anova, created_by=user.get("username", ""),
        occurred_at=body.occurred_at,
    )
    return {"code": 0, "message": "研究已记录", "data": result}


@router.get("/studies/{study_id}")
async def get_study(
    study_id: str,
    with_raw: bool = Query(default=True, description="是否返回原始读数"),
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """研究详情（结果快照 + 可选原始读数）。"""
    return {"code": 0, "message": "成功",
            "data": svc.get_study(conn, study_id, with_raw=with_raw)}


@router.post("/studies/{study_id}/recompute")
async def recompute(
    study_id: str,
    run_anova: bool = True,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """用留档读数重算，**不覆盖**留档结果。

    覆盖会让审计链断掉：半年后有人问"这个数为什么变了"，
    没有旧值就答不上来。所以这里并排返回 fresh 与 archived。
    """
    return {"code": 0, "message": "重算完成",
            "data": svc.recompute(conn, study_id, run_anova=run_anova)}


# =========================================================== 产线数据一致性
@router.get("/consistency")
async def consistency(
    item_code: str = Query(..., description="检验项目"),
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_readings: int = Query(default=3, ge=2, le=100,
                              description="每个单元格最少读数条数"),
    z_limit: float = Query(default=3.0, gt=0, le=6,
                           description="均值差判定阈值（倍标准误）"),
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """人员读数一致性检查（**不是 GR&R**）。

    产线数据里每件产品只测一次，"同一人重复测同一件"这件事不存在，
    于是重复性与零件变差无法分离——硬算出来的 %GRR 会到 90% 以上，
    那个数字是算法制造的，不是产线的真实状态。

    这个接口回答的是它答得了的问题：同一条线、同一张工单，
    不同班次的人读出来的水平一致吗？差异显著就说明测量方法没对齐。
    """
    data = svc.consistency_check(
        conn, item_code=item_code, line_id=line_id,
        date_from=date_from, date_to=date_to,
        min_readings=min_readings, z_limit=z_limit,
    )
    return {"code": 0, "message": "成功" if data.get("ok") else data.get("message"),
            "data": data}


@router.get("/advice")
async def advice(
    item_code: Optional[str] = None,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """正式 GR&R 的试验方案建议（件数/人数/次数/判定阈值/公差带提醒）。"""
    return {"code": 0, "message": "成功",
            "data": svc.design_advice(conn, item_code=item_code)}
