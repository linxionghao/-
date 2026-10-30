"""LLM / Embedding 客户端（OpenAI 兼容协议，供应商可配置）。

【核心模块】对应 docs/architecture.md 5.1 节 LLMClient 的同步雏形。
设计契约：
  - 只认 OpenAI 兼容协议（/chat/completions 与 /embeddings），
    换供应商改 config/llm.yaml，不改代码；
  - API Key 由调用方读环境变量后传入（core 不碰 os.environ）；
  - 任何失败统一抛专用异常（LLMUnavailable / EmbeddingUnavailable），
    上层决定降级策略（模板降级 / BM25-only），本模块不自行降级；
  - 阶段 2 在 call_llm 外包一层：JSON Schema 校验 + 引用校验（架构 5.4）。

依赖策略：requests 延迟到函数体内 import——core 包保持轻加载，
没装 requests 时模块本身仍可导入（单元测试不联网就能测纯逻辑）。
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np


class LLMUnavailable(Exception):
    """LLM 调用不可用（无 Key / 网络失败 / 超时 / 响应非法）。"""


class EmbeddingUnavailable(Exception):
    """向量服务不可用。捕获后上层自动降级为 BM25 单通道。"""


def call_llm(
    messages: List[Dict],
    llm_cfg: Dict,
    api_key: str,
    *,
    temperature: float = 0.2,   # 质量场景锁低温度，输出尽量稳定
    max_tokens: int = 1500,
) -> str:
    """调用 LLM 生成回答。messages 为 OpenAI 格式 [{role, content}]。

    参数:
        messages: 对话消息列表（system / user / assistant）
        llm_cfg:  config/llm.yaml 的 llm 段（base_url / model / timeout_s）
        api_key:  调用方从环境变量读出的 Key；空串视为未配置

    异常:
        LLMUnavailable: 一切失败路径的统一出口，上层据此走模板降级。
    """
    if not api_key:
        raise LLMUnavailable("API Key 为空（调用方未传入有效 Key）")
    import requests  # 延迟导入：保证 core 包轻加载、单测不联网可导入

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = llm_cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": llm_cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        resp = requests.post(
            url, headers=headers, json=payload, timeout=llm_cfg.get("timeout_s", 30)
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if not content or not content.strip():
            raise LLMUnavailable("LLM 返回了空内容")
        return content
    except LLMUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 —— 任何失败归一为"不可用"
        raise LLMUnavailable(f"LLM 调用失败: {exc}") from exc


def embed_texts(texts: List[str], emb_cfg: Dict, api_key: str) -> np.ndarray:
    """批量向量化（OpenAI 兼容 /embeddings）。

    返回与输入顺序对齐的 (n, dim) float32 矩阵。
    失败一律抛 EmbeddingUnavailable，由调用方降级（架构 4.5）。
    """
    if not api_key:
        raise EmbeddingUnavailable("API Key 为空（调用方未传入有效 Key）")
    import requests  # 延迟导入，同上

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = emb_cfg["base_url"].rstrip("/") + "/embeddings"
    try:
        resp = requests.post(
            url,
            headers=headers,
            json={"model": emb_cfg["model"], "input": texts},
            timeout=emb_cfg.get("timeout_s", 30),
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda d: d["index"])   # 按 index 排序对齐输入顺序
        return np.array([d["embedding"] for d in data], dtype="float32")
    except Exception as exc:  # noqa: BLE001
        raise EmbeddingUnavailable(f"向量服务调用失败: {exc}") from exc
