# -*- coding: utf-8 -*-
"""Agent 层：契约 + 编排 + 异常防线。

对外只需要三样东西：

- :class:`~quality_trace.core.agent.orchestrator.AgentOrchestrator`
- :func:`~quality_trace.core.agent.orchestrator.build_orchestrator`
- :func:`~quality_trace.core.agent.contracts.build_default_registry`
"""

from quality_trace.core.agent.contracts import (
    ContractRegistry,
    ContractViolation,
    DataContract,
    FieldSpec,
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
    TimeoutError_,
    classify,
    idempotency_key,
)
from quality_trace.core.agent.orchestrator import (
    INTENTS,
    AgentOrchestrator,
    Candidate,
    ReviewTask,
    Step,
    Trace,
    apply_hard_rules,
    build_orchestrator,
    heuristic_judge,
)

__all__ = [
    "INTENTS",
    "AgentError",
    "AgentOrchestrator",
    "Candidate",
    "Conflict",
    "ContractRegistry",
    "ContractViolation",
    "DataContract",
    "Deadline",
    "Degraded",
    "DuplicateRequest",
    "FieldSpec",
    "IdempotencyStore",
    "MissingData",
    "PermissionDenied",
    "ReviewTask",
    "Step",
    "TimeoutError_",
    "ToolSpec",
    "Trace",
    "apply_hard_rules",
    "build_default_registry",
    "build_orchestrator",
    "classify",
    "heuristic_judge",
    "idempotency_key",
]
