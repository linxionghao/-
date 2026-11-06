# -*- coding: utf-8 -*-
"""统计分析接口：看板、帕累托、控制图、过程能力、趋势。

权限设计（延续"三层防线"）：

    #32 GET  /analytics/overview        三角色 ✓（班组长只能看本线，参数被强制覆写）
    #33 GET  /analytics/dashboard       三角色 ✓（同上）
    #34 GET  /analytics/pareto          三角色 ✓（同上）
    #35 GET  /analytics/pareto/compare  三角色 ✓（同上）
    #36 GET  /analytics/trend           三角色 ✓
    #37 GET  /analytics/weekday         三角色 ✓
    #38 GET  /analytics/control-chart   质量工程师 / 主管（计量数据属工艺参数，不开放给班组长）
    #39 GET  /analytics/p-chart         三角色 ✓
    #40 GET  /analytics/items           质量工程师 / 主管
    #41 POST /analytics/measurements    质量工程师 / 主管（现场由检验员在 QE 账号下录入）
    #42 POST /analytics/measurements/batch 同上

为什么班组长能看不良率、却看不到控制图：
控制图里带着实测值序列、量具编号、操作者，等价于把工艺窗口暴露给现场；
而不良率是"结果"，班组长知道结果才能改进。**给结果、不给过程参数**，
是制造业权限设计里一条很实用的分界线。

这条分界线有一个很容易漏的绕道：`/dashboard` 是三角色都能调的，
而它接受 `item_code` 参数、内部会去算控制图。不额外处理的话，
班组长加一个 `?item_code=外径` 就能从"给结果"的接口里把"过程参数"取走。
处置写在 `dashboard()` 里——**丢弃**参数，不报错。
凡是"聚合接口里嵌了敏感子结果"的地方都要这样查一遍，
因为权限挂在路由上，而敏感数据挂在参数上。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from quality_trace.api.deps import get_db, require_roles
from quality_trace.core.repo import db as repo
from quality_trace.core.services import analytics as svc
from quality_trace.core.services.errors import ApiError, ErrorCode

router = APIRouter(prefix="/api/v1/analytics")

_all_roles = require_roles("quality_engineer", "shift_leader", "quality_manager")
_engineer_only = require_roles("quality_engineer", "quality_manager")


def _scope(user: Dict[str, Any], line_id: Optional[str]) -> Optional[str]:
    """把"想看的产线"收敛成"能看的产线"。

    班组长：无论传什么，一律改成本线；未绑定产线 → 40302（不是"看全部"）。
    QE/QM：透传，None 表示全厂。

    注意这里是**覆写**而不是"校验后拒绝"：前者对前端更友好（班组长页面不用
    自己判断要不要带参数），后者会让页面多一条"你没权限"的死路。
    安全性由"覆写"保证——客户端传的值根本不生效。
    """
    if user.get("role") != "shift_leader":
        return line_id or None
    own = (user.get("line_id") or "").strip()
    if not own:
        raise ApiError(ErrorCode.LINE_FORBIDDEN, "当前账号未绑定产线，无法查看统计")
    return own


@router.get("/overview")
async def overview(
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    user: Dict[str, Any] = Depends(_all_roles),
    conn=Depends(get_db),
):
    """质量总览：KPI + 趋势 + 帕累托 + 严重度分布 + 数据质量自检。"""
    data = svc.overview(
        conn, line_id=_scope(user, line_id), date_from=date_from, date_to=date_to, days=days
    )
    return {"code": 0, "message": "成功", "data": data}


@router.get("/dashboard")
async def dashboard(
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    item_code: Optional[str] = None,
    user: Dict[str, Any] = Depends(_all_roles),
    conn=Depends(get_db),
):
    """看板首页一次性数据（总览 + 产线帕累托 + 星期效应 + 可选控制图）。

    班组长传 `item_code` 会被**丢弃**，不会拿到控制图。
    本模块的权限分界线是"给结果、不给过程参数"：不良率是结果，
    班组长知道结果才能改进；而控制图里带着实测值序列、量具编号、
    操作者，等价于把工艺窗口暴露给现场。

    这里的处置是丢弃而不是报错——报错等于告诉他"这个参数有用"，
    也等于让一个正常请求变成失败。丢弃后他拿到的是一份完整的看板，
    只是没有那一张图。
    """
    if user.get("role") == "shift_leader":
        item_code = None
    data = svc.dashboard(
        conn,
        line_id=_scope(user, line_id),
        date_from=date_from,
        date_to=date_to,
        days=days,
        item_code=item_code,
    )
    return {"code": 0, "message": "成功", "data": data}


@router.get("/pareto")
async def pareto(
    dim: str = "defect_code",
    weight: str = "qty",
    top_n: int = 10,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    user: Dict[str, Any] = Depends(_all_roles),
    conn=Depends(get_db),
):
    """帕累托分析。dim 见服务的 PARETO_DIMS 白名单。"""
    try:
        data = svc.pareto_by(
            conn, dim,
            line_id=_scope(user, line_id),
            date_from=date_from, date_to=date_to, days=days,
            top_n=top_n, weight=weight,
        )
    except ValueError as exc:
        raise ApiError(ErrorCode.BAD_REQUEST, str(exc))
    return {"code": 0, "message": "成功", "data": data}


@router.get("/pareto/compare")
async def pareto_compare(
    dim: str = "defect_code",
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    user: Dict[str, Any] = Depends(_all_roles),
    conn=Depends(get_db),
):
    """本期 vs 上期（等长区间）。"""
    try:
        data = svc.compare_periods(
            conn, dim=dim, line_id=_scope(user, line_id),
            date_from=date_from, date_to=date_to, days=days,
        )
    except ValueError as exc:
        raise ApiError(ErrorCode.BAD_REQUEST, str(exc))
    return {"code": 0, "message": "成功", "data": data}


@router.get("/trend")
async def trend(
    granularity: str = "day",
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    user: Dict[str, Any] = Depends(_all_roles),
    conn=Depends(get_db),
):
    """趋势体检：方向 / 斜率 / 显著性 / 移动平均。"""
    data = svc.trend_report(
        conn, line_id=_scope(user, line_id), date_from=date_from, date_to=date_to,
        days=days, granularity=granularity,
    )
    return {"code": 0, "message": "成功", "data": data}


@router.get("/weekday")
async def weekday(
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    user: Dict[str, Any] = Depends(_all_roles),
    conn=Depends(get_db),
):
    """星期效应：哪一天最容易出问题。"""
    data = svc.weekday_report(
        conn, line_id=_scope(user, line_id), date_from=date_from, date_to=date_to, days=days
    )
    return {"code": 0, "message": "成功", "data": data}


@router.get("/p-chart")
async def p_chart(
    granularity: str = "day",
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    user: Dict[str, Any] = Depends(_all_roles),
    conn=Depends(get_db),
):
    """不良率控制图（P 图）：没有实测值也能做的 SPC。"""
    data = svc.defect_p_chart(
        conn, line_id=_scope(user, line_id), date_from=date_from, date_to=date_to,
        days=days, granularity=granularity,
    )
    return {"code": 0, "message": "成功", "data": data}


@router.get("/control-chart")
async def control_chart(
    item_code: str,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    user: Dict[str, Any] = Depends(_engineer_only),
    conn=Depends(get_db),
):
    """计量型控制图（X̄-R / I-MR）+ 过程能力。"""
    data = svc.control_chart(
        conn, item_code=item_code, line_id=line_id or None,
        date_from=date_from, date_to=date_to, days=days,
    )
    if not data.get("ok"):
        # 没数据不是错误，是"还没录"，用 200 + ok=false 表达，前端显示引导文案
        return {"code": 0, "message": "暂无数据", "data": data}
    return {"code": 0, "message": "成功", "data": data}


@router.get("/items")
async def items(
    user: Dict[str, Any] = Depends(_engineer_only),   # noqa: ARG001
    conn=Depends(get_db),
):
    """可分析的检验项目清单（下拉框）。"""
    return {"code": 0, "message": "成功", "data": {"items": repo.list_measurement_items(conn)}}


# ================================================================ 实测值录入
class MeasurementIn(BaseModel):
    """单条实测值。"""

    item_code: str = Field(min_length=1, max_length=64, description="检验项目，如「外径」")
    value: float = Field(description="实测值")
    line_id: Optional[str] = Field(default=None, description="产线；班组长会被强制覆写")
    work_order_id: Optional[str] = None
    op_code: Optional[str] = None
    subgroup_no: int = Field(default=1, ge=0, description="子组号：同一次抽检的 5 件同号")
    unit: str = "mm"
    usl: Optional[float] = None
    lsl: Optional[float] = None
    gauge: Optional[str] = None
    occurred_at: Optional[str] = None


class MeasurementBatchIn(BaseModel):
    """批量录入（Excel 导入前的接口形态）。"""

    rows: List[MeasurementIn] = Field(min_length=1, max_length=5000)


@router.post("/measurements")
async def add_measurement(
    body: MeasurementIn,
    user: Dict[str, Any] = Depends(_engineer_only),
    conn=Depends(get_db),
):
    """写一条实测值。

    规格限校验：给了 usl 和 lsl 却 usl <= lsl，直接 40001——
    这种错误一旦入库，后面所有能力分析都会得到负数 Cpk，而且没人知道源头在哪。
    """
    if body.usl is not None and body.lsl is not None and body.usl <= body.lsl:
        raise ApiError(ErrorCode.BAD_REQUEST, "规格上限必须大于下限")
    row = repo.insert_measurement(
        conn,
        item_code=body.item_code,
        value=body.value,
        line_id=_scope(user, body.line_id) or "UNKNOWN",
        work_order_id=body.work_order_id,
        op_code=body.op_code,
        subgroup_no=body.subgroup_no,
        unit=body.unit,
        usl=body.usl,
        lsl=body.lsl,
        gauge=body.gauge,
        operator=user.get("username"),
        occurred_at=body.occurred_at,
    )
    return {"code": 0, "message": "成功", "data": {"record": row}}


@router.post("/measurements/batch")
async def add_measurements(
    body: MeasurementBatchIn,
    user: Dict[str, Any] = Depends(_engineer_only),
    conn=Depends(get_db),
):
    """批量写入实测值（一次事务）。"""
    rows = []
    for r in body.rows:
        if r.usl is not None and r.lsl is not None and r.usl <= r.lsl:
            raise ApiError(ErrorCode.BAD_REQUEST, "第 %d 行规格上下限颠倒" % (len(rows) + 1))
        rows.append(
            {
                "item_code": r.item_code,
                "value": r.value,
                "line_id": _scope(user, r.line_id) or "UNKNOWN",
                "work_order_id": r.work_order_id,
                "op_code": r.op_code,
                "subgroup_no": r.subgroup_no,
                "unit": r.unit,
                "usl": r.usl,
                "lsl": r.lsl,
                "gauge": r.gauge,
                "operator": user.get("username"),
                "occurred_at": r.occurred_at,
            }
        )
    n = repo.insert_measurements(conn, rows)
    return {"code": 0, "message": "成功", "data": {"inserted": n}}
