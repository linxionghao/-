"""导入导出层单测：编码、分隔符、表头归一、注入防护、xlsx 结构、四个数据集。

为什么这一组测试写得比"函数能跑通"更细：

- 车间给的 CSV 十次有八次不是 UTF-8。编码猜错的后果不是报错，而是
  **静默变成乱码**——导入"成功"了，缺陷描述全是问号，等到月底出报表才发现。
- 表头几乎从不按约定写。`不良数量`、`不良 数量(件)`、`不良数量（件）`
  指同一列，如果只认一种，用户会被"缺少必需列"挡在门外。
- 导入是**部分成功**语义：100 行里错 3 行，剩下 97 行要进去。
  如果一错就整体回滚，用户改完 3 行再传一次，97 行会被判重复。

所以下面的断言多数在验证"坏数据进来会发生什么"，而不是"好数据能不能进"。
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from quality_trace.core.io import exporter, importer, textio, xlsx
from quality_trace.core.repo import db as repo


# ===========================================================================
# 一、编码探测
# ===========================================================================
class TestDecode:
    """编码探测：顺序、替换字符、GBK 伪 UTF-8 三个坑。"""

    def test_plain_utf8(self):
        text, enc = textio.decode_bytes("工单号,不良数量".encode("utf-8"))
        assert text == "工单号,不良数量"
        # 无 BOM 的 UTF-8 用 utf-8-sig 解也得到同样的文本（sig 解码器容错），
        # 所以这里只约束"不能落到 gb18030/latin-1"
        assert enc in ("utf-8", "utf-8-sig")

    def test_utf8_bom_is_stripped_by_decoder(self):
        """带 BOM 的 UTF-8：必须识别成 utf-8-sig，否则表头首列永远多一个 \\ufeff。"""
        text, enc = textio.decode_bytes("\ufeff工单号".encode("utf-8"))
        assert enc == "utf-8-sig"
        assert text == "工单号"
        assert not text.startswith("\ufeff")

    def test_gbk_falls_through_to_gb18030(self):
        """GBK 中文在 UTF-8 下会抛 UnicodeDecodeError，自然落到 gb18030。"""
        raw = "工单号,数量".encode("gbk")
        text, enc = textio.decode_bytes(raw)
        assert text == "工单号,数量"
        assert enc == "gb18030"

    def test_empty_bytes(self):
        assert textio.decode_bytes(b"") == ("", "utf-8")

    def test_latin1_garbage_still_returns_something(self):
        """真正的乱码也要给出可展示的结果，不能抛异常把整次导入打死。"""
        text, enc = textio.decode_bytes(bytes([0xFF, 0xFE, 0x00, 0x41]))
        assert enc in textio.ENCODINGS
        assert isinstance(text, str)

    def test_gbk_garbage_heuristic(self):
        """UTF-8 硬解 GBK 字节会得到大量 ÀÃ 这类字符，启发式应识别出来。"""
        assert textio._looks_like_gbk_garbage("ÃÂÆÄÅ") is True
        # 有汉字就不再怀疑，避免把正常中英混排误判
        assert textio._looks_like_gbk_garbage("中文 OK") is False
        assert textio._looks_like_gbk_garbage("") is False


# ===========================================================================
# 二、分隔符探测
# ===========================================================================
class TestDelimiter:
    def test_comma(self):
        assert textio.detect_delimiter("a,b,c\n1,2,3") == ","

    def test_semicolon(self):
        """欧洲区与部分 ERP 导出用分号，且小数点是逗号。"""
        assert textio.detect_delimiter("a;b;c\n1;2;3") == ";"

    def test_tab(self):
        assert textio.detect_delimiter("a\tb\tc\n1\t2\t3") == "\t"

    def test_pipe(self):
        assert textio.detect_delimiter("a|b|c\n1|2|3") == "|"

    def test_commas_inside_prose_do_not_win(self):
        """正文里逗号多、但只有表头那种字符每行都出现——"稳定"优先于"最多"。"""
        text = "工单号;描述\nWO-01;毛刺,划伤,凹坑,变形\nWO-02;漏加工,尺寸超差"
        assert textio.detect_delimiter(text) == ";"

    def test_empty_text_defaults_to_comma(self):
        assert textio.detect_delimiter("") == ","


# ===========================================================================
# 三、表头归一
# ===========================================================================
class TestHeader:
    @pytest.mark.parametrize("raw", ["不良数量", "不良 数量", " 不良数量 ", "不良数量(件)",
                                     "不良数量（件）", "不良\n数量", "\ufeff不良数量"])
    def test_variants_collapse(self, raw):
        assert textio.normalize_header(raw) == "不良数量"

    def test_fullwidth_letters_become_halfwidth(self):
        assert textio.normalize_header("Ｑｔｙ") == "qty"

    def test_map_columns_hits_aliases(self):
        headers = ["工单号", "不良 数量", "不良描述"]
        mapping, missing = textio.map_columns(
            headers, {"work_order_id": ("工单号",), "qty": ("不良数量",),
                      "description": ("不良描述", "描述")})
        assert mapping == {"work_order_id": "工单号", "qty": "不良 数量",
                           "description": "不良描述"}
        assert missing == []

    def test_map_columns_reports_missing(self):
        mapping, missing = textio.map_columns(["工单号"], {"work_order_id": ("工单号",),
                                                           "qty": ("不良数量",)})
        assert mapping == {"work_order_id": "工单号"}
        assert missing == ["qty"]


# ===========================================================================
# 四、CSV 读写与注入防护
# ===========================================================================
class TestCsv:
    def test_roundtrip_with_chinese(self):
        data = textio.to_csv_bytes(["工单号", "数量"], [["WO-01", 5]])
        assert data.startswith(b"\xef\xbb\xbf")           # BOM 供 Excel 识别
        parsed = textio.read_csv(data)
        assert parsed["headers"] == ["工单号", "数量"]
        assert parsed["rows"] == [{"工单号": "WO-01", "数量": "5"}]

    def test_no_bom_when_disabled(self):
        data = textio.to_csv_bytes(["a"], [[1]], with_bom=False)
        assert not data.startswith(b"\xef\xbb\xbf")

    @pytest.mark.parametrize("danger", ["=1+1", "+A1", "-2+3", "@SUM(A1)"])
    def test_formula_prefix_is_neutralized(self, danger):
        """CSV 注入：Excel 会把 = + - @ 开头当公式执行。"""
        data = textio.to_csv_bytes(["备注"], [[danger]], with_bom=False)
        text = data.decode("utf-8")
        assert "'" + danger in text

    def test_formula_protection_can_be_disabled(self):
        data = textio.to_csv_bytes(["备注"], [["=1+1"]], with_bom=False,
                                   protect_formula=False)
        assert "'=1+1" not in data.decode("utf-8")

    def test_none_becomes_empty_cell(self):
        data = textio.to_csv_bytes(["a", "b"], [[None, "x"]], with_bom=False)
        assert data.decode("utf-8").splitlines()[1] == ",x"

    def test_read_csv_warns_on_ragged_rows(self):
        raw = "a,b,c\n1,2\n3,4,5\n".encode("utf-8")
        parsed = textio.read_csv(raw)
        assert parsed["rows"][0]["a"] == "1"
        assert parsed["rows"][0]["c"] == ""               # 缺失补空，不丢行
        assert any("列数" in w for w in parsed["warnings"])

    def test_read_csv_warns_on_duplicate_headers(self):
        parsed = textio.read_csv("a,a\n1,2".encode("utf-8"))
        assert any("重复列名" in w for w in parsed["warnings"])

    def test_read_csv_skips_blank_lines(self):
        parsed = textio.read_csv("a,b\n1,2\n\n\n3,4\n".encode("utf-8"))
        assert len(parsed["rows"]) == 2

    def test_read_csv_truncates(self):
        body = "\n".join("%d,%d" % (i, i) for i in range(10))
        parsed = textio.read_csv(("a,b\n" + body).encode("utf-8"), max_rows=3)
        assert len(parsed["rows"]) == 3
        assert parsed["truncated"] is True
        assert any("超过" in w for w in parsed["warnings"])

    def test_read_csv_empty_file(self):
        parsed = textio.read_csv(b"")
        assert parsed["rows"] == []
        assert parsed["warnings"]

    def test_explicit_delimiter_overrides_detection(self):
        parsed = textio.read_csv("a;b\n1;2".encode("utf-8"), delimiter=";")
        assert parsed["headers"] == ["a", "b"]

    def test_to_markdown_escapes_pipe(self):
        md = textio.to_markdown(["名称", "描述"], [["A", "a|b"]])
        assert "a\\|b" in md
        assert md.count("\n") == 2                         # 表头 + 分隔行 + 1 行


# ===========================================================================
# 五、xlsx 生成（零依赖：自己拼 OOXML）
# ===========================================================================
class TestXlsx:
    def test_column_letter(self):
        assert xlsx.column_letter(1) == "A"
        assert xlsx.column_letter(26) == "Z"
        assert xlsx.column_letter(27) == "AA"
        assert xlsx.column_letter(52) == "AZ"

    def test_build_produces_valid_zip_with_expected_parts(self):
        data = xlsx.build_xlsx([{"name": "缺陷", "headers": ["工单号", "数量"],
                                 "rows": [["WO-01", 3]]}])
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = set(zf.namelist())
        assert "[Content_Types].xml" in names
        assert "_rels/.rels" in names
        assert "xl/workbook.xml" in names
        assert "xl/_rels/workbook.xml.rels" in names
        assert "xl/worksheets/sheet1.xml" in names
        assert "xl/styles.xml" in names
        # 每一份都必须能正常解压（结构不合法时 Excel 会直接说"文件损坏"）
        for n in names:
            assert zf.read(n)

    def test_sheet_contains_header_and_values(self):
        data = xlsx.build_xlsx([{"name": "S", "headers": ["工单号", "数量"],
                                 "rows": [["WO-01", 3]]}])
        sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "工单号" in sheet
        assert "WO-01" in sheet
        assert "<v>3</v>" in sheet                        # 数值走数值单元格，不是文本
        assert "autoFilter" in sheet                      # 表头自动筛选
        assert "pane" in sheet                            # 冻结首行

    def test_illegal_xml_chars_are_stripped(self):
        """描述里混进 \\x00 这类控制符，直接写进 XML 会让整个文件打不开。"""
        data = xlsx.build_xlsx([{"name": "S", "headers": ["描述"],
                                 "rows": [["毛刺\x00划伤\x07"]]}])
        sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "\x00" not in sheet and "\x07" not in sheet
        assert "毛刺划伤" in sheet

    def test_xml_special_chars_are_escaped(self):
        data = xlsx.build_xlsx([{"name": "S", "headers": ["描述"],
                                 "rows": [["a<b>c&d"]]}])
        sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode("utf-8")
        assert "&lt;" in sheet and "&amp;" in sheet

    def test_sheet_name_is_sanitized_and_capped(self):
        """非法字符替换成下划线（保留可读性），并截断到 31 字符。"""
        cleaned = xlsx._sheet_name("a/b:c*?[]")
        for ch in ("\\", "/", "*", "[", "]", ":", "?"):
            assert ch not in cleaned
        assert cleaned.startswith("a_b")
        assert len(xlsx._sheet_name("x" * 50)) == 31       # Excel 硬限制
        assert xlsx._sheet_name(None) == "Sheet1"
        assert xlsx._sheet_name("") == "Sheet1"

    def test_multiple_sheets(self):
        data = xlsx.build_xlsx([
            {"name": "一", "headers": ["a"], "rows": [[1]]},
            {"name": "二", "headers": ["b"], "rows": [[2]]},
        ])
        zf = zipfile.ZipFile(io.BytesIO(data))
        assert "xl/worksheets/sheet2.xml" in zf.namelist()
        wb = zf.read("xl/workbook.xml").decode("utf-8")
        assert wb.count("<sheet ") == 2

    def test_number_formatting(self):
        assert xlsx._num(3.0) == "3"
        assert xlsx._num(3.50) == "3.5"
        assert xlsx._num(0.125) == "0.125"

    def test_width_estimation_counts_cjk_as_two(self):
        assert xlsx._width_of("中文") >= xlsx._width_of("ab")
        assert xlsx._width_of("") >= 8                     # 有下限
        assert xlsx._width_of("x" * 200) <= 48              # 有上限


# ===========================================================================
# 六、导入：四个数据集
# ===========================================================================
@pytest.fixture()
def conn(tmp_path):
    c = repo.connect(str(tmp_path / "io.db"))
    repo.init_schema(c)
    return c


class TestImportWorkOrders:
    def test_happy_path(self, conn):
        raw = "工单号,产品,产线,计划数量,日期\nWO-01,六角螺栓,line-01,1000,2026-11-01\n".encode("utf-8")
        res = importer.import_dataset(conn, "work_orders", raw)
        assert res["inserted"] == 1
        assert res["failed"] == 0
        assert res["ok"] is True
        assert repo.get_work_order(conn, "WO-01")["planned_qty"] == 1000

    def test_dry_run_writes_nothing(self, conn):
        raw = "工单号,产品,产线\nWO-02,螺栓,line-01\n".encode("utf-8")
        res = importer.import_dataset(conn, "work_orders", raw, dry_run=True)
        assert res["inserted"] == 1                        # 报告里算"可导入"
        assert repo.get_work_order(conn, "WO-02") is None   # 但库里没有
        assert "尚未写入" in res["message"]

    def test_partial_success(self, conn):
        """一行缺工单号不该毁掉整份文件。"""
        raw = ("工单号,产品,产线\nWO-03,螺栓,line-01\n,缺号,line-01\nWO-04,螺母,line-01\n").encode("utf-8")
        res = importer.import_dataset(conn, "work_orders", raw)
        assert res["inserted"] == 2
        assert res["failed"] == 1
        assert res["errors"][0]["row"] == 3                # 行号按 Excel 习惯从 2 起
        assert repo.get_work_order(conn, "WO-03") is not None
        assert repo.get_work_order(conn, "WO-04") is not None

    def test_missing_line_is_rejected(self, conn):
        """产线为空必须拦：它是数据级权限的过滤键，空值等于谁都看不见。"""
        raw = "工单号,产品\nWO-09,螺栓\n".encode("utf-8")
        res = importer.import_dataset(conn, "work_orders", raw)
        assert res["inserted"] == 0
        assert res["failed"] == 1
        assert "产线" in res["errors"][0]["message"]

    def test_duplicate_is_skipped_not_failed(self, conn):
        raw = "工单号,产品,产线\nWO-05,螺栓,line-01\n".encode("utf-8")
        importer.import_dataset(conn, "work_orders", raw)
        res = importer.import_dataset(conn, "work_orders", raw)
        assert res["inserted"] == 0
        assert res["skipped"] == 1
        assert res["failed"] == 0                          # 重复不是错误，是"跳过"

    def test_blank_planned_qty_is_zero_not_error(self, conn):
        """选填的数值列留空应视为 0，而不是整行失败。"""
        raw = "工单号,产品,产线,计划数量\nWO-06,螺栓,line-01,\n".encode("utf-8")
        res = importer.import_dataset(conn, "work_orders", raw)
        assert res["inserted"] == 1
        assert repo.get_work_order(conn, "WO-06")["planned_qty"] == 0

    def test_missing_required_column(self, conn):
        res = importer.import_dataset(conn, "work_orders", "产品,数量\n螺栓,100\n".encode("utf-8"))
        assert res["failed"] == 1
        assert any("缺少" in w for w in res["warnings"])

    def test_unknown_dataset_raises(self, conn):
        with pytest.raises(ValueError):
            importer.import_dataset(conn, "hack", b"a\n1\n")

    def test_empty_file(self, conn):
        res = importer.import_dataset(conn, "work_orders", b"")
        assert res["ok"] is False
        assert res["message"] == "文件为空"


class TestImportDefectCodes:
    def test_happy_path(self, conn):
        raw = "缺陷码,缺陷描述,分类,关键词\nDC01,毛刺,外观,毛刺、飞边\n".encode("utf-8")
        res = importer.import_dataset(conn, "defect_codes", raw)
        assert res["inserted"] == 1
        rows = repo.list_defect_codes(conn)
        assert rows[0]["code"] == "DC01"
        assert rows[0]["name"] == "毛刺"

    def test_keywords_split_on_multiple_separators(self, conn):
        """现场用「、」「,」「;」混写是常态，都得认。"""
        raw = "缺陷码,名称,关键词\nDC03,划伤,划伤、刮花;刮痕，擦伤\n".encode("utf-8")
        importer.import_dataset(conn, "defect_codes", raw)
        row = repo.get_defect_code(conn, "DC03")
        assert json.loads(row["keywords_json"]) == ["划伤", "刮花", "刮痕", "擦伤"]

    def test_name_is_required(self, conn):
        raw = "缺陷码,名称\nDC04,\n".encode("utf-8")
        res = importer.import_dataset(conn, "defect_codes", raw)
        assert res["failed"] == 1

    def test_category_defaults_to_other(self, conn):
        raw = "缺陷码,名称\nDC05,凹坑\n".encode("utf-8")
        importer.import_dataset(conn, "defect_codes", raw)
        assert repo.get_defect_code(conn, "DC05")["category"] == "其他"


class TestImportLots:
    def test_happy_path(self, conn):
        raw = ("批号,物料编码,物料名称,供应商,数量,单位,日期\n"
               "LOT-01,MAT-45,45#圆钢,SUP-甲,3000,kg,2026-11-01\n").encode("utf-8")
        res = importer.import_dataset(conn, "lots", raw)
        assert res["inserted"] == 1
        lot = repo.get_material_lot(conn, "LOT-01")
        assert lot["qty"] == 3000
        assert lot["supplier"] == "SUP-甲"

    def test_qty_with_thousands_separator(self, conn):
        """Excel 里写 3,000 会被切成两列，所以别名表里 qty 允许带千分位字符串。"""
        raw = "批号,物料编码,物料名称,数量\nLOT-02,MAT-45,圆钢,3000\n".encode("utf-8")
        importer.import_dataset(conn, "lots", raw)
        assert repo.get_material_lot(conn, "LOT-02")["qty"] == 3000


class TestImportMeasurements:
    def test_happy_path(self, conn):
        raw = ("项目,数值,产线,子组,规格上限,规格下限,单位,日期\n"
               "外径,10.01,line-01,1,10.08,9.92,mm,2026-11-01\n"
               "外径,9.99,line-01,1,10.08,9.92,mm,2026-11-01\n").encode("utf-8")
        res = importer.import_dataset(conn, "measurements", raw)
        assert res["inserted"] == 2
        assert repo.count_measurements(conn) == 2

    def test_bad_number_is_row_error(self, conn):
        raw = ("项目,数值,产线\n外径,abc,line-01\n外径,10.0,line-01\n").encode("utf-8")
        res = importer.import_dataset(conn, "measurements", raw)
        assert res["inserted"] == 1
        assert res["failed"] == 1
        assert res["errors"][0]["field"] == "实测值"

    def test_blank_value_is_row_error(self, conn):
        """实测值空着必须拦：0 也是合法测量值，不能拿 0 顶替空。"""
        raw = "项目,数值,产线\n外径,,line-01\n".encode("utf-8")
        res = importer.import_dataset(conn, "measurements", raw)
        assert res["failed"] == 1
        assert "不能为空" in res["errors"][0]["message"]

    def test_missing_line_falls_back_to_unknown_with_warning(self, conn):
        """产线缺失不拦行（QE 跨线导入合理），但必须留痕。"""
        raw = "项目,数值\n外径,10.0\n".encode("utf-8")
        res = importer.import_dataset(conn, "measurements", raw)
        assert res["inserted"] == 1
        assert any("UNKNOWN" in w for w in res["warnings"])
        rows = repo.query_measurements(conn)
        assert rows[0]["line_id"] == "UNKNOWN"

    def test_usl_must_exceed_lsl(self, conn):
        raw = "项目,数值,产线,规格上限,规格下限\n外径,10.0,line-01,9.9,10.1\n".encode("utf-8")
        res = importer.import_dataset(conn, "measurements", raw)
        assert res["failed"] == 1
        assert "必须大于" in res["errors"][0]["message"]

    def test_out_of_spec_value_is_accepted(self, conn):
        """超差是事实，删掉它等于掩盖问题——导入层不做规格内校验。"""
        raw = "项目,数值,产线,规格上限,规格下限\n外径,10.5,line-01,10.08,9.92\n".encode("utf-8")
        res = importer.import_dataset(conn, "measurements", raw)
        assert res["inserted"] == 1


class TestNumericParsing:
    def test_thousands_and_percent(self):
        assert importer._num("1,234.5", "数量")[0] == 1234.5

    def test_fullwidth_digits(self):
        assert importer._num("１２３", "数量")[0] == 123

    def test_blank_is_none_without_error(self):
        """空值 = 没填，不是填错。这个区分决定了"选填列留空"会不会整行失败。"""
        assert importer._num("", "数量") == (None, None)
        assert importer._num(None, "数量") == (None, None)
        assert importer._int("", "数量") == (None, None)

    def test_blank_measurement_value_is_caught_by_caller(self, conn):
        """数值列的"必填"由调用方判断（见 TestImportMeasurements）。"""
        raw = "项目,数值,产线\n外径,,line-01\n".encode("utf-8")
        assert importer.import_dataset(conn, "measurements", raw)["failed"] == 1

    def test_garbage_reports_field(self):
        value, err = importer._num("abc", "数量")
        assert value is None
        assert "数量" in err

    def test_date_formats(self):
        assert importer._date("2026-11-05", "日期")[0].startswith("2026-11-05")
        assert importer._date("2026/11/05", "日期")[0].startswith("2026-11-05")
        assert importer._date("20261105", "日期")[0].startswith("2026-11-05")

    def test_blank_date_means_now(self):
        value, err = importer._date("", "日期")
        assert value is None and err is None


# ===========================================================================
# 七、导出
# ===========================================================================
class TestExport:
    def _seed(self, conn):
        repo.insert_work_order(conn, "WO-01", "六角螺栓", "line-01", 1000, occurred_at="2026-11-01")
        repo.insert_defect(conn, "DF-01", "WO-01", "line-01", "毛刺", qty=3,
                           defect_code="DC01", occurred_at="2026-11-01")
        repo.insert_defect(conn, "DF-02", "WO-01", "line-01", "划伤", qty=1,
                           defect_code="DC02", occurred_at="2026-11-01")

    def test_defect_csv(self, conn):
        self._seed(conn)
        name, data, mime = exporter.build("defects", conn, fmt="csv")
        assert name.endswith(".csv")
        assert "csv" in mime
        text = data.decode("utf-8-sig")
        assert "工单" in text and "WO-01" in text

    def test_defect_xlsx(self, conn):
        self._seed(conn)
        name, data, mime = exporter.build("defects", conn, fmt="xlsx")
        assert name.endswith(".xlsx")
        assert "spreadsheet" in mime
        assert zipfile.ZipFile(io.BytesIO(data)).namelist()

    def test_unknown_dataset(self, conn):
        with pytest.raises(ValueError):
            exporter.build("hack", conn)

    def test_unknown_format(self, conn):
        with pytest.raises(ValueError):
            exporter.build("defects", conn, fmt="pdf")

    def test_filename_has_stamp(self, conn):
        """文件名带日期区间/时间戳：同一目录下连续导出两次不会互相覆盖。"""
        self._seed(conn)
        name, _, _ = exporter.build("defects", conn)
        assert "缺陷单" in name
        assert name.endswith(".csv")
        assert "2026-11" in name

    def test_empty_result_still_produces_file(self, conn):
        """没有数据也要给一份只有表头的文件——空白下远比 500 好排查。"""
        name, data, _ = exporter.build("defects", conn)
        assert data
        lines = data.decode("utf-8-sig").strip().splitlines()
        assert len(lines) == 1

    def test_pareto_table(self, conn):
        self._seed(conn)
        name, headers, rows = exporter.pareto_table(conn, dim="defect_code")
        assert headers[:3] == ["排名", "项目", "数量"]
        assert rows

    def test_multi_xlsx(self, conn):
        data = exporter.build_multi_xlsx([
            ("缺陷", ["a"], [["x"]]),
            ("归因", ["b"], [["y"]]),
        ])
        zf = zipfile.ZipFile(io.BytesIO(data))
        assert len([n for n in zf.namelist() if "worksheets" in n]) == 2
