# -*- coding: utf-8 -*-
"""归因审批服务：状态机审批 + 8D 草稿导出（docs/stage3-design.md 4.5 节）。

状态机：submitted → approved / rejected；重复审批 → 40906；
退回必填意见（主管必须给质量工程师修改方向）。

8D 导出策略：
- 库里已有草稿（draft_8d_md 非空）→ 直接下载（不重复调用 LLM）；
- 没有 → 现场调 LLM 按证据链生成草稿并**缓存入库**；LLM 不可用 →
  抛 50002 中文错误（此时接口还没发文件头，入口层可转成 JSON 错误，
  绝不给用户半截文件）；
- 正文恒含「本报告为系统草稿，需人工复核」头——8D 只出草稿是需求红线。

【核心模块】core 约束：不 print、不读环境变量、不 import argparse。
"""

from __future__ import annotations

import json
from typing import Dict

from quality_trace.core.llm.client import LLMUnavailable, call_llm
from quality_trace.core.repo import db as repo
from quality_trace.core.services.errors import ApiError, ErrorCode

# 草稿头部红线（需求：8D 只到草稿，L4 定责必须人工）
DRAFT_BANNER = "> ⚠️ 本报告为系统草稿，需人工复核后方可进入正式 8D 流程。"


def decide(
    conn, attribution_id: str, decision: str,
    decided_by: str, note: str = "",
) -> Dict:
    """批准（approved）/ 退回（rejected）。

    - 状态机：仅 submitted 可批，否则 40906；
    - 退回必填意见：主管不写意见，工程师无从修改 → 40001。
    """
    if decision == "rejected" and not note.strip():
        raise ApiError(ErrorCode.BAD_REQUEST, "退回必须填写审批意见")

    result = repo.decide_attribution(
        conn, attribution_id, decision, decided_by, note.strip()
    )
    if not result["ok"]:
        # 含「单不存在」与「状态不允许」两种情况，分开报
        if repo.get_attribution(conn, attribution_id) is None:
            raise ApiError(ErrorCode.ATTR_NOT_FOUND)
        raise ApiError(ErrorCode.ATTR_STATE_INVALID)
    return result["attribution"]


def _build_8d_messages(attribution: Dict, defect: Dict, defect_no: str) -> list:
    """装配 8D 草稿生成提示（证据链原文进入材料区，不作事实背书）。"""
    evidence = attribution.get("evidence_json") or "[]"
    hypothesis = attribution.get("hypothesis_text") or ""
    system = (
        "你是制造业质量工程师助手。请基于给出的缺陷信息与结构化证据链，"
        "生成 8D 报告草稿（D1 组建团队 / D2 问题描述 / D3 临时围堵 / D4 根因分析 /"
        " D5 纠正措施 / D6 措施验证 / D7 预防再发 / D8 祝贺团队）。\n"
        "硬性要求：\n"
        "1. D4 只能写「疑似根因（待验证）」，禁止使用「根因已确定」等表述；\n"
        "2. 引用证据时标注来源编号；未覆盖的证据一律写「证据不足，待补充」；\n"
        "3. 输出 Markdown，第一行以「# 8D 报告草稿」开头；\n"
        "4. 全文用中文，简洁务实，不写空话。"
    )
    user = (
        f"缺陷编号：{defect_no}\n"
        f"工单：{defect.get('work_order_id', '')}（产品工序 {defect.get('op_code', '')}）\n"
        f"缺陷描述：{defect.get('description', '')}\n"
        f"数量/严重度：{defect.get('qty', '')} / {defect.get('severity', '')}\n"
        f"缺陷码：{defect.get('defect_code', '')}\n\n"
        f"结构化证据链（JSON）：\n{evidence}\n\n"
        f"L3 因果假设（待验证）：\n{hypothesis}"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def export_8d(
    conn, attribution_id: str, llm_cfg: Dict, api_key: str,
) -> Dict[str, str]:
    """导出 8D 草稿：有缓存直读，无缓存调 LLM 生成并落库。

    返回 {"filename": ..., "markdown": ...}；LLM 不可用抛 ApiError(50002)。
    """
    attribution = repo.get_attribution(conn, attribution_id)
    if attribution is None:
        raise ApiError(ErrorCode.ATTR_NOT_FOUND)

    defect_no = attribution["defect_no"]
    filename = f"8D-{defect_no}.md"

    # 1) 已有草稿：直接下载（含此前 LLM 生成后缓存的）
    if attribution.get("draft_8d_md"):
        return {"filename": filename, "markdown": attribution["draft_8d_md"]}

    # 2) 现场生成（缺陷单信息缺失时照样可生成，证据链是主材料）
    defect = repo.get_defect(conn, defect_no) or {}
    messages = _build_8d_messages(attribution, defect, defect_no)
    try:
        draft = call_llm(messages, llm_cfg, api_key, temperature=0.2, max_tokens=2000)
    except LLMUnavailable as exc:
        # 不吐半截文件：此时还没发任何响应体，入口层转统一 JSON 错误
        raise ApiError(
            ErrorCode.LLM_DEGRADED,
            f"大模型不可用，无法生成 8D 草稿（{exc}）。"
            "请配置 LLM 服务后重试，或先在归因单中人工补录草稿。",
        ) from exc

    if not draft.strip():
        raise ApiError(ErrorCode.INTERNAL_ERROR, "大模型返回了空草稿")

    # 3) 草稿加红线头并缓存入库（下次导出不再调用 LLM）
    markdown = f"# 8D 报告草稿（{defect_no}）\n\n{DRAFT_BANNER}\n\n{draft.strip()}\n"
    repo.set_attribution_draft(conn, attribution_id, markdown)
    return {"filename": filename, "markdown": markdown}


def pretty_evidence(evidence_json: str) -> list:
    """证据链 JSON → 展示结构列表（详情页用；坏 JSON 容错为空列表）。"""
    try:
        data = json.loads(evidence_json) if evidence_json else []
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []
