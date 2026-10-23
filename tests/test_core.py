"""核心流水线与归因边界测试。"""

from quality_trace import __version__
from quality_trace.core.pipeline import check_config, run
from quality_trace.utils.config import DEFAULTS


def test_version_is_three_part_number():
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_defaults_are_valid():
    assert check_config(DEFAULTS) == []


def test_invalid_direction_is_reported():
    problems = check_config({"trace": {"direction": "sideways", "max_depth": 3}})
    assert any("direction" in item for item in problems)


def test_invalid_depth_is_reported():
    problems = check_config({"trace": {"direction": "both", "max_depth": 0}})
    assert any("max_depth" in item for item in problems)


def test_confidence_floor_out_of_range_is_reported():
    problems = check_config({"attribution": {"min_records": 1, "confidence_floor": 1.5}})
    assert any("confidence_floor" in item for item in problems)


def test_run_ready_without_claiming_root_cause():
    result = run(DEFAULTS, verbose=True)
    assert result["status"] == "ready"
    joined = " ".join(result["checks"])
    # 骨架阶段不接数据源，必须显式说明"跳过"，且不得直接断言根因
    assert "跳过" in joined
    assert "根因" not in joined


def test_run_invalid_config():
    result = run({"trace": {"direction": "nope", "max_depth": 1}, "attribution": {}})
    assert result["status"] == "invalid"
    assert all(item.startswith("[配置]") for item in result["checks"])
