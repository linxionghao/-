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


# ---------------------------------------------------------------------------
# LLM 通道
# ---------------------------------------------------------------------------
NORMALIZE_SYSTEM = (
    "你是制造业质量缺陷分类助手。任务：把现场描述归类到给定缺陷码字典中的候选码。\n"
    "硬性要求：\n"
    "1. 只能使用字典中存在的 code，禁止自造；\n"
    "2. 输出严格 JSON 数组，不要任何解释文字，格式：\n"
    '   [{"code":"DC-012","confidence":0.82,"reason":"提到外径偏小"}]\n'
    "3. 最多 3 个候选，按 confidence 降序；\n"
    "4. 无法归类时输出空数组 []，不要硬凑；\n"
    "5. 你只做分类，不做合格/不合格判定。"
)


def _build_messages(description: str, codes: List[Dict[str, Any]]) -> List[Dict]:
    """字典只传 code/name/category 三列，控制提示词体积（关键词不必给模型）。"""
    items = [{"code": c["code"], "name": c["name"],
              "category": c.get("category", "其他")} for c in codes]
    user = (
        f"缺陷码字典：\n{json.dumps(items, ensure_ascii=False)}\n\n"
        f"现场描述：{description}"
    )
    return [{"role": "system", "content": NORMALIZE_SYSTEM},
            {"role": "user", "content": user}]


def _parse_candidates(raw: str, codes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """解析模型输出：容错代码块 / 前后废话 / 非法 code / 缺字段。

    任何解析失败都返回空列表，由调用方决定回退——绝不让脏输出进入落库路径。
    """
    by_code = {c["code"]: c for c in codes}
    match = re.search(r"\[[\s\S]*\]", raw or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    results: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        if code not in by_code:          # 模型自造码一律丢弃
            continue
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        meta = by_code[code]
        results.append({
            "code": code,
            "name": meta["name"],
            "category": meta.get("category", "其他"),
            "confidence": round(confidence, 2),
            "source": "llm",
            "reason": str(item.get("reason", ""))[:80],
        })

    results.sort(key=lambda x: (-x["confidence"], x["code"]))
    return results[:MAX_CANDIDATES]


def llm_candidates(
    description: str, codes: List[Dict[str, Any]],
    llm_cfg: Dict, api_key: str,
) -> List[Dict[str, Any]]:
    """调用大模型给出候选；网络/协议异常向上抛 LLMUnavailable（由 normalize 兜底）。"""
    # 延迟导入：未配置 LLM 的场景（纯关键词模式）无需加载 requests
    from quality_trace.core.llm.client import call_llm

    raw = call_llm(_build_messages(description, codes), llm_cfg, api_key,
                   temperature=0.0, max_tokens=300)
    return _parse_candidates(raw, codes)


def normalize(
    description: str, codes: List[Dict[str, Any]],
    llm_cfg: Optional[Dict] = None, api_key: str = "",
) -> Dict[str, Any]:
    """归一化入口：LLM 优先，失败/未配置回退关键词。

    返回：
        {"suggested": [...], "source": "llm"|"keyword",
         "degraded": bool, "degraded_reason": str|None}

    degraded 语义与全项目一致：**仅当配置了 LLM 但调用失败**才为 True；
    根本没配 LLM 属于既定运行模式，不算降级事件（避免演示态永远亮黄条）。
    """
    if not codes:
        return {"suggested": [], "source": "keyword",
                "degraded": False, "degraded_reason": None}

    if llm_cfg and api_key:
        try:
            suggested = llm_candidates(description, codes, llm_cfg, api_key)
            if suggested:                     # 模型给了有效候选
                return {"suggested": suggested, "source": "llm",
                        "degraded": False, "degraded_reason": None}
            reason = "大模型未给出有效候选，已回退关键词匹配"
        except Exception as exc:  # noqa: BLE001 —— 归一化不允许把主流程打挂
            reason = f"大模型不可用（{exc}），已回退关键词匹配"
        return {
            "suggested": keyword_candidates(description, codes),
            "source": "keyword", "degraded": True, "degraded_reason": reason,
        }

    return {
        "suggested": keyword_candidates(description, codes),
        "source": "keyword", "degraded": False,
        "degraded_reason": "未配置大模型，使用关键词匹配",
    }
