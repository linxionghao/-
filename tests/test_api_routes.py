# -*- coding: utf-8 -*-
"""HTTP 层测试：用 FastAPI TestClient 走真实路由与真实 SQLite。

与 service 层测试的分工：
- service 层（test_defects / test_audit …）测逻辑分支，不经过 FastAPI；
- 本文件测**接口契约**：HTTP 状态码、统一响应包、鉴权依赖、字段裁剪、页面可达。
  这些是单测最容易漏、而对接方最先踩的部分。

全部离线：临时库 + 临时上传目录 + 不配置 LLM Key（走关键词降级）。
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

import quality_trace.api.app as app_module
from quality_trace.core.repo import db as repo
from quality_trace.core.services import auth as auth_service

# 最小合法 JPEG 头（SOI + APP0），足以通过魔数校验
JPEG = base64.b64encode(
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 32
).decode()

USERS = [
    ("qe001", "qe001", "顾工", "quality_engineer", None),
    ("leader01", "leader01", "赵班组长", "shift_leader", "line-01"),
    ("leader02", "leader02", "孙班组长", "shift_leader", "line-02"),
    ("manager01", "manager01", "钱主管", "quality_manager", None),
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """独立的 app 实例：临时库 + 临时上传目录 + 关闭演示旁路。"""
    cfg = {
        "auth": {"secret": "test-secret", "token_ttl_s": 3600,
                 "dev_header_bypass": False},
        "llm": {"model": "test-model", "api_key_env": "QT_NOPE_KEY"},
        "embedding": {"enabled": False},
        "retrieval": {"top_k": 3, "recall": 10, "rrf_k": 60},
        "chat": {"db_path": str(tmp_path / "api.db"),
                 "kb_index_dir": "data/phase1_index",
                 "history_turns": 6, "rate_limit_per_min": 1000,
                 "question_max_chars": 1000, "history_max_chars": 6000,
                 "stream_timeout_s": 5, "title_max_len": 20},
        "defect": {"upload_dir": str(tmp_path / "uploads"),
                   "max_photo_mb": 2, "backfill_threshold_s": 60,
                   "list_limit": 50, "description_min_chars": 4},
    }
    monkeypatch.setattr(app_module, "load_app_config", lambda: cfg)

    app = app_module.create_app()
    with TestClient(app) as c:
        conn = app.state.db
        for username, password, name, role, line_id in USERS:
            conn.execute(
                "INSERT INTO sys_user (user_id, username, password_hash, display_name,"
                " role, line_id, status, created_at) VALUES (?,?,?,?,?,?, 'active', ?)",
                (repo.new_id("u"), username, auth_service.hash_password(password),
                 name, role, line_id, "2026-11-05T09:00:00"),
            )
        repo.insert_work_order(conn, "WO-A", "M8 螺栓", "line-01", 1000)
        repo.insert_work_order(conn, "WO-B", "M10 螺栓", "line-02", 1000)
        repo.upsert_defect_code(conn, "D-THREAD", "螺纹外径超差", "尺寸",
                                ["螺纹", "外径", "偏小", "通规"])
        repo.upsert_defect_code(conn, "D-SURF", "表面划伤", "外观", ["划伤", "毛刺"])
        conn.commit()
        yield c


def _token(client, username: str) -> str:
    r = client.post("/api/v1/auth/login",
                    json={"username": username, "password": username})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
class TestAuthApi:
    def test_login_ok(self, client):
        body = client.post("/api/v1/auth/login",
                           json={"username": "qe001", "password": "qe001"}).json()
        assert body["code"] == 0
        assert body["data"]["user"]["role"] == "quality_engineer"
        assert body["data"]["token"].count(".") == 2      # JWT 三段

    def test_login_wrong_password(self, client):
        r = client.post("/api/v1/auth/login",
                        json={"username": "qe001", "password": "bad"})
        assert r.status_code == 401
        assert r.json()["code"] == 40102
        # 不区分「用户不存在」与「密码错」，避免用户名枚举
        r2 = client.post("/api/v1/auth/login",
                         json={"username": "nobody", "password": "x"})
        assert r2.json()["code"] == 40102

    def test_me_requires_token(self, client):
        r = client.get("/api/v1/auth/me")
        assert r.status_code == 401
        assert r.json()["code"] == 40101

    def test_me_ok(self, client):
        body = client.get("/api/v1/auth/me", headers=_h(_token(client, "manager01"))).json()
        assert body["data"]["user"]["username"] == "manager01"

    def test_bad_signature(self, client):
        token = _token(client, "qe001")
        forged = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        r = client.get("/api/v1/auth/me", headers=_h(forged))
        assert r.status_code == 401
        assert r.json()["code"] == 40101

    def test_health_anonymous(self, client):
        body = client.get("/api/v1/health").json()
        assert body["code"] == 0
        assert "index_ready" in body["data"]


# ---------------------------------------------------------------------------
# 角色边界（路由级 40301）
# ---------------------------------------------------------------------------
class TestRoleGuards:
    def test_shift_leader_cannot_chat(self, client):
        r = client.post("/api/v1/chat", headers=_h(_token(client, "leader01")),
                        json={"question": "螺纹怎么检"})
        assert r.status_code == 403
        assert r.json()["code"] == 40301

    def test_qe_cannot_open_admin_documents(self, client):
        r = client.get("/api/v1/admin/documents", headers=_h(_token(client, "qe001")))
        assert r.status_code == 403 and r.json()["code"] == 40301

    def test_qe_cannot_create_defect(self, client):
        r = client.post("/api/v1/defects", headers=_h(_token(client, "qe001")),
                        json={"work_order_id": "WO-A", "description": "螺纹偏小"})
        assert r.status_code == 403 and r.json()["code"] == 40301

    def test_manager_can_list_documents(self, client):
        r = client.get("/api/v1/admin/documents", headers=_h(_token(client, "manager01")))
        assert r.status_code == 200 and r.json()["code"] == 0

    def test_manager_sees_attributions(self, client):
        r = client.get("/api/v1/admin/attributions", headers=_h(_token(client, "manager01")))
        assert r.status_code == 200 and "attributions" in r.json()["data"]


# ---------------------------------------------------------------------------
# 录入端：建单 → 确认（真实路由）
# ---------------------------------------------------------------------------
class TestDefectApi:
    def test_create_then_confirm(self, client):
        sl = _h(_token(client, "leader01"))
        r = client.post("/api/v1/defects", headers=sl,
                        json={"work_order_id": "WO-A",
                              "description": "螺纹外径偏小，通规过不去",
                              "qty": 2, "severity": "严重"})
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        assert data["status"] == "draft" and data["defect_code"] is None
        assert data["suggested"][0]["code"] == "D-THREAD"

        r2 = client.post(f"/api/v1/defects/{data['defect_no']}/confirm", headers=sl,
                         json={"defect_code": "D-THREAD"})
        assert r2.status_code == 200
        assert r2.json()["data"]["status"] == "pending"
        assert "判定" in r2.json()["data"]["status_text"]

    def test_leader_response_has_no_judgement_fields(self, client):
        """字段裁剪在 HTTP 层验证：响应 JSON 里根本不出现这些键。"""
        sl = _h(_token(client, "leader01"))
        data = client.post("/api/v1/defects", headers=sl,
                           json={"work_order_id": "WO-A",
                                 "description": "螺纹外径偏小"}).json()["data"]
        raw = json.dumps(data, ensure_ascii=False)
        for leaked in ("suggest_json", "photo_path", "line_id", "created_by"):
            assert leaked not in raw
        assert "suggested" in data        # 自己刚建单时的候选是可见的

    def test_other_line_work_order_404(self, client):
        r = client.post("/api/v1/defects", headers=_h(_token(client, "leader01")),
                        json={"work_order_id": "WO-B", "description": "螺纹外径偏小"})
        assert r.status_code == 404 and r.json()["code"] == 40406

    def test_list_only_own_line(self, client):
        sl1 = _h(_token(client, "leader01"))
        sl2 = _h(_token(client, "leader02"))
        client.post("/api/v1/defects", headers=sl1,
                    json={"work_order_id": "WO-A", "description": "螺纹外径偏小"})
        client.post("/api/v1/defects", headers=sl2,
                    json={"work_order_id": "WO-B", "description": "表面划伤明显"})
        a = client.get("/api/v1/defects", headers=sl1).json()["data"]
        b = client.get("/api/v1/defects", headers=sl2).json()["data"]
        assert len(a["defects"]) == 1 and len(b["defects"]) == 1
        assert a["defects"][0]["defect_no"] != b["defects"][0]["defect_no"]

    def test_idempotent_client_draft_id(self, client):
        sl = _h(_token(client, "leader01"))
        payload = {"work_order_id": "WO-A", "description": "螺纹外径偏小",
                   "client_draft_id": "dr-1"}
        first = client.post("/api/v1/defects", headers=sl, json=payload).json()["data"]
        again = client.post("/api/v1/defects", headers=sl, json=payload).json()["data"]
        assert first["defect_no"] == again["defect_no"]
        assert again["existing"] is True

    def test_work_order_check_cross_line_404(self, client):
        r = client.get("/api/v1/work-orders/WO-B", headers=_h(_token(client, "leader01")))
        assert r.status_code == 404 and r.json()["code"] == 40406

    def test_defect_codes_grouped(self, client):
        data = client.get("/api/v1/defect-codes",
                          headers=_h(_token(client, "leader01"))).json()["data"]
        assert data["total"] == 2
        assert {g["category"] for g in data["groups"]} == {"尺寸", "外观"}


# ---------------------------------------------------------------------------
# 照片预上传
# ---------------------------------------------------------------------------
class TestPhotoApi:
    def test_upload_returns_hex_token(self, client):
        r = client.post("/api/v1/uploads/photo",
                        headers=_h(_token(client, "leader01")),
                        json={"photo": f"data:image/jpeg;base64,{JPEG}"})
        assert r.status_code == 200, r.text
        token = r.json()["data"]["photo_token"]
        assert len(token) == 32 and all(c in "0123456789abcdef" for c in token)

    def test_non_image_rejected(self, client):
        bad = base64.b64encode(b"not an image at all").decode()
        r = client.post("/api/v1/uploads/photo",
                        headers=_h(_token(client, "leader01")),
                        json={"photo": f"data:image/jpeg;base64,{bad}"})
        assert r.status_code == 400 and r.json()["code"] == 40003

    def test_qe_cannot_upload(self, client):
        r = client.post("/api/v1/uploads/photo", headers=_h(_token(client, "qe001")),
                        json={"photo": f"data:image/jpeg;base64,{JPEG}"})
        assert r.status_code == 403 and r.json()["code"] == 40301


# ---------------------------------------------------------------------------
# 页面可达性（SSR 路由别被误删）
# ---------------------------------------------------------------------------
class TestPages:
    def test_index_is_chat_page(self, client):
        r = client.get("/")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "quality-trace" in r.text or "问答" in r.text

    def test_mobile_page(self, client):
        r = client.get("/m")
        assert r.status_code == 200 and "defect" in r.text.lower() or "不良" in r.text

    def test_admin_redirects_anonymous(self, client):
        r = client.get("/admin/login")
        assert r.status_code == 200        # 登录页对匿名开放

    def test_admin_dashboard_without_cookie(self, client):
        r = client.get("/admin/", follow_redirects=False)
        assert r.status_code in (302, 303, 307)
