"""批次 1b 主管后台测试：上传流水线 + 版本冲突 + 下架重建 + 提示词即时生效。

全部离线（无 LLM / 无 embedding）；用临时库 + 临时索引目录。
"""

from __future__ import annotations

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services import knowledge_admin as kb_admin
from quality_trace.core.services.errors import ApiError, ErrorCode

# 与生产切片参数一致的最小配置
CFG = {"retrieval": {"chunk_size": 500, "chunk_overlap": 80}}

MD_SAMPLE = """# 检验规范（测试）
## 1. 范围
本规范适用于外径 19.98mm 螺纹紧固件。
## 2. 抽样
首件三件全检，过程每两小时抽五件。
"""


def _setup(tmp_path):
    """临时库 + 临时索引目录。"""
    conn = repo.connect(str(tmp_path / "admin.db"))
    repo.init_schema(conn)
    return conn, tmp_path / "index"


def _upload(conn, index_dir, name="检验规范A", version="v1", data=None, **kw):
    """上传一份 markdown 测试语料的便捷封装。"""
    return kb_admin.upload_document(
        conn, CFG,
        filename=f"{name}.md",
        data=data if data is not None else MD_SAMPLE.encode("utf-8"),
        doc_type="standard", version=version,
        effective_from="2026-01-01", effective_to="2030-12-31",
        uploaded_by="tester", index_dir=index_dir,
        save_dir=kb_admin.Path(str(index_dir)).parent / "uploads",
        embedder=None,
        **kw,
    )


# ================================================================ 上传
class TestUpload:
    def test_upload_registers_doc_and_chunks(self, tmp_path):
        """上传 → 文档行 + 切片行 + 索引文件三处都对上。"""
        conn, idx = _setup(tmp_path)
        result = _upload(conn, idx)
        doc = result["doc"]
        assert doc["doc_name"] == "检验规范A"
        assert doc["version"] == "v1"
        assert doc["status"] == "active"
        assert doc["chunk_count"] == result["chunk_count"] == 2
        assert repo.list_docs(conn)[0]["doc_id"] == doc["doc_id"]

        # 索引文件（派生物）已重建，切片带版本元数据
        chunks = repo.list_active_chunks_for_index(conn)
        assert len(chunks) == 2
        assert chunks[0]["doc_meta"]["version"] == "v1"
        saved = (idx / "chunks.json").exists()
        assert saved

    def test_duplicate_name_version_40903(self, tmp_path):
        """同名同版本重复上传 → 40903；不同版本允许并存（v1/v2/v3 共存）。"""
        conn, idx = _setup(tmp_path)
        _upload(conn, idx)
        with pytest.raises(ApiError) as e:
            _upload(conn, idx)                      # 同名同版本
        assert e.value.code == ErrorCode.DOC_VERSION_EXISTS
        # v2 允许并存
        result = _upload(conn, idx, version="v2")
        assert result["doc"]["version"] == "v2"
        assert len(repo.list_docs(conn)) == 2

    def test_missing_required_fields_40001(self, tmp_path):
        """版本号 / 生效区间缺失 → 40001（主管的硬责任）。"""
        conn, idx = _setup(tmp_path)
        with pytest.raises(ApiError) as e:
            kb_admin.upload_document(
                conn, CFG, filename="x.md", data="# x\n正文".encode("utf-8"),
                doc_type="standard", version="",          # 版本号缺失
                effective_from="2026-01-01", effective_to="2030-12-31",
                uploaded_by="t", index_dir=idx,
            )
        assert e.value.code == ErrorCode.BAD_REQUEST

    def test_bad_doc_type_40001(self, tmp_path):
        conn, idx = _setup(tmp_path)
        with pytest.raises(ApiError) as e:
            kb_admin.upload_document(
                conn, CFG, filename="x.md", data="# x\n正文".encode("utf-8"),
                doc_type="unknown", version="v1",
                effective_from="a", effective_to="b",
                uploaded_by="t", index_dir=idx,
            )
        assert e.value.code == ErrorCode.BAD_REQUEST

    def test_unsupported_format_41501(self, tmp_path):
        """非 md/pdf → 41501。"""
        conn, idx = _setup(tmp_path)
        with pytest.raises(ApiError) as e:
            kb_admin.upload_document(
                conn, CFG, filename="x.docx", data=b"binary",
                doc_type="standard", version="v1",
                effective_from="a", effective_to="b",
                uploaded_by="t", index_dir=idx,
            )
        assert e.value.code == ErrorCode.FILE_FORMAT

    def test_pdf_upload(self, tmp_path):
        """PDF 走 pypdf 抽取路径（生成一个最小 PDF 临时文件）。"""
        conn, idx = _setup(tmp_path)
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        # 空白页无文本 → 内容为空应报 40001；断言抽取路径本身可通
        import io

        buf = io.BytesIO()
        writer.write(buf)
        with pytest.raises(ApiError) as e:
            kb_admin.upload_document(
                conn, CFG, filename="x.pdf", data=buf.getvalue(),
                doc_type="standard", version="v1",
                effective_from="a", effective_to="b",
                uploaded_by="t", index_dir=idx,
            )
        assert e.value.code == ErrorCode.BAD_REQUEST   # 内容为空


# ================================================================ 下架 / 恢复
class TestArchive:
    def test_archive_excludes_chunks_from_index(self, tmp_path):
        """下架后：状态 archived、archived_by 留痕、切片退出索引（源头过滤）。"""
        conn, idx = _setup(tmp_path)
        d1 = _upload(conn, idx, name="A")["doc"]
        d2 = _upload(conn, idx, name="B")["doc"]

        result = kb_admin.archive_document(conn, d1["doc_id"], "主管甲", idx)
        assert result["doc"]["status"] == "archived"
        assert result["doc"]["archived_by"] == "主管甲"
        assert result["index_stats"]["active_docs"] == 1     # 只剩 B 在架

        # 索引里只剩 B 的切片——废止版相似度再高也召不回
        chunks = repo.list_active_chunks_for_index(conn)
        assert all(c["source_file"] == "B" for c in chunks)

    def test_archive_missing_40403(self, tmp_path):
        conn, idx = _setup(tmp_path)
        with pytest.raises(ApiError) as e:
            kb_admin.archive_document(conn, "doc-nope", "x", idx)
        assert e.value.code == ErrorCode.DOC_NOT_FOUND

    def test_restore_brings_back(self, tmp_path):
        """误下架可恢复：恢复后切片重新进入索引。"""
        conn, idx = _setup(tmp_path)
        doc = _upload(conn, idx, name="A")["doc"]
        kb_admin.archive_document(conn, doc["doc_id"], "主管甲", idx)
        result = kb_admin.restore_document(conn, doc["doc_id"], idx)
        assert result["doc"]["status"] == "active"
        assert len(repo.list_active_chunks_for_index(conn)) == 2


# ================================================================ 提示词模板
class TestPromptTemplate:
    def test_upsert_and_history(self, tmp_path):
        """首次 upsert 建模板；再次 upsert 更新正文且历史只增不删。"""
        conn, _ = _setup(tmp_path)
        t1 = repo.upsert_template(conn, "默认", "第一条提示词", "甲")
        assert repo.get_active_template(conn)["content"] == "第一条提示词"
        repo.upsert_template(conn, "默认", "第二条提示词", "乙")
        active = repo.get_active_template(conn)
        assert active["content"] == "第二条提示词"
        assert active["updated_by"] == "乙"
        history = repo.list_template_history(conn)
        assert len(history) == 2                       # 每改一次一行
        assert history[0]["content"] == "第二条提示词"  # 最新在前

    def test_chat_reads_active_template(self, tmp_path):
        """问答链路实时读 active 模板：改完立即生效（不写死在代码里）。"""
        conn = repo.connect(":memory:")
        repo.init_schema(conn)
        repo.upsert_template(conn, "默认", "主管定制版提示词：只依据材料回答。", "甲")

        from quality_trace.core.knowledge.prompts import build_chat_messages

        messages = build_chat_messages([], "问题", [], system_prompt=None)
        # 服务层会先取 active 模板再传入；这里直接验证传入路径
        template = repo.get_active_template(conn)
        messages = build_chat_messages([], "问题", [], system_prompt=template["content"])
        assert messages[0]["content"] == "主管定制版提示词：只依据材料回答。"
        # 不传 system_prompt 时退回内置种子
        messages = build_chat_messages([], "问题", [])
        assert "知识库问答助手" in messages[0]["content"]
