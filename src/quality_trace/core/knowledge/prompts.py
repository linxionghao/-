"""RAG 问答提示词装配（纯函数）。

【核心模块】架构 4.4 节"引用溯源"的第一步落地：
  1. 每条材料以 [C1]...[Cn] 编号进入上下文；
  2. 系统提示词要求 LLM 回答时显式标注引用编号、不得把材料当事实结论、
     禁止输出"根因已确定"（需求红线，验收标准 6）。
阶段 2 将在调用方追加"服务端引用校验"：LLM 引用的编号必须真的在本次
上下文里，否则剔除——校验不信任 LLM，提示词只是辅助约束。
"""

from __future__ import annotations

from typing import Dict, List

SYSTEM_PROMPT = (
    "你是制造业质量知识库问答助手。请严格遵守：\n"
    "1) 只依据下面带编号 [C1]...[Cn] 的材料回答问题，不得使用材料之外的知识；\n"
    "2) 材料是参考依据，不是事实结论——涉及判定时须说明'按XX规范第X条'，"
    "禁止说'根因已确定'；\n"
    "3) 回答中每条信息后用【Cn】标注出处编号，无材料支撑的内容必须声明'材料未覆盖'；\n"
    "4) 材料不足或无相关内容时，直接回答'知识库中未找到相关依据'。"
)


def build_messages(question: str, retrieved: List[Dict]) -> List[Dict]:
    """把 top_k 材料编上号拼进 OpenAI 格式消息列表。

    retrieved 元素结构（examples 传入）：
        {"rank": 序号, "rrf": 分数, "chunk": {source_file, heading, content, doc_meta, ...}}
    版本信息随引用一起进上下文——为阶段 2 的"版本失效第三道闸"
    （答案侧引用校验）预留元数据。
    """
    context_lines = []
    for pos, item in enumerate(retrieved, start=1):
        c = item["chunk"]
        meta = c["doc_meta"]
        version = meta.get("version")
        version_tag = f"（版本 {version}）" if version else ""
        context_lines.append(
            f"[C{pos}] 出处：{c['source_file']} {version_tag}"
            f"｜章节：{c['heading'] or '-'}\n{c['content']}"
        )
    user_prompt = (
        "参考材料如下：\n\n" + "\n\n".join(context_lines) + f"\n\n问题：{question}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
