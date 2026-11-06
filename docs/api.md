# 接口说明

> 对应版本：v0.1.0。接口变化会在 `CHANGELOG.md` 中标注。

## 1. 命令行

```
quality-trace [--config PATH] [--verbose] [--version]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-c, --config` | `config/default.yaml` | 配置文件路径 |
| `-v, --verbose` | 关闭 | 输出调试信息（配置键列表等） |
| `--version` | — | 打印版本号后退出 |

**退出码**

| 码 | 含义 |
|---|---|
| 0 | 执行完成（配置校验通过或给出了问题清单） |
| 2 | 配置文件不存在 |

> 注意：配置校验不通过时仍返回 0，问题以 `[配置]` 前缀打印，便于在脚本里先跑通再修配置。
> 如果后续接入真实数据源，业务失败应返回非 0，需在 `main()` 中补充。

**输出示例**

```
quality-trace v0.1.0
配置: config/default.yaml
  - [配置] 校验通过
  - [追溯] 数据源未接入，跳过
  - [归因] 规则库为空，跳过
结果: ready
```

## 2. 配置项

配置文件为 YAML，两层结构。用户级覆盖写 `config/local.yaml`（已 gitignore）。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `app.name` | str | `quality-trace` | 应用名 |
| `app.log_level` | str | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `trace.direction` | str | `both` | `forward` / `backward` / `both` |
| `trace.max_depth` | int | `5` | 谱系追溯最大层数，≥1 |
| `attribution.min_records` | int | `3` | 参与统计相关的最少记录数，≥1 |
| `attribution.confidence_floor` | float | `0.6` | 疑似原因置信度下限，取 `[0, 1]` |

## 3. 环境变量

优先级最高，格式 `QT_<SECTION>__<KEY>`（两个下划线分隔层级）。

```bash
QT_APP__LOG_LEVEL=DEBUG
QT_TRACE__MAX_DEPTH=8
QT_ATTRIBUTION__CONFIDENCE_FLOOR=0.75
```

值会按 `bool → int → float → str` 的顺序自动转换。

## 4. Python API

### `quality_trace.utils.config.load_config`

```python
def load_config(
    path: Optional[Union[str, os.PathLike]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]
```

加载配置并与默认值深合并。

- `path=None`：只返回默认值
- 文件不存在：抛 `FileNotFoundError`
- 顶层不是映射：抛 `ValueError`
- 未安装 PyYAML 时，自动回退到内置的极简解析器（仅支持两层结构 + 标量）

### `quality_trace.core.pipeline.check_config`

```python
def check_config(config: Dict[str, Any]) -> List[str]
```

返回问题清单，空列表表示通过。纯函数，无副作用。

### `quality_trace.core.pipeline.run`

```python
def run(config: Dict[str, Any], verbose: bool = False) -> Dict[str, Any]
```

返回：

```python
{
    "status": "ready" | "invalid",
    "checks": ["[配置] ...", "[追溯] ...", "[归因] ..."],
}
```

**契约**：本函数不得返回任何「根因已确定」之类的结论，最多输出「疑似原因」与「待验证假设」。

---

# 第二部分：HTTP 接口

> 适用于阶段 2 之后的所有 Web 接口。问答接口的详细设计（SSE 事件、降级语义）
> 另见 [`chat-api.md`](chat-api.md)，本文只做规范汇总。

## 5. 通用约定

### 5.1 响应封装

所有 JSON 接口（含错误）统一返回：

```json
{
  "code": 0,
  "message": "成功",
  "data": {}
}
```

| 字段 | 说明 |
|---|---|
| `code` | 0 成功；非 0 为 5 位业务错误码 |
| `message` | 中文，可直接展示给最终用户；同一错误码在任何接口文案一致 |
| `data` | 成功时的载荷；错误时为 `null` |

**HTTP 状态码 = 业务码前三位**：`40001 → 400`、`40101 → 401`、`40301 → 403`、
`40406 → 404`、`40905 → 409`、`42901 → 429`、`50003 → 500`。
降级类（50001 / 50002 / 50401）属于「成功但有保留」，HTTP 仍为 200，信息放在 `data.degraded`。

### 5.2 错误码表

| 码 | 含义 | 典型场景 |
|---|---|---|
| 40001 | 请求参数缺失或格式错误 | as_of 格式非法、退回未填意见 |
| 40002 | 问题长度超限 | 超过 1000 字 |
| 40003 | 图片解码失败或超过 2MB | 非 JPEG/PNG、超大图 |
| 40101 | 未登录或身份无效 | 无令牌、令牌畸形 |
| 40102 | 登录名或密码错误 | 登录失败（不区分账号是否存在） |
| 40103 | 登录已过期 | 令牌超过 12 小时 |
| 40301 | 角色无权 | 班组长调问答、QE 调后台 |
| 40302 | 数据级越权 | 跨产线访问 |
| 40401 | 会话不存在 | 他人会话按不存在处理 |
| 40402 | 知识库索引不存在 | 未执行 ingest |
| 40403 | 文档不存在 | 已删除/错误 id |
| 40404 | 缺陷单不存在 | 他线单据同样返回此码 |
| 40405 | 归因单不存在 | — |
| 40406 | 工单不存在或不属于本线 | 跨线工单 |
| 40901 | 会话已归档 | 继续提问 |
| 40902 | 上一条仍在生成 | 并发提问 |
| 40903 | 文档版本已存在 | 同名同版本重复上传 |
| 40905 | 缺陷单状态不允许 | 重复确认缺陷码 |
| 40906 | 归因单状态不允许 | 重复审批 |
| 41301 | 文件超过 10MB | 知识库上传 |
| 41501 | 仅支持 Markdown / PDF | 其他格式 |
| 42901 | 请求过于频繁 | 单会话每分钟 20 条 |
| 50001 | 向量服务降级（非失败） | embedding 调用失败 |
| 50002 | 大模型不可用（非失败） | 无 Key / 超时 |
| 50003 | 服务内部错误 | 未预期异常 |
| 50004 | 向量化暂缺（非失败） | 文档已入库，仅关键词检索 |
| 50301 | 索引加载失败 | 索引文件损坏 |
| 50401 | 响应超时，回答截断（非失败） | 流式超 60 秒 |

### 5.3 鉴权

两种通道，优先级从高到低：

1. `Authorization: Bearer <JWT>`（接口调用、移动端）；
2. 后台页面用 HttpOnly Cookie（`qt_token`），登录表单 `POST /admin/login` 写入。

演示开关 `auth.dev_header_bypass=true` 时允许 `X-User-Id` + `X-Role` 旁路，
**生产必须关闭**。角色与数据范围：

| 角色 | 令牌携带 | 数据范围 |
|---|---|---|
| `quality_engineer` | `line_id=null` | 本人会话；全部缺陷单只读 |
| `shift_leader` | `line_id=line-01` | 本产线缺陷单，字段裁剪 |
| `quality_manager` | `line_id=null` | 全部 |

---

## 6. 认证接口

### 6.1 登录

```
POST /api/v1/auth/login
Content-Type: application/json

{"username": "qe001", "password": "qe001"}
```

成功：

```json
{
  "code": 0,
  "message": "成功",
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIs...",
    "expires_in": 43200,
    "user": {
      "user_id": "u-xxx",
      "username": "qe001",
      "display_name": "顾工（质量工程师）",
      "role": "quality_engineer",
      "line_id": null
    }
  }
}
```

失败（HTTP 401）：

```json
{"code": 40102, "message": "登录名或密码错误", "data": null}
```

**安全约定**：账号不存在与密码错误返回同一错误码，不泄露账号是否存在；
密码用 PBKDF2 哈希存储；令牌签名采用常数时间比较。

### 6.2 当前用户

```
GET /api/v1/auth/me
Authorization: Bearer <token>
```

用于前端启动时校验令牌是否仍有效。过期 → 40103，畸形/伪造 → 40101。

### 6.3 后台表单登录（Cookie）

```
POST /admin/login
Content-Type: application/x-www-form-urlencoded

username=manager01&password=manager01
```

- 成功：`303` 跳转 `/admin/`，写入 HttpOnly Cookie；
- 失败：返回登录页并显示中文错误（HTTP 401）；
- 非主管角色登录后台：`403` + 提示页「仅限质量主管」。

```
POST /admin/logout       # 清除 Cookie 并跳转登录页
```

---

## 7. 问答接口

### 7.1 发起问答（SSE）

```
POST /api/v1/chat
Authorization: Bearer <QE token>
Content-Type: application/json

{
  "session_id": "sess-xxx",          // 可空：为空自动建会话
  "question": "螺纹检验的抽样方案是什么？",
  "as_of": "2026-06-01"              // 可空：按业务时刻检索（版本硬闸）
}
```

响应 `text/event-stream`，事件序列固定：

| 事件 | data | 时机 |
|---|---|---|
| `status` | `{"stage":"retrieving","message":"正在检索依据…"}` | 检索前 |
| `sources` | `{"sources":[{"seq":1,"source_file":"…","version":"v2","clause":"3. 抽样","snippet":"…","score":0.83}]}` | 检索完成、正文之前 |
| `delta` | `{"text":"增量片段"}` | 生成中，多条 |
| `done` | `{"message_id":"…","degraded":null}` | 正常结束 |
| `error` | `{"code":50003,"message":"…"}` | 流中异常（此时 HTTP 已是 200） |

**为什么 sources 先发**：断流、超时、用户点「停止」都不会丢引用；
审计页与历史恢复都依赖已落库的引用行。

**降级示例**（无 LLM Key，HTTP 仍 200）：

```
event: sources
data: {"sources":[...]}

event: done
data: {"message_id":"msg-xxx","degraded":{"code":50002,"message":"大模型不可用，仅返回检索依据"}}
```

**as_of 语义**：非空时走数据库版本闸（`status='active'` 且生效区间覆盖该日），
不读索引文件；格式非法直接 40001。

### 7.2 会话

```
POST   /api/v1/sessions                    # 新建会话
GET    /api/v1/sessions/{id}/messages      # 历史消息 + 引用
DELETE /api/v1/sessions/{id}               # 归档（软删除）
```

历史消息响应：

```json
{
  "code": 0,
  "data": {
    "session_id": "sess-xxx",
    "messages": [
      {
        "message_id": "msg-xxx",
        "role": "assistant",
        "content": "……",
        "status": "complete",
        "created_at": "2026-11-05T22:41:03",
        "citations": [
          {"seq": 1, "source_file": "检验标准-螺纹外径检验规范",
           "version": "v2", "clause": "3. 抽样", "snippet": "…"}
        ]
      }
    ]
  }
}
```

**归属校验**：非本人会话一律 40401，不暴露存在性。

### 7.3 健康检查

```
GET /api/v1/health
```

```json
{"code": 0, "message": "健康", "data": {"status": "ok", "index_ready": true, "db": "ok"}}
```

---

## 8. 主管后台接口（仅 QM）

### 8.1 上传文档

```
POST /api/v1/admin/documents
Content-Type: multipart/form-data

file=@检验规范v3.md
doc_type=standard                 # standard / sop / case
version=v3                        # 必填
effective_from=2026-01-01         # 必填
effective_to=2027-12-31           # 必填
```

成功：

```json
{
  "code": 0,
  "message": "上传成功，索引已重建",
  "data": {
    "doc_id": "doc-xxx",
    "doc_name": "检验规范v3",
    "version": "v3",
    "chunk_count": 4,
    "vector_status": "pending",
    "index_stats": {"documents": 3, "chunks": 12}
  }
}
```

| 错误 | 场景 |
|---|---|
| 40001 | 缺版本号/生效区间 |
| 40903 | 同名同版本已存在 |
| 41301 | 文件 > 10MB |
| 41501 | 非 md/pdf |
| 50004 | 已入库但向量化暂缺（非失败） |

### 8.2 文档列表 / 切片 / 上下架

```
GET  /api/v1/admin/documents?status=active&doc_type=standard
GET  /api/v1/admin/documents/{doc_id}/chunks
POST /api/v1/admin/documents/{doc_id}/archive     # 下架，不物理删除
POST /api/v1/admin/documents/{doc_id}/restore     # 恢复上架
```

下架响应会带回重建后的索引统计，便于确认「废止版已退出候选集」：

```json
{"code": 0, "message": "已下架",
 "data": {"doc": {"doc_id": "doc-xxx", "status": "archived"},
          "index_stats": {"documents": 2, "chunks": 9}}}
```

### 8.3 提示词模板

```
GET  /api/v1/admin/prompt-templates      # 当前模板 + 最近历史
POST /api/v1/admin/prompt-templates      # 首次创建
PUT  /api/v1/admin/prompt-templates/{id} # 修改（下一条回答立即生效）
```

```json
{"code": 0, "message": "成功，下一条回答立即生效",
 "data": {"template": {"template_id": "tpl-xxx", "content": "…",
                        "updated_by": "u-xxx", "updated_at": "2026-11-05T22:41:03"}}}
```

### 8.4 对话审计

```
GET  /api/v1/admin/audit/messages?role=quality_engineer&batch_no=WO-20261030-001
                                  &date_from=2026-11-01&date_to=2026-11-05
                                  &only_flagged=1&limit=200
GET  /api/v1/admin/audit/messages/{message_id}
POST /api/v1/admin/audit/messages/{message_id}/review   {"note":"已核查"}
POST /api/v1/admin/audit/rescan
```

列表项关键字段：

```json
{
  "message_id": "msg-xxx",
  "content": "经分析，根因已确定为切刀磨损",
  "batch_no": "WO-20261030-001",
  "session_role": "quality_engineer",
  "flag_root_cause": 1,
  "flag_no_citation": 1,
  "flag_stale_version": 0,
  "reviewed_by": null
}
```

`rescan` 返回 `{"scanned": 12, "flagged": 3}`，**不覆盖** `reviewed_*` 字段。

### 8.5 归因审批与 8D

```
GET  /api/v1/admin/attributions?status=submitted
GET  /api/v1/admin/attributions/{attribution_id}
POST /api/v1/admin/attributions/{attribution_id}/approve  {"note":"同意"}
POST /api/v1/admin/attributions/{attribution_id}/reject   {"note":"请补充点检记录"}
GET  /api/v1/admin/attributions/{attribution_id}/export-8d
```

- 退回 `note` 为空 → 40001；重复审批 → 40906；
- 导出返回 `text/markdown` + `Content-Disposition: attachment; filename="8D-DF-xxx.md"`；
- 无草稿且 LLM 不可用 → 50002（JSON 错误，不是半截文件）。

---

## 9. 现场录入接口（SL）

### 9.1 缺陷码字典

```
GET /api/v1/defect-codes
```

```json
{"code": 0, "data": {
  "groups": [
    {"category": "尺寸缺陷", "items": [{"code": "D-THREAD", "name": "螺纹外径超差"}]},
    {"category": "外观缺陷", "items": [{"code": "D-SURF", "name": "表面划伤"}]}
  ],
  "total": 7
}}
```

### 9.2 建草稿（第一步）

```
POST /api/v1/defects
Authorization: Bearer <SL token>

{
  "work_order_id": "WO-20261030-001",
  "op_code": "OP30",
  "description": "螺纹外径偏小，通规止端过不去",
  "qty": 3,
  "severity": "严重",              // 轻微 / 一般 / 严重
  "occurred_at": "2026-11-05T14:30:00",   // 现场发生时刻，可空
  "photo": "data:image/jpeg;base64,...",  // 可空，与 photo_token 二选一
  "photo_token": "040ba9fe...",           // 可空，预上传返回
  "client_draft_id": "dr-...",            // 可空，离线补传幂等键
  "is_backfill": false                    // 客户端显式声明补录
}
```

成功（`status` 恒为 `draft`，`defect_code` 为空）：

```json
{
  "code": 0,
  "message": "已创建草稿，请确认缺陷码",
  "data": {
    "defect_no": "DF-20261105-0001",
    "status": "draft",
    "status_text": "待确认缺陷码",
    "defect_code": null,
    "is_backfill": false,
    "occurred_at": "2026-11-05T14:30:00",
    "recorded_at": "2026-11-05T14:30:02",
    "has_photo": true,
    "suggested": [
      {"code": "D-THREAD", "name": "螺纹外径超差", "category": "尺寸缺陷",
       "confidence": 0.95, "source": "keyword", "matched": ["外径", "偏小"]}
    ],
    "normalize": {"source": "keyword", "degraded": false,
                  "degraded_reason": "未配置大模型，使用关键词匹配"},
    "existing": false
  }
}
```

**幂等**：同一 `client_draft_id` 再次提交返回原单，`existing=true`，不产生第二条。

### 9.3 确认缺陷码（第二步）

```
POST /api/v1/defects/{defect_no}/confirm

{"defect_code": "D-THREAD"}
```

成功后 `status=pending`，`status_text="已提交，待质量工程师判定"`。
**重复确认 → 40905**；未知/停用码 → 40001；他线单据 → 40404。

### 9.4 列表与详情

```
GET /api/v1/defects                 # 班组长强制本线
GET /api/v1/defects/{defect_no}     # 他线 40404
GET /api/v1/work-orders/{id}        # 工单校验（失焦调用）
```

班组长响应字段白名单：`defect_no, work_order_id, op_code, description, qty,
severity, defect_code, status, status_text, is_backfill, occurred_at, recorded_at, has_photo`。
`suggest_json / photo_path / line_id / created_by` 等字段**服务端不返回**。

### 9.5 照片预上传

```
POST /api/v1/uploads/photo

{"photo": "data:image/jpeg;base64,..."}
```

```json
{"code": 0, "data": {"photo_token": "040ba9fe3683498888c9d301444e95b6",
                     "bytes": 10240, "ext": "jpg"}}
```

token 为 32 位 hex，存于 `data/uploads/tmp/`；建单时用 `photo_token` 引用，
服务端移动到正式目录（`data/uploads/photos/`）。非 JPEG/PNG 或超 2MB → 40003。

---

## 10. 调用示例（问答 / 后台 / 现场）

> 统计、追溯、导入导出、MSA 四个模块的调用示例见 §16。

### 10.1 问答（curl）

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"qe001","password":"qe001"}' | jq -r .data.token)

curl -N -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"question":"螺纹检验的抽样方案是什么？","as_of":"2026-06-01"}'
```

### 10.2 上传文档（curl）

```bash
curl -X POST http://127.0.0.1:8000/api/v1/admin/documents \
  -H "Authorization: Bearer $QM_TOKEN" \
  -F "file=@检验规范v3.md" -F "doc_type=standard" \
  -F "version=v3" -F "effective_from=2026-01-01" -F "effective_to=2027-12-31"
```

### 10.3 建单 + 确认（curl）

```bash
DNO=$(curl -s -X POST http://127.0.0.1:8000/api/v1/defects \
  -H "Content-Type: application/json" -H "Authorization: Bearer $SL_TOKEN" \
  -d '{"work_order_id":"WO-20261030-001","description":"螺纹外径偏小，通规过不去"}' \
  | jq -r .data.defect_no)

curl -X POST http://127.0.0.1:8000/api/v1/defects/$DNO/confirm \
  -H "Content-Type: application/json" -H "Authorization: Bearer $SL_TOKEN" \
  -d '{"defect_code":"D-THREAD"}'
```

### 10.4 一键冒烟

```bash
bash scripts/smoke_stage3.sh 8000     # 审计/审批 18 项断言
bash scripts/smoke_defects.sh 8000    # 录入端 18 项断言
```


---

# 第三部分：统计、追溯、数据交换

## 11. 统计与 SPC 分析接口

**权限**：`quality_manager` / `quality_engineer`。班组长一律 403（40301）。
**数据范围**：班组长即使被允许调用，其 `line_id` 也会被**覆写**为本人绑定的产线；
未绑定产线的班组长直接 40302，绝不退化成"全厂可见"。

所有接口都有三个通用参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `line_id` | str | 不限 | 产线过滤；SL 调用时被覆写 |
| `date_from` / `date_to` | `YYYY-MM-DD` | — | 业务日期区间（按 `occurred_at`，不是录入时间） |
| `days` | int | 30 | 未给区间时的回溯天数 |

`date_to` 会被补齐到当天 23:59:59。代码里的做法是把 `YYYY-MM-DD` 变成
`YYYY-MM-DD~`——ASCII 的 `~`(0x7E) 大于一切数字与字母，所以字符串比较天然
覆盖整天，不需要为 SQLite 写时间函数（见 `repo._end_of_day`）。

### 11.1 `GET /analytics/overview`

质量 KPI 汇总。

```json
{"code":0,"message":"成功","data":{
  "range":{"date_from":"2026-10-08","date_to":"2026-11-06","days":30},
  "kpi":{"orders":14,"defects":58,"qty":163,"defect_rate":0.0123,
         "per_order":4.14,"worst_day":"2026-10-31","worst_qty":21},
  "by_severity":[{"severity":"严重","qty":37},...],
  "by_line":[{"line_id":"line-01","qty":120}],
  "comparison":{"qty_delta":-18,"qty_ratio":-0.11,"verdict":"下降"}
}}
```

`defect_rate` 的分母是**有工单记录的生产数**，不是"缺陷单数"。
缺工单时该字段为 `null` 而不是 0——0 会被读成"零缺陷"，那是完全相反的结论。

### 11.2 `GET /analytics/dashboard`

一次返回看板需要的全部数据（KPI + 产线帕累托 + 星期效应 + 可选控制图）。

```bash
curl "http://127.0.0.1:8000/api/v1/analytics/dashboard?days=14&item_code=外径" \
  -H "Authorization: Bearer $QE_TOKEN"
```

`item_code` 给了才计算 `chart`，否则该字段为 `null`。
这样页面切项目时只发一次请求，不用并发四个。

### 11.3 `GET /analytics/pareto`

| 参数 | 可选值 | 说明 |
|---|---|---|
| `dim` | `defect_code` / `line_id` / `op_code` / `severity` / `created_by` | 白名单，防止拼接注入 |
| `weight` | `qty`（件数）/ `orders`（单数） | 同一批数据两种口径可能给出不同结论 |
| `top_n` | int，默认 10 | 其余归入"其他" |

返回每项的 `qty`、`ratio`、`cum_ratio`、`abc`（A/B/C 分级）与
`vital_few`（撑起前 80% 的项目集合）。

**ABC 分级看的是"累加前的比例"**，不是累加后：最后一个跨过 80% 线的项目
必然属于 A。写成累加后会把它错判成 B，A 类项目数少 1，聚焦就漏了一个。

### 11.4 `GET /analytics/pareto/compare`

同一维度对比"当期"与"上一期"。返回 `movers`：上升项、下降项、新出现项。

```json
{"data":{"movers":{
  "up":[{"key":"D-BURR","current":21,"previous":6,"delta":15}],
  "down":[{"key":"D-SCRATCH","current":9,"previous":18,"delta":-9}],
  "new":[{"key":"D-CRACK","current":4,"previous":0,"delta":4}]
}}}
```

`new` 单独列出来是有意的：新出现的缺陷类型往往比"某类涨了 3 倍"更要紧。

### 11.5 `GET /analytics/trend`

| 参数 | 可选值 | 说明 |
|---|---|---|
| `granularity` | `day` / `week` / `month` | `week` 以**周一**为起点（ISO），不是周日 |

返回 `series`（每桶的 `qty` / `orders` / `rate`）、`fit`（线性拟合的
`slope` / `r2` / `t_stat` / `significant`）、`ewma`（λ=0.3 的指数平滑）。

判定趋势显著性用 t 统计量而不是"看斜率正负"：`|t| > 2` 才算显著。
完全线性的序列 `ss_tot = 0`，R² 无定义，此时返回 `r2: null` +
`perfect_fit: true`——**不能**把 `r2=None` 当成"拟合失败"。

### 11.6 `GET /analytics/weekday`

按星期几分组，返回每天的均值与 `index`（相对总均值的指数，100 = 持平）。
用途很具体：周末班次的人手与照明都不同，缺陷率常有系统性抬升，
但这属于"计划安排"问题，不该记在工艺头上。

### 11.7 `GET /analytics/p-chart`

不合格品率控制图（P 图）。`granularity` 同上。

P 图的控制限**逐点不同**：`UCL_i = p̄ + 3√(p̄(1-p̄)/n_i)`。
分母（当天的生产数）每天不一样，用固定控制限图会把"产量少的那天"
误判成异常——这是手画 P 图最常见的错误。

### 11.8 `GET /analytics/control-chart`

计量型控制图，`item_code` 必填。按子组大小自动选图：

| 条件 | 选用的图 | 说明 |
|---|---|---|
| 存在 n ≥ 2 的子组 | X̄-R | 子组内极差估计 σ |
| 全部 n = 1 | I-MR | 用移动极差估计 σ（E2=2.66, D4=3.267） |
| 子组大小不一致 | 返回 `need_regroup: true` | 不给图，先要求重新分组 |

第三种情况刻意不自动兜底：混着 n=3 和 n=5 的子组算 R̄，控制限是错的，
而错的控制限会让整张图失去意义。宁可让用户回去把子组切齐。

返回值里 `rules` 是 8 条判异准则的逐条结果（Nelson 规则），
`verdict` 里第一条永远是"先看 R 图"——R 图失控时 X̄ 图的限本身不可信。

```json
{"data":{
  "chart_type":"xbar_r","subgroup_size":5,
  "xbar":{"center":10.002,"ucl":10.028,"lcl":9.976,"points":[...]},
  "r":{"center":0.043,"ucl":0.091,"lcl":0,"points":[...]},
  "rules":[{"id":1,"name":"1 点超 3σ","hit":true,"points":[78]}],
  "capability":{"cp":1.39,"cpk":1.21,"ppk":1.08,"grade":"B"},
  "verdict":{"level":"warn","text":"R 图规则 1 触发，先查量具与装夹"}
}}
```

### 11.9 `POST /analytics/measurements` 与 `/batch`

单条 / 批量录入实测值。批量接口是**单事务**，要么全进要么全不进；
单条接口给现场调试用。

```json
{"item_code":"外径","value":10.012,"line_id":"line-01",
 "work_order_id":"WO-DEMO-05","subgroup_no":31,
 "unit":"mm","usl":10.08,"lsl":9.92,"gauge":"G-01",
 "operator":"张三","occurred_at":"2026-11-06T08:00:00"}
```

`occurred_at` 可空（默认当前时间）。`usl`/`lsl` 随记录存：
规格是会改的，三个月后算 Cpk 必须用**当时**的规格，不能读当前主数据。

---

## 12. 批次谱系追溯接口

**权限**：`quality_manager` / `quality_engineer`。班组长不开放——
他能看到自己线上的批次，但完整的前向影响面（跨线、跨客户）不该给到现场。

### 12.1 数据模型：网状而不是树

`material_lot`（物料批）× `lot_consumption`（领料关系）是**多对多**：

```
批 L1 ──┐
批 L2 ──┼── 工单 WO-01 ──┐
        │                ├── 缺陷单 DF-01
批 L3 ──┴── 工单 WO-02 ──┘
```

一批料投多张工单，一张工单吃多批料。用树（parent_id）建模会在
第二张工单上直接撞墙——这不是实现选择，是问题本身的结构。

### 12.2 `GET /genealogy/lots/{lot_id}` 前向追溯

批 → 工单 → 缺陷单。返回 `orders`、`defects`、`coverage`（有工单号的比例）。

`coverage < 1` 时结果里会带 `warning`：领料记录不全时，前向追溯
**只能说明"查到这些"，不能说明"只有这些"**。把不完整说成完整，
是追溯功能最危险的失败方式。

### 12.3 `GET /genealogy/defects/{defect_no}/backward` 反向追溯

缺陷单 → 工单 → 涉及的物料批，并给出**可疑度排序**。

排序依据是"该批还出现在多少张其他工单上"：

| 条件 | 可疑度 |
|---|---|
| 该批只出现在这一张出问题的工单 | 高 |
| 出现在 2 张，其中 1 张有问题 | 中 |
| 出现在 5 张以上且都正常 | 低（更可能是偶发） |

`min_orders` 参数（默认 2）是必要的护栏：不设阈值时，
**每一批料都"值得怀疑"**，排序就退化成噪声。

返回值里恒定带一句 `caveat`：**相关不等于因果**。
同一批料进了 6 张工单只有 1 张出问题，问题更可能在那一张的工艺参数上。

### 12.4 `GET /genealogy/lots/{lot_id}/graph`

给页面画图用的三列结构（批 → 工单 → 缺陷），`max_orders` 限制工单数
（默认 20）。超过时截断并在 `truncated` 里说明，不静默省略。

### 12.5 `GET /genealogy/suppliers/scorecard`

供应商记分卡：批次数、涉及工单数、缺陷件数、`defect_rate`、`grade`。

影响面分级（用于判断要不要发 SCAR）：

| 涉及工单 | 或 缺陷件数 | 等级 |
|---|---|---|
| ≥ 10 | ≥ 2000 | 高 |
| ≥ 5 | ≥ 500 | 中 |
| 其他 | 其他 | 低 |

两个条件取**或**：一批只进 3 张工单但每单报废 800 件，同样必须升级。

---

## 13. 数据导入导出接口

**权限**：导入/导出均为 `quality_manager` / `quality_engineer`。
班组长没有导入权——这是唯一能一次写坏主数据表的入口；
现场录入的正确通道是 `/api/v1/defects`（单条、有确认步骤）。

### 13.1 `GET /io/datasets`

返回可导入/可导出的数据集、列定义、别名表与模板表头。

```json
{"data":{"importable":[
  {"key":"work_orders","label":"工单","columns":[
    {"name":"work_order_id","required":true,"aliases":["工单号","工单","单号"]}]}],
  "exportable":["defects","attributions","audit","pareto","suppliers"]}}
```

页面据此渲染表单——**加数据集不用改前端**。

### 13.2 `GET /io/template/{dataset}`

下载导入模板（表头 + 两行样例，UTF-8 带 BOM）。
带 BOM 是因为 Excel 双击打开无 BOM 的 UTF-8 CSV 会显示乱码，
车间用户第一反应是"文件坏了"。

### 13.3 `POST /io/import/preview` 与 `POST /io/import`

| 参数 | 说明 |
|---|---|
| `dataset` | 数据集 key |
| `file` | 上传文件（multipart） |
| `dry_run` | 仅预览时使用 |
| `allow_partial` | 默认 true；false 时先空跑一遍，全通过才落库 |

拆分两个 URL 而不是一个带开关的接口：误点"确认导入"不该因为参数传错
真的写库。

预览返回逐行错误：

```json
{"data":{"total":120,"inserted":117,"skipped":2,"failed":1,
  "errors":[{"row":42,"field":"产线","message":"产线不能为空"}],
  "error_overflow":0,"message":"可导入 117 行（尚未写入）"}}
```

`row` 按 Excel 习惯从 2 起（第 1 行是表头）——报"第 42 行"用户能直接定位，
报"第 41 条记录"他得自己数。

### 13.4 编码与分隔符

导入前的自动判定顺序：

```
utf-8-sig → utf-8 → gb18030 → big5 → latin-1
```

GBK 编码的中文用 utf-8 解码不报错（会得到一堆替换字符），所以
解码成功之后还要做一次 **GBK 乱码启发式检查**：连续出现
`\ufffd` 或典型 GBK 字节对乱码时回退到 gb18030。
宁可多试一轮，也不要让用户拿到一列问号。

分隔符在 `,` `;` `\t` `|` 之间按首行出现次数投票。

### 13.5 CSV 注入防护

单元格以 `=` `+` `-` `@` 开头时，导出前置一个单引号 `'`。
这是 Excel 公式注入（`=cmd|'/c calc'!A1`）的标准缓解方式。
牺牲一个可见字符，换掉一个可执行载荷。

### 13.6 `GET /io/export` 与 `/io/export/bundle`

```bash
curl -OJ "http://127.0.0.1:8000/api/v1/io/export?dataset=defects&fmt=xlsx" \
  -H "Authorization: Bearer $QE_TOKEN"
```

`fmt` 支持 `csv` / `xlsx`。`bundle` 把三张表打进一个多 sheet 的 xlsx。
XLSX 由 `core/io/xlsx.py` 手写 OOXML（zip + 6 个 part），不依赖 openpyxl——
依赖少一个，离线部署就少一个坑。

`Content-Disposition` 同时给 `filename`（ASCII 兜底）与
`filename*=UTF-8''...`（RFC 5987）。只给前者时 Chrome 会丢扩展名、
Safari 显示百分号编码串；兜底串去噪后为空则退回 `export.<ext>`。

---

## 14. 测量系统分析（MSA）接口

**权限**：`quality_manager` / `quality_engineer`。

### 14.1 `POST /msa/studies` 登记一次研究

```json
{"item_code":"外径","gauge":"G-012","tolerance":0.16,
 "rows":[{"appraiser":"张三","part":"P01","trial":1,"value":10.004}, ...],
 "occurred_at":"2026-11-03T10:00:00"}
```

返回两种算法的结果，**都报、不择优**：

| 字段 | 含义 |
|---|---|
| `average_range` | 均值极差法（现场能手算） |
| `anova` | ANOVA 法（能拆交互项） |
| `percent.grr` | %GRR（占研究变差） |
| `percent.tolerance` | %GRR（占公差带） |
| `ndc` | 可区分类别数（分辨率） |
| `verdict` | accept / conditional / reject |

两法差 >5 个百分点时提示"存在交互效应或异常值"，而不是取平均。
把差异藏起来比暴露差异危险。

`ndc` 是独立否决项：%GRR=28%（"有条件接受"）时 ndc 可能只有 4，
用这种分辨率去判 8 条判异准则等于抛硬币。

### 14.2 `GET /msa/studies/{id}` 与 `POST .../recompute`

`recompute` 用留档的原始读数重算，**只并排返回新旧结果，不覆盖归档**。
覆盖会让"当时凭什么签字"这件事永久丢失。

### 14.3 `GET /msa/consistency` —— 产线数据的读数一致性

**这不是 GR&R**，接口名与返回值都写明了。

产线数据里每件产品只测一次，`同一人重复测同一件`不存在，
重复性与零件变差无法分离。硬算出来的 %GRR 会到 90% 以上，
那是算法制造的结论（详见 ADR-0008）。

它答的是另一个问题：同一张工单上，不同班次的人读数水平一致吗？

```bash
curl "http://127.0.0.1:8000/api/v1/msa/consistency?item_code=外径&z_limit=3" \
  -H "Authorization: Bearer $QE_TOKEN"
```

判据：`z = |x̄1 - x̄2| / (s_pooled × √(1/n1 + 1/n2))`，`s_pooled` 用**组内**标准差。

用全局标准差会把工单之间的真实差异算进噪声，导致永远判"一致"——
这是这类检查最常见的错误，也是它给出假安全结论的原因。

| 返回字段 | 说明 |
|---|---|
| `sigma_pooled` | 组内合并标准差 |
| `details[]` | 逐工单：各人均值、最大均值差、z、是否超限 |
| `operator_bias[]` | 逐人：整体偏移量、z |
| `verdict` | accept / conditional / reject + 中文说明 |
| `caveat` | 恒定的口径提示："这不是 GR&R" |

### 14.4 `GET /msa/advice` 正式试验方案

返回 10 件 × 3 人 × 3 次的安排、判定阈值档位、公差带提醒。
把"要正式结论请重做试验"落成一个可点的按钮，而不是只写一句"数据不足"。

### 14.5 `GET /msa/gaps` 覆盖率与缺口

```json
{"data":{"items":2,"covered_items":2,"coverage":1.0,
  "gaps":[{"item_code":"同轴度","line_id":"line-01","records":312}]}}
```

`gaps` 是"有实测值但从未做过 MSA"的项目清单。
这是本项目里最值得被看见的一张表：它把"我们一直在画控制图，
但从没验证过尺子"变成一个可以点开、可以派活的名字。

---

## 15. 端点总表

共 74 个端点。`SL` = 班组长，`QE` = 质量工程师，`QM` = 质量主管。

| # | 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|---|
| 1 | POST | `/auth/login` | 公开 | 登录，返回 JWT + 写 HttpOnly cookie |
| 2 | GET | `/auth/me` | 全角色 | 当前身份 |
| 3 | POST | `/chat` | 全角色 | 问答（SSE 流） |
| 4 | POST | `/sessions` | 全角色 | 新建会话 |
| 5 | GET | `/sessions/{id}/messages` | 本人 | 会话历史 |
| 6 | DELETE | `/sessions/{id}` | 本人 | 删除会话 |
| 7 | GET | `/health` | 公开 | 健康检查 |
| 8 | POST | `/admin/documents` | QM/QE | 上传文档（版本+生效区间必填） |
| 9 | GET | `/admin/documents` | QM/QE | 文档列表（含已废止） |
| 10 | GET | `/admin/documents/{id}/chunks` | QM/QE | 分片预览 |
| 11 | POST | `/admin/documents/{id}/archive` | QM | 废止（触发索引重建） |
| 12 | POST | `/admin/documents/{id}/restore` | QM | 恢复 |
| 13 | GET | `/admin/prompt-templates` | QM/QE | 模板列表 + 历史 |
| 14 | POST | `/admin/prompt-templates` | QM | 新建 |
| 15 | PUT | `/admin/prompt-templates/{id}` | QM | 修改（即刻生效 + 留痕） |
| 16 | GET | `/admin/audit/messages` | QM/QE | 会话审计（可筛越界） |
| 17 | GET | `/admin/audit/messages/{id}` | QM/QE | 单条详情 |
| 18 | POST | `/admin/audit/messages/{id}/review` | QM | 复核标记 |
| 19 | POST | `/admin/audit/rescan` | QM | 全量重扫（历史数据版本失效） |
| 20 | GET | `/admin/attributions` | QM/QE | 归因单列表（按状态） |
| 21 | GET | `/admin/attributions/{id}` | QM/QE | 详情（L1–L4 + 证据） |
| 22 | POST | `/admin/attributions/{id}/approve` | QM | 审批通过 |
| 23 | POST | `/admin/attributions/{id}/reject` | QM | 驳回（必填理由） |
| 24 | GET | `/admin/attributions/{id}/export-8d` | QM | 导出 8D markdown |
| 25 | GET | `/defect-codes` | SL/QE/QM | 缺陷码候选 |
| 26 | POST | `/defects` | SL | 建单（草稿或待审） |
| 27 | POST | `/defects/{no}/confirm` | SL | 人工确认缺陷码 → pending |
| 28 | GET | `/work-orders/{id}` | SL/QE/QM | 工单信息 |
| 29 | GET | `/defects` | SL/QE/QM | 缺陷列表（SL 仅本线） |
| 30 | GET | `/defects/{no}` | SL/QE/QM | 缺陷详情（SL 不含归因结论） |
| 31 | POST | `/uploads/photo` | SL | 图片预上传（magic byte 校验） |
| 32 | GET | `/analytics/overview` | QE/QM | KPI 汇总 |
| 33 | GET | `/analytics/dashboard` | QE/QM | 看板聚合 |
| 34 | GET | `/analytics/pareto` | QE/QM | 帕累托（含 ABC） |
| 35 | GET | `/analytics/pareto/compare` | QE/QM | 两期对比 |
| 36 | GET | `/analytics/trend` | QE/QM | 趋势（拟合 + EWMA） |
| 37 | GET | `/analytics/weekday` | QE/QM | 星期效应 |
| 38 | GET | `/analytics/p-chart` | QE/QM | 不合格品率控制图 |
| 39 | GET | `/analytics/control-chart` | QE/QM | X̄-R / I-MR 控制图 |
| 40 | GET | `/analytics/items` | QE/QM | 计量项目清单 |
| 41 | POST | `/analytics/measurements` | QE/QM | 录入实测值 |
| 42 | POST | `/analytics/measurements/batch` | QE/QM | 批量录入（单事务） |
| 43 | GET | `/genealogy/overview` | QE/QM | 谱系概览 |
| 44 | GET | `/genealogy/lots` | QE/QM | 批次列表 |
| 45 | POST | `/genealogy/lots` | QE/QM | 登记批次 |
| 46 | POST | `/genealogy/lots/{id}/consume` | QE/QM | 登记领料关系 |
| 47 | GET | `/genealogy/lots/{id}` | QE/QM | 前向追溯（批→工单→缺陷） |
| 48 | GET | `/genealogy/lots/{id}/graph` | QE/QM | 图谱（页面绘图用） |
| 49 | GET | `/genealogy/defects/{no}/backward` | QE/QM | 反向追溯 + 可疑度排序 |
| 50 | GET | `/genealogy/suppliers/scorecard` | QE/QM | 供应商记分卡 |
| 51 | GET | `/io/datasets` | QE/QM | 数据集元信息 |
| 52 | GET | `/io/template/{dataset}` | QE/QM | 导入模板下载 |
| 53 | POST | `/io/import/preview` | QE/QM | 导入预览（不落库） |
| 54 | POST | `/io/import` | QE/QM | 导入落库 |
| 55 | GET | `/io/export` | QE/QM | 单表导出 csv/xlsx |
| 56 | GET | `/io/export/bundle` | QE/QM | 多表打包 xlsx |
| 57 | GET | `/msa/overview` | QE/QM | 覆盖率与结论统计 |
| 58 | GET | `/msa/gaps` | QE/QM | 未做 MSA 的项目 |
| 59 | GET | `/msa/studies` | QE/QM | 研究记录列表 |
| 60 | POST | `/msa/studies` | QE/QM | 登记研究（双算法） |
| 61 | GET | `/msa/studies/{id}` | QE/QM | 详情 |
| 62 | POST | `/msa/studies/{id}/recompute` | QE/QM | 重算（不覆盖归档） |
| 63 | GET | `/msa/consistency` | QE/QM | 人员读数一致性（非 GR&R） |
| 64 | GET | `/msa/advice` | QE/QM | 正式试验方案建议 |

页面路由（SSR，共 13 个）：

| 路径 | 角色 | 说明 |
|---|---|---|
| `/chat` | 全角色 | 质量问答（SSE + 打字机） |
| `/m` | SL | 现场录入（移动端单页） |
| `/admin/login` | 公开 | 后台登录 |
| `/admin/dashboard` | QM/QE | 概览 |
| `/admin/documents` | QM/QE | 知识库 |
| `/admin/prompts` | QM/QE | 提示词模板 |
| `/admin/audit` | QM/QE | 会话审计 |
| `/admin/attributions` | QM/QE | 归因审批 |
| `/admin/attributions/{id}` | QM/QE | 归因详情 + 8D |
| `/admin/analytics` | QM/QE | 质量看板 |
| `/admin/genealogy` | QM/QE | 批次追溯 |
| `/admin/msa` | QM/QE | 测量系统分析 |
| `/admin/data` | QM/QE | 数据导入导出 |

---

## 16. 调用示例（统计 / 追溯 / 导入导出 / MSA）

### 16.1 拉一次看板数据

```bash
QE=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"qe001","password":"qe001"}' | jq -r .data.token)

curl -s "http://127.0.0.1:8000/api/v1/analytics/dashboard?days=14&item_code=外径" \
  -H "Authorization: Bearer $QE" \
  | jq '{kpi:.data.overview.kpi, verdict:.data.chart.verdict.text}'
```

### 16.2 反查一张缺陷单用了哪些料

```bash
curl -s "http://127.0.0.1:8000/api/v1/genealogy/defects/DF-DEMO-0007/backward" \
  -H "Authorization: Bearer $QE" \
  | jq '.data.suspects[] | {lot_id, supplier, other_orders, level}'
```

### 16.3 导入一份车间 CSV（先预览）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/io/import/preview \
  -H "Authorization: Bearer $QE" \
  -F "dataset=work_orders" -F "file=@工单.csv" \
  | jq '{total:.data.total, inserted:.data.inserted, errors:.data.errors}'
```

### 16.4 登记一次 GR&R 研究

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/msa/studies \
  -H "Authorization: Bearer $QE" -H "Content-Type: application/json" \
  -d @grr.json | jq '{pct:.data.study.pct_grr, ndc:.data.study.ndc,
                      verdict:.data.study.verdict}'
```

### 16.5 页面巡检脚本

```bash
python scripts/ui_check_analytics.py 8000 --shot   # 看板 14 项
python scripts/ui_check_genealogy.py 8000 --shot   # 追溯 12 项
python scripts/ui_check_data.py     8000 --shot    # 导入导出 15 项
python scripts/ui_check_msa.py      8000 --shot    # MSA 23 项
```
