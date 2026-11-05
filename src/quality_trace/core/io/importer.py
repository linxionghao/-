"""导入：把 Excel 里的历史数据搬进系统，并且**逐行告诉你哪行有问题**。

导入功能的成败不在"能不能导进去"，而在"导错了能不能看出来"。
所以本模块的返回值里，错误明细和成功条数同等重要：

    {"total": 100, "inserted": 96, "skipped": 2, "failed": 2,
     "errors": [{"row": 5, "field": "数量", "message": "不是数字：'三件'"},
                {"row": 9, "field": "日期", "message": "无法识别日期：'上周二'"}],
     "warnings": ["第 3 行批号重复，已跳过"]}

四个设计决定：

1. **默认 `dry_run=False` 但支持预览**。路由层默认走 `dry_run=True`，
   用户点"校验"看报告，确认无误再点"导入"。真实场景里，一次导错 5000 行
   比导不进去糟糕得多——尤其是实测值，它会污染控制图且难以逐条删。
2. **部分成功，不整体回滚**。业务上更合理：100 行里 2 行格式错，
   让用户改那 2 行重导，比"全失败"友好。代价是必须幂等（见第 3 条）。
3. **幂等靠业务主键**（工单号、缺陷码、批号），不靠"表空才导"。
   重复导入同一份文件，结果是 skipped 而不是重复数据。
4. **错误只报前 50 条**。报 5000 条错误既没人看，也会把响应撑到几 MB。
   超过时给一句"还有 N 条同类错误未列出"。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..repo import db as repo
from . import textio

MAX_ERRORS = 50

# 每个数据集的列别名表。左边是系统字段，右边是现场可能写出来的列名。
# 加列只改这里，不动解析逻辑——这是"把变化关在数据里"的做法。
ALIASES: Dict[str, Dict[str, Sequence[str]]] = {
    "work_orders": {
        "work_order_id": ("工单号", "工单", "单号", "work_order_id", "wo"),
        "product_name": ("产品名称", "产品", "品名", "product"),
        "line_id": ("产线", "线别", "生产 line", "line", "line_id"),
        "planned_qty": ("计划数量", "计划产量", "数量", "planned", "qty"),
        "occurred_at": ("日期", "生产日期", "开工日期", "occurred_at"),
    },
    "defect_codes": {
        "code": ("缺陷码", "编码", "代码", "code"),
        "name": ("名称", "缺陷名称", "缺陷描述", "不良描述", "描述", "name"),
        "category": ("分类", "类别", "缺陷分类", "category"),
        "keywords": ("关键词", "关键字", "keywords"),
        "sort_no": ("排序", "序号", "sort"),
    },
    "lots": {
        "lot_id": ("批号", "物料批号", "批次号", "lot", "lot_id"),
        "material_code": ("物料编码", "物料号", "料号", "material_code"),
        "material_name": ("物料名称", "物料", "品名", "material_name"),
        "supplier": ("供应商", "厂商", "supplier"),
        "supplier_lot": ("供应商批号", "来料批号", "supplier_lot"),
        "qty": ("数量", "批量", "入库数量", "qty"),
        "unit": ("单位", "unit"),
        "occurred_at": ("日期", "入库日期", "到货日期", "occurred_at"),
    },
    "measurements": {
        "item_code": ("检验项目", "项目", "特性", "item", "item_code"),
        "value": ("实测值", "测量值", "数值", "value"),
        "line_id": ("产线", "线别", "line", "line_id"),
        "subgroup_no": ("子组号", "子组", "组号", "subgroup"),
        "unit": ("单位", "unit"),
        "usl": ("规格上限", "上限", "usl"),
        "lsl": ("规格下限", "下限", "lsl"),
        "gauge": ("量具", "量具编号", "gauge"),
        "operator": ("测量人", "操作者", "operator"),
        "occurred_at": ("时间", "测量时间", "日期", "occurred_at"),
    },
}


# ---------------------------------------------------------------------------
# 字段解析小工具
# ---------------------------------------------------------------------------
def _num(value: Any, field: str) -> Tuple[Optional[float], Optional[str]]:
    """解析数字，容忍千分位、单位后缀与全角数字。

    **空值不算错**：区分"没填"与"填错了"是两件事。
    `计划数量` 空着可以是 0，`实测值` 空着必须报错——但这由调用方判断，
    放在这里统一报错会让"选填的数值列留空"变成整行失败。
    """
    if value is None or str(value).strip() == "":
        return None, None
    s = str(value).strip().replace(",", "").replace("，", "")
    s = s.replace("mm", "").replace("MM", "").replace("件", "").replace("kg", "").strip()
    s = s.translate(str.maketrans("０１２３４５６７８９．", "0123456789."))
    try:
        return float(s), None
    except ValueError:
        return None, "%s 不是数字：%r" % (field, value)


def _int(value: Any, field: str) -> Tuple[Optional[int], Optional[str]]:
    f, err = _num(value, field)
    if err:
        return None, err
    if f is None:
        return None, err
    return int(round(f)), None


def _date(value: Any, field: str) -> Tuple[Optional[str], Optional[str]]:
    """解析日期，返回 ISO 字符串。空值表示"用当前时间"（不算错）。"""
    if value is None or str(value).strip() == "":
        return None, None
    s = str(value).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return _dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S"), None
        except ValueError:
            continue
    return None, "%s 无法识别日期：%r（建议用 2026-11-05 格式）" % (field, value)


def _required(value: Any, field: str) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return "%s 不能为空" % field
    return None


class _Report:
    """导入报告的累加器（避免每个导入函数各拼一份 dict）。"""

    def __init__(self, dataset: str, dry_run: bool) -> None:
        self.dataset = dataset
        self.dry_run = dry_run
        self.total = 0
        self.inserted = 0
        self.skipped = 0
        self.failed = 0
        self.errors: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.overflow = 0

    def error(self, row: int, field: str, message: str) -> None:
        self.failed += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append({"row": row, "field": field, "message": message})
        else:
            self.overflow += 1

    def as_dict(self, **extra: Any) -> Dict[str, Any]:
        out = {
            "ok": self.failed == 0,
            "dataset": self.dataset,
            "dry_run": self.dry_run,
            "total": self.total,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": self.errors,
            "warnings": self.warnings,
            "message": self._message(),
        }
        if self.overflow:
            out["warnings"].append("另有 %d 条同类错误未列出" % self.overflow)
        out.update(extra)
        return out

    def _message(self) -> str:
        if self.total == 0:
            return "文件里没有数据行"
        if self.dry_run:
            return "校验完成：可导入 %d 行，跳过 %d 行，失败 %d 行（尚未写入）" % (
                self.inserted, self.skipped, self.failed)
        return "导入完成：新增 %d 行，跳过 %d 行，失败 %d 行" % (
            self.inserted, self.skipped, self.failed)


# ---------------------------------------------------------------------------
# 通用入口
# ---------------------------------------------------------------------------
def import_dataset(
    conn,
    dataset: str,
    data: bytes,
    *,
    dry_run: bool = False,
    delimiter: Optional[str] = None,
) -> Dict[str, Any]:
    """按数据集导入。未知数据集抛 ValueError（由路由转 40001）。"""
    handlers: Dict[str, Callable[..., Dict[str, Any]]] = {
        "work_orders": import_work_orders,
        "defect_codes": import_defect_codes,
        "lots": import_lots,
        "measurements": import_measurements,
    }
    if dataset not in handlers:
        raise ValueError("不支持的导入类型：%s（可选 %s）" % (dataset, "、".join(handlers)))

    parsed = textio.read_csv(data, delimiter=delimiter)
    if not parsed["rows"]:
        return {"ok": False, "dataset": dataset, "total": 0, "message": "文件为空",
                "warnings": parsed["warnings"], "errors": []}

    mapping, missing = textio.map_columns(parsed["headers"], ALIASES[dataset])
    report = _Report(dataset, dry_run)
    report.warnings.extend(parsed["warnings"])

    if missing:
        # 缺列是"整份文件不可用"，不是"某几行有问题"，所以直接返回
        report.warnings.append("缺少必需列：%s（表头实际为 %s）"
                               % ("、".join(missing), "、".join(parsed["headers"])))
    result = handlers[dataset](conn, parsed["rows"], mapping, report)
    result.setdefault("encoding", parsed["encoding"])
    result.setdefault("delimiter", parsed["delimiter"])
    return result


# ---------------------------------------------------------------------------
# 各数据集
# ---------------------------------------------------------------------------
def import_work_orders(conn, rows, mapping, report: _Report) -> Dict[str, Any]:
    """导入工单。主键：工单号。"""
    if "work_order_id" not in mapping:
        report.total = len(rows)
        report.failed = len(rows)
        report.errors.append({"row": 0, "field": "work_order_id", "message": "缺少「工单号」列"})
        return report.as_dict()

    for i, row in enumerate(rows, start=2):
        report.total += 1
        wo = str(row.get(mapping["work_order_id"], "")).strip()
        err = _required(wo, "工单号")
        product = str(row.get(mapping.get("product_name", ""), "") or "未命名产品").strip()
        line_id = str(row.get(mapping.get("line_id", ""), "") or "").strip()
        qty, qerr = _int(row.get(mapping.get("planned_qty", "")), "计划数量")
        when, derr = _date(row.get(mapping.get("occurred_at", "")), "日期")

        if err or qerr or derr:
            report.error(i, "工单号", err or qerr or derr)
            continue
        if not line_id:
            report.error(i, "产线", "产线不能为空（数据级权限靠它过滤）")
            continue

        existing = repo.get_work_order(conn, wo)
        if existing:
            report.skipped += 1
            continue
        if not report.dry_run:
            repo.insert_work_order(conn, wo, product, line_id, qty or 0, occurred_at=when)
        report.inserted += 1

    return report.as_dict()


def import_defect_codes(conn, rows, mapping, report: _Report) -> Dict[str, Any]:
    """导入缺陷码字典。主键：缺陷码。

    关键词列用「、」「,」「;」分隔，导入时拆成列表——
    现场在 Excel 里写 `螺纹、外径、通规` 是最自然的方式。
    """
    if "code" not in mapping:
        report.total = len(rows)
        report.failed = len(rows)
        report.errors.append({"row": 0, "field": "code", "message": "缺少「缺陷码」列"})
        return report.as_dict()

    for i, row in enumerate(rows, start=2):
        report.total += 1
        code = str(row.get(mapping["code"], "")).strip()
        name = str(row.get(mapping.get("name", ""), "") or "").strip()
        category = str(row.get(mapping.get("category", ""), "") or "其他").strip()
        raw_kw = str(row.get(mapping.get("keywords", ""), "") or "")
        sort_no, serr = _int(row.get(mapping.get("sort_no", "")), "排序")

        if not code:
            report.error(i, "缺陷码", "缺陷码不能为空")
            continue
        if not name:
            report.error(i, "名称", "名称不能为空")
            continue
        if serr and mapping.get("sort_no") and str(row.get(mapping["sort_no"], "")).strip():
            report.error(i, "排序", serr)
            continue

        keywords = [k.strip() for k in raw_kw.replace(";", "、").replace(",", "、")
                    .replace("，", "、").split("、") if k.strip()]
        existing = repo.get_defect_code(conn, code)
        if existing:
            report.skipped += 1
            continue
        if not report.dry_run:
            repo.upsert_defect_code(conn, code, name, category, keywords, sort_no or 0)
        report.inserted += 1

    return report.as_dict()


def import_lots(conn, rows, mapping, report: _Report) -> Dict[str, Any]:
    """导入物料批。主键：批号。"""
    if "lot_id" not in mapping:
        report.total = len(rows)
        report.failed = len(rows)
        report.errors.append({"row": 0, "field": "lot_id", "message": "缺少「批号」列"})
        return report.as_dict()

    for i, row in enumerate(rows, start=2):
        report.total += 1
        lot_id = str(row.get(mapping["lot_id"], "")).strip()
        material_code = str(row.get(mapping.get("material_code", ""), "") or "").strip()
        material_name = str(row.get(mapping.get("material_name", ""), "") or "").strip()
        qty, qerr = _num(row.get(mapping.get("qty", "")), "数量")
        when, derr = _date(row.get(mapping.get("occurred_at", "")), "日期")

        if not lot_id:
            report.error(i, "批号", "批号不能为空")
            continue
        if qerr:
            report.error(i, "数量", qerr)
            continue
        if derr:
            report.error(i, "日期", derr)
            continue
        if not material_code or not material_name:
            report.error(i, "物料编码", "物料编码与名称都要填，否则追溯时只知道批号不知道是什么料")
            continue

        if repo.get_material_lot(conn, lot_id):
            report.skipped += 1
            continue
        if not report.dry_run:
            repo.insert_material_lot(
                conn, lot_id, material_code, material_name,
                supplier=str(row.get(mapping.get("supplier", ""), "") or "").strip() or None,
                supplier_lot=str(row.get(mapping.get("supplier_lot", ""), "") or "").strip() or None,
                qty=qty or 0.0,
                unit=str(row.get(mapping.get("unit", ""), "") or "kg").strip(),
                occurred_at=when,
            )
        report.inserted += 1

    return report.as_dict()


def import_measurements(conn, rows, mapping, report: _Report) -> Dict[str, Any]:
    """导入实测值（SPC 的数据源）。

    比其他三个数据集多两道校验：
    - 值必须在规格内？**不校验**，超差是事实，删掉它等于掩盖问题；
    - USL 必须大于 LSL？**校验**，这是输入错误，会让后续 Cpk 变成负数。
    """
    for field in ("item_code", "value"):
        if field not in mapping:
            report.total = len(rows)
            report.failed = len(rows)
            report.errors.append({"row": 0, "field": field,
                                  "message": "缺少「%s」列" % ("检验项目" if field == "item_code" else "实测值")})
            return report.as_dict()

    payload: List[Dict[str, Any]] = []
    unknown_line = 0
    for i, row in enumerate(rows, start=2):
        report.total += 1
        item = str(row.get(mapping["item_code"], "")).strip()
        value, verr = _num(row.get(mapping["value"], ""), "实测值")
        subgroup, serr = _int(row.get(mapping.get("subgroup_no", ""), ""), "子组号")
        usl, uerr = _num(row.get(mapping.get("usl", ""), ""), "规格上限")
        lsl, lerr = _num(row.get(mapping.get("lsl", ""), ""), "规格下限")
        when, derr = _date(row.get(mapping.get("occurred_at", "")), "时间")
        line_id = str(row.get(mapping.get("line_id", ""), "") or "").strip()

        err = None
        field = "实测值"
        if not item:
            err, field = "检验项目不能为空", "检验项目"
        elif value is None and not verr:
            err = "实测值不能为空"          # 空值：只有这一列必须填
        elif verr:
            err = verr
        elif derr:
            err, field = derr, "时间"
        elif serr:
            err, field = serr, "子组号"
        elif usl is not None and lsl is not None and usl <= lsl:
            err, field = "规格上限(%s)必须大于下限(%s)" % (usl, lsl), "规格上限"
        if err:
            report.error(i, field, err)
            continue

        if not line_id:
            # 不拦：QE 从设备导出的原始数据常常没有产线列，跨线分析是合理需求。
            # 但必须留痕——否则"看板上少了一条产线"会查半天。
            unknown_line += 1
            line_id = "UNKNOWN"

        payload.append(
            {
                "item_code": item,
                "value": value,
                "line_id": line_id,
                "subgroup_no": subgroup or 1,
                "unit": str(row.get(mapping.get("unit", ""), "") or "mm").strip(),
                "usl": usl,
                "lsl": lsl,
                "gauge": str(row.get(mapping.get("gauge", ""), "") or "").strip() or None,
                "operator": str(row.get(mapping.get("operator", ""), "") or "").strip() or None,
                "occurred_at": when,
            }
        )

    if unknown_line:
        report.warnings.append("%d 行没有产线，已记为 UNKNOWN（班组长看不到这批数据）" % unknown_line)

    if payload and not report.dry_run:
        n = repo.insert_measurements(conn, payload)
        report.inserted += n
    elif payload:
        report.inserted += len(payload)

    return report.as_dict()
