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

---

## 6. 常用查询手册

下面 6 条查询覆盖了系统全部高频读路径。每条都对应一个 `repo` 函数，SQL 不是随手写的，注释里写清了它防的那个坑。

### 6.1 按业务时刻取"当时有效"的切片（版本闸核心）

```python
repo.list_chunks_as_of(conn, as_of="2026-06-01")
```

```sql
SELECT c.*, d.doc_name, d.doc_type, d.version, d.effective_from,
       d.effective_to, d.doc_id AS _doc_id
  FROM kb_chunk c JOIN kb_doc d ON c.doc_id = d.doc_id
 WHERE d.status = 'active'
   AND (d.effective_from IS NULL OR d.effective_from = '' OR d.effective_from <= '2026-06-01')
   AND (d.effective_to   IS NULL OR d.effective_to   = '' OR d.effective_to   >= '2026-06-01')
 ORDER BY d.doc_name, d.version, c.seq_no;
```

三个细节：

- `as_of` 只取前 10 位（`str(as_of)[:10]`），因为 `effective_from/to` 是日期串；传 ISO 时刻也能工作，不用调用方先格式化；
- `IS NULL OR = ''` 的兜底不是废话：阶段 1 的旧数据没有生效区间，升级后如果严格要求 `effective_from <= ?`，这些切片会突然从检索里消失，表现为"昨天还能答，今天库里什么都检索不到"；
- 返回结构与 `data/phase1_index/chunks.json` **同构**（`source_file` / `heading` / `doc_meta`），因此检索层拿到结果可直接建 BM25，无需为 DB 路径写第二套索引代码——这是"降级不降能力"的实现方式。

### 6.2 检索时强制按产线过滤（数据级权限）

```python
repo.list_defects(conn, line_id="LINE-A", limit=50)   # 班组长
repo.list_defects(conn, limit=50)                      # 质量工程师 / 主管
```

过滤发生在 SQL 的 `WHERE line_id = ?`，不是取回后在 Python 里 `if` 掉。区别在于：前者"不存在"，后者"存在但被跳过"——后端只要有一处忘了跳，就越权了。接口层 `require_roles("shift_leader")` 与这里形成两道独立防线，任一道单独失效都不至于泄漏。

### 6.3 离线补传的幂等查询

```python
row = repo.get_defect_by_draft_id(conn, client_draft_id)
if row:                      # 已经落过库，直接返回原单
    return repo.get_defect(conn, row["defect_no"])
```

靠 `client_draft_id UNIQUE` 兜底：即使两个请求并发穿过上面的 `if`，第二条 `INSERT` 会被唯一约束打回，服务层捕获后同样返回原单。**先查后插 + 唯一约束**是幂等的最小完备实现，缺一不可。

### 6.4 对话审计列表（多条件筛选）

```python
repo.list_audit_messages(conn, role="quality_engineer", batch_no="2026-11",
                         date_from="2026-11-01", date_to="2026-11-30",
                         only_flagged=True)
```

```sql
SELECT m.*, s.user_id, s.role AS session_role, s.batch_no, s.title AS session_title
  FROM chat_message m JOIN chat_session s ON m.session_id = s.session_id
 WHERE m.role = 'assistant'
   AND (m.flag_root_cause = 1 OR m.flag_no_citation = 1 OR m.flag_stale_version = 1)
 ORDER BY m.occurred_at DESC, m.rowid DESC LIMIT 200;
```

- 只查 `role='assistant'`：审计对象是**回答**，用户提问不需要核查；
- `date_to` 自动补 `T23:59:59`，否则"筛选到 11 月 30 日"会漏掉当天所有记录（字符串比较下 `"2026-11-30T09:00" > "2026-11-30"`）；
- `ORDER BY ... , m.rowid DESC`：时间戳是秒级，同一秒内多条消息靠 `rowid` 定序，否则刷新两次顺序会变（`prompt_template_history` 有同样处理）。

### 6.5 已下架版本集合（引用失效检测的判定源）

```python
archived = repo.archived_doc_versions(conn)     # {("检验规范", "v1"), ...}
```

```sql
SELECT doc_name, version FROM kb_doc WHERE status = 'archived';
```

判定源必须是 `kb_doc.status`，和检索侧**同一个字段**。如果换成"切片上的某个标记"，就会出现检索已经排除、审计却认为有效（或反之）的分裂，审计结论就失去意义了。

### 6.6 会话回放与最近 6 轮上下文

```python
repo.list_messages(conn, session_id)                 # 全量回放（前端恢复会话）
repo.list_recent_messages(conn, session_id, limit=12) # 6 轮 = 12 条（拼提示词）
```

`build_history()` 必须在**写入本轮用户消息之前**取快照，否则当前问题会既出现在 `history` 里、又出现在 `question` 里，模型会看到同一个问题两遍；而且刚插入的用户消息后面没有 assistant 回复，还会在消息序列里留下一个空的 assistant 位。这个 bug 踩过一次，修法是把快照提前，不是什么精妙逻辑。

---

## 7. 初始化与种子数据

```bash
python -m quality_trace.core.repo.db        # 不推荐：只建表，没有数据
python scripts/seed_demo_data.py            # 推荐：建表 + 用户 + 工单 + 缺陷码
python scripts/seed_demo_data.py --kb       # 另外写入知识库切片
python scripts/seed_demo_data.py --audit    # 另外造 3 条越界问答 + 2 张归因单
```

脚本是**幂等**的：用户按 `username` 存在即跳过，工单按 `work_order_id`、缺陷码按 `code` 同理。反复执行不会产生重复行，也不会覆盖你已经改过的密码。

演示账号（密码 = 用户名）：

| 用户名 | 角色 | 能进哪些页面 | 数据范围 |
|---|---|---|---|
| `qe001` | 质量工程师 | `/`、`/admin/*` 只读不了（40302） | 全产线 |
| `sl001` | 班组长 | `/m` | 仅 `LINE-A` |
| `qm001` | 质量主管 | `/admin/*` | 全产线 + 审批权 |

自检建议（答辩现场最容易被问的三条）：

```sql
-- 1. 是否有同名同版本重复文档（应为 0）
SELECT doc_name, version, COUNT(*) FROM kb_doc GROUP BY 1,2 HAVING COUNT(*) > 1;

-- 2. 是否有确认后仍无缺陷码的单（应为 0）
SELECT defect_no FROM defect_item WHERE status='pending' AND (defect_code IS NULL OR defect_code='');

-- 3. 是否存在引用了已下架版本却未被标记的问答（应为 0）
SELECT m.message_id FROM chat_message m
  JOIN chat_message_citation c ON c.message_id = m.message_id
  JOIN kb_doc d ON d.doc_name = c.source_file AND d.version = c.version
 WHERE d.status = 'archived' AND m.flag_stale_version = 0;
```
