# 页面截图清单

全部由脚本在真实 Chrome 上生成，不是设计稿：

```bash
bash scripts/run_server.sh                     # 先起服务
python scripts/ui_check_pages.py 8000          # 生成问答页 + 后台 5 页
python scripts/ui_check.py 8000                # 生成移动端 3 张
python scripts/ui_check_analytics.py 8000 --shot   # 质量看板（14 项断言）
python scripts/ui_check_genealogy.py 8000 --shot   # 批次追溯（12 项）
python scripts/ui_check_data.py 8000 --shot        # 导入导出（15 项）
python scripts/ui_check_msa.py 8000 --shot         # MSA（23 项）
```

后四个脚本需要 `PYTHONPATH=src python scripts/seed_demo_data.py` 先灌演示数据：
看板的控制图要求有跨日子组，追溯要求有领料关系，MSA 要求有多次研究记录——
空库跑出来是一张"暂无数据"的页面，截了也没法说明问题。

| 文件 | 页面 | 这张图能证明什么 |
|---|---|---|
| `chat-answer.png` | `/` 质量工程师问答 | SSE 流式回答已渲染完，引用卡片按 `[C1][C2][C3]` 列出文档名 + 章节 + 版本号 |
| `mobile-1-login.png` | `/m` 班组长端 | 页内登录浮层（不跳转，避免丢失已填内容），手机视口布局 |
| `mobile-2-confirm.png` | 第 2 步「确认缺陷码」 | 候选码按分类分组的大按钮 + "都不对"入口；此时 `defect_code` 仍为空 |
| `mobile-3-receipt.png` | 第 3 步「回执」 | 状态文案是"已提交，待质量工程师判定"，**不含任何判定结论** |
| `admin-dashboard.png` | `/admin/` | 仪表盘统计（文档数、会话数、越界命中数、待审批数） |
| `admin-documents.png` | `/admin/documents` | 知识库列表带版本 + 生效区间 + 上下架状态与操作按钮 |
| `admin-prompts.png` | `/admin/prompts` | 提示词模板编辑区 + 历史快照入口 |
| `admin-audit.png` | `/admin/audit` | 审计列表：角色/批次/时间/仅看命中四维筛选，行内标记三类命中 |
| `admin-attributions.png` | `/admin/attributions` | 归因单列表（状态、提交人、时间）+ 待办筛选 |
| `10-analytics.png` | `/admin/analytics` | KPI 卡 + 结论横幅 + X̄-R 控制图（异常点红标）+ 帕累托 + 能力指数盒 + 星期效应 |
| `11-genealogy.png` | `/admin/genealogy` | 三列 SVG 图谱（批 → 工单 → 缺陷）+ 影响面徽章 + "相关不等于因果"提示 |
| `12-data-io.png` | `/admin/data` | 数据集选择与逐行错误表；预览与"确认导入"是两个按钮 |
| `13-msa.png` | `/admin/msa` | 覆盖率与缺口清单在上、单次研究详情在下；两种算法并排对比 |

## 截图是怎么"验"的

脚本不只截图，还断言关键元素存在（否则只截到一张白屏也算"通过"）：

- 问答页：等 SSE 结束后断言回答区非空、引用区存在 `[C1]` 文本；
- 移动端：走完"登录 → 填单 → 选码 → 回执"，断言回执里的单号形如 `DF-YYYYMMDD-NNNN`、状态文案含"待质量工程师判定"；
- 后台 5 页：逐页断言主容器渲染成功，非主管角色访问时落到 `/admin/forbidden`；
- 看板：断言 SVG 里 `path + rect + circle` 的总数 > 0（**只数 rect 会误判**：
  折线图是 `path + circle`，没有 rect）、结论横幅非空、能力指数盒存在；
- 追溯：断言三列图谱各有节点、影响面徽章有文字、班组长访问被拒；
- 导入导出：断言数据集下拉有选项、预览返回逐行错误表、重复导入被判"跳过"（验证幂等真的生效）；
- MSA：断言研究列表有行、详情同时出现 %EV/%AV 与两种算法、**阈值改严后判定更敏感**
  （性质验证，不依赖具体数据）、班组长 403。

已知现象：移动端巡检会打印一条资源 404 告警（Chrome 对空白页默认图标的请求），服务端访问日志无对应记录，不影响流程，详见 `docs/test-plan.md` §7。
