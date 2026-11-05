# -*- coding: utf-8 -*-
"""描述归一化：自由文本 → 候选缺陷码（服务端，两步落库的第一步）。

两条通道：
1. LLM 通道（优先）：把「缺陷码字典 + 现场描述」给模型，要求输出 JSON 候选；
2. 关键词回退：LLM 未配置 / 超时 / 输出不可解析时，用字典里的 keywords_json
   做命中计分 + 名称二元组相似度补分——**没有 LLM 也能用**，只是候选质量下降。

铁律：本模块只产出「候选」，不写库、不定判定。落库必须经
`POST /defects/{no}/confirm` 由班组长亲手点选（人工确认是硬闸）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# 关键词命中权重与候选上限
KEYWORD_HIT_WEIGHT = 2.0     # 每个关键词命中计 2 分
NAME_OVERLAP_WEIGHT = 3.0    # 名称二元组重合度权重
MAX_CANDIDATES = 3           # 前端最多展示 3 个大按钮候选


def _bigrams(text: str) -> set:
    """中文二元组集合（无需分词依赖，对短描述足够稳）。"""
    clean = re.sub(r"[\s，。、；：,.;:!?！？（）()\[\]【】]", "", text or "")
    return {clean[i:i + 2] for i in range(len(clean) - 1)}


def keyword_candidates(
    description: str, codes: List[Dict[str, Any]], top_n: int = MAX_CANDIDATES
) -> List[Dict[str, Any]]:
    """关键词 + 名称相似度回退匹配。

    打分规则（可解释、可测试，不引入黑盒）：
    - 字典 keywords_json 中每个关键词出现在描述里 → +2 分；
    - 缺陷名与描述的二元组重合率 × 3 分（覆盖「螺纹外径偏小」这类同义改写）。

    返回 [{code, name, category, confidence, source:'keyword', matched}]，
    按分数降序取前 top_n，分数为 0 的候选直接丢弃（宁缺勿滥）。
    """
    text = (description or "").strip()
    if not text:
        return []

    desc_grams = _bigrams(text)
    scored: List[Dict[str, Any]] = []

    for item in codes:
        try:
            keywords = json.loads(item.get("keywords_json") or "[]")
        except (ValueError, TypeError):
            keywords = []
        matched = [k for k in keywords if k and k in text]
        score = KEYWORD_HIT_WEIGHT * len(matched)

        name_grams = _bigrams(item.get("name", ""))
        if name_grams and desc_grams:
            overlap = len(name_grams & desc_grams) / len(name_grams)
            score += NAME_OVERLAP_WEIGHT * overlap

        if score <= 0:
            continue
        # 置信度：把分数压到 0.35~0.95，纯展示用途，不参与任何自动判定
        confidence = round(min(0.95, 0.35 + 0.12 * score), 2)
        scored.append({
            "code": item["code"],
            "name": item["name"],
            "category": item.get("category", "其他"),
            "confidence": confidence,
            "source": "keyword",
            "matched": matched,
        })

    scored.sort(key=lambda x: (-x["confidence"], x["code"]))
    return scored[:top_n]
