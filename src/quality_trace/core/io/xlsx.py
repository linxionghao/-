"""零依赖 XLSX 写出：不装 openpyxl 也能给用户一个能双击打开的表。

为什么不加 `openpyxl` 依赖：
    1. 需求只要求"导出可用的表"，不需要读回、不需要图表、不需要公式；
    2. 课程/答辩环境常常离线，多一个依赖就多一次 `pip install` 失败的机会；
    3. xlsx 本质是一个 zip 里放几份 XML，写出来不到 300 行，
       比排查"openpyxl 版本与 Python 3.9 不兼容"省事。

本模块**只写**不读。需要读 xlsx 的地方（比如导入功能）走 CSV，
或者让用户在 Excel 里"另存为 CSV"——把"读 xlsx"这个坑留给 Excel 自己。

写出的文件结构（最小但完整）：
    [Content_Types].xml
    _rels/.rels
    xl/workbook.xml
    xl/_rels/workbook.xml.rels
    xl/styles.xml
    xl/worksheets/sheet1.xml

三个容易踩的细节，代码里都处理了：
- 单元格文本必须转义 `& < >`，并且**剔除 XML 非法控制字符**（0x00-0x08 等）。
  质量数据里常有从 MES 复制来的 `\\x08`，不剔除会让 Excel 直接报"文件已损坏"。
- 中文列宽：Excel 的宽度单位是"字符数"，中文占 2 个字符宽，
  按字符数 ×1.8 估算才不会把 `<code>DF-20261105-0011</code>` 显示成 `####`。
- 数字与文本要分开写：写成 `t="inlineStr"` 的数字在 Excel 里无法求和，
  用户会以为"导出的数不能算"。这里用 `isinstance(v, (int, float))` 判定。
"""

from __future__ import annotations

import io
import zipfile
from typing import Iterable, List, Optional, Sequence
from xml.sax.saxutils import escape

# XML 1.0 允许的字符范围之外的一律剔除（否则 Excel 判定文件损坏）
_ILLEGAL_XML = {c: None for c in list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20))}


def column_letter(index: int) -> str:
    """1 → A，26 → Z，27 → AA。"""
    if index < 1:
        index = 1
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _clean(text: object) -> str:
    s = "" if text is None else str(text)
    return s.translate(_ILLEGAL_XML)


def _cell_xml(ref: str, value: object, style: int = 0) -> str:
    if value is None or value == "":
        return '<c r="%s" s="%d"/>' % (ref, style) if style else ""
    if isinstance(value, bool):
        return '<c r="%s" t="b"%s><v>%d</v></c>' % (
            ref, ' s="%d"' % style if style else "", 1 if value else 0)
    if isinstance(value, (int, float)):
        return '<c r="%s"%s><v>%s</v></c>' % (
            ref, ' s="%d"' % style if style else "", _num(value))
    return '<c r="%s" t="inlineStr"%s><is><t xml:space="preserve">%s</t></is></c>' % (
        ref, ' s="%d"' % style if style else "", escape(_clean(value)))


def _num(v: float) -> str:
    """数值格式化：整数不带小数点，浮点去掉尾随 0。"""
    if isinstance(v, int):
        return str(v)
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return ("%.10f" % v).rstrip("0").rstrip(".")


def _width_of(value: object) -> int:
    """估算列宽：中文按 2 个字符算，最短 8，最长 48。"""
    s = "" if value is None else str(value)
    width = 0
    for ch in s:
        width += 2 if ord(ch) > 0x2E80 else 1
    return max(8, min(48, width + 2))


def build_xlsx(
    sheets: Sequence[dict],
    *,
    title: Optional[str] = None,
) -> bytes:
    """生成 xlsx 字节。

    sheets: [{"name": "缺陷单", "headers": [...], "rows": [[...], ...]}, ...]
            多个 sheet 会写成多个工作表；名字超过 31 字符会截断（Excel 硬限制）。
    """
    if not sheets:
        sheets = [{"name": "Sheet1", "headers": [], "rows": []}]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _content_types(len(sheets)))
        z.writestr("_rels/.rels", _root_rels())
        z.writestr("xl/workbook.xml", _workbook_xml(sheets, title))
        z.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        z.writestr("xl/styles.xml", _styles_xml())
        for i, sheet in enumerate(sheets, start=1):
            z.writestr("xl/worksheets/sheet%d.xml" % i, _sheet_xml(sheet))
    return buf.getvalue()


def _content_types(n_sheets: int) -> str:
    overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet%d.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument'
        '.spreadsheetml.worksheet+xml"/>' % i
        for i in range(1, n_sheets + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.styles+xml"/>'
        + overrides
        + "</Types>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _workbook_rels(n_sheets: int) -> str:
    rels = "".join(
        '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i)
        for i in range(1, n_sheets + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + rels
        + '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
          'relationships/styles" Target="styles.xml"/>' % (n_sheets + 1)
        + "</Relationships>"
    )


def _workbook_xml(sheets: Sequence[dict], title: Optional[str]) -> str:
    items = "".join(
        '<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
        % (escape(_sheet_name(s.get("name"))), i, i)
        for i, s in enumerate(sheets, start=1)
    )
    props = ""
    if title:
        props = ('<workbookPr/>')
        del title  # 标题不进 workbookPr，只用于文件名（见 exporter）
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        + props
        + "<sheets>" + items + "</sheets></workbook>"
    )


def _sheet_name(name: Optional[str]) -> str:
    """工作表名：去非法字符，截断 31 字符（Excel 硬限制，超了直接打不开）。"""
    s = (name or "Sheet1").strip() or "Sheet1"
    for ch in ("\\", "/", "*", "[", "]", ":", "?"):
        s = s.replace(ch, "_")
    return s[:31]


def _styles_xml() -> str:
    """最小样式表：0=默认，1=表头加粗+灰底，2=数字两位小数。"""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.00"/></numFmts>'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="宋体"/></font>'
        '<font><b/><sz val="11"/><name val="宋体"/></font>'
        "</fonts>"
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEEF2F8"/>'
        '<bgColor indexed="64"/></patternFill></fill>'
        "</fills>"
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )


def _sheet_xml(sheet: dict) -> str:
    headers: List[object] = list(sheet.get("headers") or [])
    rows: Iterable[Sequence[object]] = sheet.get("rows") or []

    # 列宽：取表头与前 50 行的最大值
    widths: List[int] = []
    for i, h in enumerate(headers):
        widths.append(_width_of(h))
    preview = list(rows)[:50]
    for r in preview:
        for i, v in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], _width_of(v))

    cols_xml = ""
    if widths:
        cols_xml = "<cols>" + "".join(
            '<col min="%d" max="%d" width="%d" customWidth="1"/>' % (i + 1, i + 1, w)
            for i, w in enumerate(widths)
        ) + "</cols>"

    parts: List[str] = []
    row_no = 1
    if headers:
        cells = "".join(
            _cell_xml("%s%d" % (column_letter(i + 1), row_no), h, style=1)
            for i, h in enumerate(headers)
        )
        parts.append('<row r="%d" ht="20" customHeight="1">%s</row>' % (row_no, cells))
        row_no += 1

    for r in rows:
        cells = "".join(
            _cell_xml("%s%d" % (column_letter(i + 1), row_no), v)
            for i, v in enumerate(r)
        )
        if cells:
            parts.append('<row r="%d">%s</row>' % (row_no, cells))
        else:
            parts.append('<row r="%d"/>' % row_no)
        row_no += 1

    last_col = column_letter(max(1, len(headers))) if headers else "A"
    last_row = max(1, row_no - 1)
    pane = ""
    if headers:
        pane = ('<sheetViews><sheetView workbookViewId="0">'
                '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                "</sheetView></sheetViews>")
    autofilter = ""
    if headers and last_row > 1:
        autofilter = '<autoFilter ref="A1:%s%d"/>' % (last_col, last_row)

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + pane
        + cols_xml
        + "<sheetData>" + "".join(parts) + "</sheetData>"
        + autofilter
        + "</worksheet>"
    )
