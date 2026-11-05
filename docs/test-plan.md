# 测试与验证

> 一句话：**单测证明逻辑对，冒烟证明链路通，页面巡检证明人能用。** 三者缺一，都只能算"应该没问题"。

```
第 1 层  pytest        214 passed / 1 skipped    逻辑正确性（离线、秒级、可进 CI）
第 2 层  smoke 脚本    14 + 18 项断言            真实 HTTP 链路（起服务后跑，验鉴权/状态码/降级）
第 3 层  Playwright    2 个脚本 / 9 张截图        真实浏览器操作（原生 JS 与 SSE 只有跑起来才知道）
```

---

## 1. 为什么要三层，而不是"多写点单测"

单测能覆盖分支，覆盖不了三类东西：

| 单测测不到 | 只有真实运行才能发现 | 本项目实际踩到的例子 |
|---|---|---|
| 响应包/HTTP 状态码是否与文档一致 | 起服务 + curl | 路由层 `_json` 曾一律返回 200，body 里才是错误码 |
| SSE 事件顺序与流式渲染 | 浏览器 + 网络流 | `sources` 必须在正文前发，否则断线后引用丢失 |
| 浏览器的真实交互 | Playwright 驱动 Chrome | 登录遮罩用 `display:none` 隐藏，`wait_for_selector` 判"可见"永远超时 |

所以三层的分工不是"从粗到细"，而是**各测一段互不重叠的事实**。

---

## 2. 第 1 层：pytest（214 passed / 1 skipped，整体行覆盖率 80%，core/ 88%）

```bash
python -m pytest -q                      # 全量
python -m pytest tests/test_defects.py -q    # 单个文件
python -m pytest -o addopts="" -q        # 想要结尾那行统计（addopts 里已有 -q，再叠就没了）
```

### 2.1 用例地图

| 文件 | 用例数 | 覆盖什么 | 关键断言举例 |
|---|---|---|---|
| `test_core.py` | 少数 | 配置加载、工具函数 | 配置优先级：环境变量 > local.yaml > default.yaml |
| `test_utils.py` | 7 | 日期、文本清洗 | `as_of` 归一化 |
| `test_knowledge.py` | 21 | 切片、分词、BM25、向量、RRF、混合检索 | 正确文档排第一；关掉向量通道时 `degraded=False`（配置关闭 ≠ 降级） |
| `test_llm.py` | 4 | LLM 适配层 | 超时/网络错误统一抛 `LLMUnavailable`，不泄漏底层异常 |
| `test_cli.py` | 少数 | 阶段 1 命令行原型 | 无 API key 时打印"未配置"而不是崩溃 |
| `test_chat_repo.py` | 十余 | 会话/消息/引用的读写 | 消息 `partial → complete` 的更新路径 |
| `test_chat_service.py` | 十余 | 问答服务：历史、检索、流式 | 历史快照取在写入本轮问题**之前**（防上下文污染） |
| `test_auth.py` | 13 | 密码哈希、JWT、角色矩阵 | 过期 → 40103；签名篡改 → 40101；错角色 → 40301 |
| `test_knowledge_admin.py` | 11 | 上传/切片/下架/恢复 | 同名同版本 → 40903；下架后切片退出索引 |
| `test_version_gate.py` | 9 | 三道版本闸 | `as_of` 落在区间外 → 0 命中；`version_gate` 取值 `sql` / `index` |
| `test_audit.py` | 31 | 三条审计规则 + 复核 | 无引用且"完成"→ `flag_no_citation`；引用已下架版本 → `flag_stale_version` |
| `test_attribution.py` | 11 | 审批状态机 + 8D 导出 | 退回必填 note；未批准导出带 DRAFT 横幅；LLM 不可用 → 50002 |
| `test_defects.py` | 50 | 归一化 / 建单 / 确认 / 越权 / 照片 / 未绑定产线 | 见下 |
| `test_api_routes.py` | 25 | HTTP 层契约：状态码、响应包、鉴权依赖、字段裁剪、页面可达 | QE 建单 → 403；班组长响应 JSON 里搜不到 `suggest_json` |

唯一被 skip 的一条是"装了 faiss 才跑"的分支（`test_knowledge.py:139`），因为开发机与 CI 都故意不装 `faiss-cpu`——降级路径（numpy 余弦）才是被验证的主路径。

### 2.2 三个值得单独说的用例组

**(1) 幂等**：`test_idempotent_by_client_draft_id` —— 同一 `client_draft_id` 提交两次，必须返回同一张单且库里只有 1 条。离线队列重发、用户连点两次，都靠这条兜住。

**(2) 越权视角**：`TestUnboundShiftLeader` 7 条 —— 班组长令牌里 `line_id` 为空（配置错误）时，列表/详情/建单/确认/工单校验全部 40302，且 QE/QM 不受影响。安全默认值必须是"拒绝"。

**(3) 版本闸**：`test_version_gate.py` —— 同时存在 v1/v2/v3 的库里，`as_of=2024-06-01` 只能召回当时有效的版本；`as_of` 传非法格式 → 40001 且提示格式（不是静默忽略，静默忽略等于把"历史视角"退化成"当前视角"，用户还以为查的是历史）。

### 2.3 测试的硬约束

- **不碰网络**：LLM 一律 monkeypatch 替身，向量用确定性哈希函数，embedding 服务从不出网；
- **不碰演示库**：全部用 `tmp_path` 建临时 SQLite + 临时上传目录；
- **不依赖可选依赖**：`faiss` / `jieba` 缺失时走降级路径，测试必须两边都过（CI 就是不装的）。

---

## 3. 第 2 层：接口冒烟（真实 HTTP）

```bash
python scripts/seed_demo_data.py --kb --audit     # 准备演示数据（幂等）
bash scripts/run_server.sh                        # 起服务，默认 8000

# 另开终端
bash scripts/smoke_stage3.sh 8000                 # 主管后台：审计 + 审批
bash scripts/smoke_defects.sh 8000                # 班组长录入端
```

两个脚本都是"打印 ✓/✗，最后汇总 PASS/FAIL，有失败即 `exit 1`"，可以进 CI 或发布会前的手动验收。它们**真的登录、真的拿 token、真的发请求**，所以能抓到单测抓不到的问题。

### 3.1 `smoke_stage3.sh`（14 项断言，实测 14 通过 / 0 失败）

| 分组 | 断言 |
|---|---|
| 认证 | 三个角色各自登录成功；错误密码 → 40102；QE 访问后台接口 → 403 |
| 审计 | 列表可查；`only_flagged=1` 只返回命中行；详情含三条规则命中项；复核后 `reviewed_by` 有值 |
| 重扫 | 全量重扫不覆盖已有人工复核结论 |
| 审批 | 列表/详情；批准后状态 `approved`；退回不填 note → 40001；8D 导出含 DRAFT 横幅 |

### 3.2 `smoke_defects.sh`（18 项断言，实测 18 通过 / 0 失败）

| 分组 | 断言 |
|---|---|
| 字典与工单 | 缺陷码分组返回；本线工单可查；他线工单 → 40406 |
| 建单 | `status=draft` 且 `defect_code` 为空；返回候选码；双时间戳分离；补录标记 |
| 字段裁剪 | 班组长响应**不含** `suggest_json`（用 grep 直接断言 JSON 里没有这个键） |
| 确认 | 确认后 `pending` + 缺陷码写入；`status_text` 不含"合格/不合格"字样 |
| 幂等 | 同 `client_draft_id` 二次提交返回同一 `defect_no` |
| 越权 | 他线确认 → 404；无 token → 40101 |
| 照片 | base64 预上传拿到 32 位 hex token；坏 token → 40003 |

第 3 组那条 grep 断言是刻意写死的：接口文档说"班组长看不到判定相关字段"，那就用最笨的办法验证——响应体里搜不到这个键。

---

## 4. 第 3 层：Playwright 页面巡检

```bash
python scripts/ui_check.py 8000          # 移动端三步录入（含截图）
python scripts/ui_check_pages.py 8000    # 问答页 + 5 个后台页面（含截图）
```

需要本机装 Chrome（脚本用 `channel="chrome"`，headless）。截图统一落在 `docs/screenshots/`，既当证据，也当 README 的配图。

| 脚本 | 走什么 | 抓什么 |
|---|---|---|
| `ui_check.py` | 登录 → 填单（工单/工序/数量/描述/照片）→ 候选码 → 确认 → 回执 | 3 张：登录、确认页、回执（含补录角标） |
| `ui_check_pages.py` | 问答页发问并等流式结束；后台仪表盘/文档/提示词/审计/归因 5 页依次打开 | 6 张 |

两个已知坑（都是脚本里注释过的）：

1. 登录遮罩的"已登录"状态是 `display:none`，Playwright 默认判"可见"会一直等——改成 `wait_for_function` 检查 `classList.contains('hidden')`；
2. 页面请求 `/favicon.ico` 会 404 并污染 console 断言——两个页面都加了 `<link rel="icon" href="data:,">`。

---

## 5. 复现一次完整验证

```bash
# 1) 逻辑
python -m pytest -q

# 2) 数据 + 服务
python scripts/seed_demo_data.py --kb --audit
bash scripts/run_server.sh &

# 3) 接口
bash scripts/smoke_stage3.sh 8000
bash scripts/smoke_defects.sh 8000

# 4) 页面
python scripts/ui_check.py 8000
python scripts/ui_check_pages.py 8000
```

全程约 2 分钟。四个步骤全绿，代表：鉴权 → 检索 → 问答 → 审计 → 审批 → 录入 六段主链路在真实服务上可用。

CI（`.github/workflows/ci.yml`）只跑第 1 步 + 编译门禁，因为第 2–4 步需要 API key、Chrome 和可写的演示库，不适合放在公共 runner 上。

---

## 6. 已知测试盲区

诚实列出来，比假装全覆盖有用：

| 盲区 | 为什么没测 | 影响 |
|---|---|---|
| LLM 输出质量（回答是否"像人话"） | 不可判定、且供应商可换 | 靠三条审计规则兜底线（越界/无引用/旧版本），不测"文笔" |
| PDF 解析准确率 | 依赖 pypdf 与扫描件质量 | 上传后可在后台看切片预览，人工核对 |
| 并发写入（多用户同时建单） | SQLite WAL 下需压测才有意义 | 靠唯一约束与幂等键兜底，不做连接池压测 |
| 移动端真机（iOS Safari） | 无设备 | 只保证 Chrome 移动视口下可用 |
| 8D 导出的内容正确性 | 依赖 LLM | 导出必须经主管审批 + 带 DRAFT 横幅，人工把关 |

---

## 7. 实测记录（2026-11-05）

环境：Windows 10 · Python 3.9.13（pytest）· Python 3.12（Playwright）· 本机 Chrome headless · 服务 `127.0.0.1:8013`

| 命令 | 结果 |
|---|---|
| `python -m pytest -o addopts="" -q` | **214 passed, 1 skipped**（跳过的 1 条是"装了 faiss 才跑"，本机与 CI 都未装）；`--cov=src/quality_trace/core` → **88%**，`--cov=src/quality_trace` → **80%** |
| `bash scripts/smoke_stage3.sh 8013` | **14 通过 / 0 失败** |
| `bash scripts/smoke_defects.sh 8013` | **18 通过 / 0 失败** |
| `python scripts/ui_check_pages.py 8011` | 全部页面巡检通过（问答页 + 后台 5 页） |
| `python scripts/ui_check.py 8011` | 全部通过：登录 → 填单 → 确认缺陷码 → 回执（草稿号 `DF-20261105-0011`，状态文案"已提交，待质量工程师判定"） |

两条与"诚实"有关的备注：

1. **移动端巡检仍有一条资源 404 告警**。两个页面都已加 `<link rel="icon" href="data:,">`，服务端访问日志里也查不到 404 记录，判断是 Chrome 对空白页/新标签的默认图标请求，与业务流程无关；保留在报告里而不是从脚本里屏蔽掉——把告警藏起来比留着它更危险。
2. **冒烟脚本必须打到"启动命令正确"的服务上**。第一次在 8012 端口尝试时出现 `upstream connect failed (os error 10061)`，原因是启动命令缺 `PYTHONPATH=src`（`ModuleNotFoundError: quality_trace`），进程直接退出、端口无人监听。这类失败会被脚本如实报成 14 项全挂，而不是"静默变成 0 项"——这也说明"断言数会随服务存活情况变化"本身就是一个有效的健康信号。

---

## 8. 覆盖率现状（验收标准 #7）

需求要求 `core/` 行覆盖率 ≥ 70%。实测：

| 范围 | 覆盖率 | 说明 |
|---|---|---|
| `src/quality_trace/core`（含 services / repo / knowledge / llm） | **88%** | 验收对象，达标 |
| `src/quality_trace`（含 api 路由层） | **80%** | 补上 `test_api_routes.py` 后从 64% 提到 80% |

逐模块看，低覆盖的都是"分支多但在课程范围内用不到"的地方，不是"忘了测"：

| 模块 | 覆盖率 | 未覆盖的是哪些行 |
|---|---|---|
| `core/services/attribution.py` | 100% | —— |
| `core/services/audit.py` | 100% | —— |
| `core/services/errors.py` | 100% | —— |
| `core/repo/db.py` | 92% | 少量防御性分支（坏 JSON、空结果拼装） |
| `core/services/defects.py` | 93% | 同上 |
| `core/services/auth.py` | 95% | 令牌缺字段的异常分支 |
| `core/knowledge/retrieval.py` | 88% | `faiss` 专用路径（本机未装，走 numpy 分支） |
| `core/services/chat.py` | 81% | 流式生成的真实网络异常路径 |
| `core/services/photo.py` | 81% | 目录创建失败的 OS 异常 |
| `core/llm/client.py` | 47% | HTTP 重试/超时分支需要 mock requests 的全部异常族，收益低于成本 |

这样安排的理由：覆盖率是"有没有测"的下界指标，不是目标本身。为了把 `llm/client.py` 从 47% 推到 90%，需要给 `requests` 写 6 种异常的 mock，而这些 mock 一旦与真实 `requests` 行为漂移，反而制造"测过了但线上仍挂"的假安全感。真正的保护是调用方统一捕获 `LLMUnavailable` 并降级——这条路径由 `chat.py`（81%）和 `normalize.py`（85%）覆盖。

命令：

```bash
python -m pytest -o addopts="" -q --cov=src/quality_trace/core --cov-report=term
```
