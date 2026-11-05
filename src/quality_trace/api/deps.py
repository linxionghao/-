"""请求/响应模型与公共依赖（FastAPI Dependency）。

鉴权（阶段 3，docs/stage3-design.md 第 1 节）：
- 正式路径：Authorization: Bearer <JWT>（手写 HS256，见 core/services/auth.py）；
- 演示旁路：auth.dev_header_bypass=true 时允许 X-User-Id/X-Role 请求头绕过
  （仅供课程演示与既有页面过渡，生产必须置 false）；
- 角色校验：require_roles(...) 工厂做路由级白名单（40301），
  数据级过滤（班组长本产线）在各 repo 函数里按令牌 line_id 拼条件。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import Depends, Header, Request
from pydantic import BaseModel, Field

from quality_trace.core.services import auth as auth_service
from quality_trace.core.services.errors import ApiError, ErrorCode
from quality_trace.utils.config import load_config

# 仓库根目录（api 包向上三级）：用于定位 config / data / web 静态目录
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "config" / "llm.yaml"
WEB_DIR = REPO_ROOT / "src" / "quality_trace" / "web"

# 全部合法角色（路由级白名单从这里取）
ALL_ROLES = ("quality_engineer", "shift_leader", "quality_manager")

# 管理后台登录后写入的 Cookie 名（HttpOnly；页面路由与 JS fetch 靠它保会话）
AUTH_COOKIE = "qt_token"


# ================================================================ 请求模型
class LoginRequest(BaseModel):
    """POST /api/v1/auth/login 请求体。"""

    username: str = Field(min_length=1, max_length=64, description="登录名")
    password: str = Field(min_length=1, max_length=128, description="密码")


class ChatRequest(BaseModel):
    """POST /api/v1/chat 请求体。"""

    session_id: Optional[str] = Field(default=None, description="为空则自动创建")
    question: str = Field(min_length=1, max_length=2000, description="用户问题")
    as_of: Optional[str] = Field(
        default=None,
        description="业务时刻（YYYY-MM-DD 或 ISO 8601）：按该时刻生效的标准检索，"
                    "用于追溯历史批次当时的判定依据",
    )


# ================================================================ 响应模型
class CitationOut(BaseModel):
    """引用条目：文档名 + 条款号 + 版本号（前端卡片三要素）。"""

    seq: int
    source_file: str
    doc_id: Optional[str] = None
    clause: Optional[str] = None
    version: Optional[str] = None
    doc_type: Optional[str] = None
    chunk_id: Optional[str] = None
    score: Optional[float] = None
    snippet: Optional[str] = None


class MessageOut(BaseModel):
    """历史消息（GET /messages 用）。"""

    message_id: str
    role: str
    content: str
    status: str
    degraded: bool
    degraded_code: Optional[str] = None
    degraded_reason: Optional[str] = None
    created_at: Optional[str] = None
    citations: list = []


# ================================================================ 公共依赖
def get_config(request: Request) -> Dict[str, Any]:
    """应用配置（启动时加载一次，缓存在 app.state）。"""
    return request.app.state.config


def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
    x_role: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """解析当前登录人：JWT 优先，演示态可走请求头旁路。

    返回 {user_id, username, display_name, role, line_id}，
    其中 line_id 只来自令牌/用户表 —— 数据层过滤的唯一权威来源。

    抛 ApiError（40101/40103/40301），由全局异常处理器转统一封装。
    """
    auth_cfg: Dict[str, Any] = request.app.state.config.get("auth", {})
    secret = str(auth_cfg.get("secret", "quality-trace-demo-secret"))

    # ---- 正式路径一：Bearer JWT（API 调用方 / 页面 JS fetch） ----
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise ApiError(ErrorCode.UNAUTHORIZED)
        claims = auth_service.decode_token(token.strip(), secret)
        return _user_from_claims(claims)

    # ---- 正式路径二：Cookie JWT（管理后台页面，登录后服务端写入） ----
    cookie_token = request.cookies.get(AUTH_COOKIE)
    if cookie_token:
        claims = auth_service.decode_token(cookie_token, secret)
        return _user_from_claims(claims)

    # ---- 演示旁路：X-User-Id / X-Role（仅当配置显式打开）----
    if auth_cfg.get("dev_header_bypass") and x_user_id:
        return {
            "user_id": x_user_id,
            "username": x_user_id,
            "display_name": x_user_id,
            "role": x_role or "quality_engineer",
            "line_id": None,       # 旁路身份不带产线，数据过滤交给业务层兜底
        }

    raise ApiError(ErrorCode.UNAUTHORIZED)


def _user_from_claims(claims: Dict[str, Any]) -> Dict[str, Any]:
    """从 JWT payload 提取用户信息（单一出口，字段增改只改这里）。"""
    return {
        "user_id": claims["user_id"],
        "username": claims.get("username", ""),
        "display_name": claims.get("display_name", ""),
        "role": claims.get("role", ""),
        "line_id": claims.get("line_id"),
    }


def require_roles(*allowed: str) -> Callable[..., Dict[str, Any]]:
    """角色白名单依赖工厂（路由级权限，docs/stage3-design.md 第 2 节矩阵的执行点）。

    用法：
        user: Dict = Depends(require_roles("quality_engineer"))
        user: Dict = Depends(require_roles("shift_leader", "quality_manager"))
    """
    allowed_set = set(allowed)

    def _dependency(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        if user["role"] not in allowed_set:
            raise ApiError(ErrorCode.FORBIDDEN)
        return user

    return _dependency


def get_db(request: Request):
    """SQLite 连接（应用启动时创建，WAL 模式，跨线程共享）。"""
    return request.app.state.db


def get_llm_key() -> str:
    """LLM API Key：入口层读环境变量，core 不碰 os.environ。"""
    return os.environ.get("QT_LLM_API_KEY", "")


def get_emb_key() -> str:
    """Embedding API Key：同上。"""
    return os.environ.get("QT_EMBEDDING_API_KEY", "")


def load_app_config() -> Dict[str, Any]:
    """启动时加载配置（config/llm.yaml + QT_ 环境变量覆盖）。"""
    if CONFIG_PATH.exists():
        return load_config(CONFIG_PATH)
    return load_config()          # 配置缺失时退回内置默认值
