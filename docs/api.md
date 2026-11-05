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

