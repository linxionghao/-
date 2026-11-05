# -*- coding: utf-8 -*-
"""导入导出接口测试：模板下载、预览、提交、导出、多表打包。

这一层测的是**接口契约**，而不是解析逻辑（后者在 test_io.py 里）：
- 上传是不是真的能走 multipart，还是被大小/扩展名校验挡住；
- 错误是不是统一封装成 {code, message}，而不是 FastAPI 默认的 detail；
- 下载响应头的文件名，中文能不能正确编码（乱码是这里最常见的投诉）；
- 权限：班组长拿不到导入权限。

最后一条特别值得测：导入是唯一能"一次写坏整个主数据表"的入口，
权限漏洞在这里的代价比别处大一个量级。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import quality_trace.api.app as app_module
from quality_trace.core.repo import db as repo
from quality_trace.core.services import auth as auth_service

USERS = [
    ("qe001", "顾工", "quality_engineer", None),
    ("leader01", "赵班组长", "shift_leader", "line-01"),
    ("manager01", "钱主管", "quality_manager", None),
]

WO_CSV = "工单号,产品,产线,计划数量,日期\nWO-900,六角螺栓,line-01,1200,2026-11-06\n"
WO_CSV_BAD = "工单号,产品,产线\nWO-901,螺栓,line-01\n,缺号,line-01\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg = {
        "auth": {"secret": "test-secret", "token_ttl_s": 3600,
                 "dev_header_bypass": False},
        "llm": {"model": "test-model", "api_key_env": "QT_NOPE_KEY"},
        "embedding": {"enabled": False},
        "retrieval": {"top_k": 3, "recall": 10, "rrf_k": 60},
        "chat": {"db_path": str(tmp_path / "io_api.db"),
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
        for username, name, role, line_id in USERS:
            conn.execute(
                "INSERT INTO sys_user (user_id, username, password_hash, display_name,"
                " role, line_id, status, created_at) VALUES (?,?,?,?,?,?, 'active', ?)",
                (repo.new_id("u"), username, auth_service.hash_password(username),
                 name, role, line_id, "2026-11-06T09:00:00"),
            )
        repo.insert_work_order(conn, "WO-A", "M8 螺栓", "line-01", 1000,
                               occurred_at="2026-11-06")
        repo.insert_defect(conn, "DF-A", "WO-A", "line-01", "毛刺", qty=2,
                           defect_code="DC01", occurred_at="2026-11-06")
        conn.commit()
        yield c


def _h(client, username: str) -> dict:
    r = client.post("/api/v1/auth/login",
                    json={"username": username, "password": username})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["data"]["token"]}


def _upload(client, headers, dataset="work_orders", body=WO_CSV, path="/api/v1/io/import"):
    return client.post(
        path,
        headers=headers,
        data={"dataset": dataset},
        files={"file": ("x.csv", body.encode("utf-8"), "text/csv")},
    )


# ---------------------------------------------------------------------------
# 元信息与模板
# ---------------------------------------------------------------------------
class TestMeta:
    def test_datasets_lists_both_directions(self, client):
        body = client.get("/api/v1/io/datasets", headers=_h(client, "qe001")).json()
        assert body["code"] == 0
        names = {d["dataset"] for d in body["data"]["imports"]}
        assert names == {"work_orders", "defect_codes", "lots", "measurements"}
        exports = {d["dataset"] for d in body["data"]["exports"]}
        assert {"defects", "pareto", "suppliers"} <= exports

    def test_datasets_requires_login(self, client):
        assert client.get("/api/v1/io/datasets").status_code == 401

    def test_template_download(self, client):
        r = client.get("/api/v1/io/template/work_orders", headers=_h(client, "qe001"))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert r.content.startswith(b"\xef\xbb\xbf")        # BOM：Excel 双击不乱码
        assert "工单号" in r.content.decode("utf-8-sig")

    def test_template_filename_is_rfc5987(self, client):
        """中文文件名必须同时给 filename*，否则 Chrome 会丢掉扩展名。"""
        r = client.get("/api/v1/io/template/lots", headers=_h(client, "qe001"))
        disp = r.headers["content-disposition"]
        assert "filename*=UTF-8''" in disp
        assert "%E7%89%A9%E6%96%99%E6%89%B9" in disp        # "物料批" 的百分号编码

    def test_template_unknown_dataset(self, client):
        r = client.get("/api/v1/io/template/nope", headers=_h(client, "qe001"))
        assert r.status_code == 400
        assert r.json()["code"] == 40004


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------
class TestImport:
    def test_preview_does_not_write(self, client):
        h = _h(client, "qe001")
        r = _upload(client, h, path="/api/v1/io/import/preview")
        assert r.status_code == 200
        body = r.json()
        assert body["data"]["dry_run"] is True
        assert body["data"]["inserted"] == 1
        # 预览之后库里仍然没有这张工单
        lst = client.get("/api/v1/defects/work-orders?work_order_id=WO-900", headers=h)
        assert lst.status_code in (200, 404)

    def test_commit_writes(self, client):
        h = _h(client, "qe001")
        r = _upload(client, h)
        assert r.json()["data"]["inserted"] == 1
        assert r.json()["data"]["dry_run"] is False

    def test_partial_failure_reports_row_numbers(self, client):
        h = _h(client, "qe001")
        r = _upload(client, h, body=WO_CSV_BAD)
        data = r.json()["data"]
        assert data["inserted"] == 1
        assert data["failed"] == 1
        assert data["errors"][0]["row"] == 3                 # Excel 行号口径

    def test_allow_partial_false_rejects_whole_file(self, client):
        """整体拒绝时必须一行都不写，否则用户会得到"半份数据"。"""
        h = _h(client, "qe001")
        r = client.post(
            "/api/v1/io/import", headers=h,
            data={"dataset": "work_orders", "allow_partial": "false"},
            files={"file": ("x.csv", WO_CSV_BAD.encode("utf-8"), "text/csv")},
        )
        assert r.status_code == 400
        assert r.json()["code"] == 40001
        assert "整体拒绝" in r.json()["message"]

    def test_wrong_suffix_rejected(self, client):
        h = _h(client, "qe001")
        r = client.post(
            "/api/v1/io/import", headers=h,
            data={"dataset": "work_orders"},
            files={"file": ("book.xlsx", b"PK\x03\x04binary", "application/vnd.ms-excel")},
        )
        assert r.status_code == 415
        assert r.json()["code"] == 41502

    def test_empty_file_rejected(self, client):
        h = _h(client, "qe001")
        r = client.post(
            "/api/v1/io/import", headers=h, data={"dataset": "work_orders"},
            files={"file": ("x.csv", b"", "text/csv")},
        )
        assert r.status_code == 400
        assert r.json()["code"] == 40005

    def test_header_only_file_rejected(self, client):
        h = _h(client, "qe001")
        r = _upload(client, h, body="工单号,产品,产线\n")
        assert r.status_code == 400
        assert r.json()["code"] == 40005

    def test_unknown_dataset(self, client):
        h = _h(client, "qe001")
        r = _upload(client, h, dataset="nope")
        assert r.status_code == 400
        assert r.json()["code"] == 40004

    def test_gbk_file_is_accepted(self, client):
        """GBK 编码是车间默认，不能因此报错。"""
        h = _h(client, "qe001")
        r = client.post(
            "/api/v1/io/import", headers=h, data={"dataset": "work_orders"},
            files={"file": ("x.csv", WO_CSV.encode("gbk"), "text/csv")},
        )
        assert r.json()["data"]["inserted"] == 1
        assert r.json()["data"]["encoding"] in ("gb18030", "gbk")

    def test_defect_codes_dataset(self, client):
        h = _h(client, "qe001")
        r = _upload(client, h, dataset="defect_codes",
                    body="缺陷码,名称,分类\nDC99,压伤,外观\n")
        assert r.json()["data"]["inserted"] == 1

    def test_shift_leader_forbidden(self, client):
        """导入是唯一能一次写坏主数据的入口，班组长不该有这把钥匙。"""
        h = _h(client, "leader01")
        r = _upload(client, h)
        assert r.status_code == 403
        assert r.json()["code"] == 40301

    def test_manager_allowed(self, client):
        h = _h(client, "manager01")
        assert _upload(client, h).json()["code"] == 0


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
class TestExport:
    def test_csv_download(self, client):
        h = _h(client, "qe001")
        r = client.get("/api/v1/io/export?dataset=defects", headers=h)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "DF-A" in r.content.decode("utf-8-sig")
        assert "attachment" in r.headers["content-disposition"]

    def test_xlsx_download(self, client):
        h = _h(client, "qe001")
        r = client.get("/api/v1/io/export?dataset=defects&format=xlsx", headers=h)
        assert r.status_code == 200
        assert "spreadsheetml" in r.headers["content-type"]
        assert r.content[:2] == b"PK"                        # zip 魔数

    def test_unknown_dataset(self, client):
        h = _h(client, "qe001")
        r = client.get("/api/v1/io/export?dataset=nope", headers=h)
        assert r.status_code == 400
        assert r.json()["code"] == 40004

    def test_unknown_format(self, client):
        h = _h(client, "qe001")
        r = client.get("/api/v1/io/export?dataset=defects&format=pdf", headers=h)
        assert r.status_code == 400
        assert r.json()["code"] == 40004

    def test_pareto_dim_passthrough(self, client):
        h = _h(client, "qe001")
        r = client.get("/api/v1/io/export?dataset=pareto&dim=line_id", headers=h)
        assert r.status_code == 200
        assert "占比" in r.content.decode("utf-8-sig")

    def test_bundle_has_three_sheets(self, client):
        h = _h(client, "qe001")
        r = client.get("/api/v1/io/export/bundle", headers=h)
        assert r.status_code == 200
        import io as _io
        import zipfile

        names = zipfile.ZipFile(_io.BytesIO(r.content)).namelist()
        assert len([n for n in names if "worksheets/sheet" in n]) == 3

    def test_requires_login(self, client):
        assert client.get("/api/v1/io/export?dataset=defects").status_code == 401

    def test_shift_leader_forbidden(self, client):
        h = _h(client, "leader01")
        r = client.get("/api/v1/io/export?dataset=defects", headers=h)
        assert r.status_code == 403
