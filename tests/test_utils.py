"""配置加载测试：默认值、文件合并、环境变量优先级。"""

import pytest

from quality_trace.utils.config import DEFAULTS, load_config


def test_defaults_when_no_path():
    config = load_config(None)
    assert config["trace"]["direction"] == "both"
    assert config["app"]["name"] == "quality-trace"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "not-exist.yaml")


def test_file_overrides_but_keeps_other_defaults(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text("trace:\n  max_depth: 9\n", encoding="utf-8")

    config = load_config(path, environ={})

    assert config["trace"]["max_depth"] == 9
    assert config["trace"]["direction"] == DEFAULTS["trace"]["direction"]


def test_env_overrides_file(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text("app:\n  log_level: INFO\n", encoding="utf-8")

    config = load_config(path, environ={"QT_APP__LOG_LEVEL": "DEBUG"})

    assert config["app"]["log_level"] == "DEBUG"


def test_env_value_is_coerced_to_int():
    config = load_config(None, environ={"QT_TRACE__MAX_DEPTH": "8"})
    assert config["trace"]["max_depth"] == 8


def test_unknown_env_prefix_is_ignored():
    config = load_config(None, environ={"PATH": "x", "QT_ONLYONESEG": "1"})
    assert config["app"]["log_level"] == DEFAULTS["app"]["log_level"]


def test_defaults_are_not_mutated():
    load_config(None, environ={"QT_APP__LOG_LEVEL": "DEBUG"})
    assert DEFAULTS["app"]["log_level"] == "INFO"
