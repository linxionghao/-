# 更新记录

本文件格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 计划中

- 谱系数据模型：物料批次 → 工单 → 工序 → 设备/工装/操作者 → 检验记录 → 成品序列号
- 正向/反向追溯查询
- SPC 判异规则（Nelson / Western Electric）进入 `core/`
- 归因四层证据链：规则命中 → 统计相关 → 因果假设 → 人工确认

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
