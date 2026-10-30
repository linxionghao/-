"""阶段 3 鉴权基座测试：JWT（手写 HS256）+ 密码 + 登录 + 角色依赖。

全部离线；不依赖 FastAPI 运行环境（require_roles 用假的 user 字典直测逻辑）。
"""

from __future__ import annotations

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services import auth as auth_svc
from quality_trace.core.services.errors import ApiError, ErrorCode

SECRET = "test-secret"


def _conn(tmp_path):
    """临时库 + 建表 + 一个三角色账号齐全的种子。"""
    conn = repo.connect(str(tmp_path / "auth.db"))
    repo.init_schema(conn)
    return conn


def _seed(conn):
    """灌入班组长 + 质量工程师各一名。"""
    repo.create_user(conn, "leader01", auth_svc.hash_password("pw1"),
                     "赵班组长", "shift_leader", "line-01")
    repo.create_user(conn, "qe001", auth_svc.hash_password("pw2"),
                     "顾工", "quality_engineer", None)


# ================================================================ JWT 编解码
class TestJwt:
    def test_issue_and_decode_roundtrip(self):
        """签发 → 解析：payload 完整且附带 iat/exp。"""
        token = auth_svc.issue_token(
            {"user_id": "usr-1", "role": "quality_engineer", "line_id": "line-01"},
            SECRET,
        )
        claims = auth_svc.decode_token(token, SECRET)
        assert claims["user_id"] == "usr-1"
        assert claims["role"] == "quality_engineer"
        assert claims["line_id"] == "line-01"
        assert "exp" in claims and "iat" in claims

    def test_decode_expired_raises_40103(self):
        """过期令牌 → 40103（与无效令牌区分，前端引导重新登录）。"""
        token = auth_svc.issue_token({"user_id": "usr-1"}, SECRET, ttl_seconds=-10)
        with pytest.raises(ApiError) as e:
            auth_svc.decode_token(token, SECRET)
        assert e.value.code == ErrorCode.TOKEN_EXPIRED

    def test_tampered_signature_raises_40101(self):
        """签名被篡改 → 40101（未登录或身份无效）。"""
        token = auth_svc.issue_token({"user_id": "usr-1"}, SECRET)
        header, payload, _ = token.split(".")
        bad = f"{header}.{payload}.deadbeef"
        with pytest.raises(ApiError) as e:
            auth_svc.decode_token(bad, SECRET)
        assert e.value.code == ErrorCode.UNAUTHORIZED

    def test_wrong_secret_raises_40101(self):
        """密钥不对 → 40101。"""
        token = auth_svc.issue_token({"user_id": "usr-1"}, SECRET)
        with pytest.raises(ApiError) as e:
            auth_svc.decode_token(token, "another-secret")
        assert e.value.code == ErrorCode.UNAUTHORIZED

    def test_malformed_token_raises_40101(self):
        """结构不是三段 → 40101（不能因畸形输入炸 500）。"""
        with pytest.raises(ApiError) as e:
            auth_svc.decode_token("not-a-jwt", SECRET)
        assert e.value.code == ErrorCode.UNAUTHORIZED


# ================================================================ 密码
class TestPassword:
    def test_hash_and_verify(self):
        h = auth_svc.hash_password("s3cret")
        assert auth_svc.verify_password("s3cret", h) is True
        assert auth_svc.verify_password("wrong", h) is False

    def test_hash_is_salted_deterministic(self):
        """同一盐同一密码 → 摘要一致（seed 脚本可重复执行的前提）。"""
        assert (auth_svc.hash_password("abc")
                == auth_svc.hash_password("abc"))


# ================================================================ 登录
class TestLogin:
    def test_login_success_returns_token_and_user(self, tmp_path):
        conn = _conn(tmp_path)
        _seed(conn)
        result = auth_svc.login(conn, "leader01", "pw1", SECRET)
        assert result["token"]
        # 令牌里带着 line_id：数据层过滤的唯一权威来源
        claims = auth_svc.decode_token(result["token"], SECRET)
        assert claims["line_id"] == "line-01"
        assert claims["role"] == "shift_leader"
        assert result["user"]["display_name"] == "赵班组长"

    def test_login_wrong_password_40102(self, tmp_path):
        conn = _conn(tmp_path)
        _seed(conn)
        with pytest.raises(ApiError) as e:
            auth_svc.login(conn, "leader01", "bad", SECRET)
        assert e.value.code == ErrorCode.LOGIN_FAILED

    def test_login_unknown_user_same_40102(self, tmp_path):
        """用户不存在与密码错误同码：不泄露账号是否存在。"""
        conn = _conn(tmp_path)
        _seed(conn)
        with pytest.raises(ApiError) as e:
            auth_svc.login(conn, "ghost", "whatever", SECRET)
        assert e.value.code == ErrorCode.LOGIN_FAILED

    def test_login_disabled_user_40301(self, tmp_path):
        conn = _conn(tmp_path)
        _seed(conn)
        conn.execute("UPDATE sys_user SET status = 'disabled'"
                    " WHERE username = 'leader01'")
        conn.commit()
        with pytest.raises(ApiError) as e:
            auth_svc.login(conn, "leader01", "pw1", SECRET)
        assert e.value.code == ErrorCode.FORBIDDEN
        assert "停用" in e.value.message


# ================================================================ 用户表
class TestUserRepo:
    def test_create_user_idempotent_by_username(self, tmp_path):
        """同名用户重复创建 → 返回既有行（seed 幂等）。"""
        conn = _conn(tmp_path)
        a = repo.create_user(conn, "qe001", "h1", "顾工", "quality_engineer")
        b = repo.create_user(conn, "qe001", "h2", "顾工2", "quality_manager")
        assert a["user_id"] == b["user_id"]
        assert len(repo.list_users(conn)) == 1

    def test_get_by_username(self, tmp_path):
        conn = _conn(tmp_path)
        _seed(conn)
        assert repo.get_user_by_username(conn, "qe001")["role"] == "quality_engineer"
        assert repo.get_user_by_username(conn, "nobody") is None
