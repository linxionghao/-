# quality-trace

> 制造业质量追溯与缺陷归因工具 —— 从批次谱系出发，把「这批货为什么不良」变成可核对、可复现的证据链。

当前状态：**骨架阶段（v0.1.0）**。目录结构、配置、CLI、测试与 CI 已就位，业务逻辑待填充。

## 它要解决什么

制造现场的两个问题通常被分开处理，结果两边都不彻底：

1. **追溯**：这批成品的原料是哪一批、经过哪台设备、哪班人、哪次检验——散落在 ERP、MES、纸质单据里。
2. **归因**：不良品单上写了「尺寸超差」，但到底是料、机、法、环、测哪一环出的问题，靠老师傅经验。

本项目把两者接起来：**先有谱系，才谈归因；归因结论必须能落回具体批次、工序和时间窗口。**

## 安装

```bash
# Python 3.9+
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate      # Linux / macOS

pip install -r requirements-dev.txt
pip install -e .
```

## 运行

```bash
# 命令行入口（安装后可用）
quality-trace --version
quality-trace --config config/default.yaml

# 不安装也能跑：把 src 加进 PYTHONPATH
set PYTHONPATH=src              # Windows
python -m quality_trace.main --version

# 示例
python examples/demo.py

# 测试
pytest
```

## 目录结构

```
quality-trace/
├── README.md              门面：是什么、怎么装、怎么跑
├── LICENSE                MIT
├── .gitignore             依赖、密钥、缓存
├── .gitattributes         .sh 强制 LF
├── .editorconfig          缩进与编码
├── CHANGELOG.md           版本记录（Keep a Changelog）
├── CONTRIBUTING.md        贡献指南（课程作业，暂不收外部 PR）
├── requirements.txt       运行时依赖
├── requirements-dev.txt   开发依赖
├── pyproject.toml         打包、入口点、pytest/ruff 配置
├── src/quality_trace/     源码主目录
│   ├── __init__.py        版本号唯一来源
│   ├── main.py            CLI 入口（只做参数解析与分发）
│   ├── core/              核心业务逻辑（纯函数，可单测）
│   └── utils/             通用小工具（配置、日志）
├── docs/                  设计说明与接口文档
├── tests/                 测试，与 src 结构对应
├── examples/demo.py       可运行的示例
├── scripts/               构建与部署（sh + ps1 双份）
├── config/default.yaml    非机密默认配置
├── data/                  样例数据（小文件）
└── .github/               CI、Issue 模板、PR 模板
```

## 设计原则

- **`core/` 不依赖 CLI、不读环境变量、不打印**：所有可测逻辑放这里，测试覆盖率优先保证它。
- **配置外置**：`config/default.yaml` 提供默认值，`config/local.yaml`（已 ignore）覆盖，环境变量优先级最高。
- **结论分层**：规则命中是硬证据，统计相关是软证据，因果假设必须标注「待验证」，最终归因由人签字。
- **零魔法**：不做自动「AI 定责」。

详见 `docs/design.md`。

## 许可

MIT，见 `LICENSE`。
