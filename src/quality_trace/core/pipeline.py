"""核心流水线（骨架）。

当前版本只做配置校验与占位输出，真实的追溯查询与归因逻辑待实现。

规划中的四层归因证据链（见 docs/design.md）：
    1. 规则命中    —— SPC 判异、参数越界（硬证据）
    2. 统计相关    —— 同批次 / 同设备 / 同时段的集中度（软证据）
    3. 因果假设    —— 需实验验证，输出时必须标注「待验证」
    4. 人工确认    —— 记入 8D 报告，由人签字
本模块只负责到第 3 层，绝不输出「根因已确定」。
"""

from __future__ import annotations

from typing import Any, Dict, List

VALID_DIRECTIONS = ("forward", "backward", "both")


def check_config(config: Dict[str, Any]) -> List[str]:
    """返回配置问题清单；空列表代表通过。"""
    problems: List[str] = []

    trace = config.get("trace") or {}
    direction = trace.get("direction")
    if direction not in VALID_DIRECTIONS:
        problems.append(f"trace.direction 必须是 {VALID_DIRECTIONS} 之一，当前为 {direction!r}")

    max_depth = trace.get("max_depth")
    if not isinstance(max_depth, int) or max_depth < 1:
        problems.append(f"trace.max_depth 必须是正整数，当前为 {max_depth!r}")

    attribution = config.get("attribution") or {}
    min_records = attribution.get("min_records")
    if not isinstance(min_records, int) or min_records < 1:
        problems.append(f"attribution.min_records 必须是正整数，当前为 {min_records!r}")

    floor = attribution.get("confidence_floor")
    if not isinstance(floor, (int, float)) or not 0 <= floor <= 1:
        problems.append(f"attribution.confidence_floor 必须落在 [0, 1]，当前为 {floor!r}")

    return problems


def run(config: Dict[str, Any], verbose: bool = False) -> Dict[str, Any]:
    """执行流水线，返回结构化摘要。

    返回:
        {"status": "ready" | "invalid", "checks": [str, ...]}
    """
    checks: List[str] = []
    problems = check_config(config)

    if problems:
        status = "invalid"
        checks.extend(f"[配置] {item}" for item in problems)
    else:
        status = "ready"
        checks.append("[配置] 校验通过")
        checks.append("[追溯] 数据源未接入，跳过")
        checks.append("[归因] 规则库为空，跳过")

    if verbose:
        checks.append(f"[调试] 顶层配置键: {sorted(config.keys())}")

    return {"status": status, "checks": checks}
