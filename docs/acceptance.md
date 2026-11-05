# 验收对照

> 对照 `docs/requirements.md` 第五节「验收标准」的 8 条，逐条给出**当前状态 + 可核对的证据**。
> 状态只有三种：✅ 已达成 / 🟡 部分达成（说明差在哪）/ ⬜ 未实现（说明留到哪个阶段）。
> 这份文档的作用不是"证明都做完了"，而是让评审能自己在 2 分钟内抽查任意一条。

## 总表

| # | 验收标准 | 状态 | 证据在哪 |
|---|---|---|---|
| 1 | 反向溯源：给成品批次，列出原料批次、工序、设备、当时标准版本 | 🟡 | `GET /defects/{no}` + `attribution_order.evidence_json`；谱系表留阶段 4 |
| 2 | 正向围堵：给原料批次，列出受影响成品清单 | ⬜ | 依赖批次谱系（阶段 4），当前只有工单级 |
| 3 | as_of 正确：同一问题在 6 月与 9 月问，返回不同标准版本 | ✅ | `tests/test_version_gate.py`（9 条）+ `POST /chat` 的 `as_of` + 前端日期条 |
| 4 | 规则命中：注入参数越界，系统命中并给出证据行 | 🟡 | 审计三条规则已实现（`tests/test_audit.py` 31 条）；SPC 参数越界规则未做 |
| 5 | 归因排序：注入「3 号切刀 14:00 后磨损」，该原因进 top-3 | 🟡 | 归因单 + 证据链 + 审批 + 8D 已通；自动排序引擎未做（当前靠人工/脚本写入） |
| 6 | 边界：任何输出不得出现「根因已确定」，只能「疑似/待验证」 | ✅ | `attribution.DRAFT_BANNER`、`hypothesis_text` 恒带「待验证」、SL 文案无判定语汇 + 断言 |
| 7 | 测试：`core/` 行覆盖率 ≥ 70% | ✅ | 实测 **88%**（见 `docs/test-plan.md` §8） |
| 8 | 一键跑：`python examples/demo.py` 完成一次完整闭环 | 🟡 | 骨架 CLI 可跑但只打印 `ready`；真实闭环见 `scripts/demo_closed_loop.py` |

一句话总结：**8 条里 3 条完全达成（3 / 6 / 7），4 条部分达成（1 / 4 / 5 / 8），1 条未实现（2）。**
未实现的两类都不是"没时间做"，而是**依赖阶段 4 的批次谱系与 SPC 数据模型**——先做会把表结构钉死在错误粒度上。

---

## 逐条说明

### #1 反向溯源（🟡 部分达成）

需求原文：给成品批次，列出所用原料批次、工序、设备、当时标准版本。

**已做到**：以单件不良为起点，可以沿 `defect_item → work_order → attribution_order → chat_message_citation` 走通"这批货用了什么标准、依据哪条、谁判定、谁审批"这一段，且每个引用都带 `(doc_name, version)`：

```bash
curl -s "http://127.0.0.1:8000/api/v1/defects/DF-20261105-0001" -H "Authorization: Bearer $QE_TOKEN" | jq
```

**差在哪**：`work_order` 目前只有 `product_name / line_id / planned_qty` 三个业务字段，没有 `material_lot`（原料批次）与 `process_record`（工序参数记录）两张表，因此"3 跳内返回原料批次与设备"这一步走不到底。

**为什么留到阶段 4**：谱系的粒度决定后续所有查询的形状（批次级 or 单件级、设备绑定到工序还是绑定到班次）。这个粒度必须在有真实产线数据之后才能定，现在拍板会把表结构锁死。

### #2 正向围堵（⬜ 未实现）

需求原文：给原料批次，列出受影响成品清单，数量与生成器记录一致。

未实现的理由与 #1 相同——**没有原料批次表，就没有围堵的起点**。当前能做的替代是"按工单号围堵"：同一工单下所有不良单可一次列出（`GET /defects` 按 `work_order_id` 过滤），这在小批量场景下等价，但需求要的是批次级。

阶段 4 的落地路径：`material_lot(lot_no, supplier, received_at)` + `lot_to_batch(batch_no, lot_no, qty)` 两张表 + 一条递归 CTE 查询，`trace.max_depth`（当前 5）就是给这条查询预留的配置项。

### #3 as_of 正确（✅ 已达成）

同一问题在不同业务时刻，必须召回**当时有效**的标准版本。这条是本项目两个"陷阱"之一，机制而非提示词：

| 环节 | 实现 |
|---|---|
| 入口 | `POST /api/v1/chat` 的 `as_of` 字段（`YYYY-MM-DD` 或 ISO 时刻），非法格式 → 40001 且提示格式 |
| 检索 | `repo.list_chunks_as_of()` 在 SQL 层过滤 `status='active' AND effective_from <= as_of <= effective_to` |
| 索引外路径 | 走 DB 时 `meta.version_gate = "sql"`，走索引文件时 `"index"`，前端状态条显示当前视角 |
| 前端 | 问答页顶部日期条，清空即回到"当前有效" |

证据：`tests/test_version_gate.py` 9 条，覆盖"v1/v2/v3 三版共存时只召回当时那版"、"as_of 落在所有区间之外 → 0 命中"、"非法 as_of → 40001"。

### #4 规则命中（🟡 部分达成）

需求原文：注入一处参数越界，系统命中并给出证据行。

**已做到**：问答审计的三条规则（`core/services/audit.py`）——`flag_root_cause`（5 组越界表述正则）、`flag_no_citation`（称"完成/已确定"但零引用）、`flag_stale_version`（引用已下架版本）。命中后主管后台可按"仅看命中"筛选并逐条核查，`tests/test_audit.py` 31 条覆盖。

**差在哪**：需求里的"参数越界"指 SPC 意义上的工艺参数（如炉温超出控制限），需要 `process_record` 表与 Nelson 八规则判异，属于阶段 4。

### #5 归因排序（🟡 部分达成）

**已做到**：归因单的完整生命周期——证据链落库（`evidence_json`）、状态机（`submitted → approved/rejected`，退回必填理由）、8D Markdown 导出（带 `DRAFT` 红条 + 缓存复用）。`tests/test_attribution.py` 11 条。

**差在哪**：L1 规则命中 → L2 统计相关 → L3 假设排序这条**自动排序管线**没有实现。当前归因单的证据由人工构造（或脚本写入），因此"3 号切刀磨损能否进 top-3"无法用系统自动验证——这是需求 #5 的核心，也是阶段 4 归因引擎的主任务。

### #6 边界措辞（✅ 已达成）

任何输出不得出现"根因已确定"，只能是"疑似 / 待验证"。这是**防止 LLM 把检索内容当事实**的落地点，四处强制：

1. `attribution.DRAFT_BANNER`：8D 导出页首行固定插入红字横幅，声明"本文件为草稿，结论待验证"；
2. `attribution_order.hypothesis_text`：写入时即拼上"（待验证）"，导出时不再依赖模型自觉；
3. 班组长侧 `STATUS_TEXT`：只有"待确认缺陷码 / 已提交，待质量工程师判定"，**不出现合格、不合格、判退**等判定词；
4. 单测断言：`assert "根因已确定" not in content`、`assert "合格" not in status_text`。

### #7 覆盖率（✅ 已达成）

`core/` 行覆盖率实测 **88%**（要求 ≥ 70%），整体 `src/` **80%**。命令与逐模块明细见 `docs/test-plan.md` §8，含"为什么 `llm/client.py` 只到 47% 而不去补"的取舍说明。

### #8 一键跑闭环（🟡 部分达成）

**现状**：`python examples/demo.py` 是阶段 0 的骨架，跑通但只打印 `ready`（追溯/归因数据源未接入，主动 skip）。

**真正的闭环**由两条命令覆盖：`bash scripts/run_server.sh` 起服务，再跑两个冒烟脚本（14 + 18 项断言），完整走过"登录 → 问答 → 审计 → 审批 → 录入"。

**补齐计划**：`scripts/demo_closed_loop.py` —— 用 FastAPI TestClient 在**临时库**上跑一遍完整链路并逐步打印，一条命令、无需起服务、不污染演示数据。做完后本条改为 ✅。
