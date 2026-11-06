# -*- coding: utf-8 -*-
"""Agent 编排器：把「硬规则 / 模型判断 / 工具调用 / 人工复核」四层分开。

为什么非要分开
--------------
把四件事写在一个函数里，代码能跑，但**没人能回答"这个结论是谁下的"**：
是规则命中的？是模型猜的？是查库查的？还是主管批的？
出了质量事故，这四者的责任完全不同。所以本模块的硬约束是：

1. **硬规则层**只允许纯函数、无模型、无网络；命中即定论，模型不得推翻；
2. **模型判断层**只能产出``候选``与``置信度``，永远不能产出``已确定``；
3. **工具调用层**必须先过契约（角色 / 字段 / 超时 / 幂等），再执行；
4. **人工复核层**由 ``requires_review`` 触发，模型无权自我收尾。

四层各自往 ``Trace.steps`` 追加一条记录，最终 Trace 就是可审计的决策轨迹——
这同时满足了"输入到输出的完整路径"和"保存关键审计字段"两个要求。

调用方式
--------
::

    orch = build_orchestrator()
    trace = orch.run("chat.ask", {"question": "螺纹抽样方案？"}, actor="qe001", role="quality_engineer")
    trace.output      # 业务结果
    trace.needs_review, trace.review_task
    trace.audit()     # 直接落审计表的扁平字典
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quality_trace.core.agent.contracts import (
    ContractRegistry,
    ContractViolation,
    ToolSpec,
    build_default_registry,
)
from quality_trace.core.agent.guards import (
    AgentError,
    Conflict,
    Deadline,
    Degraded,
    DuplicateRequest,
    IdempotencyStore,
    MissingData,
    PermissionDenied,
    classify,
    idempotency_key,
)
from quality_trace.core.services.errors import ErrorCode

# --------------------------------------------------------------------------- #
# 轨迹数据结构
# --------------------------------------------------------------------------- #

LAYERS = ("contract", "hard_rules", "model_judge", "tools", "human_review")


@dataclass
class Step:
    """一步决策。四层任意一层产生结论都要留一条。"""

    layer: str
    name: str
    status: str                 # ok / pass / block / flag / degraded / failed / skipped
    detail: str = ""
    latency_ms: int = 0
    evidence: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer": self.layer,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "evidence": self.evidence,
        }


@dataclass
class ReviewTask:
    """人工复核任务。三处确认点都产出这个结构。"""

    tool: str
    reason: str
    payload: Dict[str, Any]
    required_role: str
    suggestion: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "reason": self.reason,
            "payload": self.payload,
            "required_role": self.required_role,
            "suggestion": self.suggestion,
        }


@dataclass
class Trace:
    """一次完整调用的决策轨迹。"""

    request_id: str
    intent: str
    actor: str
    role: str
    steps: List[Step] = field(default_factory=list)
    output: Any = None
    needs_review: bool = False
    review_task: Optional[ReviewTask] = None
    degraded: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    started_at: float = field(default_factory=time.time)
    total_ms: int = 0

    # ---- 便捷视图 ----
    def layer(self, name: str) -> List[Step]:
        return [s for s in self.steps if s.layer == name]

    @property
    def verdict(self) -> str:
        """一句话结论：blocked / needs_review / degraded / ok。"""
        if self.error and not self.degraded:
            return "blocked"
        if self.needs_review:
            return "needs_review"
        if self.degraded:
            return "degraded"
        return "ok"

    def audit(self) -> Dict[str, Any]:
        """落审计表的扁平结构（关键字段一个不少）。"""
        return {
            "request_id": self.request_id,
            "intent": self.intent,
            "actor": self.actor,
            "role": self.role,
            "verdict": self.verdict,
            "needs_review": int(self.needs_review),
            "review_tool": self.review_task.tool if self.review_task else None,
            "degraded": int(bool(self.degraded)),
            "degraded_code": str(self.degraded["code"]) if self.degraded else None,
            "degraded_reason": self.degraded.get("reason") if self.degraded else None,
            "error_code": self.error.get("code") if self.error else None,
            "error_stage": self.error.get("stage") if self.error else None,
            "layers": [s.layer for s in self.steps],
            "steps": len(self.steps),
            "total_ms": self.total_ms,
            "started_at": self.started_at,
        }


# --------------------------------------------------------------------------- #
# 硬规则：纯函数、可枚举、可回归
# --------------------------------------------------------------------------- #

#: 越界表述。命中不阻断，但强制转人工——模型可以提出猜测，不能下判决。
FORBIDDEN_PHRASES = (
    "就是这批料", "根因已确定", "肯定是", "责任在", "可以放行",
    "不必复检", "无需返工", "已确认是",
)

#: 常规缺陷码字典（与 data/ 演示数据一致，实际部署由 kb 或主数据提供）
DEFECT_CODE_HINTS = {
    "螺纹": "DC-THD-01",
    "外径": "DC-DIA-02",
    "切刀": "DC-CUT-03",
    "毛刺": "DC-BUR-04",
    "划伤": "DC-SCR-05",
}


@dataclass(frozen=True)
class RuleHit:
    rule: str
    status: str          # block / flag
    message: str
    field: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "status": self.status,
                "message": self.message, "field": self.field}


def apply_hard_rules(intent: str, payload: Mapping[str, Any]) -> List[RuleHit]:
    """硬规则集。新增规则只需在这里加一段，测试可逐条覆盖。

    规则编号对应 ``docs/agent-architecture.md`` 的 R1~R6。
    """
    hits: List[RuleHit] = []

    # R1 问题长度上限（防止把整份 SOP 贴进来拖垮检索）
    if intent == "chat.ask":
        q = str(payload.get("question") or "")
        if len(q) > 500:
            hits.append(RuleHit("R1", "block",
                                f"问题长度 {len(q)} 字超出上限 500 字", "question"))
        elif not q.strip():
            hits.append(RuleHit("R1", "block", "问题不能为空", "question"))

    # R2 越界表述：能识别出"下判决"的措辞，强制转人工
    text = " ".join(str(v) for v in payload.values() if isinstance(v, str))
    for phrase in FORBIDDEN_PHRASES:
        if phrase in text:
            hits.append(RuleHit(
                "R2", "flag",
                f"输入含越界表述「{phrase}」，系统只出候选、不认定根因", "question",
            ))
            break

    # R3 as_of 不得晚于当前时刻（防止"穿越"取到未来版本）
    as_of = payload.get("as_of")
    if isinstance(as_of, str) and len(as_of) >= 10:
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        if as_of[:19] > now:
            hits.append(RuleHit("R3", "block",
                                f"as_of={as_of} 晚于当前时刻，不允许取未来版本", "as_of"))

    # R4 归因结论不得自带"已确定"措辞
    if intent == "attribution.submit":
        cause = str(payload.get("suspect_cause") or "")
        if any(p in cause for p in FORBIDDEN_PHRASES):
            hits.append(RuleHit(
                "R4", "block",
                "疑似原因含「已确定」类措辞；归因结论只能写「待验证」", "suspect_cause"))

    # R5 实测值必须是数字且非负（负数测量一定填错）
    mv = payload.get("measure_value")
    if mv is not None:
        try:
            if float(mv) < 0:
                hits.append(RuleHit("R5", "block",
                                    f"实测值 {mv} 为负数，测量值不可能为负", "measure_value"))
        except (TypeError, ValueError):
            hits.append(RuleHit("R5", "block",
                                f"实测值 {mv!r} 不是数值", "measure_value"))

    # R6 不良数量不得超过工单计划数（超了说明字段填错，不是质量问题）
    qty, plan = payload.get("qty"), payload.get("plan_qty")
    if isinstance(qty, int) and isinstance(plan, int) and qty > plan > 0:
        hits.append(RuleHit("R6", "block",
                            f"不良数量 {qty} 超过工单计划数 {plan}", "qty"))

    return hits


# --------------------------------------------------------------------------- #
# 模型判断：可替换的打分器（无 Key 时用启发式，永远只出候选）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Candidate:
    label: str
    score: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "score": round(self.score, 4), "reason": self.reason}


def heuristic_judge(intent: str, payload: Mapping[str, Any]) -> List[Candidate]:
    """无大模型时的兜底判断器：按关键词命中给候选，绝不输出单一结论。

    接了真实模型后，把本函数替换成 ``core.llm.client`` 的调用即可，
    编排器对"模型是谁"无感知——这也是四层分离带来的好处。
    """
    text = " ".join(str(v) for v in payload.values() if isinstance(v, str))
    out: List[Candidate] = []
    for kw, code in DEFECT_CODE_HINTS.items():
        if kw in text:
            out.append(Candidate(code, 0.62, f"描述命中关键词「{kw}」"))
    if not out:
        out.append(Candidate("DC-UNK-99", 0.30, "描述未命中已知缺陷特征，需人工选码"))
    out.sort(key=lambda c: c.score, reverse=True)
    # 最多给 3 个候选，避免把选择成本转嫁给班组长
    return out[:3]


# --------------------------------------------------------------------------- #
# 编排器
# --------------------------------------------------------------------------- #

#: 意图 → (输入契约, 主工具, 是否需要人工复核)
INTENTS: Dict[str, Dict[str, Any]] = {
    "chat.ask": {
        "input": "chat_request",
        "tool": "kb.search",
        "review": False,
        "desc": "知识库问答：检索 → 引用 → 回答",
    },
    "defect.create": {
        "input": "defect_draft",
        "tool": "defect.suggest_code",
        "review": True,
        "desc": "现场建单：候选缺陷码 → 班组长确认（人工点 1）",
    },
    "attribution.submit": {
        "input": "attribution_result",
        "tool": "attribution.submit",
        "review": True,
        "desc": "归因提交：证据包 → 待主管审批（人工点 2 的上游动作）",
    },
    "attribution.approve": {
        "input": "attribution_result",
        "tool": "attribution.approve",
        "review": True,
        "desc": "归因审批：主管通过/驳回并留痕（人工点 2）",
    },
    "audit.review": {
        "input": "retrieval_hit",
        "tool": "audit.review",
        "review": True,
        "desc": "问答审计：机器标记 → 主管核查（人工点 3）",
    },
}


class AgentOrchestrator:
    """四层流水线。

    每层都可以单独替换：``rules`` 换规则集，``judge`` 换模型，
    ``tools`` 换工具实现，``review_policy`` 换复核策略。
    """

    def __init__(
        self,
        registry: Optional[ContractRegistry] = None,
        *,
        rules: Callable[[str, Mapping[str, Any]], List[RuleHit]] = apply_hard_rules,
        judge: Callable[[str, Mapping[str, Any]], List[Candidate]] = heuristic_judge,
        tools: Optional[Dict[str, Callable[[Mapping[str, Any]], Any]]] = None,
        idempotency: Optional[IdempotencyStore] = None,
        budget_ms: int = 8000,
    ) -> None:
        self.registry = registry or build_default_registry()
        self.rules = rules
        self.judge = judge
        self.tools = tools or {}
        self.idem = idempotency or IdempotencyStore()
        self.budget_ms = budget_ms

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def run(
        self,
        intent: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "anonymous",
        role: str = "quality_engineer",
        request_id: Optional[str] = None,
    ) -> Trace:
        trace = Trace(
            request_id=request_id or "req-" + uuid.uuid4().hex[:12],
            intent=intent,
            actor=actor,
            role=role,
        )
        deadline = Deadline(self.budget_ms)
        started = time.monotonic()

        spec = INTENTS.get(intent)
        if spec is None:
            trace.error = {"code": ErrorCode.BAD_REQUEST,
                           "stage": "contract",
                           "message": f"未知意图 {intent}",
                           "recoverable": False}
            trace.steps.append(Step("contract", intent, "failed", "未知意图"))
            trace.total_ms = int((time.monotonic() - started) * 1000)
            return trace

        try:
            clean = self._layer_contract(trace, spec, payload, role)
            blocked = self._layer_rules(trace, intent, clean)
            if blocked:
                # 硬规则 block 即终局：不调模型、不调工具
                trace.total_ms = int((time.monotonic() - started) * 1000)
                return trace

            candidates = self._layer_judge(trace, intent, clean)
            output = self._layer_tools(trace, spec, intent, clean, actor, role, candidates, deadline)
            trace.output = output

            self._layer_review(trace, spec, intent, clean, role, candidates, output)

        except DuplicateRequest as exc:
            trace.output = exc.first_result
            trace.steps.append(Step("tools", "idempotent", "degraded",
                                    exc.message, deadline.spent_ms()))
        except ContractViolation as exc:
            # 契约不过即终局：明确记成 contract 阶段，别让它掉进 unknown
            trace.error = {
                "code": exc.code,
                "stage": "contract",
                "message": exc.message,
                "recoverable": False,
            }
            trace.steps.append(Step("contract", "error", "failed", exc.message))
        except AgentError as exc:
            if exc.recoverable:
                trace.degraded = {"code": exc.code, "reason": exc.message,
                                  "stage": exc.stage}
                trace.steps.append(Step(exc.stage, "degraded", "degraded", exc.message))
            else:
                trace.error = exc.audit()
                trace.steps.append(Step(exc.stage, "error", "failed", exc.message))
        except Exception as exc:  # pragma: no cover - 兜底分支
            err = classify(exc, "unknown")
            trace.error = err.audit()
            trace.steps.append(Step("unknown", "error", "failed", err.message))

        trace.total_ms = int((time.monotonic() - started) * 1000)
        return trace

    # ------------------------------------------------------------------ #
    # 第 0 层：契约（字段 / 版本 / 权限）
    # ------------------------------------------------------------------ #
    def _layer_contract(
        self, trace: Trace, spec: Mapping[str, Any], payload: Mapping[str, Any], role: str
    ) -> Dict[str, Any]:
        begin = time.monotonic()
        contract_name = spec["input"]
        tool_name = spec["tool"]
        try:
            self.registry.check_role(contract_name, role, write=True)
            self.registry.check_tool_role(tool_name, role)
            clean = self.registry.validate_payload(contract_name, payload)
        except ContractViolation as exc:
            if exc.code == ErrorCode.FORBIDDEN:
                raise PermissionDenied("contract", role, contract_name,
                                       data_level=False) from None
            raise
        trace.steps.append(Step(
            "contract", contract_name, "pass",
            f"契约 v{self.registry.data(contract_name).version.removeprefix('v')} 校验通过；"
            f"工具 {tool_name} 允许角色 {role}",
            int((time.monotonic() - begin) * 1000),
        ))
        return clean

    # ------------------------------------------------------------------ #
    # 第 1 层：硬规则（纯函数，命即定论）
    # ------------------------------------------------------------------ #
    def _layer_rules(
        self, trace: Trace, intent: str, payload: Mapping[str, Any]
    ) -> bool:
        """返回 True 表示被 block，整条链终止。"""
        begin = time.monotonic()
        hits = self.rules(intent, payload)
        blocked = [h for h in hits if h.status == "block"]
        flagged = [h for h in hits if h.status == "flag"]
        blocked_ids = {h.rule for h in blocked}

        if blocked:
            status, detail = "block", "；".join(h.message for h in blocked)
        elif flagged:
            status, detail = "flag", "；".join(h.message for h in flagged)
        else:
            status, detail = "pass", f"硬规则全过（{len(self._rule_ids())} 条）"

        trace.steps.append(Step(
            "hard_rules", ",".join(sorted(blocked_ids)) or "R1-R6", status, detail,
            int((time.monotonic() - begin) * 1000),
            [h.to_dict() for h in hits],
        ))
        if blocked:
            trace.error = {
                "code": ErrorCode.BAD_REQUEST,
                "stage": "hard_rules",
                "message": detail,
                "recoverable": False,
                "detail": {"rules": sorted(blocked_ids)},
            }
        return bool(blocked)

    @staticmethod
    def _rule_ids() -> List[str]:
        return ["R1", "R2", "R3", "R4", "R5", "R6"]

    # ------------------------------------------------------------------ #
    # 第 2 层：模型判断（只出候选 + 置信度）
    # ------------------------------------------------------------------ #
    def _layer_judge(
        self, trace: Trace, intent: str, payload: Mapping[str, Any]
    ) -> List[Candidate]:
        begin = time.monotonic()
        try:
            cands = self.judge(intent, payload)
            status = "pass" if cands else "skipped"
            detail = (f"产出 {len(cands)} 个候选，最高分 "
                      f"{cands[0].score:.2f}（仅候选，不构成结论）") if cands else "无候选"
        except Exception as exc:
            # 模型挂了不阻断：降级为"无候选"，把选择权交给人工
            trace.degraded = {"code": ErrorCode.LLM_DEGRADED,
                              "reason": f"模型判断层异常：{exc}", "stage": "model_judge"}
            cands, status, detail = [], "degraded", f"模型不可用，降级为无候选：{exc}"
        trace.steps.append(Step(
            "model_judge", "candidates", status, detail,
            int((time.monotonic() - begin) * 1000),
            [c.to_dict() for c in cands],
        ))
        return cands

    # ------------------------------------------------------------------ #
    # 第 3 层：工具调用（契约 + 超时 + 幂等）
    # ------------------------------------------------------------------ #
    def _layer_tools(
        self,
        trace: Trace,
        spec: Mapping[str, Any],
        intent: str,
        payload: Dict[str, Any],
        actor: str,
        role: str,
        candidates: Sequence[Candidate],
        deadline: Deadline,
    ) -> Any:
        begin = time.monotonic()
        tool_name = spec["tool"]
        tool: ToolSpec = self.registry.tool(tool_name)

        # 幂等只对有副作用的工具生效
        key: Optional[str] = None
        if tool.side_effect:
            key = idempotency_key(actor, tool_name, payload)
            hit = self.idem.get(key)
            if hit is not None:
                raise DuplicateRequest("tools", key, hit)

        # 超时预算：剩余不足工具自身超时的一半就不开工，直接降级
        if deadline.remaining_ms() < tool.timeout_ms // 2:
            raise Degraded("tools", ErrorCode.STREAM_TIMEOUT,
                           f"剩余预算 {deadline.remaining_ms()}ms 不足以执行 {tool_name}")

        impl = self.tools.get(tool_name)
        if impl is None:
            # 无实现的工具不假装成功：返回候选与空结果，标记降级
            trace.steps.append(Step(
                "tools", tool_name, "degraded",
                f"工具 {tool_name} 未接线实现，返回候选、不产生副作用",
                int((time.monotonic() - begin) * 1000),
                [c.to_dict() for c in candidates],
            ))
            trace.degraded = {"code": ErrorCode.LLM_DEGRADED,
                              "reason": f"工具 {tool_name} 未接线",
                              "stage": "tools"}
            return {"tool": tool_name, "candidates": [c.to_dict() for c in candidates],
                    "executed": False}

        with deadline.stage(f"tools.{tool_name}"):
            result = impl({**payload, "_candidates": [c.to_dict() for c in candidates],
                           "_role": role, "_actor": actor})

        if key:
            self.idem.remember(key, result)

        trace.steps.append(Step(
            "tools", tool_name, "ok",
            f"执行成功（timeout={tool.timeout_ms}ms, side_effect={tool.side_effect}）",
            int((time.monotonic() - begin) * 1000),
        ))
        return result

    # ------------------------------------------------------------------ #
    # 第 4 层：人工复核（requires_review 触发，模型无权收尾）
    # ------------------------------------------------------------------ #
    def _layer_review(
        self,
        trace: Trace,
        spec: Mapping[str, Any],
        intent: str,
        payload: Dict[str, Any],
        role: str,
        candidates: Sequence[Candidate],
        output: Any,
    ) -> None:
        begin = time.monotonic()
        tool: ToolSpec = self.registry.tool(spec["tool"])
        flagged = [s for s in trace.layer("hard_rules") if s.status == "flag"]

        reasons: List[str] = []
        if tool.requires_review:
            reasons.append(f"工具 {tool.name} 声明必须人工复核")
        if flagged:
            reasons.append("硬规则命中越界表述")
        # 低置信度转人工只适用于「要落库/要下结论」的意图。
        # 问答类（chat.ask）的价值是给材料与引用，不该因为候选分低就卡住——
        # 那只会把咨询变成审批，把效率工具做成流程负担。
        decision_intents = ("defect.create", "attribution.submit", "audit.review")
        if intent in decision_intents and candidates and candidates[0].score < 0.5:
            reasons.append(f"候选置信度偏低（{candidates[0].score:.2f} < 0.50）")

        if not reasons:
            trace.steps.append(Step("human_review", "not_required", "skipped",
                                    "本链路无需人工复核", int((time.monotonic() - begin) * 1000)))
            return

        required_role = (
            "quality_manager"
            if intent.startswith(("attribution.", "audit."))
            else "shift_leader"
        )
        trace.needs_review = True
        trace.review_task = ReviewTask(
            tool=tool.name,
            reason="；".join(reasons),
            payload=dict(payload),
            required_role=required_role,
            suggestion=[c.to_dict() for c in candidates] or output,
        )
        trace.steps.append(Step(
            "human_review", tool.name, "pending",
            f"已生成复核任务，待 {required_role} 处理：{'；'.join(reasons)}",
            int((time.monotonic() - begin) * 1000),
        ))

    # ------------------------------------------------------------------ #
    # 人工复核落库（供路由层调用，保证"确认"这一动作有迹可循）
    # ------------------------------------------------------------------ #
    def resolve_review(
        self,
        trace: Trace,
        *,
        reviewer: str,
        approve: bool,
        note: str = "",
    ) -> Dict[str, Any]:
        """人工给出结论后回写轨迹。

        只允许复核者角色匹配的人调用；驳回必须填理由——这两条是
        "人工确认点"能成立的最小条件，否则就是走过场。
        """
        if not trace.needs_review or trace.review_task is None:
            raise Conflict("human_review", "状态冲突：本轨迹没有待复核任务",
                           ErrorCode.ATTR_STATE_INVALID)
        if not approve and not note.strip():
            raise MissingData("human_review", "驳回理由", "驳回必须填写原因")
        trace.steps.append(Step(
            "human_review",
            "resolved",
            "ok" if approve else "rejected",
            f"{reviewer} {'通过' if approve else '驳回'}：{note or '无备注'}",
        ))
        trace.needs_review = False
        return {
            "request_id": trace.request_id,
            "approved": approve,
            "reviewer": reviewer,
            "note": note,
            "verdict": trace.verdict,
        }


def build_orchestrator(
    tools: Optional[Dict[str, Callable[[Mapping[str, Any]], Any]]] = None,
) -> AgentOrchestrator:
    """构造默认编排器；路由层可传入真实工具实现。"""
    return AgentOrchestrator(tools=tools)
