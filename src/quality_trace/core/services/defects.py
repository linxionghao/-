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


def line_scope(user: Dict[str, Any]) -> Optional[str]:
    """数据级过滤的产线范围：班组长返回自己的产线，其余角色返回 None（全量）。

    班组长**未绑定产线**时一律 40302，绝不退化成「看全量」。
    安全相关的默认值必须是拒绝：配置漏填一个 line_id，不该变成越权。
    """
    if user.get("role") != "shift_leader":
        return None
    line_id = (user.get("line_id") or "").strip()
    if not line_id:
        raise ApiError(ErrorCode.LINE_FORBIDDEN)
    return line_id


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


def check_work_order(
    conn, user: Dict[str, Any], work_order_id: str,
) -> Dict[str, Any]:
    """移动端失焦校验：工单是否存在、是否属于本线。

    只回传必要字段（产品名、产线、状态），不把工单表整行抛给前端。
    班组长查他线工单同样返回 40406——校验接口不能变成探测工具。
    """
    work_order_id = (work_order_id or "").strip()
    if not work_order_id:
        raise ApiError(ErrorCode.BAD_REQUEST, "工单号不能为空")
    line_id = line_scope(user)
    order = _check_work_order(conn, work_order_id, line_id)
    return {
        "work_order_id": order["work_order_id"],
        "product_name": order["product_name"],
        "line_id": order["line_id"],
        "status": order["status"],
        "planned_qty": order["planned_qty"],
    }


def create_draft(
    conn, *, user: Dict[str, Any], work_order_id: str, description: str,
    op_code: str = "", qty: int = 1, severity: str = "一般",
    occurred_at: Optional[str] = None, photo: Optional[str] = None,
    photo_token: Optional[str] = None,
    client_draft_id: Optional[str] = None, declared_backfill: bool = False,
    llm_cfg: Optional[Dict] = None, api_key: str = "",
    upload_dir: str = "data/uploads",
) -> Dict[str, Any]:
    """第一步：建 draft 单 + 归一化候选码（此时**还没有**缺陷码）。

    返回内容供移动端渲染第 2 步「确认缺陷码」：
        {defect_no, status:'draft', suggested:[...], normalize:{...}, existing}
    """
    # ---- 1) 参数校验：宁可前端多拦一道，服务端也必须自己拦 ----
    work_order_id = (work_order_id or "").strip()
    description = (description or "").strip()
    if not work_order_id:
        raise ApiError(ErrorCode.BAD_REQUEST, "工单号不能为空")
    if len(description) < 4:
        raise ApiError(ErrorCode.BAD_REQUEST, "缺陷描述至少 4 个字，便于准确归类")
    if len(description) > 500:
        raise ApiError(ErrorCode.BAD_REQUEST, "缺陷描述最多 500 字")
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        raise ApiError(ErrorCode.BAD_REQUEST, "数量必须是整数") from None
    if qty < 1 or qty > 99999:
        raise ApiError(ErrorCode.BAD_REQUEST, "数量需在 1~99999 之间")
    if severity not in SEVERITIES:
        raise ApiError(ErrorCode.BAD_REQUEST, "严重度只能是 轻微/一般/严重")

    # ---- 2) 离线补传幂等：同一个 client_draft_id 永远只对应一张单 ----
    if client_draft_id:
        existing = repo.get_defect_by_draft_id(conn, client_draft_id)
        if existing:
            view = public_view(existing, user.get("role", ""))
            view.update({
                "suggested": _suggestions_of(existing),
                "normalize": {"source": "cached", "degraded": False,
                              "degraded_reason": "离线补传：返回首次提交结果"},
                "existing": True,
            })
            return view

    # ---- 3) 工单校验（班组长只能录本线工单；QE 代录时取工单所属产线）----
    line_id = line_scope(user)
    order = _check_work_order(conn, work_order_id, line_id)
    line_id = line_id or order["line_id"]

    # ---- 4) 照片先校验后建单：坏图不该留下一条无照片的脏单 ----
    blob_ext = photo_service.decode_photo(photo) if photo else None
    # 预上传 token（#29）与内联 base64 二选一；token 失效同样在建单前拦下
    if not blob_ext and photo_token:
        if photo_service.resolve_temp(photo_token, Path(upload_dir)) is None:
            raise ApiError(ErrorCode.PHOTO_INVALID)

    # ---- 5) 双时间戳与补录标记 ----
    occurred = parse_occurred_at(occurred_at)
    recorded = _now()
    backfill = is_backfill(occurred, recorded, declared_backfill)

    # ---- 6) 归一化（LLM 优先，失败回退关键词；此步绝不写库）----
    codes = repo.list_defect_codes(conn)
    norm = normalize_service.normalize(description, codes, llm_cfg, api_key)

    # ---- 7) 建单（status=draft，defect_code 留空）----
    defect_no = repo.new_defect_no(conn)
    row = repo.insert_defect(
        conn, defect_no, work_order_id, line_id, description,
        op_code=(op_code or "").strip(), qty=qty, severity=severity,
        defect_code=None,
        suggest_json=json.dumps(norm, ensure_ascii=False),
        client_draft_id=client_draft_id, status="draft",
        is_backfill=backfill, created_by=user.get("user_id", ""),
    )
    # insert_defect 用的是落库时刻，occurred_at 需按客户端时刻回写
    conn.execute(
        "UPDATE defect_item SET occurred_at=?, recorded_at=? WHERE defect_no=?",
        (occurred.isoformat(), recorded.isoformat(), defect_no),
    )
    conn.commit()
    row = repo.get_defect(conn, defect_no) or row

    # ---- 8) 照片落盘（失败已在上一步抛出，不会留下半截单）----
    if blob_ext:
        blob, ext = blob_ext
        path = photo_service.save_photo(blob, ext, defect_no, Path(upload_dir))
        repo.set_defect_photo(conn, defect_no, path)
        row = repo.get_defect(conn, defect_no) or row
    elif photo_token:
        path = photo_service.move_temp_to_defect(photo_token, defect_no,
                                                 Path(upload_dir))
        repo.set_defect_photo(conn, defect_no, path)
        row = repo.get_defect(conn, defect_no) or row

    view = public_view(row, user.get("role", ""))
    view.update({"suggested": norm["suggested"],
                 "normalize": {k: norm[k] for k in
                               ("source", "degraded", "degraded_reason")},
                 "existing": False})
    return view


def _suggestions_of(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 suggest_json 还原候选（补传返回原单时用，坏 JSON 容错为空）。"""
    try:
        data = json.loads(row.get("suggest_json") or "{}")
        return data.get("suggested", []) if isinstance(data, dict) else []
    except (ValueError, TypeError):
        return []


def confirm(
    conn, *, user: Dict[str, Any], defect_no: str, defect_code: str,
) -> Dict[str, Any]:
    """第二步：班组长亲手点选缺陷码 → draft 转 pending。

    - 只写分类字段，不写判定；
    - 已确认过的单不可改（40905），避免审计时出现「同一单两个码」；
    - 他线单据一律按不存在处理（40404），不暴露存在性。
    """
    defect_code = (defect_code or "").strip()
    if not defect_code:
        raise ApiError(ErrorCode.BAD_REQUEST, "请选择缺陷码后再提交")

    line_id = line_scope(user)
    row = repo.get_defect(conn, defect_no)
    if row is None:
        raise ApiError(ErrorCode.DEFECT_NOT_FOUND)
    if line_id and row["line_id"] != line_id:
        raise ApiError(ErrorCode.DEFECT_NOT_FOUND)

    code_row = repo.get_defect_code(conn, defect_code)
    if code_row is None or code_row.get("status") != "active":
        raise ApiError(ErrorCode.BAD_REQUEST, "缺陷码不存在或已停用，请重新选择")

    updated = repo.confirm_defect_code(conn, defect_no, defect_code,
                                       user.get("user_id", ""))
    if updated is None:
        raise ApiError(ErrorCode.DEFECT_STATE_INVALID)

    view = public_view(updated, user.get("role", ""))
    view["suggested"] = _suggestions_of(updated)
    return view


def list_for_user(conn, user: Dict[str, Any], limit: int = 50) -> List[Dict]:
    """列表：班组长只查本线（SQL 层过滤），字段再做一次白名单裁剪。"""
    line_id = line_scope(user)
    rows = repo.list_defects(conn, line_id=line_id, limit=limit)
    return [public_view(r, user.get("role", "")) for r in rows]


def get_for_user(conn, user: Dict[str, Any], defect_no: str) -> Dict[str, Any]:
    """详情：他线单据对班组长表现为「不存在」。"""
    line_id = line_scope(user)
    row = repo.get_defect(conn, defect_no)
    if row is None:
        raise ApiError(ErrorCode.DEFECT_NOT_FOUND)
    if line_id and row["line_id"] != line_id:
        raise ApiError(ErrorCode.DEFECT_NOT_FOUND)
    return public_view(row, user.get("role", ""))