# 阶段 3 · 三角色鉴权 + 主管后台 & 班组长录入端 · 设计方案

> 前置：复用阶段 2 的统一封装 `{code, message, data}`、错误码表（`core/services/errors.py`）、
> SQLite 数据层（`core/repo/db.py`）、检索链路（`core/knowledge/`）、LLM 客户端（`core/llm/`）。
> 状态：**已确认（2026-10-30）**。
>
> **决策记录**：A① 手写 HS256 JWT（零新依赖）｜ B① Jinja2 服务端渲染 ｜
> C① pypdf 抽取 PDF ｜ D① 最小集 10 张表（谱系全表留阶段 4）。
> 交付批次：1a 鉴权基座 → 1b 知识库+提示词 → 1c 审计+审批 → 2 班组长端。

---

## 1. 共用基础：JWT 鉴权

### 1.1 登录与令牌

| 项 | 设计 |
|---|---|
| 接口 | `POST /api/v1/auth/login`，body `{username, password}`；`GET /api/v1/auth/me` |
| 算法 | HS256（对称签名，密钥读 `config` + `QT_AUTH__SECRET` 环境变量） |
| payload | `{user_id, role, line_id, exp, iat}` —— **line_id 进令牌**，数据层过滤直接用它 |
| 有效期 | 12 小时（课程演示场景不做刷新令牌） |
| 传递 | `Authorization: Bearer <token>` |
| 密码存储 | SHA-256(密码 + 固定盐)，不做 bcrypt（课程演示定位，注释里说明生产应换） |
| 兼容 | 阶段 2 的 `X-User-Id/X-Role` 请求头降级保留，仅当 `auth.dev_header_bypass: true` 时生效（默认 false）；`/chat` 等既有接口同步改造为 JWT 优先 |
| 用户来源 | 新表 `sys_user`，附 `scripts/seed_demo_data.py` 灌入三个演示账号 |

### 1.2 权限执行的两级机制（不靠前端隐藏）

1. **路由级**：FastAPI 依赖注入 `require_roles("quality_manager")` —— 角色白名单校验，返回 40301；
2. **数据级**：所有查询在 **repo 层 SQL 拼条件**——班组长 `line_id` 固定来自令牌 payload，前端传参里的 line_id 一律忽略；越权访问他线数据返回 **40302**。

前端隐藏按钮只是体验优化，**后端永远重新校验**。

---

## 2. 权限矩阵（角色 × 接口）

角色缩写：SL=班组长 / QE=质量工程师 / QM=质量主管。✅=允许，❌=40301，◐=允许但数据范围受限。

| # | 接口 | SL | QE | QM | 数据范围（在 repo 层过滤） |
|---|---|---|---|---|---|
| **认证** | | | | | |
| 1 | POST /api/v1/auth/login | ✅ | ✅ | ✅ | — |
| 2 | GET /api/v1/auth/me | ✅ | ✅ | ✅ | 本人 |
| **问答（阶段 2 既有）** | | | | | |
| 3 | POST /api/v1/chat（SSE） | ❌ | ✅ | ❌ | 本人会话 |
| 4 | POST /api/v1/sessions | ❌ | ✅ | ❌ | 本人 |
| 5 | GET /api/v1/sessions/{id}/messages | ❌ | ✅ | ❌ | 本人会话（QM 走审计接口 #16） |
| 6 | DELETE /api/v1/sessions/{id} | ❌ | ✅ | ❌ | 本人 |
| 7 | GET /api/v1/health | ✅ | ✅ | ✅ | — |
| **主管后台 · 知识库管理** | | | | | |
| 8 | POST /api/v1/admin/documents（上传 md/pdf） | ❌ | ❌ | ✅ | 全部 |
| 9 | GET /api/v1/admin/documents（列表+筛选） | ❌ | ❌ | ✅ | 全部 |
| 10 | GET /api/v1/admin/documents/{id}/chunks（核对切片） | ❌ | ❌ | ✅ | 全部 |
| 11 | POST /api/v1/admin/documents/{id}/archive（下架） | ❌ | ❌ | ✅ | 全部 |
| 12 | POST /api/v1/admin/documents/{id}/restore（恢复） | ❌ | ❌ | ✅ | 全部 |
| **主管后台 · 提示词模板** | | | | | |
| 13 | GET /api/v1/admin/prompt-templates | ❌ | ❌ | ✅ | 全部 |
| 14 | PUT /api/v1/admin/prompt-templates/{id}（立即生效） | ❌ | ❌ | ✅ | 全部 |
| **主管后台 · 对话审计** | | | | | |
| 15 | GET /api/v1/admin/audit/messages（按角色/批次/时间/越界类型筛选） | ❌ | ❌ | ✅ | 全部（跨线跨角色） |
| 16 | GET /api/v1/admin/audit/messages/{id}（含引用与命中切片） | ❌ | ❌ | ✅ | 全部 |
| 17 | POST /api/v1/admin/audit/messages/{id}/review（标记已核查+备注） | ❌ | ❌ | ✅ | 全部 |
| 18 | POST /api/v1/admin/audit/rescan（补扫历史消息的越界标记） | ❌ | ❌ | ✅ | 全部 |
| **主管后台 · 归因审批** | | | | | |
| 19 | GET /api/v1/admin/attributions（列表） | ❌ | ◐ 只读 | ✅ | QE 看自己提交的 |
| 20 | GET /api/v1/admin/attributions/{id}（证据链详情） | ❌ | ◐ 只读 | ✅ | 同上 |
| 21 | POST /api/v1/admin/attributions/{id}/approve | ❌ | ❌ | ✅ | 全部 |
| 22 | POST /api/v1/admin/attributions/{id}/reject（退回必须填意见） | ❌ | ❌ | ✅ | 全部 |
| 23 | GET /api/v1/admin/attributions/{id}/export-8d（下载 Markdown） | ❌ | ❌ | ✅ | 全部 |
| **班组长 · 缺陷录入** | | | | | |
| 24 | POST /api/v1/defects（创建草稿+候选缺陷码） | ✅ | ❌ | ❌ | 工单必须属于本人产线（40406） |
| 25 | POST /api/v1/defects/{no}/confirm（确认缺陷码后正式落库） | ✅ | ❌ | ❌ | 本人创建且状态为 draft |
| 26 | GET /api/v1/defects（列表） | ◐ 本产线 | ✅ 全量 | ✅ 全量 | SL 强制 `line_id = 令牌line_id` |
| 27 | GET /api/v1/defects/{no}（详情） | ◐ 本产线+字段裁剪 | ✅ | ✅ | **SL 服务端裁掉归因/判定字段** |
| 28 | GET /api/v1/defect-codes（分组字典） | ✅ | ✅ | ✅ | 全量 |
| 29 | POST /api/v1/uploads/photo | ✅ | ❌ | ❌ | 本人 |

**两条硬约束的落点**：
- 「班组长看不到归因结论与标准判定」→ #26/#27 的 SELECT 列白名单按角色裁剪，归因单关联、判定结果列对 SL 的 SQL 根本不出现；
- 「提交即 pending，不自动判定」→ 缺陷状态机 `draft → pending`（确认动作只定**缺陷码分类**，不是合格性判定）；`judged` 状态属于阶段 4 质量工程师判定，本阶段任何人无入口写入。

---

## 3. 新增表（10 张）与错误码增补

### 3.1 表清单（沿用双时间列与幂等建表风格）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `sys_user` | user_id PK · username UNIQUE · password_hash · display_name · role · **line_id** · status · created_at | 三角色演示账号由 seed 脚本灌入 |
| `kb_doc` | doc_id PK · doc_name · doc_type · **version · effective_from · effective_to** · status(active/archived) · source_path · chunk_count · vector_status(pending/ok) · uploaded_by · occurred_at · recorded_at · archived_at · archived_by | 知识库权威注册表；**废止=archive 下架，永不物理删除** |
| `kb_chunk` | chunk_id PK · doc_id FK · seq_no · content · clause · char_len · occurred_at · recorded_at | 对齐架构 3.2；索引重建时从这里读 |
| `prompt_template` | template_id PK · name · content · status(active/retired) · updated_by · updated_at | 全库**仅一条 active**；修改即 UPDATE 并写历史 |
| `prompt_template_history` | history_id PK · template_id FK · content · updated_by · updated_at | 每次修改追加一行（修改人+时间可审计） |
| `defect_code` | code PK · name · category(分组) · keywords_json · severity_default · sort_no · status | 缺陷码字典；keywords_json 供 LLM 不可用时的关键词回退匹配 |
| `work_order` | work_order_id PK · product_name · **line_id** · status · planned_qty · occurred_at · recorded_at | 简化工单表（架构完整谱系表留阶段 4） |
| `defect_item` | defect_no PK · work_order_id FK · line_id · op_code · description · qty · severity · defect_code FK(可空=draft 阶段) · suggest_json(候选码+来源 llm/keyword) · photo_path · client_draft_id UNIQUE(幂等) · status(draft/pending) · **is_backfill** · occurred_at · recorded_at · created_by · confirmed_at | **双时间戳**；is_backfill=补录标记 |
| `attribution_order` | attribution_id PK · defect_no FK · evidence_json · hypothesis_text · status(submitted/approved/rejected) · **8d_draft_md** · submitted_by · submitted_at · decided_by · decided_at · decision_note | QE 提交（阶段 4 建生成入口），本阶段先做审批+导出 |
| `chat_message` 增列 | **flag_root_cause · flag_no_citation · flag_stale_version**（0/1）· reviewed_by · reviewed_at | 越界自动标记；`chat_session` 增列 **batch_no**（可选，问答时传，审计按批次筛选） |

### 3.2 新增错误码（沿用 5 位，挂进 `errors.py`）

| code | HTTP | 中文文案 | 场景 |
|---|---|---|---|
| 40102 | 401 | 登录名或密码错误 | login 校验失败 |
| 40103 | 401 | 登录已过期，请重新登录 | JWT exp 超时 |
| 40302 | 403 | 无权访问其他产线的数据 | 数据级越权 |
| 40403 | 404 | 文档不存在或已删除 | doc_id 无效 |
| 40404 | 404 | 缺陷单不存在 | defect_no 无效 |
| 40405 | 404 | 归因单不存在 | attribution_id 无效 |
| 40406 | 404 | 工单不存在或不属于当前产线 | SL 提交时校验 |
| 40903 | 409 | 该文档的此版本号已存在 | doc_name+version 冲突 |
| 40905 | 409 | 缺陷单当前状态不允许此操作 | 非 draft 单 confirm |
| 40906 | 409 | 当前仅允许退回未审批的归因单 | 重复审批 |
| 41301 | 413 | 文件超过大小限制（10MB） | 上传超限 |
| 41501 | 415 | 仅支持 Markdown 或 PDF 格式 | 格式校验 |
| 40003 | 400 | 图片解码失败或超过 2MB | 照片 base64 |
| 50004 | 200 | 向量服务不可用，文档已入库但仅支持关键词检索（**降级**） | 上传时 embedding 失败，doc.vector_status=pending |

---

## 4. 质量主管后台（服务端渲染，类 SaaS）

### 4.1 页面清单（`/admin/*`，Jinja2 模板 + 原生 JS 增强交互）

| 路径 | 页面 | 关键交互 |
|---|---|---|
| /admin/login | 登录页 | 表单提交 → JWT 存 Cookie(HttpOnly) → 302 到仪表盘 |
| /admin | 仪表盘 | 统计卡片：文档数/活跃会话/越界标记数/待审批归因单（服务端渲染数字） |
| /admin/documents | 知识库管理 | 上传表单 + 文档表格（版本/生效区间/状态/切片数/操作）；下架需 JS 二次确认 |
| /admin/prompts | 提示词模板 | 编辑器（textarea）+ 保存；下方历史修改记录（人+时间）；保存后立即生效提示 |
| /admin/audit | 对话审计 | 筛选栏（角色/批次号/时间范围/仅看越界）+ 记录表；越界行红标+类型徽章；点开抽屉看引用与命中切片；「标记已核查」 |
| /admin/attributions | 归因审批列表 | 待审/已审 Tab；详情页展示证据链、假设、缺陷关联；批准/退回（退回必须填意见）；「导出 8D」下载 .md |

服务端渲染原则：**列表与详情由后端模板直出**（刷新即可见、可打印），仅上传进度、二次确认、抽屉展开等用原生 JS 增强；不做前端框架。

### 4.2 上传流水线（#8 → #12）

```
上传(md/pdf) → 格式与大小校验(41501/41301)
  → 必填项校验: doc_type / version / effective_from / effective_to
  → 版本冲突检查(doc_name+version → 40903)
  → 抽文本: md 直接读 / pypdf 抽纯文本
  → 复用 ingest.chunk_text 切片(500字/80重叠) → 写 kb_doc + kb_chunk
  → 向量化: 成功→vector_status=ok; 失败→50004 降级入库(vector_status=pending, 黄标提示)
  → 全量索引重建: 从 kb_chunk 读全部 status=active 文档 → BM25+FAISS 落盘
     (课规模<1万切片, 同步重建秒级; 阶段1的 data/knowledge/*.md 由 seed 脚本登记入库)
下架(archive): status→archived + 记 archived_at/by → 触发索引重建(该文档切片即刻退出检索)
```

版本语义：同一 `doc_name` 允许 v1/v2/v3 并存（历史审计需要），但**检索与引用校验永远只认 active 版本**；下架即废止，物理文件与行都保留。

### 4.3 提示词模板即时生效机制

- 修改后 `PUT` → UPDATE active 行 + 追加 history 行（修改人=令牌 user_id）；
- `core/services/chat.py` **每次请求实时读 active 模板**（本地 SQLite 直查，不缓存）——彻底兑现「不写死在代码里、改完立即生效」；
- `core/knowledge/prompts.py` 里的 SYSTEM_PROMPT 退化为**兜底种子**：建库时若无 active 模板则自动灌入；
- prompt_hash 仍随消息落库（改用模板内容的哈希），审计可追溯每条回答用的哪个版本提示词。

### 4.4 越界回答自动检测（三条规则，入库时同步跑 + #18 补扫）

| 规则 | 判定 | 标记列 |
|---|---|---|
| 直接给根因结论 | assistant 正文命中正则组：`根因(已)?(确定\|是\|为)`、`可以确定.{0,6}根因`、`根本原因就是` 等 | flag_root_cause |
| 无引用作答 | assistant status=complete 且正文非空且引用行数为 0 | flag_no_citation |
| 引用已废止版本 | 任一 citation 的 (source_file, version) 在 kb_doc 中 status=archived | flag_stale_version |

标记是**疑似**（红标+徽章提示主管人工核查），不自动拦截回答——拦截属阶段 4 版本硬闸；audit 页支持 `only_flagged=true` 筛选，`review` 写入 reviewed_by/at 形成「机器标记 → 人工核查」闭环。

### 4.5 归因审批与 8D 导出

- 详情页证据链 = `evidence_json`（结构化：命中规则/统计线索/引用切片）+ `hypothesis_text`（L3 假设，恒带「待验证」）；
- 批准/退回：状态机 `submitted → approved/rejected`，退回**必填** decision_note（40906 防重复操作）；
- 导出 8D：`8d_draft_md` 已有则直接下载；为空则**现场调 LLM 生成草稿并缓存**（正文恒含「本报告为系统草稿，需人工复核」头），LLM 不可用时返回 50002 中文错误、不吐半截文件；下载 `Content-Disposition: attachment; filename=8D-{defect_no}.md`。

---

## 5. 班组长现场录入端（移动优先单页）

### 5.1 页面（`/m`，单 HTML，无路由）

三个步骤条控制的单页：**填单 → 确认缺陷码 → 成功回执**。

| 区块 | 设计 |
|---|---|
| 工单号 | 大输入框 + 联网时失焦即校验（40406 提前报错，不留到提交） |
| 工序码 | 大按钮组（横向滑动） |
| 描述 | textarea，失焦不强制；提交时触发归一化 |
| 数量/严重度 | 数量 stepper；严重度三档大按钮（轻微/一般/严重） |
| 拍照 | `<input capture>` → canvas 压缩（最长边 1280、JPEG 0.7、≤2MB）→ base64 随 JSON 提交 |
| 提交 | 大按钮；成功进入第 2 步「确认缺陷码」 |
| 确认缺陷码 | 候选码按 category 分组大按钮 +「都不对」；选定 → confirm → 第 3 步 |
| 成功回执 | 缺陷编号大字 + status=pending +（如为补录）黄色「补录」徽章 |
| 暂存箱 | 顶栏角标「N 条待补传」；断网提交自动入队 |

### 5.2 双时间戳与补录标记

- `occurred_at`：客户端提交时刻（离线暂存时=暂存时刻），ISO 格式随 body 传；
- `recorded_at`：服务端落库时刻；
- `is_backfill`：服务端判定 `recorded_at - occurred_at > 60 秒` **或** 客户端显式声明补录 → 打标；前端据它在列表与回执显示「补录」徽章。

### 5.3 描述归一化（两步落库，人工确认是硬闸）

```
POST /defects（第一步, 建 draft）
  → LLM 归一化: 描述文本 + defect_code 字典 → 候选码列表(带 confidence)
  → LLM 不可用 → 关键词回退: defect_code.keywords_json 命中计分排序
  → 返回 {defect_no, status:'draft', suggested:[{code,name,confidence,source}]}
POST /defects/{no}/confirm（第二步, 人工确认后才正式落库）
  → body {defect_code}（必选, 班组长亲手点选）
  → status: draft → pending；suggest_json 留存候选供审计
  → 不确认的单保持 draft，不进入下游（QE 也看不到）
```

分类≠判定：confirm 只写缺陷码（分类字段），**不触碰任何合格性判定**。

### 5.4 弱网/断网暂存协议（localStorage）

```
key: qt_defect_queue = [ {client_draft_id, payload(含 occurred_at 与照片 base64), created_at, retries} ]
触发入队: navigator.onLine === false 或 POST 网络失败(非 4xx 业务错误)
补传: online 事件 + 启动时 + 每 30s 轮询 → 逐条 POST
幂等: client_draft_id UNIQUE，重复补传返回原单(code=0, data.existing=true)，绝不落两条
限制: 队列上限 20 条(照片压缩后仍可能大)，超限提示「请先连接网络」
retries > 5 的条目标红，可单条删除
```

### 5.5 SL 可见性裁剪（服务端）

`GET /defects`、`GET /defects/{no}` 对 SL 的 SELECT 列白名单：`defect_no, work_order_id, op_code, description, qty, severity, defect_code, status, is_backfill, occurred_at, recorded_at` —— 归因关联、判定结论、候选码来源等字段在 SQL 层就不出现；状态只显示「待确认/已提交」，无任何判定语汇。

---

## 6. 实现批次（确认后执行）

| 批次 | 内容 | 验收 |
|---|---|---|
| 批次 1a | JWT + sys_user + 权限依赖注入 + 登录页 + 既有接口鉴权改造 + 全量测试回归 | 三角色登录、越权返回 40301/40302 |
| 批次 1b | 主管后台：知识库上传/下架 + 索引重建 + 提示词模板管理 + 页面 | 上传→检索可见；下架→废止版不再被引用；改提示词→下一条回答生效 |
| 批次 1c | 主管后台：对话审计（三规则自动标记+筛选+核查）+ 归因审批 + 8D 导出 + seed 数据 | 构造三类越界样本全部被标记；审批状态机正确 |
| 批次 2 | 班组长端：defects 接口（归一化两步流）+ 单页 + 离线暂存 + 数据范围裁剪 | 断网提交→恢复补传→补录徽章；SL 看不到他线与归因字段 |

---

## 7. 待拍板（确认后开写批次 1a）

| # | 问题 | 选项 |
|---|---|---|
| A | JWT 实现 | ① 手写 HS256（hmac+base64+json，零新依赖，与项目风格一致）；② PyJWT 库（标准但多一个依赖） |
| B | 主管后台模板引擎 | ① Jinja2（FastAPI 官方搭配，多一个轻依赖）；② 纯 Python 字符串拼 HTML（零依赖但页面复杂后难维护） |
| C | PDF 文本抽取 | ① pypdf（纯文本抽取够用，轻量）；② pdfplumber（版面更强，依赖重） |
| D | 本阶段建表范围 | ① 最小集（本文 10 张，work_order 为简表，谱系全表留阶段 4）；② 按架构 8 表一次建齐（material_batch/process_event/inspection 全上，超出本阶段需要） |

我倾向 **A① + B① + C① + D①**：零/轻依赖、够课程演示、表随功能走。
