# -*- coding: utf-8 -*-
"""缺陷录入服务：两步落库（draft → 人工确认 → pending）。

现场录入的核心约束（来自需求与设计文档第 5 节）：
1. **双时间戳**：occurred_at 是现场发生时刻（客户端传，离线暂存时=暂存时刻），
   recorded_at 由服务端落库时生成；两者相差 > 60 秒自动打「补录」标记；
2. **只分类不判定**：服务只写缺陷码（分类），任何合格性判定都不在这里发生；
   状态只到 pending，判定权在质量工程师；
3. **人工确认是硬闸**：归一化给出的候选只是建议，必须班组长点选后 confirm 才生效；
4. **幂等**：离线补传靠 client_draft_id 唯一键，重复提交返回原单而非新单。

【核心模块】core 约束：不 print、不读环境变量、不 import argparse。
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from quality_trace.core.repo import db as repo
from quality_trace.core.services import normalize as normalize_service
from quality_trace.core.services import photo as photo_service
from quality_trace.core.services.errors import ApiError, ErrorCode

# 补录判定阈值：录入时刻比发生时刻晚 60 秒以上即视为补录
BACKFILL_THRESHOLD_SECONDS = 60

# 班组长可见字段白名单（SQL 层就该裁剪；这里再做一次响应裁剪双保险）
SL_VISIBLE_FIELDS = (
    "defect_no", "work_order_id", "op_code", "description", "qty", "severity",
    "defect_code", "status", "is_backfill", "occurred_at", "recorded_at",
)

# 对外状态文案：不带任何判定语汇（不出现合格/不合格/判退）
STATUS_TEXT = {"draft": "待确认缺陷码", "pending": "已提交，待质量工程师判定"}

# 严重度合法值
SEVERITIES = ("轻微", "一般", "严重")


def _now() -> _dt.datetime:
    """本地当前时间（与 repo 的 ISO 秒级格式保持同一时区语义）。"""
    return _dt.datetime.now().replace(microsecond=0)


def parse_occurred_at(raw: Optional[str]) -> _dt.datetime:
    """解析客户端传来的发生时刻；缺失/非法一律用服务端当前时间兜底。

    只接受 ISO 8601（`2026-11-05T14:30:00` 或带 Z/时区偏移），
    时区感知的时间统一转成本地朴素时间，便于与 recorded_at 直接相减。
    """
    if not raw:
        return _now()
    try:
        value = _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return _now()
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    # 客户端时钟超前（>5 分钟）时按服务端时间记，避免负的「补录时长」
    if value > _now() + _dt.timedelta(minutes=5):
        return _now()
    return value.replace(microsecond=0)


def is_backfill(occurred: _dt.datetime, recorded: _dt.datetime,
                declared: bool = False) -> bool:
    """补录判定：客户端显式声明，或录入比发生晚超过阈值。"""
    if declared:
        return True
    return (recorded - occurred).total_seconds() > BACKFILL_THRESHOLD_SECONDS


def public_view(row: Dict[str, Any], role: str) -> Dict[str, Any]:
    """按角色裁剪响应字段。

    班组长（shift_leader）只拿到白名单字段：候选码来源、归因关联、判定结论
    这些字段不在响应里出现——**不是前端隐藏，是服务端根本不返回**。
    """
    view = {
        "defect_no": row.get("defect_no"),
        "work_order_id": row.get("work_order_id"),
        "op_code": row.get("op_code") or "",
        "description": row.get("description") or "",
        "qty": row.get("qty"),
        "severity": row.get("severity") or "",
        "defect_code": row.get("defect_code"),
        "status": row.get("status"),
        "status_text": STATUS_TEXT.get(row.get("status") or "", row.get("status")),
        "is_backfill": bool(row.get("is_backfill")),
        "occurred_at": row.get("occurred_at"),
        "recorded_at": row.get("recorded_at"),
        "has_photo": bool(row.get("photo_path")),
    }
    if role != "shift_leader":
        # 质量工程师/主管可见：候选码与照片路径（审计与追溯需要）
        view["suggest_json"] = row.get("suggest_json") or ""
        view["photo_path"] = row.get("photo_path")
        view["line_id"] = row.get("line_id")
        view["created_by"] = row.get("created_by")
        view["confirmed_at"] = row.get("confirmed_at")
    return view


def _check_work_order(conn, work_order_id: str, line_id: Optional[str]) -> Dict:
    """工单必须存在，且（班组长）必须属于本产线——否则 40406，不泄露他线工单。"""
    row = conn.execute(
        "SELECT * FROM work_order WHERE work_order_id = ?", (work_order_id,)
    ).fetchone()
    if row is None:
        raise ApiError(ErrorCode.WORK_ORDER_NOT_FOUND)
    order = dict(row)
    if line_id and order["line_id"] != line_id:
        raise ApiError(ErrorCode.WORK_ORDER_NOT_FOUND)
    return order