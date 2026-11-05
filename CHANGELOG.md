# 更新记录

本文件格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增（工程化收尾：文档体系 + CI + 接口层测试）

- 文档：新增 `docs/data-model.md`（12 表逐表 + 双时间戳 + 索引 + 查询手册 + 自检 SQL）、`docs/permissions.md`（三角色边界 + 三层防线 + 31 接口矩阵）、`docs/test-plan.md`（三层验证 + 用例地图 + 覆盖率现状 + 实测记录）、`docs/acceptance.md`（8 条验收标准逐条对照）、`docs/engineering.md`（分层约束 + 错误与降级 + 提交规范）、`docs/deployment.md`（本地/局域网/生产前六件事/排错表）
- ADR：新建 `docs/adr/`，6 条决策记录（手写 JWT、SSE、三道版本闸、三色证据、双时间戳、降级非失败）+ 索引与模板
- CI：`.github/workflows/ci.yml`——Python 3.9/3.11 双版本，编译门禁（ruff E9/F63/F7/F82）+ 全量单测 + shell 语法检查；**故意不装 faiss/jieba**，以此验证降级路径
- 仓库规范：`.editorconfig`、`.gitattributes`（换行固定、二进制识别）
- 测试：新增 `tests/test_api_routes.py` 25 例（HTTP 状态码、统一响应包、鉴权依赖、字段裁剪、页面可达），全量 214 通过 / 1 跳过；`core/` 覆盖率 88%，整体 80%
- 新增 `scripts/demo_closed_loop.py`：一条命令在临时库上跑完「认证 → 检索 → 版本闸 → 录入 → 确认 → 归因 → 审批 → 审计」15 项断言，无需起服务、不污染演示数据
- README 增「文档索引」表，测试徽章同步 214，自检清单加入一键闭环脚本

### 修复

- `fix(defects)`：班组长令牌未绑定 `line_id` 时原先退化为"全量可见"，改为 `line_scope()` 统一 40302 拒绝（新增 7 条单测覆盖列表/详情/建单/确认/工单校验/空串/QE 不受影响）
- `fix(knowledge)`：无 jieba 时中英混排（如「螺纹外径 19.98mm 公差」）会被整段当成一个词，导致降级路径检索失效；改为按汉字段二元组 + ASCII 段分别切分
- `fix(seed)`：知识库跳过日志重复打印版本前缀（`vv1` → `v1`）
- 文档口径修正：演示账号统一为 `leader01` / `manager01`；`as_of` 带 SQL 时间闸的返回 `mode` 实为 `bm25_only`，验收断言改看命中版本集合
### 新增（阶段 3 · 批次 1c：对话审计 + 归因审批）

- 数据层：`chat_message` 增审计标记列（根因结论/无引用/废止版本 + 人工核查三列）与 `chat_session.batch_no`，SQLite 逐条 ALTER 迁移，旧库可直接升级
- 新增 `work_order` / `defect_item` / `attribution_order` 三表（工单与缺陷为简表，完整谱系留阶段 4）
- 审计服务 `core/services/audit.py`：三规则纯函数 + 单条落库 + 全量补扫；**补扫不覆盖人工核查结论**，规则升级可反复重跑
- 归因服务 `core/services/attribution.py`：状态机（submitted→approved/rejected，重复审批 40906）、退回必填意见、8D 草稿导出（有缓存直读 / 无缓存调 LLM 并缓存 / 不可用报 50002 且不留半截文件）
- 问答链路收尾自动触发审计：回答落库后同步跑三规则，越界即时可见
- 主管后台新增三页：对话审计（角色/批次/时间/仅越界四维筛选 + 核查）、归因审批列表、归因详情（证据链 + 批准/退回 + 8D 下载）；导航与仪表盘统计同步
- JSON 接口 #15–#23：审计筛选/详情/核查/补扫、归因列表/详情/批准/退回/导出 8D
- 测试：`tests/test_audit.py`（31 例）+ `tests/test_attribution.py`（11 例），全量 131 通过

### 新增（阶段 3 · 批次 1b：主管后台——知识库 + 提示词）

- 数据层：`kb_doc`（版本号+生效区间必填、下架≠删除、唯一索引防同名同版本）、`kb_chunk`、`prompt_template` + 历史表四张新表
- 服务层 `core/services/knowledge_admin.py`：上传流水线（md/pypdf 抽取 → 切片 → 落库 → 全量索引重建）、下架/恢复（**废止版切片在源头退出索引，实测零泄漏**）、向量化失败 50004 降级（文档照常入库）
- **DB 为权威、索引文件为派生物**：上传/下架都从 kb_chunk 重建 chunks.json，问答链路无改动
- 提示词模板：存库不写死，`/chat` 每次请求实时读 active 模板（改完立即生效）；每次修改记录修改人+时间（历史只增不删）
- JSON 接口（#8–#14，主管专属）：上传（multipart）、列表、切片查看、下架/恢复、模板读/改/建
- 主管后台页面（Jinja2 服务端渲染）：登录（HttpOnly Cookie 会话）、仪表盘、知识库管理、提示词模板；非主管角色 403 页
- 鉴权依赖支持 Cookie 通道（页面与 JS fetch 共用）；`seed_demo_data.py --kb` 登记示例语料并初始化默认模板
- 修复：登录页失败分支误把业务码 40102 当 HTTP 状态码（连接中断）→ 改为取前三位
- 测试：上传/版本冲突/下架零泄漏/恢复/模板 upsert+历史等 11 个新用例（全量 87 通过）；真实服务 13 项端到端验证通过

### 新增（阶段 3 · 三角色鉴权与双端界面，批次 1a）

- 方案设计 `docs/stage3-design.md`：权限矩阵（角色 × 接口，29 项含数据范围）、10 张新表、越界自动检测三规则、离线暂存协议；决策 A①手写 HS256 + B①Jinja2 + C①pypdf + D①最小建表
- 鉴权服务 `core/services/auth.py`：手写 HS256 JWT（零新依赖）、密码哈希、登录（40102 用户名/密码同码防探测、40103 过期与 40101 无效区分）
- 用户表 `sys_user` 与读写函数（username 幂等 seed）
- 入口层：`POST /api/v1/auth/login` + `GET /api/v1/auth/me`；`get_current_user`（JWT 优先 + `auth.dev_header_bypass` 演示旁路）与 `require_roles()` 角色白名单工厂
- 既有问答接口全部切换到新鉴权（质量工程师白名单；班组长令牌调 /chat 实测 40301）
- `scripts/seed_demo_data.py`：三角色演示账号（qe001 / leader01 / manager01）
- 错误码表扩充：40102/40103/40302 + 知识库/缺陷/归因/上传系列（40403–40406、40903/40905/40906、41301/41501、50004）
- 测试：JWT 往返、过期/篡改/畸形、登录、幂等 seed 等 13 个新用例（全量 74 通过）

### 修复（整体运行体检发现）

- **会话接口漏挂鉴权**（严重）：`GET /sessions/{id}/messages`、`DELETE /sessions/{id}` 此前无任何身份校验，匿名可读任意会话历史——补上角色白名单 + 本人归属校验
- **跨用户会话写入**（中）：质量工程师可用他人 session_id 通过 `/chat` 提问——`resolve_session` 增加归属校验，他人会话按 40401 处理（不暴露存在性）
- **HTTP 状态码对齐**：路由级 `_json` 帮助函数此前恒返回 200，现按错误码前三位返回（40401→404 / 40301→403），与全局异常处理器和文档约定一致
- 新增 2 个归属校验回归用例（全量 76 通过），并用真实服务 12 项端到端复验

### 新增（阶段 2 · 质量工程师问答入口）

- 接口设计 `docs/chat-api.md`：统一错误码（含中文文案表）、SSE 事件序列、降级语义（50001/50002/50401 非失败）
- `POST /api/v1/chat`：SSE 流式问答（status → sources → delta → done），最近 6 轮历史上下文
- 会话管理接口：创建 / 历史拉取（含引用）/ 归档；`GET /api/v1/health` 健康检查
- 数据层 `core/repo/db.py`：chat_session / chat_message / chat_message_citation 三表（引用结构化落库可审计）
- 服务层 `core/services/chat.py`：校验、限流、并发拒绝、历史窗口、降级决策、流式事件生成
- LLM 适配层新增 `stream_chat()` 流式方法（requests stream=True，零新依赖）
- 单页聊天界面 `web/chat.html`：fetch 流式解析、打字机效果、检索状态条、引用卡片（文档名+条款号+版本号）
- 启动脚本 `scripts/run_server.sh` / `run_server.ps1`
- 测试：三表读写、问答编排离线降级链路等 20 个新用例（全量 61 通过）

### 计划中

- 阶段 3 批次 1c：对话审计（越界三规则自动标记）+ 归因审批 + 8D 导出
- 阶段 3 批次 2：班组长移动录入端（缺陷两步落库 + 离线暂存 + 数据范围裁剪）
- 聊天页 chat.html 接入 JWT 登录（当前仍走 X-User-Id 演示旁路）
- 谱系数据模型：物料批次 → 工单 → 工序 → 设备/工装/操作者 → 检验记录 → 成品序列号
- 正向/反向追溯查询
- SPC 判异规则（Nelson / Western Electric）进入 `core/`
- 归因四层证据链：规则命中 → 统计相关 → 因果假设 → 人工确认
- 版本失效三道闸接入检索（架构 4.3）：as_of 硬过滤 + 引用版本校验

## [0.1.0] - 2026-10-23

### 新增

- 项目骨架：`src/` 布局、`pyproject.toml` 打包与入口点
- CLI 入口 `quality-trace`，支持 `--version` / `--config` / `--verbose`
- 配置加载：`config/default.yaml` 默认值，PyYAML 缺失时回退到内置极简解析
- 核心流水线占位实现 `core.pipeline.run()`，返回结构化摘要
- 单元测试：配置加载、流水线、CLI
- CI：GitHub Actions 在 Python 3.9 / 3.12 上跑 pytest
- 构建脚本：`scripts/build.sh` / `scripts/build.ps1`
- 文档：`docs/design.md`、`docs/api.md`
