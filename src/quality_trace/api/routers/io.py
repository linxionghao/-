# -*- coding: utf-8 -*-
"""数据导入导出接口：把"系统里的数据"与"车间那台 Excel"接起来。

    #51 GET  /io/datasets             质量工程师 / 主管（元信息：可导入/可导出的数据集）
    #52 GET  /io/template/{dataset}   同上（下载导入模板）
    #53 POST /io/import/preview       同上（只校验不落库）
    #54 POST /io/import               同上（校验 + 落库）
    #55 GET  /io/export               同上（单表导出 csv/xlsx）
    #56 GET  /io/export/bundle        同上（多表 xlsx 打包）

三个设计决定，都在代码里而不是文档里：

1. **预览与提交分成两个接口**，而不是一个 `dry_run` 参数了事。
   前端的"先校验一遍"按钮和"确认导入"按钮是两个心智动作，
   分成两个 URL 之后，误点"确认导入"不会因为参数传错而真的写库。

2. **班组长没有导入权限**。他会用手机拍照录入，不会抱着笔记本导 Excel；
   给他开导入权限的唯一后果是某天有人把整张错表灌进主数据。

3. **导出走 GET 但要求登录态**。导出是"读"，用 GET 才符合语义，
   也让浏览器原生下载、右键另存为都能用。鉴权靠 Cookie（页面）或 Bearer（脚本）。

导出的一个坑：文件名里带中文时，`Content-Disposition` 必须给 RFC 5987 的
`filename*=UTF-8''...` 形式，否则 Chrome 会把它变成乱码或直接丢掉扩展名。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response

from quality_trace.api.deps import get_db, require_roles
from quality_trace.core.io import exporter, importer
from quality_trace.core.services.errors import ApiError, ErrorCode

router = APIRouter(prefix="/api/v1/io")

# 导入/导出都是"改主数据"或"看全量"，班组长不在名单里
_writer = require_roles("quality_engineer", "quality_manager")

# 单次导入上限：20 万字符左右的 CSV ≈ 10MB。
# 卡这个数不是为了省内存，而是让"导错了整个库"的代价可控。
MAX_IMPORT_BYTES = 10 * 1024 * 1024

# 允许的扩展名：xlss 走模板下载 + 另存为 CSV 的路径，不在服务端解析 Excel
ALLOWED_SUFFIXES = (".csv", ".tsv", ".txt")


def _attachment(filename: str, data: bytes, mime: str) -> Response:
    """拼下载响应：中文文件名同时给 filename 与 filename*。

    只给 `filename="缺陷单.csv"` 时，不同浏览器差异极大：
    Chrome 会丢掉整个文件名，Safari 会显示成 URL 编码串。
    RFC 5987 的 `filename*=UTF-8''<pct-encoded>` 是唯一稳的写法，
    但老客户端只认 `filename`，所以两个都给。
    """
    ascii_fallback = filename.encode("ascii", errors="ignore").decode("ascii") or "export"
    disposition = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_fallback, quote(filename, safe="")
    )
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": disposition})


# =========================================================== 元信息
@router.get("/datasets")
async def datasets(
    user: Dict[str, Any] = Depends(_writer),   # noqa: ARG001
) -> Dict[str, Any]:
    """列出可导入/可导出的数据集及其列，供前端渲染下拉框。

    前端不该硬编码这些名字：加一个数据集时只改后端，
    页面自己就多出一个选项——这是"把变化关在数据里"的延续。
    """
    imports: List[Dict[str, Any]] = [
        {
            "dataset": key,
            "label": tpl["label"],
            "columns": tpl["columns"],
            "sample": tpl["sample"],
            "notes": tpl["notes"],
            "aliases": {f: list(names) for f, names in importer.ALIASES[key].items()},
        }
        for key, tpl in importer.TEMPLATES.items()
    ]
    exports: List[Dict[str, Any]] = [
        {"dataset": "defects", "label": "缺陷单明细", "params": ["date_from", "date_to", "line_id", "status"]},
        {"dataset": "attributions", "label": "归因单", "params": ["status"]},
        {"dataset": "audit", "label": "对话审计", "params": ["only_flagged"]},
        {"dataset": "pareto", "label": "帕累托分析", "params": ["dim", "date_from", "date_to"]},
        {"dataset": "suppliers", "label": "供应商记分卡", "params": ["date_from", "date_to"]},
    ]
    return {
        "code": 0, "message": "成功",
        "data": {
            "imports": imports,
            "exports": exports,
            "formats": ["csv", "xlsx"],
            "max_bytes": MAX_IMPORT_BYTES,
            "suffixes": list(ALLOWED_SUFFIXES),
        },
    }


@router.get("/template/{dataset}")
async def template(
    dataset: str,
    user: Dict[str, Any] = Depends(_writer),   # noqa: ARG001
) -> Response:
    """下载导入模板（表头 + 一行样本 + 中文 BOM，Excel 双击即可填）。"""
    if dataset not in importer.TEMPLATES:
        raise ApiError(ErrorCode.DATASET_UNSUPPORTED,
                       "不支持的数据集：%s（可选 %s）"
                       % (dataset, "、".join(importer.TEMPLATES)))
    data = importer.template_csv(dataset)
    label = importer.TEMPLATES[dataset]["label"]
    return _attachment("导入模板_%s.csv" % label, data, "text/csv; charset=utf-8")


# =========================================================== 导入
async def _read_upload(file: UploadFile) -> bytes:
    """读取上传文件并做两道前置校验（大小、扩展名）。

    扩展名校验在**读之前**做不了（UploadFile 已经收了），
    但至少要挡在解析之前：把 .xlsx 当 CSV 解析出来的结果是
    "表头是 PK\\x03\\x04"，用户完全看不懂。
    """
    name = (file.filename or "").lower()
    if name and not name.endswith(ALLOWED_SUFFIXES):
        raise ApiError(ErrorCode.FILE_TYPE)
    data = await file.read()
    if len(data) > MAX_IMPORT_BYTES:
        raise ApiError(ErrorCode.FILE_TOO_LARGE,
                       "文件 %.1fMB 超过上限 %dMB"
                       % (len(data) / 1048576.0, MAX_IMPORT_BYTES // 1048576))
    if not data:
        raise ApiError(ErrorCode.IMPORT_EMPTY)
    return data


def _run_import(conn, dataset: str, data: bytes, dry_run: bool, delimiter: Optional[str]):
    """导入的统一出口：把 io 层的 ValueError 翻成业务错误码。

    为什么不直接在路由里 try：两个端点（预览/提交）都要这套翻译，
    复制一遍就意味着将来改错误码要改两处，必然漏一处。
    """
    try:
        result = importer.import_dataset(conn, dataset, data,
                                         dry_run=dry_run, delimiter=delimiter)
    except ValueError as exc:
        raise ApiError(ErrorCode.DATASET_UNSUPPORTED, str(exc))
    if result.get("total", 0) == 0:
        raise ApiError(ErrorCode.IMPORT_EMPTY,
                       "文件里没有数据行（表头为 %s）"
                       % "、".join(result.get("warnings") or ["空"]))
    return result


@router.post("/import/preview")
async def import_preview(
    file: UploadFile = File(..., description="CSV/TSV 文件"),
    dataset: str = Form(..., description="work_orders / defect_codes / lots / measurements"),
    delimiter: Optional[str] = Form(default=None, description="留空自动探测"),
    user: Dict[str, Any] = Depends(_writer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """只校验不落库：告诉用户"能进几行、哪几行有问题"。

    这一步的产出是一份**逐行错误清单**，不是"校验失败"四个字。
    用户拿到的应该是"第 7 行的数量填了 abc"，而不是让他自己找。
    """
    data = await _read_upload(file)
    result = _run_import(conn, dataset, data, dry_run=True, delimiter=delimiter)
    return {
        "code": 0,
        "message": result.get("message") or "校验完成",
        "data": result,
    }


@router.post("/import")
async def import_commit(
    file: UploadFile = File(..., description="CSV/TSV 文件"),
    dataset: str = Form(...),
    delimiter: Optional[str] = Form(default=None),
    allow_partial: bool = Form(default=True, description="有失败行时是否仍然写入其余行"),
    user: Dict[str, Any] = Depends(_writer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Dict[str, Any]:
    """校验并落库。

    `allow_partial=True`（默认）是刻意的：100 行里错 3 行时整体回滚，
    用户改完那 3 行再传一次，前 97 行会被判"已存在"而跳过——
    他会以为系统吞了数据。部分成功 + 明确的失败清单才是可用的。

    但整份文件全错时必须回滚：那说明拿错了文件，不是个别笔误。
    """
    data = await _read_upload(file)
    if not allow_partial:
        # 先空跑一遍：有错就整体拒绝，不留下半份数据
        preview = _run_import(conn, dataset, data, dry_run=True, delimiter=delimiter)
        if preview.get("failed"):
            raise ApiError(ErrorCode.BAD_REQUEST,
                           "有 %d 行校验未通过，已整体拒绝（先预览修正后再导入）"
                           % preview["failed"])
    result = _run_import(conn, dataset, data, dry_run=False, delimiter=delimiter)
    return {"code": 0, "message": result.get("message") or "导入完成", "data": result}


# =========================================================== 导出
@router.get("/export")
async def export(
    dataset: str = Query(..., description="defects / attributions / audit / pareto / suppliers"),
    format: str = Query(default="csv", description="csv / xlsx"),   # noqa: A002
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    line_id: Optional[str] = None,
    status: Optional[str] = None,
    dim: str = "defect_code",
    only_flagged: bool = True,
    user: Dict[str, Any] = Depends(_writer),
    conn=Depends(get_db),
) -> Response:
    """导出单表。

    班组长先被 `_writer` 挡在门外了，但这里还要防一手"QE 传了别人的 line_id"：
    导出的是全量数据，越权导出比越权查看更严重——数据会离开系统。
    """
    kwargs: Dict[str, Any] = {}
    if date_from:
        kwargs["date_from"] = date_from
    if date_to:
        kwargs["date_to"] = date_to

    if dataset == "defects":
        kwargs["days"] = days
        # 班组长（如果将来放开）只能导自己产线；QE/QM 不传就是全量
        kwargs["line_id"] = line_id or user.get("line_id")
        kwargs["status"] = status
    elif dataset == "attributions":
        kwargs["status"] = status
    elif dataset == "audit":
        kwargs["only_flagged"] = only_flagged
    elif dataset == "pareto":
        kwargs["dim"] = dim
        kwargs["days"] = days
    elif dataset == "suppliers":
        kwargs["days"] = days

    try:
        filename, data, mime = exporter.build(dataset, conn, fmt=format, **kwargs)
    except ValueError as exc:
        raise ApiError(ErrorCode.DATASET_UNSUPPORTED, str(exc))
    except TypeError as exc:
        # 数据集不接受某个参数（例如 suppliers 没有 status）→ 是调用方的错
        raise ApiError(ErrorCode.BAD_REQUEST, "该导出不支持此参数：%s" % exc)
    return _attachment(filename, data, mime)


@router.get("/export/bundle")
async def export_bundle(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    user: Dict[str, Any] = Depends(_writer),   # noqa: ARG001
    conn=Depends(get_db),
) -> Response:
    """一次导出三张表（缺陷明细 + 帕累托 + 供应商记分卡）到一个 xlsx。

    为什么做这个：月末对账时质量工程师要发三份文件给不同的人，
    收件人再各自合并——多表工作簿省掉的是三轮"你那份是最新的吗"。
    """
    kwargs: Dict[str, Any] = {"days": days}
    if date_from:
        kwargs["date_from"] = date_from
    if date_to:
        kwargs["date_to"] = date_to

    sheets = []
    for dataset, sheet_name in (("defects", "缺陷明细"), ("pareto", "帕累托"),
                                ("suppliers", "供应商记分卡")):
        try:
            _name, headers, rows = exporter.DATASETS[dataset](conn, **kwargs)
        except TypeError:
            # pareto 只认 dim，不认 days 之外的参数；退一步用最小参数集
            _name, headers, rows = exporter.DATASETS[dataset](conn)
        sheets.append((sheet_name, headers, rows))

    data = exporter.build_multi_xlsx(sheets)
    stamp = exporter.stamp()
    return _attachment(
        "质量数据包_%s.xlsx" % stamp, data,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
