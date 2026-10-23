"""配置加载。

优先级（后者覆盖前者）：
    DEFAULTS  <  config/default.yaml  <  config/local.yaml（由调用方传入）
              <  环境变量 QT_<SECTION>__<KEY>

环境变量示例：
    QT_APP__LOG_LEVEL=DEBUG
    QT_TRACE__MAX_DEPTH=8

没有安装 PyYAML 时，回退到内置的极简扁平解析器（只支持两层 section + 标量）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

try:  # pragma: no cover - 取决于运行环境
    import yaml

    _HAS_YAML = True
except ModuleNotFoundError:  # pragma: no cover
    _HAS_YAML = False

ENV_PREFIX = "QT_"
ENV_SEP = "__"

DEFAULTS: Dict[str, Any] = {
    "app": {"name": "quality-trace", "log_level": "INFO"},
    "trace": {"direction": "both", "max_depth": 5},
    "attribution": {"min_records": 3, "confidence_floor": 0.6},
}


def _coerce(text: str) -> Any:
    """把字符串转成 bool / int / float / str。"""
    value = text.strip().strip("\"'")
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    for caster in (int, float):
        try:
            return caster(value)
        except ValueError:
            continue
    return value


def _parse_flat(text: str) -> Dict[str, Any]:
    """极简 YAML 解析：支持两层结构（section: 下挂 key: value）。"""
    data: Dict[str, Any] = {}
    section: Optional[str] = None

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        indented = raw[:1] in (" ", "\t")
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()

        if not indented and not value.strip():
            section = key
            data.setdefault(section, {})
            continue

        if indented and section:
            data[section][key] = _coerce(value)
        else:
            section = None
            data[key] = _coerce(value)

    return data


def _merge(base: Dict[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """深合并，override 优先。"""
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            result[key] = _merge(dict(current), value)
        else:
            result[key] = value
    return result


def _apply_env(config: Dict[str, Any], environ: Mapping[str, str]) -> Dict[str, Any]:
    for name, raw in environ.items():
        if not name.startswith(ENV_PREFIX) or ENV_SEP not in name:
            continue
        path = name[len(ENV_PREFIX):].lower().split(ENV_SEP)
        if len(path) != 2:
            continue
        section, key = path
        if isinstance(config.get(section), Mapping):
            config[section][key] = _coerce(raw)
    return config


def load_config(
    path: Optional[Union[str, "os.PathLike[str]"]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """加载配置并合并默认值。

    参数:
        path:    配置文件路径；为 None 时只返回默认值。
        environ: 环境变量映射，默认取 os.environ（便于测试注入）。

    异常:
        FileNotFoundError: path 非空但文件不存在。
    """
    config: Dict[str, Any] = {
        section: dict(values) for section, values in DEFAULTS.items()
    }

    if path is not None:
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))
        text = file_path.read_text(encoding="utf-8")
        loaded = (yaml.safe_load(text) if _HAS_YAML else _parse_flat(text)) or {}
        if not isinstance(loaded, Mapping):
            raise ValueError(f"配置文件顶层必须是映射: {file_path}")
        config = _merge(config, loaded)

    return _apply_env(config, environ if environ is not None else os.environ)
