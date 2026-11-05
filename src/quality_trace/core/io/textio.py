"""文本与 CSV 读写：把"车间导出的那份 Excel"变成可用的行。

这类代码看着琐碎，但它决定了系统的第一印象：现场把 Excel 拖进来，
十有八九是 GBK 编码、带合并单元格、表头里有空格和换行。
如果直接 `open(..., encoding="utf-8")`，用户看到的是 `UnicodeDecodeError`，
然后得出"你们系统不好用"的结论——而真正的原因是编码。

本模块只做四件事，每件都对应一个真实的坑：

1. **编码探测**：UTF-8 BOM → UTF-8 → GB18030 → Latin-1 兜底。
   顺序不能反：GB18030 几乎能"解码"任意字节序列（只是解出乱码），
   放在前面会把 UTF-8 文件解成一片问号，而且不报错——这类静默错误最难查。
2. **分隔符探测**：逗号 / 分号 / 制表符 / 竖线。欧洲区和部分 ERP 导出用分号。
3. **表头归一**：去 BOM、去首尾空格、全角转半角、去换行，
   让"不良 数量"和"不良数量"能对上同一列。
4. **CSV 注入防护**：以 `=` `+` `-` `@` 开头的单元格加前缀 `'`。
   Excel 会把这些当公式执行，`=HYPERLINK(...)` 是经典的钓鱼手法。
"""

from __future__ import annotations

import csv
import io
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# 探测顺序即优先级，原因见模块 docstring
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1")

# 候选分隔符：出现次数最多的那个胜出
DELIMITERS = (",", ";", "\t", "|")

# CSV 注入的危险前缀
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def decode_bytes(data: bytes) -> Tuple[str, str]:
    """把字节解码成文本，返回 (文本, 实际使用的编码)。

    判定逻辑：先按顺序严格解码，**第一个不抛异常且不含替换字符**的胜出。
    为什么还要检查替换字符：`latin-1` 永不抛异常，若把它排前面会把所有中文
    变成 `ä¸­æ–‡`；这里至少让"看起来像乱码"的结果继续往后找。
    """
    if not data:
        return "", "utf-8"
    for enc in ENCODINGS:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 替换字符出现说明这次解码是"勉强成功"，继续尝试下一个
        if "\ufffd" in text and enc != "latin-1":
            continue
        # UTF-8 解 GBK 中文时经常能"成功"但解出的是生僻字，靠启发式再筛一次
        if enc == "utf-8" and _looks_like_gbk_garbage(text):
            continue
        return text, enc
    return data.decode("latin-1", errors="replace"), "latin-1"


def _looks_like_gbk_garbage(text: str) -> bool:
    """UTF-8 解码 GBK 字节时，常见结果是大量"Ã Â Æ"这类拉丁补充字符。

    判据：拉丁扩展字符占比超过 15% 且没有汉字，认为是解错了。
    这是个启发式，宁可漏判（继续用 utf-8）也不要误判：
    误判会把真正的中英混排文件当成 GBK 重新解，反而更乱。
    """
    if not text:
        return False
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if han:
        return False
    latin_ext = sum(1 for ch in text if "\u00c0" <= ch <= "\u00ff")
    return latin_ext / len(text) > 0.15


def detect_delimiter(text: str, sample_lines: int = 5) -> str:
    """猜分隔符：取前几行里出现次数最稳定且最多的那个。

    "稳定"比"最多"更重要：正文里可能有一堆逗号（描述字段），
    但只有表头里那个字符是每行都出现的。所以先找"每行都出现"的候选，
    再在其中取次数最多的。
    """
    lines = [ln for ln in text.splitlines()[:sample_lines] if ln.strip()]
    if not lines:
        return ","
    best, best_score = ",", -1.0
    for d in DELIMITERS:
        counts = [ln.count(d) for ln in lines]
        if not counts or min(counts) == 0:
            continue
        score = min(counts) * 10 + sum(counts) / len(counts)
        if score > best_score:
            best, best_score = d, score
    return best


def normalize_header(name: str) -> str:
    """表头归一：去 BOM、去空白与换行、全角转半角、去单位括号。

    为什么去掉括号里的单位：`数量(件)`、`数量（件）`、`数量` 应该能对上同一列。
    单位另有字段承载，不该混在列名里做匹配。
    """
    s = (name or "").replace("\ufeff", "").strip()
    s = re.sub(r"[\r\n\t]+", "", s)
    s = s.replace("\u3000", " ")
    # 全角字符转半角（ASCII 可见区）
    s = "".join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in s)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)     # 去括号及内容
    s = re.sub(r"\s+", "", s)
    return s.lower()


def read_csv(
    data: bytes,
    *,
    delimiter: Optional[str] = None,
    max_rows: int = 20000,
) -> Dict[str, object]:
    """读 CSV/TSV 字节流，返回表头 + 行字典。

    返回值里的 `warnings` 不是装饰：编码猜错、分隔符猜错、行长度不一致，
    这些都必须能反馈给用户，否则他们会以为"导入成功了"，然后发现数据少了一半。
    """
    text, encoding = decode_bytes(data)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    warnings: List[str] = []

    if delimiter is None:
        delimiter = detect_delimiter(text)

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return {"headers": [], "rows": [], "encoding": encoding, "delimiter": delimiter,
                "warnings": ["文件为空或没有有效行"], "truncated": False}

    raw_headers = rows[0]
    headers = [h.strip().replace("\ufeff", "") for h in raw_headers]
    if len(set(headers)) != len(headers):
        warnings.append("表头有重复列名，后出现的列会覆盖前一个")

    body = rows[1:max_rows + 1]
    truncated = len(rows) - 1 > max_rows
    if truncated:
        warnings.append("行数超过 %d，仅导入前 %d 行" % (max_rows, max_rows))

    out: List[Dict[str, str]] = []
    bad_rows = 0
    for r in body:
        if len(r) != len(headers):
            bad_rows += 1
            if bad_rows <= 3:
                warnings.append("第 %d 行列数为 %d，与表头 %d 不一致（已按缺失补空）"
                                % (len(out) + 2, len(r), len(headers)))
        item = {}
        for i, h in enumerate(headers):
            if not h:
                continue
            item[h] = (r[i].strip() if i < len(r) else "")
        out.append(item)

    if bad_rows:
        warnings.append("共 %d 行列数与表头不一致" % bad_rows)

    return {
        "headers": headers,
        "normalized_headers": [normalize_header(h) for h in headers],
        "rows": out,
        "encoding": encoding,
        "delimiter": delimiter,
        "warnings": warnings,
        "truncated": truncated,
        "total_rows": len(rows) - 1,
    }


def map_columns(
    headers: Sequence[str], aliases: Dict[str, Sequence[str]]
) -> Tuple[Dict[str, str], List[str]]:
    """把"实际表头"映射到"系统字段"。

    aliases 形如 {"qty": ("数量", "不良数量", "qty"), "description": ("描述", "不良描述")}。
    返回 (字段→实际列名, 缺失的必填字段列表)。

    匹配用归一化后的名字，所以 `不良 数量`、`不良数量(件)` 都能命中 `不良数量`。
    这是"用户不会按你的格式填表"这条现实的工程解法：
    与其要求他们改表，不如把常见写法都认下来。
    """
    norm = {normalize_header(h): h for h in headers}
    mapping: Dict[str, str] = {}
    missing: List[str] = []
    for field, names in aliases.items():
        hit = None
        for name in names:
            key = normalize_header(name)
            if key in norm:
                hit = norm[key]
                break
        if hit:
            mapping[field] = hit
        else:
            missing.append(field)
    return mapping, missing


def to_csv_bytes(
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    with_bom: bool = True,
    protect_formula: bool = True,
) -> bytes:
    """把表格写成 CSV 字节（可直接下载）。

    - `with_bom=True`：加 UTF-8 BOM。不加的话，中文 Windows 上的 Excel
      双击打开会显示乱码，用户第一反应是"你们导出的文件坏了"。
    - `protect_formula=True`：给危险前缀加 `'`。CSV 注入不是理论问题，
      审计报告里出现用户填写的 `=cmd|...` 时，是 Excel 在替攻击者执行。
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(list(headers))
    for r in rows:
        writer.writerow([_cell(v, protect_formula) for v in r])
    text = buf.getvalue()
    return (("\ufeff" if with_bom else "") + text).encode("utf-8")


def _cell(value: object, protect_formula: bool) -> str:
    if value is None:
        return ""
    s = str(value)
    if protect_formula and s[:1] in FORMULA_PREFIXES:
        return "'" + s
    return s


def to_markdown(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    """生成 Markdown 表格（贴进报告/企微消息用）。

    管道符必须转义，否则表格会错列——这是 Markdown 表格最常见的翻车点。
    """
    def cell(v: object) -> str:
        return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(cell(v) for v in r) + " |")
    return "\n".join(lines)
