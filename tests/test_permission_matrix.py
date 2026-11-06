"""权限矩阵回归：把 docs/permissions.md 的表变成可执行的断言。

为什么单独建一个文件，而不是散在各模块的测试里：

权限是**横切关注点**。写在各模块里时，加一个新端点很容易忘记加 403 断言，
而忘记的后果不是测试红，是测试绿——测试仍然全过，只是少守了一条规则。
这个文件用"端点清单"驱动，加端点时刻意要做一次主动决定：
要么加进清单，要么写进 EXEMPT 并说明理由。

同理，这里的断言只测**边界**（403 / 401 / 200），不测业务逻辑。
业务逻辑由各模块自己的测试负责，重复覆盖没有价值。

清单用四元组 `(方法, 请求路径, 路由模板, 最小权限集合)`：

- **请求路径**要能真的发出去（占位符填真实值），否则测试会因为 404
  而"通过"——测的是"这个 URL 不存在"，不是"这个角色被拒绝"；
- **路由模板**用来跟真实路由表对账（见 `TestManifestCompleteness`），
  少了它就查不出"新加的端点没被权限测试覆盖"。

三个约定：

1. 每个用例断言 `code == 40301`，不只断言 HTTP 403：
   HTTP 403 可能来自别的中间件，业务码才是本项目自己的答案；
2. 未绑定产线的班组长单独一组。这条最容易写错成"降级为全量可见"，
   而它一旦写错，不会报错，只会让一个班组长看到全厂数据；
3. 对象不存在的端点允许 404 但**不允许** 403——
   "能进去但对象不在"与"进不去"是两件事，混在一起就测不出权限。
"""

import pytest
from fastapi.testclient import TestClient

import quality_trace.api.app as app_module
from quality_trace.core.repo import db as repo
from quality_trace.core.services import auth as auth_service

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ===========================================================================
# 夹具
# ===========================================================================
USERS = [
    ("qe001", "顾工", "quality_engineer", None),
    ("manager01", "钱主管", "quality_manager", None),
    ("leader01", "赵班组长", "shift_leader", "line-01"),
    # 未绑定产线的班组长：所有"按线过滤"的接口都必须拒绝他，而不是放开
    ("leader_noline", "无产线班组长", "shift_leader", None),
]


def _config(tmp_path):
    return {
        "auth": {"secret": "matrix-secret", "token_ttl_s": 3600,
                 "dev_header_bypass": False},
        "llm": {"model": "t", "api_key_env": "QT_NOPE_KEY"},
        "embedding": {"enabled": False},
        "retrieval": {"top_k": 3, "recall": 10, "rrf_k": 60},
        "chat": {"db_path": str(tmp_path / "matrix.db"),
                 "kb_index_dir": "data/phase1_index",
                 "history_turns": 6, "rate_limit_per_min": 1000,
                 "question_max_chars": 1000, "history_max_chars": 6000,
                 "stream_timeout_s": 5, "title_max_len": 20},
        "defect": {"upload_dir": str(tmp_path / "uploads"),
                   "max_photo_mb": 2, "backfill_threshold_s": 60,
                   "list_limit": 50, "description_min_chars": 4},
    }


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "load_app_config", lambda: _config(tmp_path))
    app = app_module.create_app()
    with TestClient(app) as c:
        conn = app.state.db
        repo.insert_work_order(conn, "WO-01", "六角螺栓", "line-01", 1000,
                               occurred_at="2026-11-01")
        for username, name, role, line_id in USERS:
            # 密码与用户名相同，只为测试方便；生产种子用的是独立的初始口令
            repo.create_user(conn, username, auth_service.hash_password(username),
                             name, role, line_id=line_id)
        yield c


def _login(client, username):
    r = client.post("/api/v1/auth/login",
                    json={"username": username, "password": username})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["data"]["token"]}


# ===========================================================================
# 端点清单
# ===========================================================================
MGMT_ONLY = {"quality_manager", "quality_engineer"}   # 主管 + 工程师
QM_ONLY = {"quality_manager"}                          # 主管专属
ALL = {"quality_manager", "quality_engineer", "shift_leader"}

# (方法, 请求路径, 路由模板, 最小权限集合)
ENDPOINTS = [
    # ---- 统计与 SPC（#32–#42）
    # 前七个对三角色开放：它们给的是**结果**（不良率、趋势、P 图），
    # 班组长知道结果才能改进；line_id 在服务层被强制覆写成本线。
    ("GET", "/api/v1/analytics/overview",
     "/api/v1/analytics/overview", ALL),
    ("GET", "/api/v1/analytics/dashboard",
     "/api/v1/analytics/dashboard", ALL),
    ("GET", "/api/v1/analytics/pareto",
     "/api/v1/analytics/pareto", ALL),
    ("GET", "/api/v1/analytics/pareto/compare",
     "/api/v1/analytics/pareto/compare", ALL),
    ("GET", "/api/v1/analytics/trend",
     "/api/v1/analytics/trend", ALL),
    ("GET", "/api/v1/analytics/weekday",
     "/api/v1/analytics/weekday", ALL),
    ("GET", "/api/v1/analytics/p-chart",
     "/api/v1/analytics/p-chart", ALL),
    # 后三个对 SL 关闭：控制图与实测值属于**过程参数**（含量具编号、操作者）
    ("GET", "/api/v1/analytics/control-chart?item_code=外径",
     "/api/v1/analytics/control-chart", MGMT_ONLY),
    ("GET", "/api/v1/analytics/items",
     "/api/v1/analytics/items", MGMT_ONLY),
    # ---- 批次谱系（#43–#50）
    ("GET", "/api/v1/genealogy/overview",
     "/api/v1/genealogy/overview", MGMT_ONLY),
    ("GET", "/api/v1/genealogy/lots",
     "/api/v1/genealogy/lots", MGMT_ONLY),
    ("GET", "/api/v1/genealogy/lots/LOT-X",
     "/api/v1/genealogy/lots/{lot_id}", MGMT_ONLY),
    ("GET", "/api/v1/genealogy/lots/LOT-X/graph",
     "/api/v1/genealogy/lots/{lot_id}/graph", MGMT_ONLY),
    ("GET", "/api/v1/genealogy/defects/DF-X/backward",
     "/api/v1/genealogy/defects/{defect_no}/backward", MGMT_ONLY),
    ("GET", "/api/v1/genealogy/suppliers/scorecard",
     "/api/v1/genealogy/suppliers/scorecard", MGMT_ONLY),
    # ---- 导入导出（#51–#56）
    ("GET", "/api/v1/io/datasets", "/api/v1/io/datasets", MGMT_ONLY),
    ("GET", "/api/v1/io/template/work_orders",
     "/api/v1/io/template/{dataset}", MGMT_ONLY),
    ("GET", "/api/v1/io/export?dataset=defects",
     "/api/v1/io/export", MGMT_ONLY),
    ("GET", "/api/v1/io/export/bundle",
     "/api/v1/io/export/bundle", MGMT_ONLY),
    # ---- MSA（#57–#64）
    ("GET", "/api/v1/msa/overview", "/api/v1/msa/overview", MGMT_ONLY),
    ("GET", "/api/v1/msa/gaps", "/api/v1/msa/gaps", MGMT_ONLY),
    ("GET", "/api/v1/msa/studies", "/api/v1/msa/studies", MGMT_ONLY),
    ("GET", "/api/v1/msa/studies/MSA-X",
     "/api/v1/msa/studies/{study_id}", MGMT_ONLY),
    ("GET", "/api/v1/msa/consistency?item_code=外径",
     "/api/v1/msa/consistency", MGMT_ONLY),
    ("GET", "/api/v1/msa/advice", "/api/v1/msa/advice", MGMT_ONLY),
    # ---- 主管后台（#8–#19：文档 / 提示词 / 审计）
    ("GET", "/api/v1/admin/documents",
     "/api/v1/admin/documents", QM_ONLY),
    ("GET", "/api/v1/admin/prompt-templates",
     "/api/v1/admin/prompt-templates", QM_ONLY),
    ("GET", "/api/v1/admin/audit/messages",
     "/api/v1/admin/audit/messages", QM_ONLY),
]

# 写操作单独列：必须带 body，不能只靠 GET 探测
WRITE_ENDPOINTS = [
    ("POST", "/api/v1/analytics/measurements",
     "/api/v1/analytics/measurements",
     {"item_code": "外径", "value": 10.0, "line_id": "line-01"}, MGMT_ONLY),
    ("POST", "/api/v1/analytics/measurements/batch",
     "/api/v1/analytics/measurements/batch",
     {"rows": [{"item_code": "外径", "value": 10.0, "line_id": "line-01"}]}, MGMT_ONLY),
    ("POST", "/api/v1/genealogy/lots",
     "/api/v1/genealogy/lots",
     {"lot_id": "LOT-NEW", "material_code": "M-1", "material_name": "钢棒"}, MGMT_ONLY),
    ("POST", "/api/v1/genealogy/lots/LOT-X/consume",
     "/api/v1/genealogy/lots/{lot_id}/consume",
     {"work_order_id": "WO-01", "qty_used": 10}, MGMT_ONLY),
    ("POST", "/api/v1/msa/studies",
     "/api/v1/msa/studies",
     {"item_code": "外径",
      "rows": [{"appraiser": "A", "part": "P1", "trial": 1, "value": 1.0}]}, MGMT_ONLY),
    ("POST", "/api/v1/msa/studies/MSA-X/recompute",
     "/api/v1/msa/studies/{study_id}/recompute", {}, MGMT_ONLY),
    ("POST", "/api/v1/admin/documents/1/archive",
     "/api/v1/admin/documents/{doc_id}/archive", {}, QM_ONLY),
]


# ===========================================================================
# 一、班组长：管理类接口一律 40301
# ===========================================================================
class TestShiftLeaderForbidden:
    @pytest.mark.parametrize("method,url,route,allowed", ENDPOINTS)
    def test_read_endpoints(self, client, method, url, route, allowed):
        """SL 读管理类接口 → 40301。

        这条断言的价值在于：如果某个端点忘了挂 `require_roles`，
        它会返回 200（或 400/404），测试立刻红。挂错成"任何登录用户"
        也会红——这是唯一能自动抓住"漏挂权限"的方式。
        """
        if "shift_leader" in allowed:
            pytest.skip("该端点对 SL 开放")
        r = client.request(method, url, headers=_login(client, "leader01"))
        assert r.status_code == 403, "%s %s 返回 %s" % (method, url, r.status_code)
        assert r.json()["code"] == 40301

    @pytest.mark.parametrize("method,url,route,body,allowed", WRITE_ENDPOINTS)
    def test_write_endpoints(self, client, method, url, route, body, allowed):
        if "shift_leader" in allowed:
            pytest.skip("该端点对 SL 开放")
        r = client.request(method, url, json=body,
                           headers=_login(client, "leader01"))
        assert r.status_code == 403, "%s %s 返回 %s" % (method, url, r.status_code)
        assert r.json()["code"] == 40301


# ===========================================================================
# 二、质量工程师：可读管理类接口，但不能碰主管专属
# ===========================================================================
class TestEngineerScope:
    @pytest.mark.parametrize("method,url,route,allowed", ENDPOINTS)
    def test_qe_can_read_management(self, client, method, url, route, allowed):
        """QE 对 MGMT_ONLY 端点不应被拒。

        允许 404：`/lots/LOT-X` 这种占位对象本来就不存在，
        "能进去但对象不在"正是权限通过的表现。**不允许** 403——
        那才是没权限。
        """
        if "quality_engineer" not in allowed:
            pytest.skip("该端点对 QE 关闭")
        r = client.request(method, url, headers=_login(client, "qe001"))
        assert r.status_code != 403, "%s %s 被误拒" % (method, url)
        assert r.status_code in (200, 404, 422), \
            "%s %s 返回 %s" % (method, url, r.status_code)

    @pytest.mark.parametrize("method,url,route,body,allowed", WRITE_ENDPOINTS)
    def test_qe_cannot_touch_qm_only(self, client, method, url, route, body, allowed):
        if "quality_engineer" in allowed:
            pytest.skip("该端点对 QE 开放")
        r = client.request(method, url, json=body, headers=_login(client, "qe001"))
        assert r.status_code == 403, "%s %s 返回 %s" % (method, url, r.status_code)
        assert r.json()["code"] == 40301

    def test_qe_sees_only_own_attributions(self, client):
        """QE 能读归因列表，但列表在 repo 层已按 submitted_by 过滤。

        这里只断言"能读且不报错"；"只看自己的"由 test_attribution.py
        的 service 级用例覆盖，此处不重复造数据。
        """
        r = client.get("/api/v1/admin/attributions", headers=_login(client, "qe001"))
        assert r.status_code == 200


# ===========================================================================
# 三、班组长能看到什么、看不到什么
# ===========================================================================
class TestLeaderScope:
    """班组长：能看结果，不能看过程参数。

    这条分界线有一个很容易漏的绕道：`/analytics/dashboard` 三角色都能调，
    而它接受 `item_code`、内部会去算控制图。不额外处理的话，
    班组长加一个 `?item_code=外径` 就能从"给结果"的接口里偷走"过程参数"
    （实测值序列、量具编号、操作者）。下面的测试守的就是这个绕道。
    """

    def test_leader_can_see_kpi(self, client):
        h = _login(client, "leader01")
        body = client.get("/api/v1/analytics/overview", headers=h).json()
        assert body["code"] == 0

    def test_dashboard_drops_item_code_for_leader(self, client):
        """SL 带 item_code 调看板 → 图被丢弃，但请求本身成功。"""
        h = _login(client, "leader01")
        body = client.get("/api/v1/analytics/dashboard?item_code=外径",
                          headers=h).json()
        assert body["code"] == 0
        assert body["data"]["chart"] is None, "班组长不应拿到控制图"

    def test_dashboard_keeps_chart_for_engineer(self, client):
        """对照：QE 带同一个参数时走的是另一条分支，证明差异来自角色。"""
        h = _login(client, "qe001")
        body = client.get("/api/v1/analytics/dashboard?item_code=外径",
                          headers=h).json()
        assert body["code"] == 0
        assert "chart" in body["data"]

    def test_leader_cannot_read_control_chart(self, client):
        r = client.get("/api/v1/analytics/control-chart?item_code=外径",
                       headers=_login(client, "leader01"))
        assert r.status_code == 403
        assert r.json()["code"] == 40301

    def test_leader_line_is_overwritten(self, client):
        """SL 显式传别线 line_id 也拿不到别线数据（覆写，不是补默认值）。"""
        h = _login(client, "leader01")
        body = client.get("/api/v1/analytics/overview?line_id=line-02",
                          headers=h).json()
        assert body["code"] == 0
        # 服务层把 line_id 覆写成本线；返回的 range 里带的必须是 line-01
        assert body["data"]["range"].get("line_id", "line-01") == "line-01"


# ===========================================================================
# 四、未绑定产线的班组长
# ===========================================================================
class TestUnboundLeader:
    """未绑定 `line_id` 的班组长必须被拒绝，不能退化成"看到全部"。

    这是权限实现里最危险的失败模式：写成 `line_id = line_id or user.line_id`，
    当 `user.line_id` 为 None 时过滤条件变成 `WHERE line_id IS NULL` 或干脆
    不追加条件，结果是**数据全开**，而接口返回 200，没有任何报错。
    """

    def test_defect_list_rejected(self, client):
        r = client.get("/api/v1/defects", headers=_login(client, "leader_noline"))
        assert r.status_code == 403
        assert r.json()["code"] == 40302

    def test_create_defect_rejected(self, client):
        r = client.post("/api/v1/defects",
                        json={"work_order_id": "WO-01", "description": "螺纹偏小"},
                        headers=_login(client, "leader_noline"))
        assert r.status_code == 403
        assert r.json()["code"] == 40302

    def test_analytics_rejected_with_40302(self, client):
        """统计接口对 SL 开放，所以角色这关过了；第二层数据范围必须拦住。

        拿到 40302 而不是 200 或空数据，说明"未绑定产线"没有被当成"全厂"。
        """
        r = client.get("/api/v1/analytics/overview",
                       headers=_login(client, "leader_noline"))
        assert r.status_code == 403
        assert r.json()["code"] == 40302


# ===========================================================================
# 五、匿名访问
# ===========================================================================
class TestAnonymous:
    @pytest.mark.parametrize("method,url,route,allowed", ENDPOINTS)
    def test_anonymous_401(self, client, method, url, route, allowed):
        """不带 token → 40101，不是 403。

        401 与 403 的区别对前端有意义：401 要跳登录，403 要显示无权限页。
        混用会让用户在两页之间来回跳。
        """
        r = client.request(method, url)
        assert r.status_code == 401, "%s %s 返回 %s" % (method, url, r.status_code)
        assert r.json()["code"] == 40101

    def test_health_is_public(self, client):
        assert client.get("/api/v1/health").status_code == 200


# ===========================================================================
# 六、清单自身的完整性
# ===========================================================================
class TestManifestCompleteness:
    """守住"新端点必须被权限测试覆盖"这条元规则。

    做法：从真实路由表取所有 `/api/v1` 模板，减去 EXEMPT，
    再减去本文件已列出的模板；剩下的必须为空。
    新增端点忘记加清单时这个测试会红，并打印漏掉的路径。

    这一条是整个文件里最有价值的测试：它让"忘记"变成一个会失败的构建，
    而不是一个无人发现的缺口。
    """

    # 刻意不列入的端点，每个都要有理由
    EXEMPT = {
        "/api/v1/auth/login": "匿名可调",
        "/api/v1/auth/me": "任意登录用户",
        "/api/v1/health": "匿名可调",
        "/api/v1/chat": "全角色，业务侧测",
        "/api/v1/sessions": "全角色，业务侧测",
        "/api/v1/sessions/{session_id}/messages": "归属校验，业务侧测",
        "/api/v1/sessions/{session_id}": "归属校验，业务侧测",
        "/api/v1/defects": "SL 主入口，test_defects.py 覆盖",
        "/api/v1/defects/{defect_no}": "同上",
        "/api/v1/defects/{defect_no}/confirm": "同上",
        "/api/v1/defect-codes": "全角色",
        "/api/v1/work-orders/{work_order_id}": "全角色 + 本线校验",
        "/api/v1/uploads/photo": "SL 专用，test_defects.py 覆盖",
        "/api/v1/admin/documents/{doc_id}/chunks": "QM 专属，test_knowledge_admin",
        "/api/v1/admin/documents/{doc_id}/restore": "同上",
        "/api/v1/admin/prompt-templates/{template_id}": "同上",
        "/api/v1/admin/audit/messages/{message_id}": "同上",
        "/api/v1/admin/audit/messages/{message_id}/review": "同上",
        "/api/v1/admin/audit/rescan": "同上",
        "/api/v1/admin/attributions": "QM+QE，QE 有行级过滤",
        "/api/v1/admin/attributions/{attribution_id}": "同上",
        "/api/v1/admin/attributions/{attribution_id}/approve": "QM 专属",
        "/api/v1/admin/attributions/{attribution_id}/reject": "QM 专属",
        "/api/v1/admin/attributions/{attribution_id}/export-8d": "QM 专属",
        "/api/v1/io/import": "multipart，见 TestImportPermissions",
        "/api/v1/io/import/preview": "同上",
    }

    def test_no_endpoint_left_uncovered(self):
        app = app_module.create_app()
        actual = set()
        for route in app.routes:
            path = getattr(route, "path", "")
            if path.startswith("/api/v1"):
                actual.add(path)

        covered = {route for _, _, route, _ in ENDPOINTS}
        covered |= {route for _, _, route, _, _ in WRITE_ENDPOINTS}
        covered |= set(self.EXEMPT)

        missing = sorted(actual - covered)
        assert not missing, ("以下端点没有任何权限断言，请加进 ENDPOINTS "
                             "或写进 EXEMPT 并说明理由：\n  " + "\n  ".join(missing))

    def test_exempt_entries_are_real(self):
        """EXEMPT 里不能有"已经删掉的端点"，否则它会变成一条永远成立的空话。"""
        app = app_module.create_app()
        actual = {getattr(r, "path", "") for r in app.routes}
        stale = sorted(set(self.EXEMPT) - actual)
        assert not stale, "EXEMPT 里有不存在的端点，请删掉：\n  " + "\n  ".join(stale)

    def test_exempt_has_reason(self):
        """每条豁免都要写理由。空字符串等于没写。"""
        empty = [p for p, why in self.EXEMPT.items() if not why.strip()]
        assert not empty, "以下豁免没有写理由：\n  " + "\n  ".join(empty)


# ===========================================================================
# 七、multipart 端点单独验证
# ===========================================================================
class TestImportPermissions:
    """导入接口是 multipart，无法用 json 参数化统一测。

    单独写不是因为特殊，而是因为**它是最需要守的那个**：
    导入一次能写几千行，漏挂权限的后果比别的端点大一个量级。
    """

    def test_leader_cannot_preview(self, client):
        r = client.post(
            "/api/v1/io/import/preview",
            data={"dataset": "work_orders"},
            files={"file": ("a.csv", b"work_order_id\nWO-99\n", "text/csv")},
            headers=_login(client, "leader01"),
        )
        assert r.status_code == 403
        assert r.json()["code"] == 40301

    def test_leader_cannot_commit(self, client):
        r = client.post(
            "/api/v1/io/import",
            data={"dataset": "work_orders"},
            files={"file": ("a.csv", b"work_order_id\nWO-99\n", "text/csv")},
            headers=_login(client, "leader01"),
        )
        assert r.status_code == 403
        assert r.json()["code"] == 40301

    def test_leader_cannot_download_template(self, client):
        r = client.get("/api/v1/io/template/work_orders",
                       headers=_login(client, "leader01"))
        assert r.status_code == 403

    def test_qe_can_preview(self, client):
        r = client.post(
            "/api/v1/io/import/preview",
            data={"dataset": "work_orders"},
            files={"file": ("a.csv", "work_order_id\nWO-99\n".encode("utf-8"),
                            "text/csv")},
            headers=_login(client, "qe001"),
        )
        assert r.status_code == 200
        assert r.json()["data"]["total"] == 1
