# -*- coding: utf-8 -*-
"""把 Markdown 转成本项目排版的 docx。

支持的语法（够用即可，不做完整 CommonMark）：
    # / ## / ###   标题
    | a | b |      表格（含 | --- | 分隔行）
    ```            代码块
    - / 1.         列表
    >              提示块
    ---            分隔线（忽略）
    **粗体**       行内

表格列宽按列数均分正文宽度，列数越多字号越小——这是排版上的取舍：
宁可字小一点，也不要让 8 列挤成竖排单字。

用法：
    python scripts/md2docx.py <input.md> <output.docx> --title "标题" [--header "页眉"]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from docx_kit import Doc  # noqa: E402

#: 正文可用宽度（厘米）
TEXT_WIDTH = 14.6


def split_row(line: str) -> list:
    """拆表格行。支持 \\| 转义竖线。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    parts, buf, i = [], "", 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            buf += "|"
            i += 2
            continue
        if ch == "|":
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
        i += 1
    parts.append(buf.strip())
    return parts


def is_sep(line: str) -> bool:
    s = line.strip()
    if not s.startswith("|"):
        return False
    body = s.replace("|", "").replace(" ", "")
    return bool(body) and set(body) <= set("-:") and len(body) >= 3


def disp_len(s: str) -> int:
    """显示宽度：中文/全角算 2，ASCII 算 1。"""
    return sum(2 if ord(c) > 127 else 1 for c in str(s))


def auto_widths(headers: list, rows: list, total: float = TEXT_WIDTH) -> list:
    """按每列实际内容长度分配列宽。

    按列数硬编码宽度的问题在于：同样是 4 列，「编号|用例|断言|实测」和
    「场景|用例数|通过|不通过」的合理分配完全不同。这里用每列内容的 80 分位
    长度做权重——取分位而非最大值，是为了不让个别超长单元格把整列撑爆。
    """
    ncol = len(headers)
    if ncol <= 1:
        return [total]

    weights = []
    for ci in range(ncol):
        cells = [headers[ci]] + [r[ci] if ci < len(r) else "" for r in rows]
        lens = sorted(disp_len(c) for c in cells)
        p80 = lens[min(len(lens) - 1, int(len(lens) * 0.8))]
        weights.append(max(3.0, min(p80, 30)) ** 0.85)

    s = sum(weights)
    widths = [max(1.35, total * w / s) for w in weights]
    # 触底列被抬到最小宽度后总和会超，再归一一次
    k = total / sum(widths)
    return [round(w * k, 2) for w in widths]


def font_for(ncol: int) -> float:
    if ncol >= 7:
        return 7.2
    if ncol >= 5:
        return 8.0
    return 8.8


def convert(md_text: str, out_path: Path, *, title: str, header: str,
            subtitle: list) -> Path:
    doc = Doc(out_path, header_text=header, title=title, subtitle=subtitle)

    lines = md_text.splitlines()
    i, n = 0, len(lines)

    # 跳过 md 里的一级大标题（已用作 docx 标题）
    while i < n and not lines[i].strip():
        i += 1
    if i < n and lines[i].startswith("# "):
        i += 1

    para_buf: list = []

    def flush_para() -> None:
        nonlocal para_buf
        if not para_buf:
            return
        text = " ".join(x.strip() for x in para_buf).strip()
        para_buf = []
        if text:
            doc.p(text)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 空行
        if not stripped:
            flush_para()
            i += 1
            continue

        # 分隔线：忽略
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", stripped):
            flush_para()
            i += 1
            continue

        # 代码块
        if stripped.startswith("```"):
            flush_para()
            i += 1
            code_lines = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i].rstrip())
                i += 1
            i += 1
            if code_lines:
                doc.code("\n".join(code_lines))
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            text = m.group(2).strip()
            # 去掉编号前缀里的多余空格
            if level <= 2:
                doc.h1(text)
            elif level == 3:
                doc.h2(text)
            else:
                doc.h3(text)
            i += 1
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < n and is_sep(lines[i + 1]):
            flush_para()
            headers = split_row(stripped)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                if is_sep(lines[i]):
                    i += 1
                    continue
                cells = split_row(lines[i])
                # 补齐/截断到表头列数
                if len(cells) < len(headers):
                    cells += [""] * (len(headers) - len(cells))
                elif len(cells) > len(headers):
                    cells = cells[: len(headers) - 1] + [" | ".join(cells[len(headers) - 1:])]
                rows.append(cells)
                i += 1
            if rows:
                ncol = len(headers)
                doc.table(
                    headers, rows,
                    widths=col_widths(ncol),
                    font_size=font_for(ncol),
                    align_center_cols=tuple(range(ncol)) if ncol <= 4 else (0,),
                    zebra=False,
                )
            continue

        # 引用 / 提示
        if stripped.startswith(">"):
            flush_para()
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip("> ").strip())
                i += 1
            doc.note(" ".join(x for x in buf if x))
            continue

        # 列表
        if re.match(r"^([-*+]|\d+\.)\s+", stripped):
            flush_para()
            items = []
            while i < n:
                s2 = lines[i].strip()
                if re.match(r"^([-*+]|\d+\.)\s+", s2):
                    items.append(re.sub(r"^([-*+]|\d+\.)\s+", "", s2))
                    i += 1
                elif s2 and not s2.startswith("|") and not s2.startswith("#") \
                        and not s2.startswith("```") and not s2.startswith(">"):
                    # 列表项的续行
                    if items:
                        items[-1] += s2
                        i += 1
                    else:
                        break
                else:
                    break
            if items:
                doc.bullets(items)
            continue

        # 普通段落
        para_buf.append(stripped)
        i += 1

    flush_para()
    return doc.save()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Markdown 转 docx")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--title", default="")
    ap.add_argument("--header", default="")
    ap.add_argument("--subtitle", action="append", default=[])
    args = ap.parse_args(argv)

    src = Path(args.src)
    dst = Path(args.dst)
    text = src.read_text(encoding="utf-8")

    # 标题取 md 首个 # 行
    title = args.title
    if not title:
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break

    out = convert(text, dst, title=title, header=args.header or title,
                  subtitle=args.subtitle)
    print("已生成", out, "%.1f KB" % (out.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
