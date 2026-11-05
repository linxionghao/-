"""批次 1c 对话审计测试：三条越界规则 + 单条检测 + 全量补扫。

设计要点：
- `detect_flags` 是纯函数，测试无需数据库，覆盖全部正则分支与边界；
- `flag_message` / `rescan_all` 走临时库，验证「落库 + 不覆盖人工核查结论」；
- 规则二只对 complete 消息生效——partial 不算无引用作答，这是审计口径的关键。
"""

from __future__ import annotations

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services import audit


# ---------------------------------------------------------------------------
# 规则一：根因结论表述（系统只允许 L3「因果假设（待验证）」）
# ---------------------------------------------------------------------------
class TestRuleRootCause:
    """覆盖 5 组正则：已确定 / 就是 / 根本原因是 / 可以确认 / 无需验证。"""

    @pytest.mark.parametrize("text", [
        "根因已确定：切刀磨损。",              # 根因(已)?(确定|查明|找到|锁定)
        "根因查明为对刀误差。",
        "根因就是冷却液浓度偏低。",
        "根因是夹具定位面磨损。",
        "根本原因就是来料硬度超差。",
        "根本原因为刀具寿命到期。",
        "可以确认本次问题的根因在热处理工序。",
        "根因无需进一步验证。",
        "根本原因不必再做验证。",
    ])
    def test_hits_when_conclusive(self, text):
        """任何宣称根因已定的表述都必须命中规则一。"""
        flags = audit.detect_flags(text, "complete", [], set())
        assert flags["root_cause"] is True

    @pytest.mark.parametrize("text", [
        "疑似根因：切刀磨损（待验证）。",       # 合规表述：L3 假设
        "初步判断可能与刀具寿命相关，需进一步验证。",
        "以下为待验证假设，不构成根因结论。",
        "本次不讨论根因，仅说明围堵措施。",
    ])
    def test_misses_when_hypothesis(self, text):
        """「疑似 / 待验证」表述不得误报——误报会让审计失去可信度。"""
        flags = audit.detect_flags(text, "complete", [], set())
        assert flags["root_cause"] is False

    def test_empty_content_safe(self):
        """空正文不抛异常、不命中（LLM 返回空时走降级，不该再叠一条越界）。"""
        assert audit.detect_flags("", "complete", [], set())["root_cause"] is False
        assert audit.detect_flags(None, "complete", [], set())["root_cause"] is False
