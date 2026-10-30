"""core/llm/client 的单元测试（镜像 src/quality_trace/core/llm）。

不联网：只验证"无 Key / Key 为空"的失败路径与异常契约——
真实供应商调用属于集成测试，放在 examples 演示环节人工验证。
"""

from __future__ import annotations

import pytest

from quality_trace.core.llm.client import (
    EmbeddingUnavailable,
    LLMUnavailable,
    call_llm,
    embed_texts,
)

LLM_CFG = {"base_url": "https://api.invalid.example/v1", "model": "demo", "timeout_s": 1}
EMB_CFG = {"base_url": "https://api.invalid.example/v1", "model": "demo", "timeout_s": 1}


class TestCallLLM:
    def test_empty_key_raises(self):
        with pytest.raises(LLMUnavailable, match="API Key"):
            call_llm([{"role": "user", "content": "hi"}], LLM_CFG, api_key="")

    def test_network_error_wrapped(self):
        # Key 有效但地址不可达：任何失败必须统一包装为 LLMUnavailable
        with pytest.raises(LLMUnavailable, match="LLM 调用失败"):
            call_llm(
                [{"role": "user", "content": "hi"}], LLM_CFG, api_key="sk-demo"
            )


class TestEmbedTexts:
    def test_empty_key_raises(self):
        with pytest.raises(EmbeddingUnavailable, match="API Key"):
            embed_texts(["文本"], EMB_CFG, api_key="")

    def test_network_error_wrapped(self):
        with pytest.raises(EmbeddingUnavailable, match="向量服务调用失败"):
            embed_texts(["文本"], EMB_CFG, api_key="sk-demo")
