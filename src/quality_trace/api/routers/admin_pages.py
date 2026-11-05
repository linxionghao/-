# -*- coding: utf-8 -*-
"""主管后台 · 页面路由（服务端渲染，Jinja2，docs/stage3-design.md 4.1 节）。

与 JSON 接口的分工：
- 页面路由（/admin/*）：登录页、仪表盘、知识库、提示词——**后端模板直出**，
  刷新/打印可用；页面身份走 HttpOnly Cookie（qt_token），过期自动踢回登录页；
- JSON 接口（/api/v1/admin/*）：页面里的增强交互（上传/下架/保存模板）
  由原生 JS fetch 调用，同一 Cookie 自动随请求发送。

页面守卫语义：未登录 → 302 登录页；非主管角色 → 403 页面（不是 JSON）。
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Dict

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from quality_trace.api.deps import AUTH_COOKIE, WEB_DIR, get_current_user
from quality_trace.core.repo import db as repo
from quality_trace.core.services import auth as auth_service
from quality_trace.core.services.errors import ApiError, ErrorCode

router = APIRouter(prefix="/admin", include_in_schema=False)

templates = Jinja2Templates(directory=str(WEB_DIR / "admin"))


# ================================================================ 页面守卫
def _page_user(request: Request):
    """解析页面访问者；未登录/令牌无效返回 None（路由跳登录页）。

    页面场景不用 JSON 401，而是 302 到 /admin/login，带 next 参数回跳。
    """
    try:
        return get_current_user(request, None, None, None)
    except ApiError:
        return None


def _require_manager(request: Request):
    """主管页面守卫：返回 (user, 错误响应)。错误响应非空时路由直接返回它。"""
    user = _page_user(request)
    if user is None:
        return None, RedirectResponse("/admin/login", status_code=303)
    if user["role"] != "quality_manager":
        return user, HTMLResponse(
            templates.get_template("forbidden.html").render(
                {"request": request, "user": user}
            ),
            status_code=403,
        )
    return user, None


# ================================================================ 登录 / 登出
@router.get("/login")
async def login_page(request: Request):
    """登录页（已登录主管直接进仪表盘）。"""
    user = _page_user(request)
    if user and user["role"] == "quality_manager":
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"request": request, "error": None},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """表单登录：成功写 HttpOnly Cookie 并 303 到仪表盘；失败回登录页带错误。"""
    auth_cfg = request.app.state.config.get("auth", {})
    secret = str(auth_cfg.get("secret", "quality-trace-demo-secret"))
    ttl = int(auth_cfg.get("token_ttl_s", 43200))
    try:
        result = auth_service.login(
            request.app.state.db, username, password, secret, ttl
        )
    except ApiError as exc:
        # 业务码（40102）→ HTTP 状态码（401）：页面表单没有统一封装，状态码自己算
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"request": request, "error": exc.message},
            status_code=int(str(exc.code)[:3]),
        )
    resp = RedirectResponse("/admin", status_code=303)
    # HttpOnly：JS 读不到令牌，防 XSS 窃取；路径限定 /（含 API 路径）
    resp.set_cookie(AUTH_COOKIE, result["token"], max_age=ttl,
                    httponly=True, samesite="lax")
    return resp


@router.get("/logout")
async def logout():
    """登出：清 Cookie 回登录页。"""
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(AUTH_COOKIE)
    return resp


# ================================================================ 仪表盘
@router.get("/")
async def dashboard(request: Request):
    """仪表盘：文档/切片/会话/消息统计（服务端直出，刷新即可见）。"""
    user, err = _require_manager(request)
    if err:
        return err
    conn = request.app.state.db
    stats = {
        "docs_active": _count(conn, "kb_doc", "status='active'"),
        "docs_archived": _count(conn, "kb_doc", "status='archived'"),
        "chunks": _count(conn, "kb_chunk"),
        "sessions": _count(conn, "chat_session"),
        "messages": _count(conn, "chat_message"),
        "users": _count(conn, "sys_user"),
        # 批次 1c：越界标记与待审批归因单（主管最关心的两个数）
        "flagged": _count(
            conn, "chat_message",
            "role='assistant' AND (flag_root_cause=1 OR flag_no_citation=1"
            " OR flag_stale_version=1) AND reviewed_by IS NULL"),
        "attr_pending": _count(conn, "attribution_order", "status='submitted'"),
    }
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={"request": request, "user": user, "stats": stats},
    )


def _count(conn, table: str, where: str = "") -> int:
    """统计表行数（仪表盘小工具，只读）。"""
    sql = f"SELECT COUNT(*) AS n FROM {table}"   # where 由代码拼入，无注入面
    if where:
        sql += f" WHERE {where}"
    row = conn.execute(sql).fetchone()
    return int(row["n"]) if row else 0


# ================================================================ 知识库管理
@router.get("/documents")
async def documents_page(request: Request):
    """知识库管理页：上传表单 + 文档表格（含版本/生效区间/状态/操作）。"""
    user, err = _require_manager(request)
    if err:
        return err
    docs = repo.list_docs(request.app.state.db)
    return templates.TemplateResponse(
        request=request, name="documents.html",
        context={"request": request, "user": user,
                 "docs": docs, "doc_types": ["standard", "sop_manual", "8d_case"]},
    )


# ================================================================ 提示词模板
@router.get("/prompts")
async def prompts_page(request: Request):
    """提示词模板管理页：编辑器 + 修改历史（改完下一条回答立即生效）。"""
    user, err = _require_manager(request)
    if err:
        return err
    conn = request.app.state.db
    template = repo.get_active_template(conn)
    history = repo.list_template_history(conn)

    # 修改人显示名映射：库里存 user_id，页面要给人看的名字
    name_map = {u["user_id"]: u["display_name"] for u in repo.list_users(conn)}
    name_map.setdefault("seed", "初始化脚本")
    for row in ([template] if template else []) + list(history):
        row["updated_by_name"] = name_map.get(row["updated_by"], row["updated_by"])

    return templates.TemplateResponse(
        request=request, name="prompts.html",
        context={"request": request, "user": user,
                 "template": template, "history": history},
    )


# ================================================================ 对话审计（批次 1c）
@router.get("/audit")
async def audit_page(
    request: Request,
    role: str = "", batch_no: str = "",
    date_from: str = "", date_to: str = "",
    only_flagged: str = "",
):
    """对话审计页：筛选栏（角色/批次/时间/仅看越界）+ 记录表（服务端直出）。

    越界行红底 + 三类徽章；点开 <details> 看引用切片；JS 调核查接口后局部刷新。
    """
    user, err = _require_manager(request)
    if err:
        return err
    conn = request.app.state.db
    rows = repo.list_audit_messages(
        conn,
        role=role or None, batch_no=batch_no or None,
        date_from=date_from or None, date_to=date_to or None,
        only_flagged=only_flagged == "1",
    )
    # 引用随行带出（抽屉展开即看，不必再发请求）
    for row in rows:
        row["citations"] = repo.get_citations(conn, row["message_id"])
    flagged_count = sum(
        1 for r in rows
        if r["flag_root_cause"] or r["flag_no_citation"] or r["flag_stale_version"]
    )
    return templates.TemplateResponse(
        request=request, name="audit.html",
        context={"request": request, "user": user, "messages": rows,
                 "flagged_count": flagged_count,
                 "filters": {"role": role, "batch_no": batch_no,
                             "date_from": date_from, "date_to": date_to,
                             "only_flagged": only_flagged == "1"}},
    )


# ================================================================ 归因审批（批次 1c）
@router.get("/attributions")
async def attributions_page(request: Request, status: str = "submitted"):
    """归因审批列表：待审/已审 Tab（status 参数驱动，服务端渲染）。"""
    user, err = _require_manager(request)
    if err:
        return err
    conn = request.app.state.db
    if status not in ("submitted", "approved", "rejected", "all"):
        status = "submitted"
    rows = repo.list_attributions(
        conn, status=None if status == "all" else status
    )
    # 关联缺陷单概要与提交人显示名（表格直接展示）
    name_map = {u["user_id"]: u["display_name"] for u in repo.list_users(conn)}
    for row in rows:
        row["defect"] = repo.get_defect(conn, row["defect_no"]) or {}
        row["submitted_by_name"] = name_map.get(
            row["submitted_by"], row["submitted_by"] or "—")
        row["decided_by_name"] = name_map.get(
            row["decided_by"], row["decided_by"] or "")
    pending_total = _count(conn, "attribution_order", "status='submitted'")
    return templates.TemplateResponse(
        request=request, name="attributions.html",
        context={"request": request, "user": user, "rows": rows,
                 "status": status, "pending_total": pending_total},
    )


@router.get("/attributions/{attribution_id}")
async def attribution_detail_page(request: Request, attribution_id: str):
    """归因单详情：证据链 + L3 假设 + 审批动作 + 导出 8D。"""
    user, err = _require_manager(request)
    if err:
        return err
    conn = request.app.state.db
    from quality_trace.core.services import attribution as attr_service

    row = repo.get_attribution(conn, attribution_id)
    if row is None:
        return templates.TemplateResponse(
            request=request, name="attribution_detail.html",
            context={"request": request, "user": user,
                     "attribution": None, "defect": {},
                     "evidence": [], "not_found": True},
            status_code=404,
        )
    defect = repo.get_defect(conn, row["defect_no"]) or {}
    evidence = attr_service.pretty_evidence(row["evidence_json"])
    name_map = {u["user_id"]: u["display_name"] for u in repo.list_users(conn)}
    row["submitted_by_name"] = name_map.get(
        row["submitted_by"], row["submitted_by"] or "—")
    row["decided_by_name"] = name_map.get(
        row["decided_by"], row["decided_by"] or "")
    return templates.TemplateResponse(
        request=request, name="attribution_detail.html",
        context={"request": request, "user": user, "attribution": row,
                 "defect": defect, "evidence": evidence, "not_found": False},
    )


# ================================================================ 质量看板
@router.get("/analytics")
async def analytics_page(request: Request):
    """质量看板（SPC / 帕累托 / 趋势）。

    权限：主管与质量工程师。班组长不进后台——他的界面是 /m，
    看板上的"哪条线最差"对现场是压力而不是工具，现场需要的是"我这一单该选哪个码"。

    页面只出**骨架**，数据由 /api/v1/analytics/dashboard 现取。
    为什么这页不做服务端直出：控制图要按项目切换、筛选要联动，
    每一项都走一次整页刷新，体验会退回到 2005 年；而分析接口天然是 JSON 的。
    与文档页/审计页的取舍不同，那两页的核心是"看列表"，适合直出。
    """
    user = _page_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] not in ("quality_manager", "quality_engineer"):
        return HTMLResponse(
            templates.get_template("forbidden.html").render(
                {"request": request, "user": user}
            ),
            status_code=403,
        )

    conn = request.app.state.db
    today = _dt.date.today()
    ctx = {
        "request": request,
        "user": user,
        "lines": repo.distinct_lines(conn),
        "items": repo.list_measurement_items(conn),
        "default_from": (today - _dt.timedelta(days=29)).isoformat(),
        "default_to": today.isoformat(),
    }
    return templates.TemplateResponse(request=request, name="analytics.html", context=ctx)


# ================================================================ 批次追溯
@router.get("/genealogy")
async def genealogy_page(request: Request):
    """批次谱系页（正查 / 反查 / 供应商记分卡）。

    页面只出骨架，数据走 /api/v1/genealogy/*：
    三个 tab 之间是"换数据不换页面"的关系，做成整页刷新会丢掉当前选中的批次。
    """
    user = _page_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] not in ("quality_manager", "quality_engineer"):
        return HTMLResponse(
            templates.get_template("forbidden.html").render(
                {"request": request, "user": user}
            ),
            status_code=403,
        )
    return templates.TemplateResponse(
        request=request, name="genealogy.html",
        context={"request": request, "user": user},
    )


# ================================================================ 数据导入导出
@router.get("/data")
async def data_page(request: Request):
    """数据导入导出页。

    权限：主管与质量工程师。**班组长不进这页**——导入是唯一能一次写坏
    主数据表的入口，而现场录入的正确通道是 /m 的单条提交。

    这页的数据全部走 /api/v1/io/*：数据集清单由后端给出（加数据集不用改页面），
    文件上传必须走 XHR（表单直传拿不到逐行错误清单）。
    """
    user = _page_user(request)
    if user is None:
        return RedirectResponse("/admin/login", status_code=303)
    if user["role"] not in ("quality_manager", "quality_engineer"):
        return HTMLResponse(
            templates.get_template("forbidden.html").render(
                {"request": request, "user": user}
            ),
            status_code=403,
        )
    return templates.TemplateResponse(
        request=request, name="data.html",
        context={"request": request, "user": user},
    )
