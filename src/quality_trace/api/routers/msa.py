# -*- coding: utf-8 -*-
"""MSA 测量系统分析接口：先证明尺子准，再谈工艺稳。

    #57 GET  /msa/overview                  质量工程师 / 主管
    #58 GET  /msa/studies                   同上（研究记录列表）
    #59 POST /msa/studies                   同上（提交一次研究：计算 + 留档）
    #60 GET  /msa/studies/{id}              同上（详情：结果快照 + 原始读数）
    #61 POST /msa/studies/{id}/recompute    同上（用留档读数重算，不覆盖）
    #62 GET  /msa/from-measurements         同上（预览：从实测值折叠出的数据）
    #63 POST /msa/from-measurements         同上（一键折叠 + 计算 + 留档）
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


# =========================================================== 从实测值折叠
@router.get("/from-measurements")
async def preview_from_measurements(
    item_code: str = Query(..., description="检验项目"),
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    max_parts: int = 10,
    max_trials: int = 3,
    user: Dict[str, Any] = Depends(_engineer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """预览：产线实测值会被折叠成什么样的 MSA 数据。

    先给预览而不是直接算，是因为折叠是**近似**的（零件按工单归并），
    用户得先看一眼"零件数/试测次数/剔除了几个零件"，再决定要不要出结论。
    """
    built = svc.build_rows_from_measurements(
        conn, item_code=item_code, line_id=line_id,
        date_from=date_from, date_to=date_to,
        max_parts=max_parts, max_trials=max_trials,
    )
    if not built.get("ok"):
        return {"code": 0, "message": built.get("message") or "无法折叠",
                "data": built}
    # 预览同时给一份"如果用这批数据算，结论会是什么"
    preview = calc.overview(built["rows"])
    built["preview"] = preview
    return {"code": 0, "message": "成功", "data": built}


class FromMeasurementsIn(BaseModel):
    """一键从实测值生成研究记录。"""

    item_code: str = Field(min_length=1)
    line_id: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    tolerance: Optional[float] = Field(default=None, gt=0)
    max_parts: int = Field(default=10, ge=2, le=10)
    max_trials: int = Field(default=3, ge=2, le=5)


@router.post("/from-measurements")
async def create_from_measurements(
    body: FromMeasurementsIn,
    user: Dict[str, Any] = Depends(_engineer),
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """一键：折叠 → 计算 → 留档（source=measurements，证据强度弱于正式试验）。

    max_parts 上限卡 10，因为 AIAG 的 K3 系数表到 10 为止；
    放开上限只会得到一个"系数靠外推"的假结论。
    """
    result = svc.study_from_measurements(
        conn, item_code=body.item_code, created_by=user.get("username", ""),
        line_id=body.line_id, date_from=body.date_from, date_to=body.date_to,
        tolerance=body.tolerance, max_parts=body.max_parts,
        max_trials=body.max_trials,
    )
    return {"code": 0, "message": "研究已记录（数据来自产线实测值）",
            "data": result}
