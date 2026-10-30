"""主管后台服务：知识库上传/下架 + 索引重建 + 提示词模板管理。

【核心模块】docs/stage3-design.md 4.2–4.3 节的实现（批次 1b）。

核心约定：
- **DB 为权威，索引文件为派生物**：kb_doc/kb_chunk 是唯一事实源，
  任何上传/下架都触发全量索引重建（课规模 <1 万切片，同步秒级）；
- **废止 = 下架（archive），永不物理删除**：原始文件与切片行全部保留，
  归因审计仍可回溯"当时引用了什么"；
- 上传时 embedding 不可用 → 文档照常入库（vector_status=pending，
  50004 降级非失败），BM25 检索不受影响；
- core 约束：不 print、不读环境变量（Key 由入口层传入）。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from quality_trace.core.knowledge import ingest as kb_ingest
from quality_trace.core.knowledge import retrieval as kb_retrieval
from quality_trace.core.repo import db as repo
from quality_trace.core.services.errors import ApiError, ErrorCode

# 允许的语料类型（与架构 3.2 知识库三表一致）
ALLOWED_DOC_TYPES = ("standard", "sop_manual", "8d_case")

# 允许的上传格式与大小上限（docs/stage3-design.md 4.2）
ALLOWED_EXTS = (".md", ".pdf")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


# ================================================================ 文本抽取
def extract_text(filename: str, data: bytes) -> tuple:
    """从上传文件抽纯文本，返回 (文本, 不含 front-matter 的正文, 扩展名)。

    - .md：UTF-8 直读；有 front-matter 则解析并剥离（元数据以表单字段为准）；
    - .pdf：pypdf 逐页抽取纯文本（决策 C①：检验标准类文档够用）。
    """
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ApiError(ErrorCode.FILE_FORMAT)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError(ErrorCode.FILE_TOO_LARGE)

    if ext == ".md":
        text = data.decode("utf-8", errors="replace")
        _meta, body = kb_ingest.parse_front_matter(text)
        return text, body, ext

    # ---- PDF：pypdf 抽取（延迟导入，无 PDF 场景不加载） ----
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 —— pypdf 对坏文件抛的异常类型不稳定
        raise ApiError(ErrorCode.BAD_REQUEST, f"PDF 解析失败：{exc}") from None
    body = "\n\n".join(p.strip() for p in pages if p.strip())
    return body, body, ext


# ================================================================ 上传
def upload_document(
    conn,
    cfg: Dict[str, Any],
    *,
    filename: str,
    data: bytes,
    doc_type: str,
    version: str,
    effective_from: str,
    effective_to: str,
    uploaded_by: str,
    index_dir: Optional[Path] = None,
    save_dir: Optional[Path] = None,
    embedder: Optional[Callable[[List[str]], Any]] = None,
) -> Dict[str, Any]:
    """上传一份文档：校验 → 抽文本 → 切片 → 落库 → 重建索引。

    版本语义：同一 doc_name 允许 v1/v2/v3 并存（历史审计需要），
    但同名同版本唯一（40903）；检索永远只用 active 版本。
    """
    # ---- 1) 必填项与类型校验（版本号与生效区间是主管的硬责任） ----
    if doc_type not in ALLOWED_DOC_TYPES:
        raise ApiError(ErrorCode.BAD_REQUEST, "doc_type 必须是 standard / sop_manual / 8d_case")
    if not version or not effective_from or not effective_to:
        raise ApiError(ErrorCode.BAD_REQUEST, "版本号与生效区间（起/止）均必填")
    doc_name = Path(filename).stem
    if not doc_name:
        raise ApiError(ErrorCode.BAD_REQUEST, "文件名为空")

    # ---- 2) 抽文本 + 切片（复用阶段 1 的切片器） ----
    _text, body, ext = extract_text(filename, data)
    if not body.strip():
        raise ApiError(ErrorCode.BAD_REQUEST, "文档内容为空，无法切片")
    ret_cfg = cfg.get("retrieval") or {}
    chunks = kb_ingest.chunk_text(
        body,
        int(ret_cfg.get("chunk_size", 500)),
        int(ret_cfg.get("chunk_overlap", 80)),
    )

    # ---- 3) 注册 + 切片落库（同名同版本冲突 → 40903） ----
    try:
        doc = repo.insert_doc(
            conn, doc_name, doc_type, version.strip(),
            effective_from.strip(), effective_to.strip(),
            uploaded_by=uploaded_by, chunk_count=len(chunks),
        )
    except repo.DuplicateDocVersion:
        raise ApiError(ErrorCode.DOC_VERSION_EXISTS) from None
    try:
        repo.insert_chunks(conn, doc["doc_id"], chunks)
    except Exception:
        # 切片失败则回滚文档行，避免出现零切片的"空文档"
        conn.execute("DELETE FROM kb_doc WHERE doc_id = ?", (doc["doc_id"],))
        conn.commit()
        raise

    # ---- 4) 原始文件留档（下架不删除的物理层保障） ----
    if save_dir is not None:
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        target = save_dir / f"{doc['doc_id']}{ext}"
        target.write_bytes(data)
        conn.execute("UPDATE kb_doc SET source_path = ? WHERE doc_id = ?",
                     (str(target), doc["doc_id"]))
        conn.commit()

    # ---- 5) 索引重建（DB 权威 → 派生物） ----
    stats = rebuild_index(conn, index_dir or Path("data/phase1_index"),
                          embedder=embedder)

    # ---- 6) 向量化（可选）：失败不阻塞入库，50004 降级 ----
    vector_ok = False
    if embedder is not None:
        try:
            _embed_and_save(conn, doc["doc_id"], chunks, embedder, index_dir)
            vector_ok = True
        except Exception:  # noqa: BLE001 —— embedding 失败属降级，不失败
            vector_ok = False
    repo.set_doc_vector_status(conn, doc["doc_id"], vector_ok)

    return {
        "doc": repo.get_doc(conn, doc["doc_id"]),
        "chunk_count": len(chunks),
        "index_stats": stats,
        "vector_ok": vector_ok,
    }


def _embed_and_save(conn, doc_id: str, chunks: List[Dict[str, Any]],
                    embedder: Callable[[List[str]], Any],
                    index_dir: Optional[Path]) -> None:
    """对新入库文档做向量化并落盘（课规模直接整库重建向量最简单）。"""
    _ = doc_id, conn
    vectors = embedder([c["content"] for c in chunks])
    store = kb_retrieval.VectorStore(int(vectors.shape[1]))
    store.add(kb_retrieval._normalize(vectors))
    store.save(Path(index_dir or "data/phase1_index"))


# ================================================================ 下架 / 恢复
def archive_document(conn, doc_id: str, actor: str,
                     index_dir: Optional[Path] = None) -> Dict[str, Any]:
    """下架（废止）：status→archived + 触发重建（切片即刻退出检索）。"""
    if not repo.set_doc_status(conn, doc_id, "archived", actor):
        raise ApiError(ErrorCode.DOC_NOT_FOUND)
    stats = rebuild_index(conn, index_dir or Path("data/phase1_index"))
    return {"doc": repo.get_doc(conn, doc_id), "index_stats": stats}


def restore_document(conn, doc_id: str,
                     index_dir: Optional[Path] = None) -> Dict[str, Any]:
    """恢复上架（误下架补救；恢复后同样触发重建）。"""
    if not repo.set_doc_status(conn, doc_id, "active"):
        raise ApiError(ErrorCode.DOC_NOT_FOUND)
    stats = rebuild_index(conn, index_dir or Path("data/phase1_index"))
    return {"doc": repo.get_doc(conn, doc_id), "index_stats": stats}


# ================================================================ 索引重建
def rebuild_index(
    conn,
    index_dir: Path,
    embedder: Optional[Callable[[List[str]], Any]] = None,
) -> Dict[str, Any]:
    """从 kb_chunk 重建检索索引（chunks.json [+ 向量]）。

    - 只读 status=active 的文档——**废止版本的切片在源头就进不了索引**，
      相似度再高也召不回（版本闸第一道的数据基础）；
    - 无 embedder 时清理旧向量文件，避免「切片数变了、向量没变」的错位。
    """
    index_dir = Path(index_dir)
    chunks = repo.list_active_chunks_for_index(conn)
    kb_ingest.save_chunks(chunks, index_dir)

    vector_note = "skipped"
    if embedder is not None and chunks:
        try:
            vectors = embedder([c["content"] for c in chunks])
            store = kb_retrieval.VectorStore(int(vectors.shape[1]))
            store.add(kb_retrieval._normalize(vectors))
            store.save(index_dir)
            vector_note = "ok"
        except Exception:  # noqa: BLE001
            vector_note = "failed"
    else:
        # 无向量服务 → 清理可能存在的旧向量文件（BM25-only 也完全可用）
        for vec_name in ("vectors.faiss", "vectors.npy"):
            vec_path = index_dir / vec_name
            if vec_path.exists():
                vec_path.unlink()

    return {"active_docs": len({c["doc_meta"].get("doc_id") for c in chunks}),
            "total_chunks": len(chunks), "vector": vector_note}
