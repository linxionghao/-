# 页面截图清单

全部由脚本在真实 Chrome 上生成，不是设计稿：

```bash
bash scripts/run_server.sh                     # 先起服务
python scripts/ui_check_pages.py 8000          # 生成问答页 + 后台 5 页
python scripts/ui_check.py 8000                # 生成移动端 3 张
```

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

## 截图是怎么"验"的

脚本不只截图，还断言关键元素存在（否则只截到一张白屏也算"通过"）：

- 问答页：等 SSE 结束后断言回答区非空、引用区存在 `[C1]` 文本；
- 移动端：走完"登录 → 填单 → 选码 → 回执"，断言回执里的单号形如 `DF-YYYYMMDD-NNNN`、状态文案含"待质量工程师判定"；
- 后台 5 页：逐页断言主容器渲染成功，非主管角色访问时落到 `/admin/forbidden`。

已知现象：移动端巡检会打印一条资源 404 告警（Chrome 对空白页默认图标的请求），服务端访问日志无对应记录，不影响流程，详见 `docs/test-plan.md` §7。
