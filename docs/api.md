# 接口说明

> 对应版本：v0.1.0。接口变化会在 `CHANGELOG.md` 中标注。

## 1. 命令行

```
quality-trace [--config PATH] [--verbose] [--version]
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-c, --config` | `config/default.yaml` | 配置文件路径 |
| `-v, --verbose` | 关闭 | 输出调试信息（配置键列表等） |
| `--version` | — | 打印版本号后退出 |

**退出码**

| 码 | 含义 |
|---|---|
| 0 | 执行完成（配置校验通过或给出了问题清单） |
| 2 | 配置文件不存在 |

> 注意：配置校验不通过时仍返回 0，问题以 `[配置]` 前缀打印，便于在脚本里先跑通再修配置。
> 如果后续接入真实数据源，业务失败应返回非 0，需在 `main()` 中补充。

**输出示例**

```
quality-trace v0.1.0
配置: config/default.yaml
  - [配置] 校验通过
  - [追溯] 数据源未接入，跳过
  - [归因] 规则库为空，跳过
结果: ready
```

## 2. 配置项

配置文件为 YAML，两层结构。用户级覆盖写 `config/local.yaml`（已 gitignore）。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `app.name` | str | `quality-trace` | 应用名 |
| `app.log_level` | str | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `trace.direction` | str | `both` | `forward` / `backward` / `both` |
| `trace.max_depth` | int | `5` | 谱系追溯最大层数，≥1 |
| `attribution.min_records` | int | `3` | 参与统计相关的最少记录数，≥1 |
| `attribution.confidence_floor` | float | `0.6` | 疑似原因置信度下限，取 `[0, 1]` |

## 3. 环境变量

优先级最高，格式 `QT_<SECTION>__<KEY>`（两个下划线分隔层级）。

```bash
QT_APP__LOG_LEVEL=DEBUG
QT_TRACE__MAX_DEPTH=8
QT_ATTRIBUTION__CONFIDENCE_FLOOR=0.75
```

值会按 `bool → int → float → str` 的顺序自动转换。

## 4. Python API

### `quality_trace.utils.config.load_config`

```python
def load_config(
    path: Optional[Union[str, os.PathLike]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]
```

加载配置并与默认值深合并。

- `path=None`：只返回默认值
- 文件不存在：抛 `FileNotFoundError`
- 顶层不是映射：抛 `ValueError`
- 未安装 PyYAML 时，自动回退到内置的极简解析器（仅支持两层结构 + 标量）

### `quality_trace.core.pipeline.check_config`

```python
def check_config(config: Dict[str, Any]) -> List[str]
```

返回问题清单，空列表表示通过。纯函数，无副作用。

### `quality_trace.core.pipeline.run`

```python
def run(config: Dict[str, Any], verbose: bool = False) -> Dict[str, Any]
```

返回：

```python
{
    "status": "ready" | "invalid",
    "checks": ["[配置] ...", "[追溯] ...", "[归因] ..."],
}
```

**契约**：本函数不得返回任何「根因已确定」之类的结论，最多输出「疑似原因」与「待验证假设」。
