# -*- coding: utf-8 -*-
"""认证路由：登录签发 JWT + 查询当前登录人（docs/stage3-design.md 1.1 节）。

三角色共用此入口；角色差异全部在 require_roles 各路由处表达，
登录接口本身不区分角色。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from quality_trace.api.deps import (
    LoginRequest,
    get_config,
    get_current_user,
    get_db,
)
from quality_trace.core.services import auth as auth_service

router = APIRouter(prefix="/api/v1")


@router.post("/auth/login")
async def login(
    body: LoginRequest,
    conn=Depends(get_db),
    cfg: Dict[str, Any] = Depends(get_config),
):
    """登录：校验用户名密码，签发 12 小时 JWT。

    返回 {token, user}；前端把 token 存起来放 Authorization: Bearer 头。
    失败码：40102（用户名或密码错误）/ 40301（账号停用）。
    """
    auth_cfg = cfg.get("auth", {})
    result = auth_service.login(
        conn,
        body.username,
        body.password,
        secret=str(auth_cfg.get("secret", "quality-trace-demo-secret")),
        ttl_seconds=int(auth_cfg.get("token_ttl_s", 43200)),
    )
    return {
        "code": 0,
        "message": "成功",
        "data": {"token": result["token"], "user": result["user"]},
    }


@router.get("/auth/me")
async def me(user: Dict[str, Any] = Depends(get_current_user)):
    """查询当前登录人（前端进入页面时校验令牌是否仍有效）。

    令牌无效 → 40101/40103（全局异常处理器统一封装）。
    三个角色都可调（仅读取本人信息，无数据范围问题），故不设角色白名单。
    """
    return {"code": 0, "message": "成功", "data": {"user": user}}
