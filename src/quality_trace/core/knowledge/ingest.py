"""语料入库：front-matter 解析 → 标题分段 → 500 字切片 → 索引落盘。

【核心模块】阶段 1 从 examples/phase1_rag_demo.py 迁入。
后续阶段（见 docs/architecture.md 3.2 节）：切片结果写入 kb_document /
kb_chunk 表，本模块的纯函数全部复用，只替换落盘载体（JSON 文件 → SQLite）。

设计要点：
- 元数据设计对齐架构 4.1 节——能进结构化元数据的（doc_type / version /
  effective_from / effective_to / status / defect_code / equipment_id），
  全部随切片携带，绝不指望相似度去"猜"。
- 版本元数据（effective_from / effective_to / status）是架构 4.3 节
  "版本失效三道闸"的数据基础：本阶段先入库，阶段 2 在检索侧启用硬过滤。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

# 切片默认参数（真实值来自 config/llm.yaml 的 retrieval 段）
DEFAULT_CHUNK_SIZE = 500   # 切片目标长度（字符）
DEFAULT_OVERLAP = 80      # 切片重叠，防止关键语句被拦腰切断
CHUNKS_FILENAME = "chunks.json"   # 切片落盘文件名（阶段 2 换 kb_chunk 表）


def _jsonable(value: Any) -> Any:
    """YAML 会把 2026-03-01 解析成 datetime.date，落 JSON 前统一转 ISO 字符串。

    阶段 2 写 SQLite 同样受益（SQLite 没有原生 date 列类型约定）。
    """
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return value


def parse_front_matter(text: str) -> Tuple[Dict[str, Any], str]:
    """解析 markdown 顶部的 `--- ... ---` 元数据块，返回 (meta, body)。

    meta 至少包含 doc_id / doc_type；标准类语料额外携带 version /
    effective_from / effective_to / status（版本闸元数据，见模块 docstring）。
    没有 front-matter 或格式非法时返回 ({}, 原文)。
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = yaml.safe_load(parts[1]) or {}
    return meta, parts[2].lstrip("\n")


def split_sections(md_text: str) -> List[Tuple[str, str]]:
    """按标题行把正文切成 (标题, 段落) 序列，保留标题作为切片上下文。

    质量语料（标准条款 / 8D 步骤）天然按标题组织，标题对齐切片
    让引用溯源能直接定位到"某规范第几条 / 8D 哪一步"。
    """
    sections: List[Tuple[str, str]] = []
    heading, buf = "", []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if heading or body:
            sections.append((heading, body))
        buf.clear()

    for line in md_text.splitlines():
        if line.lstrip().startswith("#"):
            flush()
            heading = line.strip()
        else:
            buf.append(line)
    flush()
    return sections


def chunk_text(
    md_text: str, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP
) -> List[Dict[str, Any]]:
    """把一份文档切成若干 ~500 字切片。

    切片策略（架构 4.1 节）：
      1. 先按标题分段；
      2. 段落超过 chunk_size 时按步长 (chunk_size - overlap) 硬切并保留重叠；
      3. 每个切片带 seq_no 与所在标题，供引用溯源定位。
    """
    chunks: List[Dict[str, Any]] = []
    step = max(chunk_size - overlap, 1)
    seq = 0
    for heading, body in split_sections(md_text):
        if not body:
            continue
        pieces = (
            [body]
            if len(body) <= chunk_size
            else [
                body[i : i + chunk_size]
                for i in range(0, max(len(body) - overlap, 1), step)
            ]
        )
        for piece in pieces:
            content = f"{heading}\n{piece}" if heading else piece
            chunks.append(
                {
                    "seq_no": seq,
                    "heading": heading.strip("# ").strip(),
                    "content": content,
                    "char_len": len(content),
                }
            )
            seq += 1
    return chunks


def load_corpus(
    knowledge_dir: Path, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """读取语料目录全部 *.md，解析 + 切片。

    返回 (切片列表, 统计信息)。切片元素含 source_file 与 doc_meta。
    纯函数（无 print / 无环境变量）：统计信息由调用方决定如何展示。
    """
    docs = sorted(Path(knowledge_dir).glob("*.md"))
    if not docs:
        raise FileNotFoundError(f"{knowledge_dir} 下没有 .md 语料文件")

    chunks: List[Dict[str, Any]] = []
    per_file: Dict[str, int] = {}
    for doc_path in docs:
        meta, body = parse_front_matter(doc_path.read_text(encoding="utf-8"))
        doc_chunks = chunk_text(body, chunk_size, overlap)
        for ch in doc_chunks:
            ch["source_file"] = doc_path.name
            ch["doc_meta"] = meta
        chunks.extend(doc_chunks)
        per_file[doc_path.name] = len(doc_chunks)

    stats = {"files": per_file, "total_chunks": len(chunks)}
    return chunks, stats


def save_chunks(chunks: List[Dict[str, Any]], index_dir: Path) -> Path:
    """切片元数据落盘（JSON）。阶段 2+ 换 kb_chunk 表，签名不变。"""
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "seq_no": c["seq_no"],
            "source_file": c["source_file"],
            "heading": c["heading"],
            "content": c["content"],
            "char_len": c["char_len"],
            "doc_meta": {k: _jsonable(v) for k, v in c["doc_meta"].items()},
        }
        for c in chunks
    ]
    path = index_dir / CHUNKS_FILENAME
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_chunks(index_dir: Path) -> List[Dict[str, Any]]:
    """读取落盘切片。索引不存在时抛 FileNotFoundError（调用方提示先 --ingest）。"""
    path = Path(index_dir) / CHUNKS_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"索引不存在：{path}（请先执行 --ingest）")
    return json.loads(path.read_text(encoding="utf-8"))
