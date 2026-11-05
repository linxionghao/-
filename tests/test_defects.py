"""批次 2 班组长录入端测试：归一化 / 建单 / 确认 / 越权 / 照片。

全部离线：LLM 用 monkeypatch 替身，照片用 1x1 JPEG 的最小字节序列，
临时库 + 临时上传目录，不触碰演示数据。
"""

from __future__ import annotations

import base64
import json

import pytest

from quality_trace.core.repo import db as repo
from quality_trace.core.services import defects as defect_service
from quality_trace.core.services import normalize as normalize_service
from quality_trace.core.services import photo as photo_service
from quality_trace.core.services.errors import ApiError, ErrorCode

CODES = [
    {"code": "D-THREAD", "name": "螺纹外径超差", "category": "尺寸缺陷",
     "keywords_json": json.dumps(["螺纹", "外径", "偏小", "通规"], ensure_ascii=False)},
    {"code": "D-SURF", "name": "表面划伤", "category": "外观缺陷",
     "keywords_json": json.dumps(["划伤", "划痕", "毛刺"], ensure_ascii=False)},
    {"code": "D-OTHER", "name": "其他", "category": "其他", "keywords_json": "[]"},
]

# 最小合法 JPEG（SOI + APP0 头即可通过魔数校验，无需真实可解码）
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 32


# ---------------------------------------------------------------------------
# 归一化：关键词回退
# ---------------------------------------------------------------------------
class TestKeywordCandidates:
    def test_hits_by_keyword(self):
        result = normalize_service.keyword_candidates("螺纹外径偏小，通规过不去", CODES)
        assert result[0]["code"] == "D-THREAD"
        assert result[0]["source"] == "keyword"
        assert 0.3 <= result[0]["confidence"] <= 1.0

    def test_zero_score_dropped(self):
        """完全不相关的描述不返回候选——宁缺勿滥，避免误导现场。"""
        assert normalize_service.keyword_candidates("今天天气不错", CODES) == []

    def test_empty_description(self):
        assert normalize_service.keyword_candidates("", CODES) == []
        assert normalize_service.keyword_candidates("   ", CODES) == []

    def test_broken_keywords_tolerated(self):
        """字典里 keywords_json 写坏了也不能让建单流程挂掉。"""
        bad = [{"code": "X", "name": "坏字典", "category": "其他",
                "keywords_json": "{not json"}]
        assert normalize_service.keyword_candidates("坏字典", bad) != []

    def test_top_n_limit(self):
        result = normalize_service.keyword_candidates(
            "螺纹外径偏小 划伤 毛刺 通规", CODES, top_n=1)
        assert len(result) == 1
