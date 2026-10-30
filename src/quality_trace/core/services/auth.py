"""鉴权服务：手写 HS256 JWT + 密码哈希 + 登录校验（docs/stage3-design.md 第 1 节）。

【核心模块】阶段 3 权限基座，后续所有接口的鉴权都依赖这里。

设计要点（决策 A①：手写 HS256，零新依赖）：
- JWT 三段式 header.payload.signature，全部用标准库实现（hmac/base64/json）；
- payload 携带 {user_id, role, line_id, ...} —— **line_id 进令牌**，
  数据层过滤（班组长只看本产线）直接从令牌取，前端传什么都不算数；
- 密码存储：SHA-256(盐 + 密码)。课程演示定位；注释里说明生产应换 bcrypt/argon2；
- core 约束：不 print、不读环境变量（secret 由入口层从配置传入）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

from quality_trace.core.services.errors import ApiError, ErrorCode

# 密码哈希盐：课程演示用固定盐；生产环境应每用户独立盐 + bcrypt/argon2
PASSWORD_SALT = "quality-trace-demo-salt"

# 默认令牌有效期：12 小时（课程演示不做刷新令牌）
DEFAULT_TOKEN_TTL_S = 12 * 3600

# JWT 头部（固定 HS256）
_JWT_HEADER = {"alg": "HS256", "typ": "JWT"}


# ================================================================ 编解码工具
def _b64url_encode(data: bytes) -> str:
    """字节串 → URL 安全 Base64（JWT 规范：去掉 padding 的 '='）。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """URL 安全 Base64 → 字节串（容忍缺 padding：按 4 位对齐补 '='）。"""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


# ================================================================ 密码
def hash_password(password: str, salt: str = PASSWORD_SALT) -> str:
    """密码 → 十六进制摘要。注意：演示用 SHA-256，生产应换慢哈希。"""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str,
                    salt: str = PASSWORD_SALT) -> bool:
    """校验密码是否与存储的摘要一致（常数时间比较，防时序攻击）。"""
    return hmac.compare_digest(hash_password(password, salt), password_hash)


# ================================================================ JWT
def issue_token(payload: Dict[str, Any], secret: str,
                ttl_seconds: int = DEFAULT_TOKEN_TTL_S) -> str:
    """签发 JWT：payload 自动附加 iat（签发时刻）与 exp（过期时刻）。"""
    claims = dict(payload)
    now = int(time.time())
    claims.setdefault("iat", now)
    claims["exp"] = now + int(ttl_seconds)          # 每次签发都以当前时刻计算有效期

    header_b64 = _b64url_encode(
        json.dumps(_JWT_HEADER, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    payload_b64 = _b64url_encode(
        json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )
    # 签名内容是「头.载荷」两段原文，密钥为 UTF-8 字节
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = hmac.new(secret.encode("utf-8"), signing_input,
                         hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def decode_token(token: str, secret: str,
                 now: Optional[float] = None) -> Dict[str, Any]:
    """校验并解析 JWT，返回 payload 字典。

    失败语义（区别两种 401，前端可分别提示）：
    - 签名不对 / 结构坏 / 缺少必备声明 → 40101（令牌无效）；
    - 签名正确但 exp 已过 → 40103（登录已过期，引导重新登录）。

    参数 now 供测试注入「未来时刻」验证过期逻辑。
    """
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        raise ApiError(ErrorCode.UNAUTHORIZED) from None

    # 1) 签名校验（先验签再解析，顺序不可反）
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{header_b64}.{payload_b64}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
        raise ApiError(ErrorCode.UNAUTHORIZED)

    # 2) 解析 payload（验签通过后才解析，避免畸形数据提前炸出 500）
    try:
        claims = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(ErrorCode.UNAUTHORIZED) from None
    if not isinstance(claims, dict) or "user_id" not in claims or "exp" not in claims:
        raise ApiError(ErrorCode.UNAUTHORIZED)

    # 3) 过期校验：过期与无效分开报，前端跳转「重新登录」
    current = time.time() if now is None else now
    if int(claims["exp"]) < current:
        raise ApiError(ErrorCode.TOKEN_EXPIRED)
    return claims


# ================================================================ 登录
def login(conn, username: str, password: str, secret: str,
          ttl_seconds: int = DEFAULT_TOKEN_TTL_S) -> Dict[str, Any]:
    """登录校验：成功返回 {token, user}，失败抛 ApiError。

    安全细节：用户不存在与密码错误返回**同一个** 40102，
    不暴露「这个用户名是否存在」。
    """
    from quality_trace.core.repo import db as repo    # 延迟导入避免循环依赖

    row = repo.get_user_by_username(conn, username.strip())
    if row is None or not verify_password(password, row["password_hash"]):
        raise ApiError(ErrorCode.LOGIN_FAILED)
    if row.get("status") != "active":
        raise ApiError(ErrorCode.FORBIDDEN, "账号已停用，请联系质量主管")

    user_info = {
        "user_id": row["user_id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "line_id": row["line_id"],
    }
    # line_id 写进令牌：数据层过滤的唯一权威来源（docs/stage3-design.md 1.2）
    token = issue_token(user_info, secret, ttl_seconds)
    return {"token": token, "user": user_info}
