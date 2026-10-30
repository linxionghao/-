# -*- coding: utf-8 -*-
"""对话审计服务：越界回答自动检测（docs/stage3-design.md 4.4 节）。

三条规则（均为「疑似」标记，供主管人工核查，不做自动拦截）：
    flag_root_cause     正文命中根因结论正则组（如「根因已确定」）
    flag_no_citation    回答完成、正文非空，但引用行数为 0
    flag_stale_version  任一引用指向 kb_doc 中 status='archived' 的（文档名, 版本）

触发时机：① /chat 回答落库完成时同步跑一次（见 chat.py 第 6 步）；
         ② 主管调 POST /admin/audit/rescan 对历史消息补扫（规则升级后可重跑）。

【核心模块】core 约束：不 print、不读环境变量、不 import argparse。
"""

from __future__ import annotations

import re
from typing import Dict, List

from quality_trace.core.repo import db as repo

# ---- 规则一：根因结论正则组 ----
# 系统只允许 L3「因果假设（待验证）」；任何宣称根因已确定的表述都算越界。
# 命中即标「疑似」，不自动删改——最终由主管核查定夺。
ROOT_CAUSE_PATTERNS = [
    re.compile(r"根因(已)?(确定|查明|找到|锁定)"),
    re.compile(r"根因(就是|是|为)"),
    re.compile(r"根本原因(就是|是|为|已确定)"),
    re.compile(r"可以(确定|确认).{0,8}(根因|根本原因)"),
    re.compile(r"(根因|根本原因)(无需|不必).{0,6}验证"),
]


def detect_flags(
    content: str, status: str, citations: List[Dict],
    archived_versions: set,
) -> Dict[str, bool]:
    """纯函数：对一条 assistant 消息计算三条越界标记。

    archived_versions 是 repo.archived_doc_versions() 的结果集
    （{(doc_name, version), ...}），由调用方预取一次避免逐条查库。

    判定细节：
    - 规则二只对 status='complete' 且正文非空的消息生效（partial/failed 不算）；
    - 规则三按（文档名 + 版本号）联合判定，同一文档名的其他版本不受影响。
    """
    text = content or ""

    # 规则一：根因结论
    flag_root_cause = any(p.search(text) for p in ROOT_CAUSE_PATTERNS)

    # 规则二：无引用作答
    flag_no_citation = (
        status == "complete" and bool(text.strip()) and len(citations) == 0
    )

    # 规则三：引用已废止版本
    flag_stale_version = False
    for c in citations:
        name, version = c.get("source_file"), c.get("version")
        if name and version and (name, version) in archived_versions:
            flag_stale_version = True
            break

    return {
        "root_cause": flag_root_cause,
        "no_citation": flag_no_citation,
        "stale_version": flag_stale_version,
    }


def flag_message(conn, message_id: str) -> Dict[str, bool]:
    """对单条消息跑三规则并落库；返回标记结果（测试与调用方展示用）。"""
    message = repo.get_message(conn, message_id)
    if message is None or message["role"] != "assistant":
        return {"root_cause": False, "no_citation": False, "stale_version": False}
    citations = repo.get_citations(conn, message_id)
    flags = detect_flags(
        message["content"], message["status"], citations,
        repo.archived_doc_versions(conn),
    )
    repo.set_message_flags(
        conn, message_id,
        flags["root_cause"], flags["no_citation"], flags["stale_version"],
    )
    return flags


def rescan_all(conn) -> Dict[str, int]:
    """补扫：对全部 assistant 消息重跑三规则（规则升级后可反复执行）。

    reviewed_* 字段不动——人工核查结论不应被机器重扫覆盖。
    返回 {scanned: 总数, flagged: 命中任一规则的条数}。
    """
    archived = repo.archived_doc_versions(conn)
    scanned, flagged = 0, 0
    for message in repo.list_assistant_messages(conn):
        citations = repo.get_citations(conn, message["message_id"])
        flags = detect_flags(
            message["content"], message["status"], citations, archived
        )
        repo.set_message_flags(
            conn, message["message_id"],
            flags["root_cause"], flags["no_citation"], flags["stale_version"],
        )
        scanned += 1
        if any(flags.values()):
            flagged += 1
    return {"scanned": scanned, "flagged": flagged}
