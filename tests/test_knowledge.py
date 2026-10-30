"""core/knowledge 的单元测试（镜像 src/quality_trace/core/knowledge）。

覆盖：front-matter 解析、切片策略、分词降级、BM25 排序、RRF 融合、
向量库双后端往返、混合检索降级。全部离线，不依赖网络。
"""

from __future__ import annotations

import numpy as np
import pytest

from quality_trace.core.knowledge import retrieval as kb
from quality_trace.core.knowledge.ingest import (
    chunk_text,
    load_corpus,
    parse_front_matter,
    save_chunks,
    load_chunks,
)
from quality_trace.core.llm.client import EmbeddingUnavailable


# ---------------------------------------------------------------- front-matter
class TestFrontMatter:
    def test_parse_standard_meta(self):
        text = (
            "---\n"
            "doc_id: STD-1\n"
            "doc_type: standard\n"
            "version: v2\n"
            "effective_from: 2026-03-01\n"
            "status: active\n"
            "---\n"
            "# 标题\n正文"
        )
        meta, body = parse_front_matter(text)
        assert meta["doc_id"] == "STD-1"
        assert meta["version"] == "v2"
        assert meta["effective_from"].isoformat() == "2026-03-01"  # YAML date 对象
        assert body.startswith("# 标题")

    def test_no_front_matter(self):
        meta, body = parse_front_matter("普通正文")
        assert meta == {}
        assert body == "普通正文"

    def test_illegal_front_matter(self):
        # 只有开头的 ---，没有闭合：应返回原文
        meta, body = parse_front_matter("---\n不是元数据")
        assert meta == {} and body == "---\n不是元数据"


# ---------------------------------------------------------------- 切片
class TestChunking:
    def test_short_section_single_chunk(self):
        chunks = chunk_text("## 1. 适用范围\n\n本规范适用。", 500, 80)
        assert len(chunks) == 1
        assert chunks[0]["heading"] == "1. 适用范围"
        assert "本规范适用" in chunks[0]["content"]

    def test_long_section_hard_split_with_overlap(self):
        body = "## 条款\n" + "公" * 1200
        chunks = chunk_text(body, 500, 80)
        assert len(chunks) == 3                          # 1200 / 步长420 ≈ 3 片
        # 每片 = 标题(5字) + 换行(1字) + ≤500 字正文
        assert all(c["char_len"] <= 506 for c in chunks)
        # 相邻切片存在重叠：第二片的正文开头应出现在第一片结尾内
        assert chunks[1]["content"][-50:-20] in chunks[0]["content"]
        assert all(c["seq_no"] == i for i, c in enumerate(chunks))

    def test_empty_body_skipped(self):
        assert chunk_text("# 只有标题\n\n\n## 也是标题\n", 500, 80) == []


# ---------------------------------------------------------------- 分词
class TestTokenize:
    def test_jieba_or_bigram(self):
        tokens = kb.tokenize("螺纹外径 19.98mm 公差")
        assert any("螺纹" in t for t in tokens) or len(tokens) > 4
        # 正则按非字母数字切分，"19.98mm" 会拆成 19 / 98mm——两者之一在即通过
        assert any(t in tokens for t in ("19.98mm", "98mm"))

    def test_bigram_fallback(self, monkeypatch):
        monkeypatch.setattr(kb, "HAS_JIEBA", False)
        tokens = kb.tokenize("螺纹外径")
        assert "螺纹" in tokens and "纹外" in tokens   # 二元组


# ---------------------------------------------------------------- BM25
class TestBM25:
    DOC_A = "螺纹外径检验规范 抽样方案 AQL 首件检验"
    DOC_B = "切刀磨损 进给量下降 参数漂移 根因分析"
    DOC_C = "检验记录 判定规则 业务时间 录入时间"

    def _index(self):
        return kb.BM25Index([kb.tokenize(d) for d in (self.DOC_A, self.DOC_B, self.DOC_C)])

    def test_rank_correct_doc_first(self):
        hits = self._index().search(kb.tokenize("抽样方案是什么"), top_n=3)
        assert hits and hits[0][0] == 0                    # 命中文档 A
        assert all(s > 0 for _, s in hits)

    def test_no_hit_returns_empty(self):
        assert self._index().search(["不存在的词xyz"], top_n=3) == []

    def test_no_root_cause_claim_not_needed(self):
        # 空语料不崩溃
        empty = kb.BM25Index([])
        assert empty.search(["任何"], 3) == []


# ---------------------------------------------------------------- RRF
class TestRRF:
    def test_both_channels_rank_first(self):
        fused = kb.rrf_fuse([[0, 1, 2], [1, 3, 2]])
        best = sorted(fused.items(), key=lambda x: -x[1])
        assert best[0][0] == 1                            # 文档1两通道都排第1
        assert best[1][0] == 2                            # 文档2两通道都在前列

    def test_single_channel(self):
        assert kb.rrf_fuse([[4, 2]])[4] > kb.rrf_fuse([[4, 2]])[2]


# ---------------------------------------------------------------- 向量库
class TestVectorStore:
    def test_numpy_backend_self_query(self, tmp_path):
        rng = np.random.default_rng(7)
        vecs = rng.normal(size=(8, 4)).astype("float32")
        store = kb.VectorStore(4, backend="numpy")       # 强制降级后端
        store.add(vecs)
        store.save(tmp_path)
        loaded = kb.VectorStore.load(tmp_path)
        assert loaded.backend == "numpy"
        assert loaded.search(vecs[2], 3)[0][0] == 2      # 自查询 top1 是自己
        assert loaded.search(vecs[2], 1)[0][1] == pytest.approx(1.0, abs=1e-4)

    def test_faiss_backend_if_available(self, tmp_path):
        if not kb.HAS_FAISS:
            pytest.skip("faiss-cpu 未安装（降级路径已由 numpy 用例覆盖）")
        rng = np.random.default_rng(7)
        vecs = rng.normal(size=(8, 4)).astype("float32")
        store = kb.VectorStore(4, backend="faiss")
        store.add(vecs)
        store.save(tmp_path)
        loaded = kb.VectorStore.load(tmp_path)
        assert loaded.backend == "faiss"
        assert loaded.search(vecs[5], 3)[0][0] == 5


# ---------------------------------------------------------------- 混合检索
class TestHybridSearch:
    def _bm25(self):
        return kb.BM25Index([kb.tokenize(d) for d in (
            "螺纹外径检验规范 抽样方案",
            "切刀磨损 进给量下降",
            "检验记录 判定规则",
        )])

    RET_CFG = {"top_k": 2, "recall": 5, "rrf_k": 60}

    def test_bm25_only_when_disabled_by_config(self):
        # 配置主动关闭向量（embedder/索引都没提供）→ 单通道，但不算"降级事件"
        ranked, meta = kb.hybrid_search("抽样方案", self._bm25(), None, None, self.RET_CFG)
        assert meta["mode"] == "bm25_only"
        assert meta["degraded"] is False
        assert len(ranked) <= 2 and ranked[0][0] == 0

    def test_partial_vector_assets_degrade(self, tmp_path):
        # 有向量索引但没有向量化函数（或反之）→ 半残状态，标记降级
        vecs = np.eye(3, dtype="float32")
        store = kb.VectorStore(3, backend="numpy")
        store.add(vecs)
        ranked, meta = kb.hybrid_search("抽样方案", self._bm25(), store, None, self.RET_CFG)
        assert meta["mode"] == "bm25_only"
        assert meta["degraded"] is True

    def test_embedding_failure_degrades(self, tmp_path):
        vecs = np.eye(3, dtype="float32")
        store = kb.VectorStore(3, backend="numpy")
        store.add(vecs)

        def broken_embedder(text):
            raise EmbeddingUnavailable("模拟向量服务宕机")

        ranked, meta = kb.hybrid_search(
            "抽样方案", self._bm25(), store, broken_embedder, self.RET_CFG
        )
        assert meta["mode"] == "bm25_only"
        assert meta["degraded"] is True
        assert "模拟向量服务宕机" in meta["degrade_reason"]

    def test_hybrid_mode(self, tmp_path):
        vecs = np.eye(3, dtype="float32")
        store = kb.VectorStore(3, backend="numpy")
        store.add(vecs)

        # 嵌入函数让"切刀"问题的向量正好是文档1 → 向量通道命中
        def embedder(text):
            return vecs[1] if "切刀" in text else vecs[0]

        ranked, meta = kb.hybrid_search(
            "切刀磨损", self._bm25(), store, embedder, self.RET_CFG
        )
        assert meta["mode"] == "hybrid" and meta["degraded"] is False
        assert ranked[0][0] == 1                            # 两通道都指向文档1


# ---------------------------------------------------------------- 入库落盘
class TestIngestRoundtrip:
    def test_save_load_chunks(self, tmp_path):
        chunks, stats = load_corpus(_corpus_dir(tmp_path))
        assert stats["total_chunks"] == len(chunks) > 0
        path = save_chunks(chunks, tmp_path / "idx")
        loaded = load_chunks(tmp_path / "idx")
        assert loaded == [
            {**c, "doc_meta": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                               for k, v in c["doc_meta"].items()}}
            for c in chunks
        ]
        assert path.exists()

    def test_load_missing_index(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_chunks(tmp_path)


def _corpus_dir(tmp_path):
    d = tmp_path / "knowledge"
    d.mkdir()
    (d / "std.md").write_text(
        "---\ndoc_id: S1\ndoc_type: standard\nversion: v2\n"
        "effective_from: 2026-03-01\nstatus: active\n---\n"
        "## 适用范围\n本规范适用于螺纹外径检验，抽样方案按 AQL 执行。",
        encoding="utf-8",
    )
    (d / "case.md").write_text(
        "---\ndoc_id: C1\ndoc_type: 8d_case\ndefect_code: DEF-THD-01\n---\n"
        "## D4 根因分析\n切刀磨损导致进给量下降，参数漂移。",
        encoding="utf-8",
    )
    return d
