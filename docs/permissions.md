# 权限设计

> 对应代码：`api/deps.py`（`require_roles`）、`api/routers/*.py`、`core/services/defects.py`（`public_view`）、`core/repo/db.py`（SQL 过滤）
> 对应测试：`tests/test_auth.py`、`tests/test_defects.py`（越权视角的断言单独成组）

三个角色不是"三个权限等级"，而是**三种互不重叠的视角**。先把边界说清楚，接口才好切：

| 角色 | 在车间里是谁 | 只关心什么 | 绝不能看到什么 |
|---|---|---|---|
| `quality_engineer`（QE） | 质量工程师 | 查依据、做归因、写 8D 草稿 | 不能审批自己的归因结论 |
| `shift_leader`（SL） | 班组长 | 报不良、确认缺陷码、看自己线的单 | **不能看到任何归因结论、判定结论、标准判定细节** |
| `quality_manager`（QM） | 质量主管 | 维护知识库与提示词、审计问答、审批归因 | ——（唯一有跨线全量数据与审批权的人） |

SL 那一行的"绝不能看到"是本系统最重要的一条约束。班组长是**数据源**，不是判断者：一旦他能看到"这批不良大概率是热处理炉温异常"，录入的下一步就会变成"照着结论凑描述"，数据源被污染，之后所有归因都建立在被污染的数据上。所以这条约束必须在代码里强制，不能靠培训。

---

## 1. 三层防线

同一件事用三种彼此独立的方式防，任一层单独失效都不会导致泄漏。

### 第一层：接口层 `require_roles`

```python
# api/deps.py
def require_roles(*allowed: str) -> Callable[..., Dict[str, Any]]:
    """角色白名单依赖工厂（路由级权限的执行点）。"""
    allowed_set = set(allowed)

    def _dependency(user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        if user["role"] not in allowed_set:
            raise ApiError(ErrorCode.FORBIDDEN)      # 40301
        return user

    return _dependency
```

用法是把它挂成接口依赖，**不写在函数体第一行**：

```python
_qm_only = require_roles("quality_manager")

@router.post("/documents")
async def upload_document(user: Dict[str, Any] = Depends(_qm_only), ...):
    ...
```

挂成依赖的意义在于：**鉴权在路由分发阶段就完成**，处理函数根本没有机会在"忘了检查"的情况下先查一次库。写成函数体里的 `if` 则相反——只要有人漏写一行，接口就是敞开的。

### 第二层：数据层 SQL 过滤

```python
# core/repo/db.py
def list_defects(conn, *, line_id=None, status=None, limit=50):
    sql = "SELECT * FROM defect_item WHERE 1=1"
    if line_id:
        sql += " AND line_id = ?"      # 班组长只看本线，过滤在 SQL 里
```

对应服务层：

```python
# core/services/defects.py
def line_scope(user) -> Optional[str]:
    """数据级过滤的产线范围：班组长返回自己的产线，其余角色返回 None（全量）。"""
    if user.get("role") != "shift_leader":
        return None
    line_id = (user.get("line_id") or "").strip()
    if not line_id:
        raise ApiError(ErrorCode.LINE_FORBIDDEN)     # 40302
    return line_id
```

过滤发生在 `WHERE`，不是取回后用 Python 跳过。区别不只是性能：前者是"数据不存在"，后者是"数据存在但我没给你"——后者只要漏一个分支就泄漏，且日志里看不出来。

班组长**没有** `line_id` 时（配置错误），早期实现会让它看到全量——这是真实存在过的缺口，已修：`line_scope` 直接 40302，不允许降级成"全量可见"。安全相关的默认值必须是"拒绝"，不是"放行"。

### 第三层：字段白名单

接口权限只能控制"能不能调这个接口"，控制不了"这个接口返回哪些字段"。缺陷单详情里同时有现场事实（描述、数量、照片）和判定结论（缺陷码、候选码、归因），班组长能调 `/defects/{no}`，但**不能拿到后一半**：

```python
# core/services/defects.py
def public_view(row, role):
    """班组长只拿到白名单字段：候选码来源、归因关联、判定结论根本不出现在响应里。"""
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
        # 质量工程师/主管可见：候选码、照片路径、产线（审计与追溯需要）
        view["suggest_json"] = row.get("suggest_json") or ""
        view["photo_path"] = row.get("photo_path")
        view["line_id"] = row.get("line_id")
        view["created_by"] = row.get("created_by")
        view["confirmed_at"] = row.get("confirmed_at")
    return view
```

用"先写死基础视图、再按角色追加"而不是"从整行里删字段"：将来 `defect_item` 加了 `root_cause_hint` 之类的新字段，删字段的写法会默认泄漏，追加的写法默认不泄漏。照片同理——班组长只拿到 `has_photo` 布尔值（自己拍的应该能看，但路径里的单号可用于推断别线数据，所以不下发）。

`status_text` 也做了角色区分：SL 看到的是"已提交，待质量工程师判定"，而不是 `pending`——原始状态码会让人以为"还没提交成功"。

---

## 2. 完整矩阵（角色 × 接口）

图例：`✓` 允许 · `—` 403（40301/40302）· `自己` 仅本人数据 · `本线` 仅 `line_id` 匹配

### 2.1 认证

| # | 接口 | QE | SL | QM | 备注 |
|---|---|---|---|---|---|
| 1 | `POST /auth/login` | ✓ | ✓ | ✓ | 匿名可调；失败统一 40102，不区分"用户不存在/密码错" |
| 2 | `GET /auth/me` | ✓ | ✓ | ✓ | 返回 `role`、`line_id`、`display_name`，前端据此渲染入口 |

### 2.2 问答（QE 主入口）

| # | 接口 | QE | SL | QM | 备注 |
|---|---|---|---|---|---|
| 3 | `POST /chat`（SSE） | ✓ | — | — | 见下方"为什么主管不能问" |
| 4 | `POST /sessions` | ✓ | — | — | |
| 5 | `GET /sessions/{id}/messages` | 自己 | — | — | 非本人会话 → **40401**（不暴露存在性） |
| 6 | `DELETE /sessions/{id}` | 自己 | — | — | 同上 |
| 7 | `GET /health` | ✓ | ✓ | ✓ | 匿名可调，不返回配置细节 |

`POST /chat` 里另有一处**跨用户写**防护：请求带 `session_id` 时，服务端先校验该会话的 `user_id` 等于当前用户，否则按"会话不存在"处理（40401）。缺这一步，任何 QE 都能往同事的会话里插消息，把对方的上下文污染成任意内容。

### 2.3 主管后台（QM 专属）

| # | 接口 | QE | SL | QM | 备注 |
|---|---|---|---|---|---|
| 8 | `POST /admin/documents` | — | — | ✓ | 版本 + 生效区间必填；同名同版本 40903 |
| 9 | `GET /admin/documents` | — | — | ✓ | |
| 10 | `GET /admin/documents/{id}/chunks` | — | — | ✓ | 切片预览（核对切分是否切断条款） |
| 11 | `POST /admin/documents/{id}/archive` | — | — | ✓ | 下架 ≠ 删除，检索立即排除 |
| 12 | `POST /admin/documents/{id}/restore` | — | — | ✓ | 恢复后需重建索引 |
| 13 | `GET /admin/prompt-templates` | — | — | ✓ | |
| 14 | `POST /admin/prompt-templates` | — | — | ✓ | 新建即置为 active，旧模板转 archived |
| 15 | `PUT /admin/prompt-templates/{id}` | — | — | ✓ | 写历史快照后更新，下一次问答立即生效 |
| 16 | `GET /admin/audit/messages` | — | — | ✓ | 支持角色/批次/日期/仅看命中 |
| 17 | `GET /admin/audit/messages/{id}` | — | — | ✓ | 含引用明细与三条规则的命中项 |
| 18 | `POST /admin/audit/messages/{id}/review` | — | — | ✓ | 标记已核查，不改动机器标记 |
| 19 | `POST /admin/audit/rescan` | — | — | ✓ | 全量重扫，保留人工核查结论 |
| 20 | `GET /admin/attributions` | ✓ | — | ✓ | QE 可看（自己要跟进），不可改 |
| 21 | `GET /admin/attributions/{id}` | ✓ | — | ✓ | 同上 |
| 22 | `POST /admin/attributions/{id}/approve` | — | — | ✓ | 只有主管能批准 |
| 23 | `POST /admin/attributions/{id}/reject` | — | — | ✓ | 退回必填 `note`，否则 40001 |
| 24 | `GET /admin/attributions/{id}/export-8d` | — | — | ✓ | 未批准也能导，但带红色 DRAFT 横幅 |

为什么 QE 能读归因单却不能审批：归因是 QE 提交的，他必须能看到自己提交的东西处于什么状态（被退回了才知道要改），但"自己写、自己批"等于没有审批环节。

为什么主管不能问 `/chat`：这是**角色隔离的刻意选择**。主管的入口是审计与审批，不是问答；如果主管也能问，审计页面就出现"自己审自己的问答"，`reviewed_by` 与 `session.user_id` 会重合。这个约束在阶段 4 若放开，应当同时禁止自查自己的会话。

### 2.4 现场录入（SL 主入口）

| # | 接口 | QE | SL | QM | 备注 |
|---|---|---|---|---|---|
| 25 | `GET /defect-codes` | ✓ | ✓ | ✓ | 缺陷码字典，只读 |
| 26 | `POST /defects` | — | ✓ | — | 建草稿；SL 强制本线，工单不属于本线 → 40406 |
| 27 | `POST /defects/{no}/confirm` | — | 本线 | — | 只能 `draft → pending`，不能改数量/描述 |
| 28 | `GET /defects` | ✓ | 本线 | ✓ | SL 的 `line_id` 过滤在 SQL 层 |
| 29 | `GET /defects/{no}` | ✓ | 本线 | ✓ | 字段级裁剪见 `public_view` |
| 30 | `GET /work-orders/{id}` | ✓ | 本线 | ✓ | 移动端失焦校验；跨线返回 40406 |
| 31 | `POST /uploads/photo` | — | ✓ | — | 预上传，返回 32 位 hex token |

服务层的 `create_draft` 其实支持质量工程师代录（QE 建单时产线取**工单所属产线**，不接受客户端传值，`tests/test_defects.py::test_qe_uses_work_order_line` 覆盖），但**接口层没有对 QE 放开**：网页端的代录入口一旦存在，就等于给了一个"可往任意产线写单"的入口，而代录是低频操作，走脚本或由主管在后台处理更合适。权限收紧时宁可留一个"以后再加"的缺口，也不要先开一个没人审计的写入面。

### 2.5 统计与 SPC（按"结果 / 过程参数"分界）

这个模块**不是**整体只给管理层。分界线是：

**给结果**（三角色）——不良率、趋势、帕累托、星期效应、P 图。
班组长知道本线结果才能改进，且 `line_id` 在服务层被强制覆写成本线。

**给过程参数**（QM + QE）——控制图、实测值序列、量具编号、操作者。
控制图里带着这些，等价于把工艺窗口暴露给现场。

| # | 接口 | QE | SL | QM | 备注 |
|---|---|---|---|---|---|
| 32 | `GET /analytics/overview` | ✓ | 本线 | ✓ | KPI 汇总 |
| 33 | `GET /analytics/dashboard` | ✓ | 本线 | ✓ | SL 的 `item_code` 被丢弃（见下） |
| 34 | `GET /analytics/pareto` | ✓ | 本线 | ✓ | |
| 35 | `GET /analytics/pareto/compare` | ✓ | 本线 | ✓ | |
| 36 | `GET /analytics/trend` | ✓ | 本线 | ✓ | |
| 37 | `GET /analytics/weekday` | ✓ | 本线 | ✓ | |
| 38 | `GET /analytics/p-chart` | ✓ | 本线 | ✓ | 只有不良率，没有实测值 |
| 39 | `GET /analytics/control-chart` | ✓ | — | ✓ | 含实测值序列、量具、操作者 |
| 40 | `GET /analytics/items` | ✓ | — | ✓ | 项目清单也是一种工艺信息 |
| 41 | `POST /analytics/measurements` | ✓ | — | ✓ | 现场由检验员在 QE 账号下录入 |
| 42 | `POST /analytics/measurements/batch` | ✓ | — | ✓ | 同上 |

**这里有一个真实修过的漏洞**：`/dashboard` 三角色都能调，而它接受
`item_code`、内部会去算控制图。班组长加一个 `?item_code=外径`
就能从"给结果"的接口里把"过程参数"完整取走。

根因是权限挂在**路由**上，敏感数据挂在**参数**上，两者没对上。
现在的处置是在 `dashboard()` 里丢弃 SL 的 `item_code`：

```python
if user.get("role") == "shift_leader":
    item_code = None        # 丢弃，不报错
```

丢弃而不是报错：报错等于告诉他"这个参数有用"，也让一个正常请求变成失败。
回归测试 `test_permission_matrix.py::TestLeaderScope` 守这条。

凡是"聚合接口里嵌了敏感子结果"的地方，都要按这个思路查一遍，
只看路由上的 `require_roles` 是查不出来的。

### 2.6 批次追溯、数据交换、MSA（QM + QE）

| # | 接口 | QE | SL | QM | 备注 |
|---|---|---|---|---|---|
| 43–50 | `/genealogy/*` | ✓ | — | ✓ | 前向影响面跨线、跨客户，不给现场 |
| 51–56 | `/io/*` | ✓ | — | ✓ | **导入是唯一能一次写坏主数据的入口** |
| 57–64 | `/msa/*` | ✓ | — | ✓ | 班组长看到"尺子不合格"只会今天不测了 |

三条具体理由：

1. **追溯要的是全局视图**。前向追溯会跨到别的产线、别的客户订单上，
   而班组长的数据边界就是本线——给他看跨线结果，等于亲手拆掉那条边界。
2. **导入没有确认步骤**。现场录入走 `/defects` 有两步（草稿 → 人工确认），
   导入是一步写几千行。这条通道必须比现场录入更严，不是更松。
3. **MSA 的结论会被误用**。%GRR 不合格的正确反应是立项改量具，
   而现场最直接的反应是"那我今天别测了"。结论要给能处置的人。

数据层过滤（第二层防线）在这四个模块里同样生效：

```python
def _scope(user, line_id):
    """SL 的 line_id 被覆写成自己的产线；未绑定直接 40302。"""
    if user["role"] == "shift_leader":
        if not user.get("line_id"):
            raise ApiError(ErrorCode.LINE_NOT_BOUND)   # 40302
        return user["line_id"]
    return line_id
```

注意这里**不是**"SL 传什么就过滤什么"：如果写成 `line_id = line_id or user["line_id"]`，
SL 显式传一个别线 ID 就能绕过。必须是覆写，不是补默认值。

### 2.7 页面

页面级权限与接口级独立实现（`admin_pages._require_manager()` 读 Cookie 里的 JWT），
因为页面走的是浏览器 Cookie 通道，不能依赖前端 JS 加 `Authorization` 头。

页面权限分两档：

| 路径 | 谁能打开 | 打不开时 |
|---|---|---|
| `/`（问答） | QE | 跳 `/admin/login` |
| `/m`（移动录入） | SL | 页内登录浮层（不跳转，避免丢失已填内容） |
| `/admin/login` | 匿名 | —— |
| `/admin`（概览） | QM | `/admin/forbidden`（403 页面，不跳登录，避免"登录了还是打不开"的困惑） |
| `/admin/documents` | QM | 同上 |
| `/admin/prompts` | QM | 同上 |
| `/admin/audit` | QM | 同上 |
| `/admin/attributions` | QM | 同上 |
| `/admin/attributions/{id}` | QM | 同上 |
| `/admin/analytics` | QM + QE | 同上 |
| `/admin/genealogy` | QM + QE | 同上 |
| `/admin/msa` | QM + QE | 同上 |
| `/admin/data` | QM + QE | 同上 |

**页面权限比接口权限更严，有一处刻意的落差**：QE 能调
`GET /admin/attributions`（看自己提交的归因单），但打不开
`/admin/attributions` 页面。原因是那个页面的列表默认是全量的，
详情页还带审批按钮——把审批按钮渲染给 QE 再靠 JS 禁用，是最容易被
"审查元素"绕过的做法。QE 看归因状态走接口或看板，不走这个页面。

反向的落差也存在：`/admin/audit` 页面的接口是 QM 专属，
但页面本身也做了 QM 检查——两层都做不是冗余，是防止以后接口权限放宽时
页面忘了同步。

---

## 3. 认证通道

两套通道并存，因为浏览器页面和 API 客户端的约束不同：

| 通道 | 用在哪 | 实现 |
|---|---|---|
| `Authorization: Bearer <jwt>` | 所有 API（curl、前端 fetch） | `current_user` 依赖优先读 header |
| `qt_token` HttpOnly Cookie | `/admin/*` SSR 页面 | 页面路由读 cookie；`HttpOnly` 防 XSS 窃取，`SameSite=Lax` 防 CSRF |

两者解的是同一个 token（HS256，手写实现，见 `core/services/auth.py`），因此不存在"页面登录了但接口调不通"的状态。

演示用的旁路 `auth.dev_header_bypass`（默认关闭）：

```yaml
auth:
  dev_header_bypass: false     # 打开后可用 X-User-Id / X-Role 直连，仅本地联调用
```

打开后 `X-Role: quality_manager` 就能拿到主管权限。这是**明确的演示开关**，不是"忘了关的后门"：默认 false，`config/default.yaml` 里带注释说明风险，且不读环境变量（避免生产环境被一个环境变量打开）。

---

## 4. 越权测试对照表

权限这种东西，"写了"不等于"生效"。每条防线都有对应断言：

| 断言 | 测试 |
|---|---|
| SL 访问 `/chat` → 403 | `test_auth.py`（角色矩阵参数化） |
| QE 访问 `/admin/documents` → 403 | `test_auth.py` |
| SL 建单时工单不属于本线 → 40406 | `test_defects.py::test_work_order_must_belong_to_line` |
| SL 未绑定产线 → 40302（不是全量可见） | `test_defects.py::TestUnboundShiftLeader`（7 条） |
| SL 确认别条产线的单 → 404 | `test_defects.py::test_other_line_cannot_confirm` |
| SL 列表只返回本线 | `test_defects.py::test_list_filtered_by_line` |
| SL 取别线单据 → 404（非 403） | `test_defects.py::test_get_other_line_404` |
| QE 可查任意产线工单 | `test_defects.py::test_qe_can_check_any_line` |
| 非本人会话读消息 → 40401 | `test_chat_repo.py` / `test_chat_service.py` |
| 过期 token → 40103 | `test_auth.py` |
| 篡改签名 → 40101 | `test_auth.py` |
| SL 读管理类接口（27 个）→ 40301 | `test_permission_matrix.py::TestShiftLeaderForbidden` |
| SL 写管理类接口（7 个）→ 40301 | `test_permission_matrix.py::test_write_endpoints` |
| SL 带 `item_code` 调看板 → 图被丢弃 | `test_permission_matrix.py::TestLeaderScope::test_dashboard_drops_item_code_for_leader` |
| SL 带别线 `line_id` → 仍只看本线 | `test_permission_matrix.py::test_leader_line_is_overwritten` |
| 未绑定产线 SL 调统计 → 40302 | `test_permission_matrix.py::TestUnboundLeader::test_analytics_rejected_with_40302` |
| 匿名访问任意接口 → 40101 | `test_permission_matrix.py::TestAnonymous` |
| 新端点没有权限断言 → 构建失败 | `test_permission_matrix.py::TestManifestCompleteness` |
| SL 上传导入文件 / 下模板 → 403 | `test_permission_matrix.py::TestImportPermissions` |

`TestManifestCompleteness` 值得单独说一句：它从真实路由表取所有
`/api/v1` 路径，减去测试清单与 `EXEMPT`，剩下的必须为空。
这一条把"忘记给新端点加权限测试"从一个人为疏漏变成了一个会失败的构建。
没有它，前面那些断言再多，也只是守住了"已经想到的那些"。

"别线取单返回 404 而不是 403"是刻意的：403 等于告诉对方"这个单号存在，只是你没权限"，单号是可枚举的（`DF-20261105-0001`），返回 403 就成了一个枚举器。404 让越权和不存在不可区分。

---

## 5. 已知取舍

- **没有实现行级权限的通用框架**（如 RBAC 表驱动）。三个角色、一条产线维度，硬编码在 `require_roles` 与 `scope_line_id` 里更易读、更不容易配错；角色扩到 6 个以上时再抽象。
- **页面与接口的权限是两份代码**。有重复，但换来的是两条通道各自独立可测；共用一份反而会让"页面登录态"和"API token"耦合。
- **QE 与 QM 都能读归因单**，区别只在写权限。如果将来要"QE 只能看自己提交的"，需要在 `list_attributions` 加 `submitted_by` 过滤——目前没做，因为 QE 之间互相查看归因草稿在质量部门内是合理的协作行为。
