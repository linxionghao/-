# 数据模型

> 对应代码：`src/quality_trace/core/repo/db.py`（建表 + 全部读写函数）
> 对应设计：`docs/architecture.md` §3.1 的 8 + 3 表，经阶段 3 落地为 **12 张表**
> 状态：已实现并通过单测（`tests/test_chat_repo.py`、`test_defects.py`、`test_audit.py`、`test_attribution.py`）

数据库选型是 SQLite（WAL 模式），单文件 `data/trace.db`。选它的理由不是"轻量"，而是**课程项目阶段真正会被反复读写的关系只有这 12 张表**，且 SQLite 支持我们最需要的两件事：可空外键上的 `JOIN` 过滤（版本闸）、以及外部可读的单一文件（答辩时可以直接用 DB Browser 打开看数据）。

---

## 1. 表清单与归属域

| # | 表 | 域 | 作用 | 关键约束 |
|---|---|---|---|---|
| 1 | `sys_user` | 组织 | 用户与角色、产线绑定 | `username` 唯一；`role ∈ {quality_engineer, shift_leader, quality_manager}` |
| 2 | `kb_doc` | 知识库 | 文档元数据（版本 + 生效区间 + 上下架） | `(doc_name, version)` 唯一 → 重复上传 40903 |
| 3 | `kb_chunk` | 知识库 | 切片正文与所在章节 | `doc_id` 外键；`(doc_id, seq_no)` 索引 |
| 4 | `prompt_template` | 提示词 | 当前生效提示词 | 业务上全库仅一条 `status='active'` |
| 5 | `prompt_template_history` | 提示词 | 每次修改留全量快照 | 写入即追加，不更新不删除 |
| 6 | `chat_session` | 问答 | 会话 | `user_id` 归属校验的锚点 |
| 7 | `chat_message` | 问答 | 消息 + 降级标记 + 审计标记 | `status ∈ {complete, partial, failed}` |
| 8 | `chat_message_citation` | 问答 | 结构化引用（可单独审计） | `(message_id, seq)` 索引 |
| 9 | `work_order` | 生产 | 工单（简表） | `line_id` 是数据级权限的锚点 |
| 10 | `defect_item` | 生产 | 缺陷单（双时间 + 补录 + 幂等键） | `client_draft_id` 唯一 |
| 11 | `attribution_order` | 归因 | 归因单 + 审批状态 + 8D 缓存 | `status ∈ {submitted, approved, rejected}` |
| 12 | `defect_code` | 主数据 | 缺陷码字典（归一化候选来源） | `code` 主键 |

与设计稿的差异（都是减法，不是偷偷加表）：

- 设计稿的 `retrieval_log` 未单独建表，检索命中直接落在 `chat_message.retrieved_json` + `chat_message.query_id`，少一张表少一次写入；
- 设计稿的批次谱系（`batch` / `material_lot` / `process_record`）留到阶段 4，本阶段 `work_order` 只保留 `line_id` 与 `planned_qty` 两个真正被用到的字段；
- 缺陷码字典 `defect_code` 是设计稿之外的**第 12 张**，因为描述归一化必须有一个可维护的码表作候选来源，否则只能硬编码在代码里。

---

## 2. 双时间戳：`occurred_at` / `recorded_at`

除 `defect_code`（纯字典，无时间语义）外，**所有表都成对出现**这两列：

| 列 | 含义 | 由谁决定 |
|---|---|---|
| `occurred_at` | 业务时刻——这件事在车间/业务上"发生"的时间 | 业务方（可被补录修正） |
| `recorded_at` | 录入时刻——这条记录"进系统"的时间 | 服务端 `datetime.now()`，不可被客户端指定 |

为什么必须分开：班组长在 14:05 发现不良，但手机没信号，15:20 才补传。如果用 `recorded_at` 当业务时间，SPC 的按小时不良率会在 15 点凭空出现一个尖峰，而 14 点干干净净——**质量问题被时间轴抹平了**。

落到具体实现：

- `defect_item.occurred_at` 允许客户端上传时指定（现场时间），`recorded_at` 一律服务端生成；
- 两者的差值 > 60s 时 `is_backfill = 1`（常量 `BACKFILL_THRESHOLD_SECONDS`，见 `core/services/defects.py`），前端凭据页展示"补录"角标，审计导出时也能把补录单单独筛出；
- `kb_doc.occurred_at` 是"文档生效"的业务时刻（默认取 `effective_from`），`recorded_at` 是上传时刻——下架一份 2024 年的旧标准，不影响它在 2024 年那段时间的有效性。

---

## 3. 三张知识库表：为什么版本信息必须落在 `kb_doc` 而不是切片里

```sql
kb_doc    (doc_id, doc_name, doc_type, version, effective_from, effective_to, status, ...)
   │ 1
   │
   │ N
kb_chunk  (chunk_id, doc_id, seq_no, content, clause, char_len, ...)
```

版本、生效区间、上下架状态**只存一份**，在 `kb_doc` 上；`kb_chunk` 完全不冗余这些字段。这不是范式洁癖，而是三道版本闸的实现前提：

1. **索引重建闸**：`status='archived'` 的文档，其切片在重建索引时直接不进入向量库与 BM25 语料；
2. **SQL 时间闸**：`repo.list_chunks_as_of(conn, as_of)` 用 `JOIN kb_doc` 后过滤 `status='active' AND effective_from <= as_of <= effective_to`——因为版本信息只有一份，这个过滤条件永远和"现行版本"的定义一致，不会被某张表忘了改；
3. **审计复核闸**：`audit.detect_flags()` 拿引用里的 `(doc_name, version)` 去比对 `repo.archived_doc_versions()`，发现引用了已下架版本就打 `flag_stale_version`。

如果版本号冗余进切片，那么"下架文档"就要改 N 行切片，漏一行就漏一道闸。**一个字段只有一个家**是这三道闸能成立的根本原因。

`clause` 列（切片所在标题）是引用溯源的定位锚点：回答里出现 `[C2]` 时，前端展示的是"《螺纹外径检验规范》v2 · 3.2 抽样方案"，而不是一句无出处的正文。

---

## 4. 索引与唯一约束：每个都是被 bug 逼出来的

| 约束 / 索引 | 防的是什么 |
|---|---|
| `kb_doc(doc_name, version)` UNIQUE | 同名同版本重复上传 → 切片翻倍、检索结果里同一段话出现两次。业务层翻译成 40903，提示"该文档该版本已存在，请先下架或改版本号" |
| `defect_item.client_draft_id` UNIQUE | 离线队列重发。手机端断网重试、用户连点两次提交，都会带同一个 `client_draft_id`，第二次直接返回首次结果（幂等），不会产生两张缺陷单 |
| `defect_item(line_id, occurred_at)` | 班组长列表只查自己产线 + 按时间倒序，这是唯一高频查询路径 |
| `chat_message(session_id, occurred_at)` | 会话回放；`build_history` 取最近 6 轮 |
| `chat_message_citation(message_id, seq)` | 引用按序展示，`seq` 即 `[C1]` 的编号 |
| `attribution_order(status, submitted_at)` | 主管待办列表（`status='submitted'` 按提交时间排） |
| `kb_doc(status, doc_type)` | 知识库列表按"现行 / 已下架 + 语料类型"筛选 |
| `kb_chunk(doc_id, seq_no)` | 重建索引时按文档顺序读切片 |

外键全部开启（`PRAGMA foreign_keys=ON`）：切片不可能挂在不存在文档上，引用不可能挂在不存在消息上，缺陷不可能挂在不存在工单上。

---

## 5. 迁移策略：只加列，不改列，不删列

SQLite 没有 `ADD COLUMN IF NOT EXISTS`，因此 `db.py` 用一份**逐条 try 的增列清单**：

```python
_MIGRATION_SQL = [
    "ALTER TABLE chat_message ADD COLUMN flag_root_cause INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chat_message ADD COLUMN flag_no_citation INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chat_message ADD COLUMN flag_stale_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chat_message ADD COLUMN reviewed_by TEXT",
    "ALTER TABLE chat_message ADD COLUMN reviewed_at TEXT",
    "ALTER TABLE chat_message ADD COLUMN reviewed_note TEXT",
    "ALTER TABLE chat_session ADD COLUMN batch_no TEXT",
]
```

`init_schema()` 先 `executescript(SCHEMA_SQL)`（全部 `IF NOT EXISTS`），再逐条执行增列；列已存在时抛 `OperationalError`，捕获后忽略。于是**同一个函数既能建新库，也能升级阶段 2 留下的旧库**，且可以无限次重复调用。

三条纪律：

- 新字段一律带 `DEFAULT`，保证旧行可读（`flag_*` 默认 0，即"未命中"，语义正确）；
- 不改列类型、不改列名——SQLite 的列改名要靠重建表，课程项目阶段不值得引入迁移框架；
- 需要"废弃"的列不删，只停止写入并在文档标注（如 `defect_item.status` 预留 `judged`，阶段 4 才启用）。
