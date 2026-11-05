# 0003. 版本失效用"三道闸"，不只靠提示词

- 状态：已采纳
- 日期：2026-10-28
- 影响范围：`core/repo/db.py`（`list_chunks_as_of`）、`core/services/chat.py`、`core/services/knowledge_admin.py`、`core/services/audit.py`

## 背景

知识库里同时存在同一份检验标准的 v1 / v2 / v3。检索只按相似度召回，最相似的那条可能是**已废止**的版本。后果不是"回答不够好"，而是"按作废标准判定"，在质量体系里这是事故。

需求明确要求"给出机制，不要只说在提示词里强调"。提示词方案为什么不行：模型没有任何可靠手段知道哪条切片是现行版——`effective_to` 就在文本里，但它会不会遵守是概率问题；只要有一次不遵守，就是一次错误判定，而错误判定不可撤销（已经判退的货）。

## 决策

三道互相独立的闸，任一道生效都能阻止废止版进入回答：

### 闸一：索引重建时物理排除

文档下架（`kb_doc.status = 'archived'`）后，`knowledge_admin.rebuild_index()` 重建时**不把它的切片写进向量库与 BM25 语料**。已下架的文档在检索层面"不存在"。

验收方式：下架一份文档后立即提问，命中数从 N 变 0（真实服务上验证过）。

### 闸二：SQL 时间闸（唯一权威判定）

带 `as_of` 提问时，不用索引文件，直接从库里取"当时有效"的切片：

```sql
SELECT c.*, d.doc_name, d.doc_type, d.version, d.effective_from, d.effective_to
  FROM kb_chunk c JOIN kb_doc d ON c.doc_id = d.doc_id
 WHERE d.status = 'active'
   AND (d.effective_from IS NULL OR d.effective_from = '' OR d.effective_from <= ?)
   AND (d.effective_to   IS NULL OR d.effective_to   = '' OR d.effective_to   >= ?)
```

**版本信息只在 `kb_doc` 存一份**，所以"检索排除谁"与"审计认为谁有效"永远用同一个字段判断，不会分裂。这也是为什么切片表不冗余版本号（见 `docs/data-model.md` 第 3 节）。

### 闸三：审计复核（事后兜底）

回答落库后，`audit.detect_flags()` 把引用里的 `(doc_name, version)` 与 `repo.archived_doc_versions()` 比对，命中就打 `flag_stale_version = 1`。主管在后台按"仅看命中"逐条核查。

这一闸对**历史上已经产生的回答**有效——即使前两闸当时有 bug，事后也能把受影响的回答筛出来。

## 后果

好处：

- 不依赖模型自觉：三道闸都在代码里，可断言、可回归测试（`tests/test_version_gate.py` 9 条）；
- 时间视角可验证：`scripts/demo_closed_loop.py` 第 3 步断言"as_of=2024-06-01 时命中版本集合必须是 `['v1']`"，而不是"命中条数 > 0"；
- 边界明确：区间外时刻返回 0 命中，而不是悄悄给现行版——**宁可空，不可错**。

代价：

- 带 `as_of` 的查询走 DB 而非索引文件，无法用 FAISS，退化为 BM25 单通道（切片量 < 10 万时实际影响很小）；
- 需要在 UI 上解释"当前是历史视角"（问答页顶部日期条 + 状态条显示 `version_gate`）；
- 索引重建成为下架流程的必经步骤，少做一步就漏一闸。

未来若要改：把 `as_of` 的过滤下沉到检索层（给 FAISS 加 metadata filter）时，闸二仍在，只需保证过滤条件不变。

## 替代方案

| 方案 | 为什么不选 |
|---|---|
| 提示词里强调"只用现行版本" | 概率性遵守，且模型看不到 `status`；一次失效就是一次错误判定 |
| 只保留现行版本入索引（删掉旧版） | 无法回答"这批货当时按什么标准判的"——追溯场景反而废了 |
| 检索后按版本过滤（先召回再丢弃） | 可行但脆弱：丢弃后可能凑不满 top-k，且"为什么少了三条"难以解释；SQL 闸在源头过滤更彻底 |
| 给切片加 `is_current` 字段 | 冗余字段必然与 `kb_doc.status` 漂移，闸二和闸三会用不同字段判断，审计结论就失去意义 |
