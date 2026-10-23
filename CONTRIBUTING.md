# 贡献指南

## 一句话

本项目是课程作业，**暂不接受外部 PR**。如果你是从 GitHub 看到它并想复用，请遵守 `LICENSE`（MIT），保留版权声明即可。

## 本地开发

```bash
pip install -r requirements-dev.txt
pip install -e .
pytest
```

## 提交前自检

1. `pytest` 全绿
2. `ruff check .` 无告警
3. 新增的核心逻辑在 `tests/` 里有对应测试
4. 版本号与 `CHANGELOG.md` 对得上（`src/quality_trace/__init__.py` 与 `pyproject.toml` 两处）

## 代码约定

- `core/` 里不出现 `print`、`argparse`、`os.environ`
- 配置项一律从 `config/default.yaml` 读，不在代码里写魔数
- 函数短、可测；副作用集中在 `main.py` 和 `utils/`

## 分支与提交

- 主分支 `main` 保持可运行
- 提交信息用「动词 + 对象」，例如 `feat: 增加批次反向查询`
- 一个提交只做一件事
