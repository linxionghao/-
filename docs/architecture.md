# 技术架构设计 v0.1

> 依据：`docs/requirements.md`（最终需求基线）。本文只给方案，不含实现代码。
> 定位：制造业质量追溯 + 缺陷归因 + 知识库增强（RAG），课程项目、单机可演示、LLM 走 API 可替换。

## 0. 选型变更声明（对 requirements.md 的修订）

需求清单第四节「技术选型」原为零第三方依赖（argparse + http.server）。本架构按新硬性约束**修订**为 FastAPI + LLM API + 混合检索，其余需求（双时间语义、as_of 硬闸、归因四层、三角色 RBAC、8D 草稿边界）**全部保留并强化**。

付出的代价与对策：

| 变更 | 代价 | 对策 |
|---|---|---|
| http.server → FastAPI(异步) | 增加依赖 | requirements 锁版本；答辩机备离线 wheel 目录 |
| 引入 LLM API | 外部服务不可控 | 适配层 + 模板降级，核心追溯/围堵查询零 LLM 依赖 |
| 引入向量检索 | 需要 embedding 服务 | SQLite BLOB 存向量（<10万切片不需向量库），无服务时 BM25 降级 |
| 引入 RAG | 「拿文档当事实」风险 | 本方案第 4.3 节机制正面解决 |

---

## 1. 五层架构（文字版）

```
┌─────────────────────────────────────────────────────────────┐
│ L1 入口层  api/            FastAPI Routers · 鉴权/RBAC · 校验    │
│ L2 服务层  core/services   用例编排 · 事务 · as_of 解析          │
│ L3 编排层  core/attribution core/knowledge core/llm            │
│           归因流水线 · 证据装配 · 引用校验 · 提示装配 · LLM 适配   │
│ L4 检索层  knowledge/retrieval  切分 · 混合召回 · 版本硬过滤 · 降级 │
│ L5 数据层  core/repo + models   SQLite 8+3 表 · 双时间列 · 迁移   │
└─────────────────────────────────────────────────────────────┘
```

### 1.1 各层职责

| 层 | 模块 | 职责 | 明确不做什么 |
|---|---|---|---|
| **L1 入口层** | `api/routers/`、`api/schemas/`、`api/deps.py` | HTTP 路由、JWT/API Key 鉴权、三角色 RBAC、Pydantic 请求/响应模型、统一错误码、审计埋点（谁、何时、调了什么） | 不含业务逻辑；不直接拼 SQL |
| **L2 服务层** | `TraceService`、`DefectIntakeService`、`AttributionService`、`KnowledgeIngestService`、`ExportService`(8D)、`AuditQueryService` | 用例编排、事务边界、数据侧权限过滤、**as_of 解析**（把请求上下文翻译成业务时刻）、调用编排层并落库 | 不做检索细节、不碰 LLM 提示词 |
| **L3 编排层** | 归因流水线（L1规则→L2统计→L3假设）、证据装配器、**引用校验器**、提示装配、LLM 适配器 | 决定「查什么、喂什么给 LLM、校验什么」；唯一的「知识 + 数据」汇合点 | 不直接写业务表（写入委托服务层仓库） |
| **L4 检索层** | IngestPipeline（切分/元数据）、Retriever（双通道召回 + 融合排序 + 版本过滤）、EmbeddingClient | 纯检索：给定查询与过滤条件，返回带完整元数据的 chunk 列表 | 不调 LLM；不判断「哪版标准有效」（有效性由 L5 的 spec_version 表决定） |
| **L5 数据层** | repositories、entities、migrations | SQLite 表、双时间列约束、FTS5 虚拟表、append-only 审计 | 不含业务规则 |

### 1.2 跨层调用规则（硬性）

| # | 规则 |
|---|---|
| R1 | 入口层**只能**调服务层；禁止触达编排/检索/数据层 |
| R2 | 服务层之间不互调；共享逻辑上提到编排层或下沉到数据层 |
| R3 | 编排层可调检索层与 LLM 适配层；**写操作一律回服务层仓库**，编排层无落库副作用 |
| R4 | 检索层不调 LLM，LLM 适配层不调检索层——两者的装配只发生在编排层（保证可测试、可降级） |
| R5 | 一切读操作必须携带 as_of 语义；缺省 = 请求时刻的业务时间（bitemporal 查询走 `occurred_at`，永不走 `recorded_at`） |
| R6 | 跨层依赖一律走接口（`typing.Protocol`），测试逐层可 mock |

---

## 2. 目录结构（与现有骨架兼容）

现有骨架 `src/quality_trace/{core,utils}`、`main.py`、`utils/config.py`、`core/pipeline.py` 全部保留，向下兼容增量扩展：

```
quality-trace/
├── docs/                      # requirements / design / api / architecture(本文)
├── config/
│   ├── settings.yaml          # 全局：数据库路径、LLM provider、检索开关
│   ├── rules.yaml             # Nelson 规则与阈值（沿用现有 YAML 机制）
│   ├── prompts/               # 提示词模板（按用途分文件，版本号管理）
│   └── llm.yaml               # provider 注册：base_url / api_key_env / model
├── data/                      # 运行时生成，不入库
│   ├── trace.db               # SQLite（结构化 + KB 三表）
│   ├── raw_docs/              # 三类语料原件（md/docx/pdf）
│   └── exports/               # 8D 草稿输出
├── examples/
│   └── demo_week1.py          # 第一周验收冒烟脚本（沿用 examples 惯例）
├── scripts/                   # 初始化库、灌种子数据、备份（ps1/sh 成对）
├── src/quality_trace/
│   ├── main.py                # FastAPI app 工厂 + CLI 入口（argparse 保留）
│   ├── api/                   # 【新增】L1
│   │   ├── routers/           # trace / defect / attribution / knowledge / export / audit
│   │   ├── schemas/           # Pydantic 请求/响应模型（镜像 routers）
│   │   └── deps.py            # 鉴权、角色、as_of 提取
│   ├── core/
│   │   ├── services/          # 【新增】L2 六个服务类
│   │   ├── attribution/       # 【重构自 pipeline.py】L3：rules(L1判异) / stats(L2集中度) / orchestrator
│   │   ├── knowledge/         # 【新增】L3+L4：ingest / retrieval / citation(引用校验)
│   │   ├── llm/               # 【新增】L3 适配层：client / providers / fallback / validator
│   │   ├── export/            # 8D 草稿渲染（Markdown）
│   │   ├── models/            # entities：dataclass，双时间列
│   │   └── repo/              # L5 repositories + migrations + FTS
│   └── utils/                 # config.py(已有) / clock.py(bitemporal工具) / ids.py / ids
├── tests/                     # 镜像 src：tests/quality_trace/{api,core,utils}
├── requirements.txt / requirements-dev.txt
└── pyproject.toml
```

约定：`core/pipeline.py` 不删，重构为 `attribution/orchestrator.py` 的薄委托，保持向后兼容。

---

## 3. 数据库表设计（SQLite，8 结构化 + 3 知识库）

通用约定：每表自带 `id`(PK)、`occurred_at`(业务时间)、`recorded_at`(录入时间)；审计表 append-only。

### 3.1 结构化 8 表

| 表 | 字段（关键） | 说明 |
|---|---|---|
| `material_lot` 物料批次 | lot_id PK · supplier_id · material_code · received_at · qty · serial(预留,可空) · status | 追溯起点；serial 只预留不承诺 |
| `work_order` 工单 | wo_id PK · product_code · line_id · planned_qty · actual_qty · started_at · ended_at · status | 谱系骨架 |
| `operation_event` 工序事件 | event_id PK · wo_id FK · op_code · equipment_id · operator_id · started_at · ended_at · params_json · recipe_version | 解冻/切粒/检品/包装；recipe_version 指向工艺版本 |
| `inspection` 检验记录 | insp_id PK · wo_id FK · op_code · inspected_at · sample_qty · defect_qty · inspector_id · **spec_id + spec_version**（录入时 as_of 解析后固化） | 双保险：查询时仍可再 as_of 复核 |
| `defect` 缺陷条目 | defect_id PK · insp_id FK · defect_code · severity · qty · image_path · reported_by(班组长) · occurred_at · status(待归因/归因中/已关闭) | 一张不良品单 = 一条 defect |
| `spec_version` 标准版本 | spec_id · version · op_code · param_name · lower · upper · unit · **effective_from · effective_to** · status(draft/active/superseded/void) · source_doc_id FK→kb_document | **全系统唯一权威源**；同 spec_id 多版本构成版本谱 |
| `attribution_case` 归因单 | case_id PK · defect_id FK · evidence_json · hypotheses_json · status(draft/confirmed/rejected) · confirmed_by · confirmed_at · llm_meta_json(model·prompt_hash·request_id·retrieval_ids) | confirmed_by 非空才算结论（L4） |
| `audit_log` 审计日志 | log_id PK · actor · role · action · object_type · object_id · occurred_at · recorded_at · detail_json | **append-only**，禁 UPDATE/DELETE（触发器兜底） |

### 3.2 知识库 3 表

| 表 | 字段（关键） | 说明 |
|---|---|---|
| `kb_document` 文档 | doc_id PK · doc_type(standard/sop_manual/8d_case) · title · source_path · checksum · version_tag · effective_from/to(仅 standard 类) · status · ingested_at · owner | 一份原件一行；重灌以 checksum 幂等 |
| `kb_chunk` 切片 | chunk_id PK · doc_id FK · seq_no · content · char_span(起止) · embedding(BLOB 可空) · embedding_model · fts_text | 元数据不散存：spec_id、version、effective_from/to、status、equipment_id、defect_code、op_code 全部**提升为独立列**，供 SQL 硬过滤 |
| `retrieval_log` 检索日志 | log_id PK · query_id · case_id · query_text · mode(hybrid/bm25) · recalled_json(chunk_id·score·filters) · final_context_ids · degraded(bool) · latency_ms · occurred_at | 每次检索可完整重放；归因审计的组成部分 |

附：`kb_chunk_fts` 为 SQLite FTS5 虚拟表（外置 content 表，中文按 bigram 或 jieba 分词建立），向量存 BLOB、量小直接 cosine，**不引入向量数据库**，预留 pgvector/Qdrant 接口位。

---

## 4. 知识库与检索方案

### 4.1 三类语料与切分策略

| 语料 | 切分策略 | 关键元数据 |
|---|---|---|
| ① 质量制度与检验标准 | **按条款结构切**：标题层级 + 条款号为边界，一条款一 chunk；表格按「表头 + 行」组包，不跨表 | spec_id、version、effective_from/to、status、op_code |
| ② 工艺文件/SOP/设备手册 | 章节层级 + 语义段落，300–500 字、重叠 ~15%；SOP 按工序步骤切 | op_code、equipment_id、recipe_version |
| ③ 历史 8D 案例 | **按 8D 步骤结构切**：D2 现象 / D4 根因 / D5 纠正措施各自成 chunk，保上下文头（案例号+现象摘要） | defect_code、root_cause_confirmed(bool)、closure_effective |

原则：能进结构化元数据的绝不指望 embedding 相似度去「猜」。

### 4.2 混合检索与融合排序

```
查询 → 双通道召回
   ├─ BM25（FTS5，中文分词）
   └─ 向量（embedding，cosine）
→ 元数据硬过滤（版本有效性，见 4.3）
→ RRF 融合（k=60）+ 规则加分（defect_code/op_code/equipment 精确命中 +0.15）
→ top_k：召回 50 → 融合过滤后 8~12 条进上下文
```

### 4.3 版本与失效处理 —— 陷阱一的正面试答

问题：库里同时存在 v1/v2/v3 三版检验标准，检索只按相似度召回，怎么保证不把已废止版本当现行标准？

**答案：三道闸，机制不依赖提示词。**

1. **权威源分离（第一道闸）**：`spec_version` 结构化表是「哪版有效」的唯一权威；知识库 chunk 只是标准正文的检索形态。检索层**根本无权判断**版本有效性——它只执行过滤。
2. **查询期硬过滤（第二道闸）**：编排层先按缺陷的 `occurred_at`（as_of）从 `spec_version` 解析出该时刻应生效的 `(spec_id, version)` 集合，作为**必过条件**下推检索层，SQL 级执行 `effective_from <= as_of < effective_to AND status = 'active'`。相似度再高的废止条款也进不了上下文。历史对比模式（"为什么 6 月判合格 9 月判不合格"）下才允许召回旧版本，且响应强制携带「该版本已于 X 日被 vX 替代」标注。
3. **答案侧校验（第三道闸）**：引用校验器逐条检查 LLM 输出所引 chunk 的版本元数据，与本次 as_of 不符的引用**剔除该证据**并降级标注「无知识库佐证」，同时写入 `retrieval_log`。即使前两道被绕过（如手写 prompt 直调），第三道在落库前拦截。

配套：ingest 时登记版本谱——同 `spec_id` 新版本入库，事务内将旧版 `effective_to` 封口、`status → superseded`，人工可确认撤销。LLM 被破坏也无法改变数据库里的生效区间。

### 4.4 引用与溯源 —— 陷阱二的正面试答

问题：LLM 如何避免把检索到的文档内容当成事实结论？

**答案：三色分离 + 服务端强制校验，提示词只是辅助。**

**（a）三色证据模型**——所有归因输出的陈述必须归入且仅归入一类：

| 色 | 来源 | 标记 | 可否单独作为结论 |
|---|---|---|---|
| 事实(Fact) | 结构化表查询结果 | 带「表名 + 行 id + 字段值」 | 可（如"3号切刀 14:00 后参数越界"） |
| 参考(Reference) | 知识库 chunk | 带 chunk_id + 文档版本 + 生效区间 | 不可，只作佐证 |
| 假设(Hypothesis) | LLM 生成 | 恒带「待验证」标记 | 永不可（需求红线：不出现「根因已确定」） |

**（b）服务端引用校验（不信 LLM）**：
- 每个 chunk 进 prompt 时带编号 `[C1]…[Cn]`；输出契约强制每条 claim 携带 `citations: [chunk_id]`；
- 校验器执行：① 引用的 chunk_id **必须 ∈ 本请求上下文集合**（防编造引用）；② 引用 chunk 的版本元数据必须通过 4.3 第三道闸；③ 无引用的结论性陈述 → 剥离或强制改标「模型推测，无依据」；
- 校验失败 → 一次修复性重试 → 仍失败则丢弃 L3 层，只出 L1/L2 结果并标注「LLM 输出未通过校验」。

**（c）全链路留痕可重放**：`evidence_json` 每条证据形如 `{type: data_row|kb_chunk, ref, excerpt, version}`；`retrieval_log` 记录本次喂了哪些 chunk、分数、过滤条件；`attribution_case.llm_meta_json` 记录 model、prompt_hash、request_id。三个月后追问「当时为什么这么判断」可完整复现。

### 4.5 无 embedding 服务时的降级

| 项 | 方案 |
|---|---|
| 检测 | `EmbeddingClient` 启动/调用时 health_check；失败 → Retriever 切 **BM25-only**，响应 `meta.degraded = true` |
| 补偿 | BM25-only 下召回扩到 100，靠元数据过滤 + 规则加分排序；中文用 bigram 保证召回下限 |
| 代价声明 | 同义改写召回率下降——如实写进 retrieval_log 与演示口径，不隐瞒 |
| 恢复 | 服务恢复后新查询自动回 hybrid；存量 BLOB 向量按需补算，不阻塞在线查询 |

---

## 5. LLM 适配层

### 5.1 接口定义（唯一入口，供应商可替换）

```python
class LLMRequest:   # dataclass
    messages: list[Message]
    response_schema: type[BaseModel] | None   # 注册的输出契约，如 AttributionDraft
    temperature: float = 0.2
    max_tokens: int = 2000
    timeout_s: int = 10
    purpose: str                              # "attribution" | "kb_qa" | "rewrite"
    meta: dict                                 # 透传 case_id / query_id，便于审计关联

class LLMResponse:
    raw: str
    parsed: BaseModel | None                   # response_schema 校验通过后才有值
    provider: str; model: str; request_id: str
    usage: TokenUsage
    degraded: bool = False                     # 走了备用/模板时置位

class LLMClient(Protocol):
    async def generate(self, req: LLMRequest) -> LLMResponse: ...
```

Provider 在 `config/llm.yaml` 注册（`base_url` / `api_key_env` / `model`），换供应商只改配置不改代码。

### 5.2 输入输出契约

- 归因场景唯一出口契约 `AttributionDraft`：`hypotheses[]`（每条含 text、evidence_refs、caveats）、`facts[]`、`references[]`、`no_root_cause_claim: true`（schema 级常量，LLM 无字段可违背）；
- **禁止自由文本结论**：渲染 8D 草稿只消费 parsed 结构，不消费 raw。

### 5.3 失败降级链

```
主 provider（10s 超时，1 次重试）
  → 备用 provider（配置链）
  → 熔断：连续 3 次失败暂停 60s，直接走底
  → 底：本地规则模板 —— 不用 LLM，直接输出 L1 规则命中 + L2 统计排序，
        标注「LLM 不可用，本次无假设层」
```

硬边界：**追溯、围堵、as_of 查询、审批流永不依赖 LLM**——LLM 全挂时系统退化为「纯规则归因工具」，核心验收标准 1–3 照常通过。

### 5.4 输出校验（服务端三步）

1. **结构校验**：pydantic 按注册 schema 校验，非法 JSON / 缺字段 → 修复性重试 1 次；
2. **引用校验**：4.4(b) 全套；
3. **红线校验**：断言输出不含「根因已确定」类表述（对齐需求验收标准 6），命中即整体拒收降级。

每次调用记录 prompt_hash + request_id 进 `llm_meta_json` / `audit_log`。

---

## 6. 第一周最小验证目标

原则：**先证伪两个陷阱，再铺功能。**

| 天 | 交付 | 验收动作 |
|---|---|---|
| D1–2 | 8+3 表迁移、双时间列、`spec_version` as_of 查询单测 | 同一参数在 6 月/9 月两个 as_of 下命中不同版本（验收标准 3 前置） |
| D2–3 | 构造 v1/v2/v3 三版检验标准入库（一版已废止）+ 版本封口事务 | **陷阱一验证**：as_of 查询 0 次召回废止版；历史模式可召回但带废止标注 |
| D3–4 | 混合检索 demo + BM25-only 降级开关 + 引用校验器 | **陷阱二验证**：手工构造「LLM 引用不存在的 chunk」，校验器剔除；断 embedding 开关，检索仍可用且 `degraded=true` |
| D4–5 | LLM 适配层 + mock provider + 1 个真实 provider；归因单 e2e | 缺陷单 → L1/L2 → L3 假设 → 校验 → 草稿落库；断 LLM → 退化为纯 L1/L2 输出 |
| D5 | `examples/demo_week1.py` 一键冒烟 | 全链路一次调用 < 30s；retrieval_log 可重放 |

**周末打勾清单（全过才算第一周达标）：**

- [ ] as_of=6月 vs as_of=9月，召回不同版本标准，废止版零泄漏
- [ ] 伪造引用被服务端剔除，非提示词功劳（单测可证）
- [ ] 无 embedding → BM25 降级可用且如实标记
- [ ] 无 LLM → 归因仍输出 L1/L2，追溯查询完全不受影响
- [ ] 「根因已确定」字样全链路不存在（schema 常量 + 单测断言）
- [ ] 每次归因可凭 retrieval_log + llm_meta_json 完整重放

---

## 7. 遗留风险

| 风险 | 应对 |
|---|---|
| 中文 BM25 分词质量不稳 | 首选 bigram 保底；jieba 作为增强项可开关 |
| SQLite 并发写瓶颈 | 单机课程场景够用；WAL 模式 + 写操作串行化，预留 PG 迁移路径 |
| LLM 输出不稳定导致归因质量波动 | temperature 锁 0.2、schema 强约束、熔断降级；质量指标只统计通过校验的输出 |
| 三类语料原件不足 | 第 8 周前最低配置：2 份标准（≥2 版本）+ 3 份 SOP + 5 个历史 8D 案例，demo 用生成器补量 |
