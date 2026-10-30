# 质量工程师问答入口 · 接口设计（阶段 2）

> 前置：`docs/api.md` 目前只规范 CLI 与配置，本文是首个 HTTP 接口规范，确认后并入 `docs/api.md`。
> 复用：检索链路直接用 `core/knowledge/`（BM25 + 向量 + RRF），LLM 走 `core/llm/client.py`。
> 状态：**已确认并实现**（2026-10-30）。
>
> **决策记录**：A① 新增 3 张表 ｜ B① requests 流式 + 线程桥接（零新依赖）｜
> C① 请求头模拟身份 ｜ D① 消息允许 UPDATE 改状态。
> 实现落点：`src/quality_trace/api/`（入口层）、`core/services/chat.py`（服务层）、
> `core/repo/db.py`（三表）、`web/chat.html`（前端）。
> 启动方式：`bash scripts/run_server.sh`（或 PowerShell 版），默认 http://127.0.0.1:8000/ 。
> 全量测试：61 个用例通过（含本接口的离线降级链路）。

---

## 1. 统一响应封装与错误码（先定错误，再定接口）

所有 JSON 接口一律返回：

```json
{ "code": 0, "message": "成功", "data": { } }
```

`code != 0` 即失败，`message` 恒为中文可直接展示给用户的文案，`data` 为 null。

| code | HTTP | message（中文） | 触发场景 | 前端处理 |
|---|---|---|---|---|
| 0 | 200 | 成功 | 正常 | — |
| 40001 | 400 | 请求参数缺失或格式错误 | 缺 `question` / JSON 解析失败 | 红条提示 |
| 40002 | 400 | 问题长度超出限制（最多 1000 字） | `len(question) > 1000` | 红条提示 |
| 40101 | 401 | 未登录或身份无效 | 缺 `X-User-Id` 请求头 | 跳登录（演示态：提示配置用户） |
| 40301 | 403 | 当前角色无权使用问答功能 | 角色 ≠ 质量工程师 | 红条 + 禁用输入框 |
| 40401 | 404 | 会话不存在或已被删除 | `session_id` 无效 | 前端重建会话 |
| 40402 | 404 | 知识库索引不存在，请先执行入库 | 未跑 `--ingest` | 红条 + 提示管理员 |
| 40901 | 409 | 会话已归档，无法继续提问 | `session.status = archived` | 提示新建会话 |
| 40902 | 409 | 上一条回答仍在生成中，请稍候 | 同会话并发提问 | 禁用发送按钮 |
| 42901 | 429 | 请求过于频繁，请稍后再试 | 单会话 > 20 次/分钟 | 红条 + 冷却倒计时 |
| 50001 | 200 | 向量服务不可用，已切换关键词检索 | embedding 失败（**降级，非失败**） | 黄条提示，答案照常 |
| 50002 | 200 | 大模型不可用，仅返回检索依据 | LLM 失败（**降级，非失败**） | 黄条 + 只渲染引用区 |
| 50401 | 200 | 大模型响应超时，回答可能不完整 | 流式超时（**降级**） | 黄条 + 标记「已截断」 |
| 50003 | 500 | 服务内部错误，请稍后重试 | 未捕获异常 | 红条 + 展示 request_id |
| 50301 | 503 | 检索索引加载失败，请联系管理员 | 索引文件损坏 | 红条 |

**降级与失败的边界（关键约定）**：
- `50001 / 50002 / 50401` 属于**降级**：HTTP 200、body `code = 0`，降级信息放 `data.degraded`，回答仍然可用；
- 其余非零 code 属于**失败**：不产出回答；
- 但 SSE 流一旦开始，HTTP 状态码已发出（200），**流中错误只能通过 SSE `error` 事件携带错误码**（见 3.3）。

---

## 2. 接口清单

| # | 方法 | 路径 | 用途 |
|---|---|---|---|
| 1 | POST | `/api/v1/chat` | **流式问答主入口**（SSE） |
| 2 | POST | `/api/v1/sessions` | 新建会话（不传 session_id 时后端也会自动建） |
| 3 | GET | `/api/v1/sessions/{session_id}/messages` | 拉取历史消息（刷新页面恢复现场） |
| 4 | DELETE | `/api/v1/sessions/{session_id}` | 归档会话 |
| 5 | GET | `/api/v1/health` | 健康检查：embedding / LLM / 索引可用性 |
| 6 | GET | `/` | 单页聊天界面（静态 HTML） |

身份通过请求头传递（演示态，不做 JWT）：`X-User-Id: qe-001`、`X-Role: quality_engineer`。

---

## 3. `POST /api/v1/chat`（核心）

### 3.1 请求

```http
POST /api/v1/chat
Content-Type: application/json
Accept: text/event-stream
X-User-Id: qe-001
X-Role: quality_engineer
```

```json
{ "session_id": "sess-9f2c...", "question": "切刀磨损导致螺纹外径偏小，怎么处理？" }
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string | 否 | 为空时后端自动创建新会话 |
| `question` | string | 是 | 1–1000 字，前后空白会被 trim |

### 3.2 处理时序

```
1. 校验身份与参数        → 失败：JSON 错误返回（4xx/5xx），不进流
2. 会话存在性 / 并发检查 → 失败：同上
3. 用户消息落库（chat_message, role=user, status=complete）
4. 取最近 6 轮历史（见第 5 节）
5. 检索：core/knowledge.hybrid_search → top3 + meta
6. 写 retrieval_log（复用架构表），拿到 query_id
7. 打开 SSE，先发 status「正在检索依据…」→ 再发 sources
8. 调 LLM 流式（core/llm 新增 stream 方法），逐块发 delta
9. 流结束：assistant 消息 + 引用落库 → 发 done
   任一步失败：发 error 事件（带错误码）+ 已生成内容按 partial 落库
```

### 3.3 SSE 事件序列

响应头：`Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`。

| event | 时机 | data 结构 |
|---|---|---|
| `status` | 检索开始 | `{"stage":"retrieving","message":"正在检索依据…"}` |
| `sources` | 检索完成（**先于正文**，保证断流也有依据） | `{"sources":[{...}]}` 见第 6 节 |
| `delta` | 每收到一段流式文本 | `{"text":"…"}` |
| `done` | 正常结束 | `{"message_id":"...","session_id":"...","degraded":{...},"latency_ms":1820,"history_turns":6}` |
| `error` | 流中失败 | `{"code":50003,"message":"服务内部错误，请稍后重试","request_id":"..."}` |
| `: ping` | 每 15 秒心跳 | 注释行，防代理掐断 |

为什么 `sources` 早于正文发：断流/超时时引用仍完整落库并可渲染，符合架构 4.4「引用可审计」；前端把它渲染在回答卡片**底部**（DOM 顺序与事件顺序解耦）。

### 3.4 前端行为约定

- `status` → 显示「正在检索依据…」打字动画，禁用发送按钮；
- `delta` → 打字机追加（requestAnimationFrame 平滑，见第 7 节）；
- `done` → 解除禁用，若 `degraded` 非空显示黄色提示条；
- `error` → 显示 `code + message`，已收到内容标「已中断」；
- 用户点「停止」→ `AbortController` 关闭连接，后端检测到断连后写 partial 消息。

---

## 4. 新增数据表（3 张）

⚠️ **需要先确认**：架构第 3 节定义的是 8 张结构化表 + 3 张知识库表，**没有会话/消息表**。这里增补 3 张，列为架构第 3.1 节的扩展。

| 表 | 字段 | 说明 |
|---|---|---|
| `chat_session` 会话 | session_id PK · user_id · role · title · status(active/archived) · message_count · occurred_at · recorded_at · last_message_at | title 取首条用户问题前 20 字 |
| `chat_message` 消息 | message_id PK · session_id FK · role(user/assistant) · content · status(complete/partial/failed) · degraded(bool) · degraded_reason · degraded_code · retrieved_json(命中切片 + rrf + mode) · query_id(→retrieval_log) · model · prompt_hash · latency_ms · occurred_at · recorded_at | 沿用双时间列约定；**流式中断也要写 partial**，保证审计不丢 |
| `chat_message_citation` 引用 | citation_id PK · message_id FK · seq · source_file · doc_id · clause(条款号/章节) · version · doc_type · chunk_id · score · snippet · created_at | 引用结构化落库（不塞 JSON），便于审计与后续版本校验 |

索引：`chat_message(session_id, created_at)`、`chat_message_citation(message_id)`；`chat_message` 只追加、不 UPDATE（与审计日志同策略，partial→complete 走新行或状态字段二选一，见待确认项）。

---

## 5. 历史上下文规则（最近 6 轮）

- 取本会话**最后 12 条**消息（user + assistant 各 6），按 `occurred_at` 正序还原；
- 若最后一条是 user（说明上轮异常中断），丢弃它以保证成对；
- 字符预算兜底：历史总长 > 6000 字时，从最旧一侧成对丢弃，至少保留 1 轮；
- 拼装顺序：`system`（引用与红线约束）→ 历史 user/assistant 正文 → 当前问题；
- **历史 assistant 只带正文**，不重复拼接引用材料（引用由本次检索实时给出，避免旧引用污染）。

---

## 6. 引用契约（回答底部展示）

`sources` 事件与落库的 `chat_message_citation` 同构：

```json
{
  "seq": 1,
  "doc_name": "检验标准-螺纹外径检验规范v2.md",
  "clause": "3. 抽样方案",
  "version": "v2",
  "doc_type": "standard",
  "chunk_id": "7",
  "score": 0.0164,
  "snippet": "首件检验：每班次开工后前 3 件全检…"
}
```

展示规则：
- 卡片固定三要素：**文档名 + 条款号 + 版本号**；版本号缺失显示「—」，`doc_type` 映射中文标签（standard→检验标准 / sop_manual→工艺文件 / 8d_case→历史8D）；
- 点击展开 `snippet`（原文片段）；
- **前向兼容**：架构 4.3 的版本闸上线后，若引用到已废止版本，卡片需追加红色「已废止（v2 起失效）」标记——字段现在就留好，届时只改渲染不改结构。

---

## 7. 前端页面契约（单页 HTML + 原生 JS）

| 区域 | 内容 | 状态 |
|---|---|---|
| 顶部栏 | 会话标题、当前角色「质量工程师」、健康状态灯 | 调 `/health` |
| 消息区 | 用户气泡 / 助手气泡；助手卡片底部挂引用区 | — |
| 状态条 | 「正在检索依据…」/「正在生成回答…」/ 降级黄条 / 错误红条 | SSE `status`/`degraded`/`error` |
| 输入区 | 文本框 + 发送 + 停止按钮 | idle / streaming |
| 引用区 | 文档名 · 条款号 · 版本号，可展开片段 | SSE `sources` |

要点：
- 用原生 `EventSource` 无法发 POST → 用 `fetch` + `ReadableStream` 手动解析 SSE，或 `XMLHttpRequest`；**推荐 fetch 流式解析**（可控、能带请求头）；
- 打字机：`delta` 入队，`requestAnimationFrame` 每帧吐 1–2 字，避免整段闪现；
- 停止：`AbortController.abort()`，后端捕获断连写 partial；
- 刷新恢复：调 `/sessions/{id}/messages` 重建气泡（不重放打字机）。

---

## 8. 待你拍板（确认后我才写完整代码）

| # | 问题 | 选项 |
|---|---|---|
| A | 会话/消息表 | ① 新增 3 张表（本文方案，可审计）；② 只加 `chat_session`+`chat_message`，引用塞 `message.meta_json`（省事但不可单独审计） |
| B | LLM 流式实现 | ① `requests(stream=True)` + `asyncio.to_thread`（零新依赖，与现有 client 一致）；② 引入 `httpx.AsyncClient`（原生异步，更干净，多一个依赖） |
| C | 鉴权 | ① 请求头 `X-User-Id` / `X-Role` 模拟（课程演示最快）；② 简单 JWT + 密码（更完整，多写代码） |
| D | 并发写库 | ① `chat_message` 允许 UPDATE 改 status（简单）；② 严格 append-only（与审计日志一致，需状态表或版本号） |

我倾向 **A① + B① + C① + D①**：与现有零依赖风格一致，演示最快，架构要求的审计能力也能保住。
