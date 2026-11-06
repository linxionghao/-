# -*- coding: utf-8 -*-
"""极简 docx 排版工具：A4 / 中文样式 / 三线感表格 / 页眉页脚 / 插图。

为什么不用 Office 编辑器通道
---------------------------
测试文档与运行手册是**程序化生成**的（用例表动辄 40 行、图 6 张），
走 python-docx 直接产出比逐个 API 调用快一个数量级，且可纳入版本管理、
可重复生成——改了用例重跑脚本即可，不会出现"文档与实测不一致"。

排版约定（与 docs/ 下既有 Word 文档保持一致）
--------------------------------------------
- A4，上下 2.54cm / 左右 3.17cm
- 正文 宋体 10.5pt，1.5 倍行距，首行缩进 2 字符
- 一级标题 16pt 黑体、二级 14pt、三级 12pt
- 表格：浅灰表头 + 细边框 + 跨页重复表头 + 行不跨页
- 页眉右对齐带下框线，页脚居中页码
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

BODY_FONT = "宋体"
HEAD_FONT = "黑体"
EN_FONT = "Times New Roman"
GRAY = "595959"
HEAD_FILL = "EDEFF2"
BORDER = "C8CCD4"

BODY_PT = 10.5
LINE_SPACING = 1.5


# --------------------------------------------------------------------------- #
# 底层工具
# --------------------------------------------------------------------------- #

def _set_run_font(run, *, name: str, size: float, bold: bool = False,
                  color: Optional[str] = None, en: str = EN_FONT) -> None:
    run.font.name = en
    run.font.size = Pt(size)
    run.font.bold = bold
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), name)
    rfonts.set(qn("w:ascii"), en)
    rfonts.set(qn("w:hAnsi"), en)


#: 行内标记：**粗体** 与 `代码`。用非贪婪匹配，避免一行里多个粗体被整段吞掉。
INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def _add_rich(para, text: str, *, size: float, bold: bool = False,
              color: Optional[str] = None, name: str = BODY_FONT) -> None:
    """把一段带 **粗体** / `代码` 标记的文本拆成多个 run 写入段落。

    单独抽出来是因为段落、列表项、表格单元格都需要同一套行内解析——
    之前只有段落做了 `**` 分割，列表和表格里的标记会原样露出星号和反引号。
    """
    for token in INLINE_RE.split(str(text)):
        if not token:
            continue
        if len(token) > 4 and token.startswith("**") and token.endswith("**"):
            run = para.add_run(token[2:-2])
            _set_run_font(run, name=name, size=size, bold=True, color=color)
        elif len(token) > 2 and token.startswith("`") and token.endswith("`"):
            run = para.add_run(token[1:-1])
            _set_run_font(run, name=name, size=size, bold=bold, color="34506B",
                          en="Consolas")
        else:
            run = para.add_run(token)
            _set_run_font(run, name=name, size=size, bold=bold, color=color)


def _para_border_bottom(paragraph, color: str = "D0D3D9", sz: int = 4) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(sz))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), color)
    pbdr.append(bottom)
    ppr.append(pbdr)


def _cell_shade(cell, fill: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def _table_borders(table, color: str = BORDER, sz: int = 4) -> None:
    tblpr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), str(sz))
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    tblpr.append(borders)


def _row_no_split(row, header: bool = False) -> None:
    trpr = row._tr.get_or_add_trPr()
    if not trpr.find(qn("w:cantSplit")):
        trpr.append(OxmlElement("w:cantSplit"))
    if header and not trpr.find(qn("w:tblHeader")):
        trpr.append(OxmlElement("w:tblHeader"))


def _fixed_layout(table) -> None:
    tblpr = table._tbl.tblPr
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    tblpr.append(layout)


# --------------------------------------------------------------------------- #
# 文档封装
# --------------------------------------------------------------------------- #

class Doc:
    """一份 A4 中文文档。"""

    def __init__(
        self,
        path: Path,
        *,
        header_text: str = "",
        title: str = "",
        subtitle: Sequence[str] = (),
    ) -> None:
        self.path = Path(path)
        self.doc = Document()
        self._setup_page()
        self._setup_styles()

        if header_text:
            self._setup_header(header_text)
        self._setup_footer()

        if title:
            self.title(title, subtitle)

    # ---- 页面与样式 ----
    def _setup_page(self) -> None:
        s = self.doc.sections[0]
        s.page_width = Cm(21)
        s.page_height = Cm(29.7)
        s.top_margin = Cm(2.54)
        s.bottom_margin = Cm(2.54)
        s.left_margin = Cm(3.17)
        s.right_margin = Cm(3.17)
        s.header_distance = Cm(1.4)
        s.footer_distance = Cm(1.4)

    def _setup_styles(self) -> None:
        normal = self.doc.styles["Normal"]
        normal.font.name = EN_FONT
        normal.font.size = Pt(BODY_PT)
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
        pf = normal.paragraph_format
        pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
        pf.line_spacing = LINE_SPACING
        pf.space_before = Pt(3)
        pf.space_after = Pt(3)

    def _setup_header(self, text: str) -> None:
        hdr = self.doc.sections[0].header
        p = hdr.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        run = p.add_run(text)
        _set_run_font(run, name=BODY_FONT, size=9, color="888888")
        _para_border_bottom(p)

    def _setup_footer(self) -> None:
        ftr = self.doc.sections[0].footer
        p = ftr.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run()
        _set_run_font(run, name=BODY_FONT, size=9, color="666666")

        fld_begin = OxmlElement("w:fldChar")
        fld_begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = " PAGE "
        fld_end = OxmlElement("w:fldChar")
        fld_end.set(qn("w:fldCharType"), "end")
        for el in (fld_begin, instr, fld_end):
            run._r.append(el)

    # ---- 标题与正文 ----
    def title(self, text: str, subtitle: Sequence[str] = ()) -> None:
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.space_after = Pt(8)
        run = p.add_run(text)
        _set_run_font(run, name=HEAD_FONT, size=18, bold=True, color="1F2937")

        for line in subtitle:
            sp = self.doc.add_paragraph()
            sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
            sp.paragraph_format.space_after = Pt(2)
            r = sp.add_run(line)
            _set_run_font(r, name=BODY_FONT, size=9, color=GRAY)

    def h1(self, text: str) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(16)
        p.paragraph_format.space_after = Pt(8)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        _set_run_font(run, name=HEAD_FONT, size=15, bold=True, color="111827")
        _para_border_bottom(p, color="D6DAE3", sz=6)

    def h2(self, text: str) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(5)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        _set_run_font(run, name=HEAD_FONT, size=12.5, bold=True, color="1F2937")

    def h3(self, text: str) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(8)
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        _set_run_font(run, name=HEAD_FONT, size=11, bold=True, color="374151")

    def p(self, text: str, *, indent: bool = True, size: float = BODY_PT,
          color: Optional[str] = None, bold: bool = False,
          align: str = "left", space_after: float = 3) -> None:
        para = self.doc.add_paragraph()
        para.alignment = {
            "left": WD_ALIGN_PARAGRAPH.LEFT,
            "center": WD_ALIGN_PARAGRAPH.CENTER,
            "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
            "right": WD_ALIGN_PARAGRAPH.RIGHT,
        }[align]
        if indent:
            para.paragraph_format.first_line_indent = Pt(BODY_PT * 2)
        para.paragraph_format.space_after = Pt(space_after)
        _add_rich(para, text, size=size, bold=bold, color=color)

    def bullets(self, items: Iterable[str], *, marker: str = "·") -> None:
        for it in items:
            para = self.doc.add_paragraph()
            para.paragraph_format.left_indent = Pt(BODY_PT * 2)
            para.paragraph_format.first_line_indent = Pt(-BODY_PT)
            para.paragraph_format.space_after = Pt(2)
            run = para.add_run(f"{marker} ")
            _set_run_font(run, name=BODY_FONT, size=BODY_PT)
            _add_rich(para, it, size=BODY_PT)

    def code(self, text: str) -> None:
        for line in text.splitlines():
            para = self.doc.add_paragraph()
            para.paragraph_format.left_indent = Pt(14)
            para.paragraph_format.space_after = Pt(0)
            para.paragraph_format.line_spacing = 1.15
            run = para.add_run(line if line else " ")
            _set_run_font(run, name=BODY_FONT, size=9, color="374151", en="Consolas")

    # ---- 表格 ----
    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        widths: Optional[Sequence[float]] = None,
        font_size: float = 9,
        caption: Optional[str] = None,
        align_center_cols: Sequence[int] = (),
        zebra: bool = False,
    ) -> None:
        """插入表格。widths 单位为厘米，总和建议 ≤ 14.6（正文宽度）。"""
        if caption:
            cp = self.doc.add_paragraph()
            cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cp.paragraph_format.space_before = Pt(6)
            cp.paragraph_format.space_after = Pt(2)
            cp.paragraph_format.keep_with_next = True
            r = cp.add_run(caption)
            _set_run_font(r, name=HEAD_FONT, size=9.5, bold=True, color="374151")

        ncol = len(headers)
        table = self.doc.add_table(rows=1, cols=ncol)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False
        _fixed_layout(table)
        _table_borders(table)

        widths = widths or [14.6 / ncol] * ncol
        if len(widths) != ncol:
            widths = [14.6 / ncol] * ncol

        # 表头
        hdr = table.rows[0]
        _row_no_split(hdr, header=True)
        for i, text in enumerate(headers):
            cell = hdr.cells[i]
            cell.width = Cm(widths[i])
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            _cell_shade(cell, HEAD_FILL)
            para = cell.paragraphs[0]
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            para.paragraph_format.space_before = Pt(2)
            para.paragraph_format.space_after = Pt(2)
            # 表头与首行数据保持同页，避免表头孤零零留在页脚
            para.paragraph_format.keep_with_next = True
            run = para.add_run(str(text))
            _set_run_font(run, name=HEAD_FONT, size=font_size, bold=True, color="1F2937")

        for ri, row in enumerate(rows):
            tr = table.add_row()
            _row_no_split(tr)
            for ci in range(ncol):
                cell = tr.cells[ci]
                cell.width = Cm(widths[ci])
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                if zebra and ri % 2 == 1:
                    _cell_shade(cell, "F7F8FA")
                para = cell.paragraphs[0]
                para.alignment = (
                    WD_ALIGN_PARAGRAPH.CENTER if ci in align_center_cols
                    else WD_ALIGN_PARAGRAPH.LEFT
                )
                para.paragraph_format.space_before = Pt(2)
                para.paragraph_format.space_after = Pt(2)
                value = "" if ci >= len(row) else str(row[ci])
                _add_rich(para, value, size=font_size)

        # 表后留一点空
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    # ---- 图片 ----
    def image(self, img_path: Path, *, caption: str = "", width_cm: float = 14.4) -> None:
        para = self.doc.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        para.paragraph_format.space_before = Pt(6)
        para.paragraph_format.space_after = Pt(2)
        para.add_run().add_picture(str(img_path), width=Cm(width_cm))
        if caption:
            cp = self.doc.add_paragraph()
            cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cp.paragraph_format.space_after = Pt(8)
            r = cp.add_run(caption)
            _set_run_font(r, name=BODY_FONT, size=9, color=GRAY)

    # ---- 其它 ----
    def page_break(self) -> None:
        self.doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def note(self, text: str) -> None:
        """浅底提示块，用于结论、口径说明。"""
        para = self.doc.add_paragraph()
        para.paragraph_format.left_indent = Pt(8)
        para.paragraph_format.space_before = Pt(5)
        para.paragraph_format.space_after = Pt(5)
        _add_rich(para, text, size=9.5, color="374151")

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(self.path))
        return self.path
