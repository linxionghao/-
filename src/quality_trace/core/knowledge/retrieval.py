"""混合检索：分词 → BM25 + 向量双通道 → RRF 融合排序。

【核心模块】阶段 1 从 examples/phase1_rag_demo.py 迁入。
依赖注入设计：embedder（向量化函数）由调用方传入——本模块不读配置、
不碰网络、不 print，向量服务可用与否完全由外层决定（架构 4.5 降级原则：
检索层不判断供应商，只执行；不可用时外层直接不注入 embedder）。

降级链（全部自动、不阻塞）：
  - jieba 未安装   → 汉字二元组(bigram)分词
  - faiss 未安装   → numpy 暴力余弦（切片 < 10 万时结果无差别）
  - 无 embedder    → BM25 单通道，meta.degraded = True（如实标记，不隐瞒）
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---- 可选依赖：缺失时走降级路径，导入失败不算错误 ----
try:  # pragma: no cover - 取决于运行环境
    import faiss
    HAS_FAISS = True
except ImportError:  # pragma: no cover
    HAS_FAISS = False

try:  # pragma: no cover
    import jieba
    HAS_JIEBA = True
except ImportError:  # pragma: no cover
    HAS_JIEBA = False

# 与 LLM 适配层共用一套"服务不可用"异常语义
from quality_trace.core.llm.client import EmbeddingUnavailable

Embedder = Callable[[str], np.ndarray]
# embedder(text) -> 已归一化前的原始向量；失败抛 EmbeddingUnavailable

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff a-zA-Z0-9]+")
# 中英混排再切分：汉字段 或 ASCII 段（含小数点的规格值，如 19.98mm / M8）
_MIX_RE = re.compile(r"[\u4e00-\u9fff]+|[a-zA-Z0-9.]+")


def _fallback_tokens(part: str) -> List[str]:
    """无 jieba 时的切分：汉字段取二元组，ASCII 段整体保留。

    为什么不能直接对整段判 `fullmatch(汉字)`：`_TOKEN_RE` 的字符类里含空格，
    "螺纹外径 19.98mm 公差" 会整体作为一个 seg 传进来，fullmatch 失败后
    落到 else 分支，整句变成**一个词**——检索直接失效。降级路径必须能处理
    中英混排，否则"装了 jieba 才对、不装就废"。
    """
    out: List[str] = []
    for sub in _MIX_RE.findall(part):
        if re.fullmatch(r"[\u4e00-\u9fff]+", sub):
            if len(sub) == 1:
                out.append(sub)                       # 单字：无法组成二元组
            else:
                out.extend(sub[i : i + 2] for i in range(len(sub) - 1))
        else:
            out.append(sub.lower())
    return out


def tokenize(text: str) -> List[str]:
    """中文分词：jieba 精确模式；未安装则按汉字二元组切分（保底方案）。"""
    tokens: List[str] = []
    for seg in _TOKEN_RE.findall(text):
        seg = seg.strip()
        if not seg:
            continue
        if HAS_JIEBA:
            tokens.extend(w.strip() for w in jieba.lcut(seg) if w.strip())
        else:
            for part in seg.split():
                tokens.extend(_fallback_tokens(part))
    return tokens


class BM25Index:
    """经典 Okapi BM25（纯内存）。

    阶段 2+ 迁移 SQLite FTS5 虚拟表时保持 search() 签名不变，
    届时数据量大也能换载体、不动调用方。
    """

    K1 = 1.5   # 词频饱和系数
    B = 0.75   # 文档长度归一系数

    def __init__(self, tokenized_corpus: List[List[str]]):
        self.corpus = tokenized_corpus
        self.n_docs = len(tokenized_corpus)
        self.doc_len = [len(d) for d in tokenized_corpus]
        self.avgdl = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0
        self.tf: List[Dict[str, int]] = []
        df: Dict[str, int] = {}
        for doc in tokenized_corpus:
            freq: Dict[str, int] = {}
            for t in doc:
                freq[t] = freq.get(t, 0) + 1
            self.tf.append(freq)
            for term in freq:
                df[term] = df.get(term, 0) + 1
        # Okapi IDF：ln(1 + (N - df + 0.5) / (df + 0.5))，天然非负
        self.idf = {
            t: math.log(1.0 + (self.n_docs - n + 0.5) / (n + 0.5)) if self.n_docs else 0.0
            for t, n in df.items()
        }

    def search(self, query_tokens: Sequence[str], top_n: int) -> List[Tuple[int, float]]:
        """返回 [(切片下标, 得分)]，按得分降序，最多 top_n 条，过滤零分。"""
        scores = [0.0] * self.n_docs
        for term in query_tokens:
            idf = self.idf.get(term)
            if not idf:
                continue
            for i, freq in enumerate(self.tf):
                tf = freq.get(term, 0)
                if not tf:
                    continue
                denom = tf + self.K1 * (
                    1 - self.B + self.B * self.doc_len[i] / (self.avgdl or 1)
                )
                scores[i] += idf * tf * (self.K1 + 1) / denom
        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [(i, s) for i, s in ranked[:top_n] if s > 0]


def _normalize(vecs: np.ndarray) -> np.ndarray:
    """L2 归一化：归一化后内积 == 余弦相似度。"""
    norms = np.linalg.norm(vecs, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vecs / norms).astype("float32")


class VectorStore:
    """双后端向量库：faiss.IndexFlatIP（可用时）/ numpy 暴力余弦（降级）。

    两个后端结果一致（IndexFlatIP 是精确检索），backend 字段仅用于
    审计与演示时说明实际走了哪条路。
    """

    def __init__(self, dim: int, backend: Optional[str] = None):
        if backend is None:
            backend = "faiss" if HAS_FAISS else "numpy"
        if backend == "faiss" and not HAS_FAISS:
            backend = "numpy"
        self.backend = backend
        self.dim = dim
        if self.backend == "faiss":
            self.index = faiss.IndexFlatIP(dim)
        else:
            self.matrix: Optional[np.ndarray] = None

    def add(self, vectors: np.ndarray) -> None:
        """批量入库（自动归一化）。"""
        vectors = _normalize(np.asarray(vectors, dtype="float32"))
        if self.backend == "faiss":
            self.index.add(vectors)
        else:
            self.matrix = vectors

    def search(self, query: np.ndarray, top_n: int) -> List[Tuple[int, float]]:
        """查询单个向量 → [(切片下标, 余弦相似度)]。"""
        q = _normalize(np.asarray([query], dtype="float32"))[0]
        if self.backend == "faiss":
            scores, ids = self.index.search(q[None, :], top_n)
            return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i >= 0]
        if self.matrix is None:
            return []
        sims = self.matrix @ q
        order = np.argsort(-sims)[:top_n]
        return [(int(i), float(sims[i])) for i in order]

    # ---- 落盘 / 加载：按后端区分文件，互不误读 ----
    def save(self, folder: Path) -> None:
        if self.backend == "faiss":
            faiss.write_index(self.index, str(folder / "vectors.faiss"))
        else:
            np.save(str(folder / "vectors.npy"), self.matrix)

    @classmethod
    def load(cls, folder: Path) -> "VectorStore":
        """按落盘文件自动选后端；FAISS 文件存在但库缺失时给明确指引。"""
        f_path, n_path = Path(folder) / "vectors.faiss", Path(folder) / "vectors.npy"
        if f_path.exists() and HAS_FAISS:
            idx = faiss.read_index(str(f_path))
            store = cls.__new__(cls)
            store.backend, store.index, store.dim = "faiss", idx, idx.d
            return store
        if f_path.exists() and not HAS_FAISS:
            raise RuntimeError("索引是 FAISS 格式但未安装 faiss-cpu：pip install faiss-cpu")
        if n_path.exists():
            mat = np.load(str(n_path))
            store = cls.__new__(cls)
            store.backend, store.matrix, store.dim = "numpy", mat, mat.shape[1]
            return store
        raise FileNotFoundError(f"未找到向量索引文件：{folder}")


def rrf_fuse(rankings: List[List[int]], k: int = 60) -> Dict[int, float]:
    """倒数排名融合（架构 4.2 节）。

    BM25 分与余弦分不可通约，所以不比分数只比名次——鲁棒且免调参。
    k=60 是 RRF 论文的默认常数，通过 config/retrieval.rrf_k 可改。
    """
    fused: Dict[int, float] = {}
    for ranking in rankings:
        for rank_pos, idx in enumerate(ranking, start=1):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank_pos)
    return fused


def hybrid_search(
    question: str,
    bm25: BM25Index,
    vectors: Optional[VectorStore],
    embedder: Optional[Embedder],
    ret_cfg: Dict,
) -> Tuple[List[Tuple[int, float]], Dict[str, object]]:
    """混合检索入口（纯函数：不 print，降级信息全部进 meta 由调用方展示）。

    参数:
        bm25:     已建好的 BM25 索引（永不下线，是降级兜底）
        vectors:   向量库；None 表示无向量索引（如入库时向量服务就不可用）
        embedder: 单文本向量化函数；None 表示本次降级走 BM25 单通道
        ret_cfg:  retrieval 配置（top_k / recall / rrf_k）

    返回:
        (top_k 切片下标与 RRF 分, 运行元信息)。元信息含 mode(hybrid/bm25_only)、
        degraded(bool)、degrade_reason——阶段 2 直接写 retrieval_log 表。
    """
    top_k, recall = int(ret_cfg.get("top_k", 3)), int(ret_cfg.get("recall", 10))
    meta: Dict[str, object] = {"mode": "bm25_only", "degraded": False}

    # ---- 通道一：BM25 关键词召回 ----
    bm25_hits = bm25.search(tokenize(question), recall)
    bm25_ranking = [i for i, _ in bm25_hits]

    # ---- 通道二：向量召回（embedder 与索引都可用才走） ----
    vector_ranking: List[int] = []
    if vectors is not None and embedder is not None:
        try:
            q_vec = embedder(question)
            vector_ranking = [i for i, _ in vectors.search(q_vec, recall)]
            meta["mode"] = "hybrid"
        except EmbeddingUnavailable as exc:
            # 检索服务失败不让问答挂掉：如实标记降级原因
            meta["degraded"] = True
            meta["degrade_reason"] = str(exc)
    elif vectors is not None or embedder is not None:
        meta["degraded"] = True
        meta["degrade_reason"] = "向量索引或向量化函数缺失，仅 BM25 单通道"

    # ---- RRF 融合 → top_k ----
    fused = rrf_fuse([bm25_ranking, vector_ranking], k=int(ret_cfg.get("rrf_k", 60)))
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]
    meta["scores"] = [{"chunk_index": i, "rrf": round(s, 6)} for i, s in ranked]
    return ranked, meta
