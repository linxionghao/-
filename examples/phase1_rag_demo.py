# -*- coding: utf-8 -*-
"""
阶段 1 最小原型（薄入口版）：知识库 RAG 问答 CLI
=================================================

核心逻辑已按 docs/architecture.md 第 2 节迁入 src/quality_trace/core/：

    core/knowledge/ingest.py     parse_front_matter / chunk_text / load_corpus / 落盘
    core/knowledge/retrieval.py  tokenize / BM25Index / VectorStore / rrf_fuse / hybrid_search
    core/knowledge/prompts.py    SYSTEM_PROMPT / build_messages
    core/llm/client.py           call_llm / embed_texts（供应商可配置）
    utils/config.py              配置加载（既有模块，直接复用）

本文件只做编排接线：读环境变量 Key、解析 CLI、打印结果、写检索日志——
即 core 约束禁止的"脏活"全部留在这层，核心模块保持纯函数可单测。

用法（在仓库根目录执行）：
    python examples/phase1_rag_demo.py --ingest              # 建库（语料变更后重跑）
    python examples/phase1_rag_demo.py --ask "螺纹检验的抽样方案是什么？"
    python examples/phase1_rag_demo.py --ingest --ask "..."  # 一条龙

接真实 LLM / 向量服务：
    1. 改 config/llm.yaml 的 base_url / model；
    2. 设置环境变量 QT_LLM_API_KEY（及开向量时的 QT_EMBEDDING_API_KEY）；
    3. 要开向量检索，把 embedding.enabled 改 true。

降级行为（自动，不阻塞）：
  - faiss 未安装     → numpy 暴力余弦      - LLM 不可用   → 只返回检索材料
  - 向量服务不可用   → BM25 单通道[降级]    - 无 Key      → 同上
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]           # 仓库根目录
sys.path.insert(0, str(ROOT / "src"))                # 让脚本找到 src/ 包

from quality_trace.core.knowledge import ingest as kb_ingest          # noqa: E402
from quality_trace.core.knowledge import retrieval as kb_retrieval    # noqa: E402
from quality_trace.core.knowledge.prompts import build_messages        # noqa: E402
from quality_trace.core.llm.client import (                           # noqa: E402
    LLMUnavailable,
    call_llm,
    embed_texts,
)
from quality_trace.utils.config import load_config                    # noqa: E402

KNOWLEDGE_DIR = ROOT / "data" / "knowledge"         # 语料目录
INDEX_DIR = ROOT / "data" / "phase1_index"          # 索引落盘目录
CONFIG_PATH = ROOT / "config" / "llm.yaml"          # 供应商配置


def _make_embedder(emb_cfg: Dict) -> Optional[kb_retrieval.Embedder]:
    """按配置装配向量化函数；未开启则返回 None（检索层据此走 BM25 单通道）。

    环境变量 Key 在这层读出后传给 core——core 不碰 os.environ（编码约束）。
    """
    if not bool(emb_cfg.get("enabled", False)):
        return None
    api_key = os.environ.get(emb_cfg.get("api_key_env", ""), "")

    def _embed(text: str):
        # VectorStore.search 内部会做 L2 归一化，这里返回原始向量即可
        return embed_texts([text], emb_cfg, api_key)[0]

    return _embed


def do_ingest(cfg: Dict) -> None:
    """建库：语料 → 切片 → BM25（永远建）+ 向量索引（可用才建）→ 落盘。"""
    ret_cfg, emb_cfg = cfg["retrieval"], cfg["embedding"]
    embedder = _make_embedder(emb_cfg)

    # ---- 1. 读语料、切片（core 负责，纯函数） ----
    chunks, stats = kb_ingest.load_corpus(
        KNOWLEDGE_DIR,
        chunk_size=int(ret_cfg.get("chunk_size", 500)),
        overlap=int(ret_cfg.get("chunk_overlap", 80)),
    )
    print(f"[入库] 发现 {len(stats['files'])} 份语料：")
    for name, n in stats["files"].items():
        print(f"  - {name}: {n} 片")

    # ---- 2. BM25 索引 ----
    bm25 = kb_retrieval.BM25Index([kb_retrieval.tokenize(c["content"]) for c in chunks])

    # ---- 3. 向量索引（embedding 服务可用才建） ----
    vectors: Optional[kb_retrieval.VectorStore] = None
    if embedder is not None:
        try:
            matrix = embed_texts([c["content"] for c in chunks], emb_cfg,
                                 os.environ.get(emb_cfg.get("api_key_env", ""), ""))
            vectors = kb_retrieval.VectorStore(matrix.shape[1])
            vectors.add(matrix)
            print(f"[入库] 向量索引建立成功（dim={matrix.shape[1]}, backend={vectors.backend}）")
        except Exception as exc:  # noqa: BLE001 —— EmbeddingUnavailable 及其他
            print(f"[降级] 向量服务不可用，仅建 BM25 索引：{exc}")
            vectors = None
    else:
        print("[入库] embedding.enabled=false，仅建 BM25 索引")

    # ---- 4. 落盘 ----
    path = kb_ingest.save_chunks(chunks, INDEX_DIR)
    if vectors is not None:
        vectors.save(INDEX_DIR)
    print(f"[入库] 完成：共 {stats['total_chunks']} 片 → {path.parent}")


def do_ask(question: str, cfg: Dict) -> None:
    """问答：加载索引 → 混合检索 → 写检索日志 → 装配提示词 → 调 LLM。"""
    emb_cfg, llm_cfg, ret_cfg = cfg["embedding"], cfg["llm"], cfg["retrieval"]

    # ---- 1. 加载索引 ----
    chunks = kb_ingest.load_chunks(INDEX_DIR)      # 不存在时抛 FileNotFoundError
    bm25 = kb_retrieval.BM25Index([kb_retrieval.tokenize(c["content"]) for c in chunks])

    vectors: Optional[kb_retrieval.VectorStore] = None
    has_vector_file = (INDEX_DIR / "vectors.faiss").exists() or (INDEX_DIR / "vectors.npy").exists()
    if has_vector_file and bool(emb_cfg.get("enabled", False)):
        vectors = kb_retrieval.VectorStore.load(INDEX_DIR)

    # ---- 2. 混合检索（core 纯函数，降级信息在 meta 里） ----
    ranked, meta = kb_retrieval.hybrid_search(
        question, bm25, vectors, _make_embedder(emb_cfg), ret_cfg
    )
    if meta.get("degraded"):
        print(f"[降级] {meta.get('degrade_reason')}")
    if not ranked:
        print("[检索] 未命中任何材料。")
        return

    retrieved = [
        {"rank": pos, "rrf": score, "chunk": chunks[idx]}
        for pos, (idx, score) in enumerate(ranked, start=1)
    ]
    print("\n" + "=" * 62)
    print(f"[检索] 命中材料（mode={meta['mode']}，RRF 融合排名）：")
    for pos, item in enumerate(retrieved, start=1):
        c = item["chunk"]
        print(
            f"  [C{pos}] {c['source_file']} ｜ {c['heading'] or '-'} "
            f"｜ {item['rrf']:.4f} ｜ doc_type={c['doc_meta'].get('doc_type', '-')}"
        )
    print("=" * 62)

    # ---- 3. 检索日志（阶段 2 的 retrieval_log 表雏形，append-only 可重放） ----
    log_entry = {"ts": _dt.datetime.now().isoformat(timespec="seconds"),
                 "query": question, **meta}
    with (INDEX_DIR / "retrieval_log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    # ---- 4. 调 LLM（不可用则降级为"仅返回材料"） ----
    try:
        answer = call_llm(
            build_messages(question, retrieved), llm_cfg,
            os.environ.get(llm_cfg.get("api_key_env", ""), ""),
        )
    except LLMUnavailable as exc:
        print(f"[降级] LLM 不可用，本次仅返回检索结果：{exc}")
        answer = None

    if answer:
        print("\n[回答]")
        print(answer)
    else:
        print("\n[回答] 本次无 LLM 回答（见上方降级说明），材料已列出。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="阶段1 RAG 最小原型：知识入库 + 混合检索 + LLM 问答（核心逻辑在 src/）"
    )
    parser.add_argument("--ingest", action="store_true", help="读取 data/knowledge/*.md 建库")
    parser.add_argument("--ask", metavar="QUESTION", help="提问（先 --ingest 建库）")
    parser.add_argument("--top-k", type=int, default=None,
                         help="覆盖 top_k（调试用，默认取配置）")
    args = parser.parse_args()

    if not args.ingest and not args.ask:
        parser.print_help()
        return 0

    cfg = load_config(CONFIG_PATH)   # 既有配置加载器（含 QT_ 环境变量覆盖）
    if args.top_k is not None:
        cfg["retrieval"]["top_k"] = args.top_k

    print(f"[环境] faiss={'已安装' if kb_retrieval.HAS_FAISS else '未安装(降级numpy)'}，"
          f"jieba={'已安装' if kb_retrieval.HAS_JIEBA else '未安装(降级bigram)'}，"
          f"embedding.enabled={cfg['embedding'].get('enabled')}")

    try:
        if args.ingest:
            do_ingest(cfg)
        if args.ask:
            do_ask(args.ask, cfg)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
