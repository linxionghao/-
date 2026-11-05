# -*- coding: utf-8 -*-
"""缺陷照片处理：校验 + 落盘（移动端已在浏览器侧压缩，服务端只做兜底校验）。

为什么不在服务端压缩：现场用手机拍照，压缩放在浏览器 canvas 里做，能把
2~5MB 的原图在上传前降到 100~300KB，弱网下这是决定「能不能传上去」的关键。
服务端只负责三件事：
1. 校验大小与真实格式（不信任 Content-Type 与扩展名）；
2. 按 magic bytes 识别 JPEG/PNG，其他一律拒；
3. 落盘到 data/uploads/photos/<年-月>/<缺陷号>.<ext>，返回相对路径入库。

安全约定：
- 文件名由**服务端生成的缺陷编号**决定，用户无法控制路径（无穿越风险）；
- 只写不读、不做任何解码/渲染，避免图片解析库漏洞面。
"""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from typing import Tuple

from quality_trace.core.services.errors import ApiError, ErrorCode

MAX_PHOTO_BYTES = 2 * 1024 * 1024       # 与错误码 40003 文案一致：2MB
_DATA_URL_RE = re.compile(r"^data:image/(jpeg|jpg|png);base64,", re.IGNORECASE)

# 文件头魔数 → 扩展名（顺序敏感：JPEG 先判）
_MAGIC = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
]


def decode_photo(data_url: str) -> Tuple[bytes, str]:
    """解析前端传来的 base64 图片，返回 (原始字节, 扩展名)。

    接受两种形态：`data:image/jpeg;base64,....` 或纯 base64 字符串。
    任何异常统一抛 40003（文案固定，避免把解析细节暴露给前端）。
    """
    raw = (data_url or "").strip()
    if not raw:
        raise ApiError(ErrorCode.PHOTO_INVALID)

    # 去掉 data URL 前缀（没有前缀时按纯 base64 处理）
    payload = _DATA_URL_RE.sub("", raw)
    if "," in payload and payload.startswith("data:"):
        raise ApiError(ErrorCode.PHOTO_INVALID)

    # base64 长度上限粗筛：2MB 原始数据编码后约 2.7MB，超了直接拒，省一次解码
    if len(payload) > MAX_PHOTO_BYTES * 4 // 3 + 16:
        raise ApiError(ErrorCode.PHOTO_INVALID)

    try:
        blob = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(ErrorCode.PHOTO_INVALID) from None

    if not blob:
        raise ApiError(ErrorCode.PHOTO_INVALID)
    if len(blob) > MAX_PHOTO_BYTES:
        raise ApiError(ErrorCode.PHOTO_INVALID)

    for magic, ext in _MAGIC:
        if blob.startswith(magic):
            return blob, ext
    raise ApiError(ErrorCode.PHOTO_INVALID)


def save_photo(blob: bytes, ext: str, defect_no: str, upload_dir: Path) -> str:
    """按缺陷号落盘，返回相对仓库根的路径字符串（入库用，不存绝对路径）。"""
    month_dir = upload_dir / "photos" / defect_no[:10].replace("-", "")[:6]
    month_dir.mkdir(parents=True, exist_ok=True)
    target = month_dir / f"{defect_no}.{ext}"
    target.write_bytes(blob)
    return str(target).replace("\\", "/")


def attach_photo(
    conn, defect_no: str, data_url: str, upload_dir: Path,
) -> str:
    """校验 + 落盘 + 回写路径；data_url 为空时直接返回空串（照片是可选项）。"""
    if not (data_url or "").strip():
        return ""
    from quality_trace.core.repo import db as repo

    blob, ext = decode_photo(data_url)
    path = save_photo(blob, ext, defect_no, Path(upload_dir))
    repo.set_defect_photo(conn, defect_no, path)
    return path
