# -*- coding: utf-8 -*-
"""数据契约与工具契约。

为什么要单独一层
----------------
MVP 最容易失控的地方不是模型答错，而是**同一个字段在不同环节含义不同**：
质检员填的 ``unit`` 是 mm 而标准里是 μm，班组长传的 ``line_id`` 是自己的产线
而调用方期望全厂，工具被质量工程师调用时带了主管才有的权限位。
这些都是契约问题，不是模型问题，只能靠显式声明 + 入口校验拦住。

本模块只做三件事：

1. ``FieldSpec`` / ``DataContract`` —— 声明一个数据集有哪些字段、什么类型、
   什么单位、当前版本、谁可以读写；
2. ``ToolSpec`` —— 声明一个工具能做什么、需要什么输入、产出什么、
   允许哪些角色调用、超时多少、是否有副作用、是否必须人工复核；
3. ``validate_payload`` / ``check_role`` / ``check_version`` —— 在编排入口
   把契约落到实际请求上，不满足即抛契约错误，不进入后续任何层。

设计约束
--------
- 纯函数 + 不可变数据，不碰数据库、不碰网络，方便单元测试；
- 版本号用 ``v1`` / ``v2`` 这类短串，比较时按 ``as_of`` 取当时有效版本；
- 权限判定复用 ``core/services/auth`` 的角色常量，不另造一套。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from quality_trace.core.services.errors import ApiError, ErrorCode

# --------------------------------------------------------------------------- #
# 字段与数据集契约
# --------------------------------------------------------------------------- #

#: 允许的字段类型（够用即可，不做完整 JSON Schema）
DTYPES = ("str", "int", "float", "bool", "list", "dict", "datetime")


@dataclass(frozen=True)
class FieldSpec:
    """一个字段的契约。

    :param name: 字段名（蛇形，与数据库列名一致）
    :param dtype: 类型，取值见 :data:`DTYPES`
    :param required: 是否必填
    :param unit: 计量单位；无量纲字段留空。单位不一致是最常见的口径事故来源
    :param allowed: 枚举白名单；非空时取值必须落在其中
    :param desc: 中文说明，直接用于文档与错误提示
    """

    name: str
    dtype: str
    required: bool = True
    unit: Optional[str] = None
    allowed: Optional[Tuple[str, ...]] = None
    desc: str = ""

    def __post_init__(self) -> None:
        if self.dtype not in DTYPES:
            raise ValueError(
                f"字段 {self.name} 的类型 {self.dtype!r} 不在 {DTYPES} 之内"
            )


@dataclass(frozen=True)
class DataContract:
    """一个数据集的契约：字段集合 + 版本 + 读写角色。

    ``version`` 用于 ``as_of`` 取版本；``min_reader_role`` 表示最低可读角色，
    越权读写一律在入口拒绝，不靠下游"看不见就当没有"。
    """

    name: str
    version: str
    fields: Tuple[FieldSpec, ...]
    reader_roles: Tuple[str, ...]
    writer_roles: Tuple[str, ...]
    desc: str = ""

    def field_map(self) -> Dict[str, FieldSpec]:
        return {f.name: f for f in self.fields}

    @property
    def required_fields(self) -> List[str]:
        return [f.name for f in self.fields if f.required]


# --------------------------------------------------------------------------- #
# 工具契约
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ToolSpec:
    """一个可被 Agent 调用的工具。

    ``requires_review`` 是本项目最重要的一位：置 True 的工具**不得**由模型
    直接收尾，必须产生人工复核任务后才算完成。三处人工确认点都靠它表达，
    而不是散落在各路由的 if 里。
    """

    name: str
    purpose: str
    input_contract: str
    output_contract: str
    allowed_roles: Tuple[str, ...]
    timeout_ms: int = 3000
    side_effect: bool = False
    requires_review: bool = False
    degrade_code: Optional[int] = None

    def __post_init__(self) -> None:
        if self.timeout_ms <= 0:
            raise ValueError(f"工具 {self.name} 的 timeout_ms 必须为正")


# --------------------------------------------------------------------------- #
# 契约错误（继承 ApiError，走统一 {code, message, data} 封装）
# --------------------------------------------------------------------------- #

class ContractViolation(ApiError):
    """契约不满足。code 沿用 40001，message 里写清是哪个字段/哪个角色。"""

    def __init__(self, message: str, code: int = ErrorCode.BAD_REQUEST) -> None:
        super().__init__(code, message)


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

class ContractRegistry:
    """数据契约与工具契约的注册表。

    刻意做成实例而非全局单例：测试可以各自建一份，互不污染。
    """

    def __init__(self) -> None:
        self._data: Dict[str, DataContract] = {}
        self._tools: Dict[str, ToolSpec] = {}

    # ---- 注册 ----
    def register_data(self, contract: DataContract) -> None:
        if contract.name in self._data:
            raise ContractViolation(f"数据契约 {contract.name} 重复注册")
        self._data[contract.name] = contract

    def register_tool(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ContractViolation(f"工具 {spec.name} 重复注册")
        if spec.input_contract not in self._data:
            raise ContractViolation(
                f"工具 {spec.name} 引用了未注册的输入契约 {spec.input_contract}"
            )
        if spec.output_contract not in self._data:
            raise ContractViolation(
                f"工具 {spec.name} 引用了未注册的输出契约 {spec.output_contract}"
            )
        self._tools[spec.name] = spec

    # ---- 查询 ----
    def data(self, name: str) -> DataContract:
        try:
            return self._data[name]
        except KeyError:
            raise ContractViolation(f"未注册的数据契约：{name}") from None

    def tool(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ContractViolation(f"未注册的工具：{name}") from None

    def tools(self) -> List[ToolSpec]:
        return list(self._tools.values())

    def tool_names(self) -> List[str]:
        return sorted(self._tools)

    # ---- 校验 ----
    def validate_payload(
        self,
        contract_name: str,
        payload: Mapping[str, Any],
        *,
        partial: bool = False,
    ) -> Dict[str, Any]:
        """按契约校验入参，返回规整后的字典（只保留契约内字段）。

        :param partial: 为 True 时只校验出现的字段（用于更新场景），
                        为 False 时要求必填字段齐全。
        """
        contract = self.data(contract_name)
        specs = contract.field_map()

        unknown = [k for k in payload if k not in specs]
        if unknown:
            raise ContractViolation(
                f"{contract.name} 不接受字段 {sorted(unknown)}；"
                f"契约 v{contract.version} 只定义了 {sorted(specs)}"
            )

        clean: Dict[str, Any] = {}
        for name, spec in specs.items():
            if name not in payload:
                if spec.required and not partial:
                    raise ContractViolation(
                        f"{contract.name} 缺少必填字段 {name}（{spec.desc or spec.dtype}）"
                    )
                continue
            clean[name] = _coerce(contract.name, spec, payload[name])
        return clean

    def check_role(
        self,
        contract_name: str,
        role: str,
        *,
        write: bool = False,
    ) -> None:
        contract = self.data(contract_name)
        allowed = contract.writer_roles if write else contract.reader_roles
        if role not in allowed:
            action = "写入" if write else "读取"
            raise ContractViolation(
                f"{contract.name} 不允许角色 {role} {action}；"
                f"允许的角色为 {list(allowed)}",
                code=ErrorCode.FORBIDDEN,
            )

    def check_tool_role(self, tool_name: str, role: str) -> None:
        spec = self.tool(tool_name)
        if role not in spec.allowed_roles:
            raise ContractViolation(
                f"工具 {tool_name} 不允许角色 {role} 调用；"
                f"允许的角色为 {list(spec.allowed_roles)}",
                code=ErrorCode.FORBIDDEN,
            )


def _coerce(contract_name: str, spec: FieldSpec, value: Any) -> Any:
    """按声明类型做最小转换与校验。

    只做**不会丢信息**的转换：``"12"`` → 12 可以，``"12.7"`` → 12 不行。
    宁可报错也不静默截断，否则脏数据会一路流到统计结论里。
    """
    if value is None:
        if spec.required:
            raise ContractViolation(
                f"{contract_name}.{spec.name} 不允许为空"
            )
        return None

    dtype = spec.dtype
    if dtype == "str":
        out: Any = value if isinstance(value, str) else str(value)
    elif dtype == "int":
        if isinstance(value, bool):
            raise ContractViolation(f"{contract_name}.{spec.name} 需要整数，收到布尔值")
        if isinstance(value, int):
            out = value
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            out = int(value.strip())
        else:
            raise ContractViolation(
                f"{contract_name}.{spec.name} 需要整数，收到 {value!r}"
            )
    elif dtype == "float":
        if isinstance(value, bool):
            raise ContractViolation(f"{contract_name}.{spec.name} 需要数值，收到布尔值")
        if isinstance(value, (int, float)):
            out = float(value)
        elif isinstance(value, str):
            try:
                out = float(value.strip())
            except ValueError:
                raise ContractViolation(
                    f"{contract_name}.{spec.name} 需要数值，收到 {value!r}"
                ) from None
        else:
            raise ContractViolation(
                f"{contract_name}.{spec.name} 需要数值，收到 {value!r}"
            )
    elif dtype == "bool":
        if isinstance(value, bool):
            out = value
        elif isinstance(value, str) and value.lower() in ("true", "false"):
            out = value.lower() == "true"
        elif isinstance(value, int) and value in (0, 1):
            out = bool(value)
        else:
            raise ContractViolation(
                f"{contract_name}.{spec.name} 需要布尔值，收到 {value!r}"
            )
    elif dtype == "list":
        if not isinstance(value, (list, tuple)):
            raise ContractViolation(
                f"{contract_name}.{spec.name} 需要列表，收到 {type(value).__name__}"
            )
        out = list(value)
    elif dtype == "dict":
        if not isinstance(value, Mapping):
            raise ContractViolation(
                f"{contract_name}.{spec.name} 需要对象，收到 {type(value).__name__}"
            )
        out = dict(value)
    else:  # datetime：只接受 ISO 8601 字符串
        if not isinstance(value, str) or len(value) < 10:
            raise ContractViolation(
                f"{contract_name}.{spec.name} 需要 ISO 8601 时间串，收到 {value!r}"
            )
        out = value

    if spec.allowed is not None and out not in spec.allowed:
        raise ContractViolation(
            f"{contract_name}.{spec.name} 取值 {out!r} 不在允许范围 {list(spec.allowed)}"
        )
    return out


# --------------------------------------------------------------------------- #
# 默认契约集（与现有库表、路由一一对应，改动需同步 docs/data-tool-contracts.md）
# --------------------------------------------------------------------------- #

def build_default_registry() -> ContractRegistry:
    """构造 MVP 内置的契约集。

    只声明被 Agent 链路真正用到的数据与工具；其余 70 余个 REST 端点仍走
    各自路由的入参校验，不强行纳入本层，避免为统一而统一。
    """
    reg = ContractRegistry()

    # ---- 数据集：问答请求 ----
    reg.register_data(DataContract(
        name="chat_request",
        version="v2",
        desc="质量工程师向知识库提问",
        reader_roles=("quality_engineer", "quality_manager", "shift_leader"),
        writer_roles=("quality_engineer", "quality_manager", "shift_leader"),
        fields=(
            FieldSpec("question", "str", True, desc="自然语言问题"),
            FieldSpec("session_id", "str", False, desc="续聊的会话号，空则新建"),
            FieldSpec(
                "as_of", "datetime", False,
                desc="按此时刻取当时有效的标准版本；空则用当前时刻",
            ),
            FieldSpec(
                "item_code", "str", False, desc="限定料号，缩小检索范围",
            ),
        ),
    ))

    # ---- 数据集：检索产物 ----
    reg.register_data(DataContract(
        name="retrieval_hit",
        version="v1",
        desc="一条检索命中（回答必须挂引用，不允许裸答案）",
        reader_roles=("quality_engineer", "quality_manager", "shift_leader"),
        writer_roles=("quality_engineer", "quality_manager"),
        fields=(
            FieldSpec("seq", "int", True, desc="引用序号，与正文 [n] 对应"),
            FieldSpec("source_file", "str", True, desc="来源文档名"),
            FieldSpec("clause", "str", False, desc="条款号或小节名"),
            FieldSpec("version", "str", True, desc="文档版本，用于版本闸校验"),
            FieldSpec("chunk_id", "str", True, desc="切片号，可回到原文"),
            FieldSpec("score", "float", False, desc="融合后的相关度"),
        ),
    ))

    # ---- 数据集：缺陷建单 ----
    reg.register_data(DataContract(
        name="defect_draft",
        version="v3",
        desc="班组长现场建单（草稿态，缺陷码须人工确认）",
        reader_roles=("shift_leader", "quality_engineer", "quality_manager"),
        writer_roles=("shift_leader",),
        fields=(
            FieldSpec("work_order_id", "str", True, desc="工单号"),
            FieldSpec("defect_desc", "str", True, desc="缺陷现象描述"),
            FieldSpec("line_id", "str", True, desc="产线；班组长由服务端覆写为本线"),
            FieldSpec("qty", "int", True, desc="不良数量，件"),
            FieldSpec(
                "plan_qty", "int", False,
                desc="工单计划数；服务端可从工单带出，用于校验不良数是否超计划",
            ),
            FieldSpec(
                "defect_code", "str", False,
                desc="缺陷码；草稿阶段可为空，确认阶段必填",
            ),
            FieldSpec(
                "measure_value", "float", False,
                unit="mm", desc="实测值，单位固定 mm，不接受 μm 混填",
            ),
        ),
    ))

    # ---- 数据集：归因结论 ----
    reg.register_data(DataContract(
        name="attribution_result",
        version="v2",
        desc="异常归因结论（止于「待验证」，不得输出「已确定」）",
        reader_roles=("quality_engineer", "quality_manager"),
        writer_roles=("quality_engineer", "quality_manager"),
        fields=(
            FieldSpec("defect_no", "str", True, desc="缺陷单号"),
            FieldSpec("suspect_cause", "str", True, desc="疑似原因，措辞须为待验证"),
            FieldSpec("evidence", "list", True, desc="证据行，每条可回溯到数据"),
            FieldSpec(
                "confidence", "float", True,
                desc="可信度 0~1，仅表示证据强度，不表示已确认",
            ),
            FieldSpec(
                "status", "str", True,
                allowed=("submitted", "approved", "rejected"),
                desc="审批状态；approved 只能由主管产生",
            ),
        ),
    ))

    # ---- 工具：检索 ----
    reg.register_tool(ToolSpec(
        name="kb.search",
        purpose="按问题检索知识库切片，返回带版本与条款的引用",
        input_contract="chat_request",
        output_contract="retrieval_hit",
        allowed_roles=("quality_engineer", "quality_manager", "shift_leader"),
        timeout_ms=2000,
        side_effect=False,
        requires_review=False,
        degrade_code=ErrorCode.EMBED_DEGRADED,
    ))

    # ---- 工具：缺陷码候选推荐（模型判断，但必须人工确认）----
    reg.register_tool(ToolSpec(
        name="defect.suggest_code",
        purpose="依据缺陷描述给候选缺陷码，供班组长点选",
        input_contract="defect_draft",
        output_contract="defect_draft",
        allowed_roles=("shift_leader", "quality_engineer"),
        timeout_ms=1500,
        side_effect=False,
        requires_review=True,          # ← 人工确认点 1
    ))

    # ---- 工具：归因提交（质量工程师发起，需主管审批）----
    reg.register_tool(ToolSpec(
        name="attribution.submit",
        purpose="提交归因结论与证据包，等主管审批；提交不等于生效",
        input_contract="attribution_result",
        output_contract="attribution_result",
        allowed_roles=("quality_engineer", "quality_manager"),
        timeout_ms=2000,
        side_effect=True,
        requires_review=True,          # ← 提交后必须有人批
    ))

    # ---- 工具：归因审批（人工复核，主管专属）----
    reg.register_tool(ToolSpec(
        name="attribution.approve",
        purpose="主管审批归因结论，驳回须填理由",
        input_contract="attribution_result",
        output_contract="attribution_result",
        allowed_roles=("quality_manager",),
        timeout_ms=3000,
        side_effect=True,
        requires_review=True,          # ← 人工确认点 2
    ))

    # ---- 工具：审计核查（人工复核，主管专属）----
    reg.register_tool(ToolSpec(
        name="audit.review",
        purpose="主管对机器标记的风险问答逐条核查并留痕",
        input_contract="retrieval_hit",
        output_contract="retrieval_hit",
        allowed_roles=("quality_manager",),
        timeout_ms=2000,
        side_effect=True,
        requires_review=True,          # ← 人工确认点 3
    ))

    return reg
