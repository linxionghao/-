# -*- coding: utf-8 -*-
"""异常与降级防线。

用户在需求里点名六类路径，本模块逐一落地——不是为了写六个类，而是为了让
**每类异常都有确定的错误码、确定的审计字段、确定的用户可见文案**：

===============  ============  ===========================================
路径             错误码        处理方式
===============  ============  ===========================================
超时             50401         记 ``timeout``，返回已完成部分的证据，不空转
缺数据           404xx/40001   指出缺哪个字段，进人工补录队列，不猜
冲突             409xx         版本/状态冲突一律拒绝，不覆盖，不静默取后者
越权             403xx         数据级越权 40302，路由级 40301，均记审计
重复请求         40902         幂等键去重，第二次返回首次结果并标 ``duplicate``
降级             50001/50002   不算失败：产出降级产物 + ``degraded`` 标记
===============  ============  ===========================================

统一原则：**降级不等于失败，但降级必须被看见**。所以每个异常都携带
``stage`` 与 ``audit`` 两个字段，直接并入 Agent 的决策轨迹。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from quality_trace.core.services.errors import ApiError, ErrorCode


# --------------------------------------------------------------------------- #
# 六类异常的基类与子类
# --------------------------------------------------------------------------- #

class AgentError(ApiError):
    """Agent 链路上的异常基类：带阶段、可审计、可降级。

    :param code: 错误码，沿用 ``ErrorCode``
    :param message: 用户可见中文
    :param stage: 出错阶段，写进审计后能定位到是哪一层炸的
    :param recoverable: 是否可降级继续（True 时编排器不会中断整条链）
    """

    def __init__(
        self,
        code: int,
        message: str,
        *,
        stage: str = "unknown",
        recoverable: bool = False,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(code, message)
        self.stage = stage
        self.recoverable = recoverable
        self.detail = detail or {}

    def audit(self) -> Dict[str, Any]:
        """写进审计记录的最小集合。"""
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "recoverable": self.recoverable,
            "detail": self.detail,
        }


class TimeoutError_(AgentError):
    """超时：单工具或整条链超预算。"""

    def __init__(self, stage: str, budget_ms: int, spent_ms: int) -> None:
        super().__init__(
            ErrorCode.STREAM_TIMEOUT,
            f"{stage} 阶段超出时间预算（{spent_ms}ms / {budget_ms}ms），"
            "已返回当前可用结果",
            stage=stage,
            recoverable=True,
            detail={"budget_ms": budget_ms, "spent_ms": spent_ms},
        )


class MissingData(AgentError):
    """缺数据：必填字段缺失或检索为空。不猜、不编，转人工补录。"""

    def __init__(self, stage: str, what: str, hint: str = "") -> None:
        msg = f"{stage} 阶段缺少{what}"
        if hint:
            msg += f"（{hint}）"
        super().__init__(
            ErrorCode.BAD_REQUEST,
            msg,
            stage=stage,
            recoverable=False,
            detail={"missing": what, "hint": hint},
        )


class Conflict(AgentError):
    """冲突：版本冲突 / 状态机冲突 / 同一单据被并发改写。"""

    def __init__(self, stage: str, what: str, code: int = ErrorCode.DOC_VERSION_EXISTS) -> None:
        super().__init__(
            code,
            f"{stage} 阶段发生{what}，已拒绝本次写入（不覆盖、不取后者）",
            stage=stage,
            recoverable=False,
            detail={"conflict": what},
        )


class PermissionDenied(AgentError):
    """越权：路由级 40301 / 数据级 40302。"""

    def __init__(
        self,
        stage: str,
        role: str,
        what: str,
        *,
        data_level: bool = False,
    ) -> None:
        code = ErrorCode.LINE_FORBIDDEN if data_level else ErrorCode.FORBIDDEN
        kind = "数据级" if data_level else "路由级"
        super().__init__(
            code,
            f"{stage} 阶段{kind}越权：角色 {role} 不能访问{what}",
            stage=stage,
            recoverable=False,
            detail={"role": role, "target": what, "data_level": data_level},
        )


class DuplicateRequest(AgentError):
    """重复请求：幂等键命中，返回首次结果并标记。"""

    def __init__(self, stage: str, key: str, first_result: Any = None) -> None:
        super().__init__(
            ErrorCode.STREAM_BUSY,
            f"{stage} 阶段检测到重复请求（幂等键 {key}），已返回首次结果",
            stage=stage,
            recoverable=True,
            detail={"idempotency_key": key},
        )
        self.first_result = first_result


class Degraded(AgentError):
    """降级：能力受损但结论仍可用（无 Key、向量服务不可用等）。"""

    def __init__(self, stage: str, code: int, reason: str) -> None:
        super().__init__(
            code,
            f"{stage} 阶段降级：{reason}",
            stage=stage,
            recoverable=True,
            detail={"reason": reason},
        )


# --------------------------------------------------------------------------- #
# 超时预算
# --------------------------------------------------------------------------- #

@dataclass
class Deadline:
    """一条链的总预算，按阶段扣减。

    不用 ``signal.alarm``：Windows 上没有 SIGALRM，且流式响应需要跨线程。
    用"进入时校验、超了就不开工"的协作式预算，确定性更好，也好测。
    """

    budget_ms: int
    started: float = field(default_factory=time.monotonic)
    marks: List[Dict[str, Any]] = field(default_factory=list)

    def spent_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def remaining_ms(self) -> int:
        return max(self.budget_ms - self.spent_ms(), 0)

    def check(self, stage: str) -> None:
        if self.spent_ms() > self.budget_ms:
            raise TimeoutError_(stage, self.budget_ms, self.spent_ms())

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """记录一个阶段的耗时；超预算时在**进入前**就抛，避免白干活。"""
        self.check(name)
        begin = time.monotonic()
        try:
            yield
        finally:
            self.marks.append({
                "stage": name,
                "elapsed_ms": int((time.monotonic() - begin) * 1000),
                "cumulative_ms": self.spent_ms(),
            })


# --------------------------------------------------------------------------- #
# 幂等
# --------------------------------------------------------------------------- #

def idempotency_key(actor: str, tool: str, payload: Dict[str, Any]) -> str:
    """由「调用者 + 工具 + 规范化入参」生成幂等键。

    只对**有副作用**的工具用；查询类重复执行无代价，不必拦。
    """
    import hashlib
    import json

    norm = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    raw = f"{actor}|{tool}|{norm}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class IdempotencyStore:
    """进程内幂等表（MVP 够用；多实例部署时换成 Redis 同接口即可）。"""

    def __init__(self, ttl_seconds: int = 600) -> None:
        self._ttl = ttl_seconds
        self._rows: Dict[str, Dict[str, Any]] = {}

    def _alive(self, key: str) -> bool:
        row = self._rows.get(key)
        if row is None:
            return False
        if time.monotonic() - row["at"] > self._ttl:
            self._rows.pop(key, None)
            return False
        return True

    def get(self, key: str) -> Optional[Any]:
        return self._rows[key]["result"] if self._alive(key) else None

    def remember(self, key: str, result: Any) -> None:
        self._rows[key] = {"at": time.monotonic(), "result": result}

    def size(self) -> int:
        return sum(1 for k in list(self._rows) if self._alive(k))


# --------------------------------------------------------------------------- #
# 兜底：把任意异常翻译成可审计的 AgentError
# --------------------------------------------------------------------------- #

def classify(exc: BaseException, stage: str) -> AgentError:
    """把未预期异常归到六类中最近的一类，保证不裸奔到 500。"""
    if isinstance(exc, AgentError):
        return exc
    if isinstance(exc, ApiError):
        return AgentError(
            exc.code, exc.message, stage=stage,
            recoverable=str(exc.code).startswith("5"),
        )
    if isinstance(exc, (TimeoutError,)):
        return TimeoutError_(stage, 0, 0)
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return MissingData(stage, str(exc))
    if isinstance(exc, PermissionError):
        return PermissionDenied(stage, "unknown", str(exc))
    return AgentError(
        ErrorCode.INTERNAL_ERROR,
        f"{stage} 阶段内部错误：{exc}",
        stage=stage,
    )
