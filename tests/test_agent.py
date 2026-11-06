# -*- coding: utf-8 -*-
"""Agent 层测试：契约、四层分离、六类异常、三处人工复核点。

重点不是覆盖率，而是**把"谁下的结论"钉死**：
硬规则能一票否决模型、模型只能出候选、工具必须先过契约、
requires_review 的工具不得由模型收尾。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping

import pytest

from quality_trace.core.agent import (
    AgentOrchestrator,
    Conflict,
    ContractRegistry,
    ContractViolation,
    DataContract,
    Deadline,
    DuplicateRequest,
    FieldSpec,
    IdempotencyStore,
    MissingData,
    PermissionDenied,
    TimeoutError_,
    ToolSpec,
    build_default_registry,
    build_orchestrator,
    idempotency_key,
)
from quality_trace.core.agent.orchestrator import apply_hard_rules, heuristic_judge
from quality_trace.core.services.errors import ApiError, ErrorCode


# --------------------------------------------------------------------------- #
# 契约层
# --------------------------------------------------------------------------- #

def test_default_registry_has_three_review_points():
    """三处人工确认点必须由 requires_review 显式声明，而不是散在各路由的 if 里。

    归因这一处由「提交 + 审批」两个工具组成（提交产生待审任务，审批给出结论），
    所以 requires_review 的工具是 4 个，但确认点仍然是 3 处。
    """
    reg = build_default_registry()
    review_tools = {t.name for t in reg.tools() if t.requires_review}
    assert review_tools == {
        "defect.suggest_code",   # 人工点 1：班组长确认缺陷码
        "attribution.submit",    # 人工点 2 上游：提交待审
        "attribution.approve",   # 人工点 2：主管审批归因
        "audit.review",          # 人工点 3：主管核查问答
    }
    # 确认点按业务归类后必须正好 3 处
    points = {
        "缺陷码确认": {"defect.suggest_code"},
        "归因审批": {"attribution.submit", "attribution.approve"},
        "问答审计核查": {"audit.review"},
    }
    assert len(points) == 3


def test_attribution_approve_is_manager_only():
    """审批是主管专属：质量工程师不得自行批准自己的归因结论。"""
    reg = build_default_registry()
    with pytest.raises(ContractViolation):
        reg.check_tool_role("attribution.approve", "quality_engineer")


def test_registry_rejects_unregistered_input_contract():
    reg = ContractRegistry()
    with pytest.raises(ContractViolation, match="未注册的输入契约"):
        reg.register_tool(ToolSpec(
            name="x", purpose="p", input_contract="nope", output_contract="nope",
            allowed_roles=("quality_manager",),
        ))


def test_validate_payload_requires_fields():
    reg = build_default_registry()
    result = reg.validate_payload("chat_request", {"question": "抽样方案？"})
    assert result == {"question": "抽样方案？"}


def test_validate_payload_rejects_missing_required():
    reg = build_default_registry()
    with pytest.raises(ContractViolation, match="缺少必填字段 question"):
        reg.validate_payload("chat_request", {})


def test_validate_payload_rejects_unknown_field():
    """未知字段必须报错：静默丢弃会让调用方以为传进去了。"""
    reg = build_default_registry()
    with pytest.raises(ContractViolation, match="不接受字段"):
        reg.validate_payload("chat_request", {"question": "q", "foo": 1})


def test_int_field_rejects_float_string():
    """'12.7' 不能悄悄变成 12，宁可报错。"""
    reg = build_default_registry()
    with pytest.raises(ContractViolation, match="需要整数"):
        reg.validate_payload("defect_draft", {
            "work_order_id": "WO-1", "defect_desc": "外径偏小",
            "line_id": "line-01", "qty": "12.7",
        })


def test_int_field_accepts_numeric_string():
    reg = build_default_registry()
    out = reg.validate_payload("defect_draft", {
        "work_order_id": "WO-1", "defect_desc": "外径偏小",
        "line_id": "line-01", "qty": "12",
    })
    assert out["qty"] == 12


def test_enum_field_rejects_bad_value():
    reg = build_default_registry()
    with pytest.raises(ContractViolation, match="不在允许范围"):
        reg.validate_payload("attribution_result", {
            "defect_no": "D-1", "suspect_cause": "切刀磨损",
            "evidence": [], "confidence": 0.7, "status": "confirmed",
        })


def test_reader_role_denied():
    reg = build_default_registry()
    with pytest.raises(ContractViolation) as exc:
        reg.check_role("attribution_result", "shift_leader")
    assert exc.value.code == ErrorCode.FORBIDDEN


def test_writer_role_denied_for_defect_draft():
    """只有班组长能建单；质量工程师只能读。"""
    reg = build_default_registry()
    reg.check_role("defect_draft", "quality_engineer", write=False)
    with pytest.raises(ContractViolation):
        reg.check_role("defect_draft", "quality_engineer", write=True)


def test_tool_role_denied_for_attribution_approve():
    reg = build_default_registry()
    with pytest.raises(ContractViolation, match="不允许角色"):
        reg.check_tool_role("attribution.approve", "quality_engineer")


# --------------------------------------------------------------------------- #
# 硬规则
# --------------------------------------------------------------------------- #

def test_rule_r1_blocks_long_question():
    hits = apply_hard_rules("chat.ask", {"question": "螺" * 501})
    assert [h.rule for h in hits] == ["R1"]
    assert hits[0].status == "block"


def test_rule_r1_blocks_empty_question():
    hits = apply_hard_rules("chat.ask", {"question": "   "})
    assert any(h.rule == "R1" and h.status == "block" for h in hits)


def test_rule_r2_flags_verdict_language():
    """越界表述只 flag 不 block：仍给材料，但强制转人工。"""
    hits = apply_hard_rules("chat.ask", {"question": "就是这批料的问题吧？"})
    assert len(hits) == 1
    assert hits[0].rule == "R2" and hits[0].status == "flag"


def test_rule_r3_blocks_future_as_of():
    hits = apply_hard_rules("chat.ask", {"question": "抽样？", "as_of": "2099-01-01T00:00:00"})
    assert any(h.rule == "R3" and h.status == "block" for h in hits)


def test_rule_r4_blocks_determined_cause():
    hits = apply_hard_rules("attribution.submit", {"suspect_cause": "根因已确定是切刀"})
    assert any(h.rule == "R4" and h.status == "block" for h in hits)


def test_rule_r5_blocks_negative_measure():
    hits = apply_hard_rules("defect.create", {"measure_value": -1.2})
    assert any(h.rule == "R5" and h.status == "block" for h in hits)


def test_rule_r6_blocks_qty_over_plan():
    hits = apply_hard_rules("defect.create", {"qty": 6000, "plan_qty": 5000})
    assert any(h.rule == "R6" and h.status == "block" for h in hits)


# --------------------------------------------------------------------------- #
# 模型判断层
# --------------------------------------------------------------------------- #

def test_judge_returns_candidates_not_verdict():
    cands = heuristic_judge("chat.ask", {"question": "螺纹外径偏小"})
    assert cands
    assert all(0.0 <= c.score <= 1.0 for c in cands)
    assert len(cands) <= 3, "候选超过 3 个等于把选择成本推给人"
    # 只出候选，不带任何"已确定"语义
    assert all("确定" not in c.reason for c in cands)


def test_judge_falls_back_when_no_keyword():
    cands = heuristic_judge("chat.ask", {"question": "今天天气如何"})
    assert cands[0].label == "DC-UNK-99"
    assert cands[0].score < 0.5


# --------------------------------------------------------------------------- #
# 编排器：四层顺序与短路
# --------------------------------------------------------------------------- #

def _tools_ok() -> Dict[str, Any]:
    return {
        "kb.search": lambda p: {"tool": "kb.search", "hits": 2,
                                "answer": "按 GB/T 2828.1 一般检验水平 II 抽样"},
        "defect.suggest_code": lambda p: {"tool": "defect.suggest_code",
                                          "draft": p.get("defect_desc")},
        "attribution.submit": lambda p: {"tool": "attribution.submit", "status": "submitted"},
        "attribution.approve": lambda p: {"tool": "attribution.approve", "status": "approved"},
        "audit.review": lambda p: {"tool": "audit.review", "reviewed": 1},
    }


def test_happy_path_layers_in_order():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("chat.ask", {"question": "螺纹外径的抽样方案？"},
                 actor="qe001", role="quality_engineer")
    assert t.verdict == "ok"
    assert [s.layer for s in t.steps] == [
        "contract", "hard_rules", "model_judge", "tools", "human_review",
    ]
    assert t.output["answer"].startswith("按 GB/T")


def test_hard_rule_block_short_circuits_model_and_tools():
    """硬规则一票否决：模型和工具都不该被调用。"""
    called: List[str] = []

    def spy_judge(intent, payload):
        called.append("judge")
        return []

    def spy_tool(payload):
        called.append("tool")
        return {}

    orch = AgentOrchestrator(
        tools={"kb.search": spy_tool},
        judge=spy_judge,
    )
    t = orch.run("chat.ask", {"question": "螺" * 501},
                 actor="qe001", role="quality_engineer")
    assert t.verdict == "blocked"
    assert called == [], "被 block 后不得再调模型或工具"
    assert t.error["stage"] == "hard_rules"


def test_contract_violation_stops_before_rules():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("chat.ask", {"question": "q", "unknown_field": 1},
                 actor="qe001", role="quality_engineer")
    assert t.verdict == "blocked"
    assert t.error["stage"] == "contract"
    assert [s.layer for s in t.steps] == ["contract"]


def test_permission_denied_maps_to_403():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("attribution.submit", {
        "defect_no": "D-1", "suspect_cause": "切刀磨损待验证",
        "evidence": [], "confidence": 0.7, "status": "submitted",
    }, actor="leader01", role="shift_leader")
    assert t.verdict == "blocked"
    assert t.error["code"] == ErrorCode.FORBIDDEN


def test_unknown_intent_rejected():
    orch = build_orchestrator()
    t = orch.run("nope.nope", {}, actor="x", role="quality_manager")
    assert t.verdict == "blocked"
    assert "未知意图" in t.error["message"]


# --------------------------------------------------------------------------- #
# 人工复核：三处确认点
# --------------------------------------------------------------------------- #

def test_defect_create_requires_review_by_shift_leader():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("defect.create", {
        "work_order_id": "WO-20261030-001", "defect_desc": "螺纹外径偏小",
        "line_id": "line-01", "qty": 30,
    }, actor="leader01", role="shift_leader")
    assert t.needs_review is True
    assert t.review_task.required_role == "shift_leader"
    assert t.review_task.tool == "defect.suggest_code"
    # 候选只作建议，不写入
    assert t.review_task.suggestion


def test_attribution_requires_quality_manager():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("attribution.submit", {
        "defect_no": "D-1", "suspect_cause": "切刀磨损待验证",
        "evidence": [{"src": "meas_record", "id": 12}],
        "confidence": 0.72, "status": "submitted",
    }, actor="qe001", role="quality_engineer")
    assert t.needs_review is True
    assert t.review_task.required_role == "quality_manager"


def test_audit_review_requires_manager():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("audit.review", {
        "seq": 1, "source_file": "规范v2", "version": "v2", "chunk_id": "2",
    }, actor="manager01", role="quality_manager")
    assert t.needs_review is True
    assert t.review_task.required_role == "quality_manager"


def test_chat_with_verdict_language_forced_to_review():
    """越界表述会让本该自动完成的问答转人工，这是边界设计的核心。"""
    orch = build_orchestrator(_tools_ok())
    t = orch.run("chat.ask", {"question": "就是这批料的问题，怎么处理？"},
                 actor="qe001", role="quality_engineer")
    assert t.needs_review is True
    assert "越界表述" in t.review_task.reason


def test_resolve_review_requires_note_on_reject():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("defect.create", {
        "work_order_id": "WO-1", "defect_desc": "外径", "line_id": "line-01", "qty": 1,
    }, actor="leader01", role="shift_leader")
    with pytest.raises(MissingData, match="驳回理由"):
        orch.resolve_review(t, reviewer="leader01", approve=False, note="")
    out = orch.resolve_review(t, reviewer="leader01", approve=True, note="已核对实物")
    assert out["approved"] is True
    assert t.needs_review is False


def test_resolve_review_without_task_conflicts():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("chat.ask", {"question": "抽样方案？"},
                 actor="qe001", role="quality_engineer")
    with pytest.raises(Conflict):
        orch.resolve_review(t, reviewer="qe001", approve=True)


# --------------------------------------------------------------------------- #
# 六类异常
# --------------------------------------------------------------------------- #

def test_timeout_guard_raises_when_budget_exceeded():
    d = Deadline(budget_ms=0)
    time.sleep(0.002)
    with pytest.raises(TimeoutError_):
        d.check("tools")


def test_timeout_is_recoverable():
    e = TimeoutError_("tools", 3000, 3200)
    assert e.recoverable is True
    assert e.audit()["code"] == ErrorCode.STREAM_TIMEOUT


def test_insufficient_budget_degrades_instead_of_running():
    """预算不够时不硬跑工具，直接降级并说明原因。"""
    orch = AgentOrchestrator(tools=_tools_ok(), budget_ms=1)
    time.sleep(0.002)
    t = orch.run("chat.ask", {"question": "抽样？"}, actor="qe001",
                 role="quality_engineer")
    assert t.degraded is not None
    assert t.degraded["stage"] == "tools"


def test_duplicate_request_returns_first_result():
    """有副作用工具的重复请求必须返回首次结果，不能执行两次。"""
    calls: List[str] = []

    def slow_tool(payload):
        calls.append("run")
        return {"approved": True, "n": len(calls)}

    orch = AgentOrchestrator(
        tools={"attribution.submit": slow_tool},
        idempotency=IdempotencyStore(),
    )
    payload = {
        "defect_no": "D-9", "suspect_cause": "切刀磨损待验证",
        "evidence": [], "confidence": 0.8, "status": "submitted",
    }
    t1 = orch.run("attribution.submit", payload, actor="qe001", role="quality_engineer")
    t2 = orch.run("attribution.submit", payload, actor="qe001", role="quality_engineer")
    assert len(calls) == 1, "第二次不得真正执行"
    assert t2.output == t1.output


def test_idempotency_key_stable_regardless_of_order():
    a = idempotency_key("u1", "t", {"x": 1, "y": 2})
    b = idempotency_key("u1", "t", {"y": 2, "x": 1})
    assert a == b
    assert idempotency_key("u2", "t", {"x": 1, "y": 2}) != a


def test_missing_data_reports_what_is_missing():
    e = MissingData("hard_rules", "实测值", "请补录或标记免检")
    assert "实测值" in e.message
    assert e.recoverable is False


def test_judge_exception_degrades_not_fails():
    """模型挂了不算失败：降级为无候选，把选择权交回人工。"""
    def boom(intent, payload):
        raise RuntimeError("模型超时")

    orch = AgentOrchestrator(judge=boom, tools=_tools_ok())
    t = orch.run("chat.ask", {"question": "抽样方案？"},
                 actor="qe001", role="quality_engineer")
    assert t.degraded is not None
    assert t.degraded["code"] == ErrorCode.LLM_DEGRADED
    assert t.output is not None, "降级仍须产出可用结果"


def test_unauthorized_role_blocked_at_contract_layer():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("audit.review", {
        "seq": 1, "source_file": "f", "version": "v2", "chunk_id": "1",
    }, actor="leader01", role="shift_leader")
    assert t.verdict == "blocked"
    assert t.error["stage"] == "contract"


# --------------------------------------------------------------------------- #
# 审计字段
# --------------------------------------------------------------------------- #

def test_audit_contains_all_key_fields():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("chat.ask", {"question": "抽样方案？"},
                 actor="qe001", role="quality_engineer", request_id="req-fixed")
    a = t.audit()
    for key in ("request_id", "intent", "actor", "role", "verdict",
                "needs_review", "degraded", "degraded_code",
                "error_code", "layers", "total_ms", "started_at"):
        assert key in a, f"审计缺字段 {key}"
    assert a["request_id"] == "req-fixed"
    assert a["layers"] == ["contract", "hard_rules", "model_judge", "tools", "human_review"]


def test_trace_verdict_priorities():
    orch = build_orchestrator(_tools_ok())
    ok = orch.run("chat.ask", {"question": "抽样方案？"}, actor="a", role="quality_engineer")
    assert ok.verdict == "ok"
    blocked = orch.run("chat.ask", {"question": ""}, actor="a", role="quality_engineer")
    assert blocked.verdict == "blocked"
    review = orch.run("defect.create", {
        "work_order_id": "W", "defect_desc": "外径", "line_id": "line-01", "qty": 1,
    }, actor="a", role="shift_leader")
    assert review.verdict == "needs_review"


def test_step_evidence_carried_into_trace():
    orch = build_orchestrator(_tools_ok())
    t = orch.run("chat.ask", {"question": "螺纹外径偏小"}, actor="a", role="quality_engineer")
    judge = t.layer("model_judge")[0]
    assert judge.evidence and judge.evidence[0]["label"].startswith("DC-")
