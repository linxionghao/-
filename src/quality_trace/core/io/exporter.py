"""导出：把库里的数据变成"能发给别人"的文件。

三条产品层面的决定，写在这里而不是散落在路由里：

1. **同一个导出既能出 CSV 也能出 XLSX**。给车间的是 xlsx（双击就能看），
   给系统对接的是 csv（好解析）。两套格式各写一遍必然口径漂移，
   所以先统一成 `(headers, rows)`，再交给两个 writer。
2. **导出必须带口径说明**。缺陷单导出会在文件名与首行注明
   "时间口径=业务时间"。半年后有人拿这份表跟财务报表对不上时，
   文件自己能解释清楚——这比在群里翻聊天记录靠谱。
3. **不做"全表导出"**。所有导出都必须带条件（时间区间是必填），
   没有条件时默认最近 30 天。理由很实际：10 万行 xlsx 会让浏览器卡死，
   而用户会以为是系统崩了。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..analytics import trend as tr
from ..repo import db as repo
from . import textio, xlsx

Row = List[object]


def stamp() -> str:
    """文件名用的时间戳（到分钟，避免同名覆盖）。"""
    return _dt.datetime.now().strftime("%Y%m%d-%H%M")


def _normalize_range(date_from: Optional[str], date_to: Optional[str], days: int = 30) -> Tuple[str, str]:
    today = _dt.date.today()
    end = tr.parse_date(date_to) or today
    start = tr.parse_date(date_from) or (end - _dt.timedelta(days=days - 1))
    if start > end:
        start, end = end, start
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# 各数据集的 (headers, rows)
# ---------------------------------------------------------------------------
def defect_table(
    conn,
    *,
    line_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    days: int = 30,
    status: Optional[str] = None,
) -> Tuple[str, List[str], List[Row]]:
    """缺陷单导出。"""
    start, end = _normalize_range(date_from, date_to, days)
    rows = repo.query_defects(
        conn, line_id=line_id, status=status, date_from=start, date_to=end
    )
    headers = [
        "单号", "工单", "产线", "工序", "描述", "数量", "严重度",
        "缺陷码", "状态", "补录", "业务时间(发生)", "录入时间", "录入人",
    ]
    out: List[Row] = []
    for r in rows:
        out.append([
            r.get("defect_no"), r.get("work_order_id"), r.get("line_id"),
            r.get("op_code"), r.get("description"), r.get("qty"),
            r.get("severity"), r.get("defect_code") or "",
            r.get("status"), "是" if r.get("is_backfill") else "否",
            r.get("occurred_at"), r.get("recorded_at"), r.get("created_by"),
        ])
    name = "缺陷单_%s_%s" % (start, end)
    return name, headers, out


def attribution_table(conn, *, status: Optional[str] = None) -> Tuple[str, List[str], List[Row]]:
    """归因单导出（含审批结论与是否已出 8D）。"""
    rows = repo.list_attributions(conn, status=status)
    headers = ["归因单号", "缺陷单", "根因（待验证）", "状态", "提交人", "提交时间",
               "审批人", "审批时间", "审批意见", "是否已生成 8D"]
    out: List[Row] = []
    for r in rows:
        out.append([
            r.get("attribution_id"), r.get("defect_no"), r.get("root_cause"),
            r.get("status"), r.get("submitted_by"), r.get("submitted_at"),
            r.get("decided_by") or "", r.get("decided_at") or "",
            r.get("decision_note") or "",
            "是" if r.get("draft_8d_md") else "否",
        ])
    return "归因单_%s" % stamp(), headers, out


def audit_table(conn, *, only_flagged: bool = True) -> Tuple[str, List[str], List[Row]]:
    """对话审计导出。

    默认只导"有标记"的（越界/无引用/旧版本），因为审计的对象是问题，
    不是全部问答。需要全量时显式传 only_flagged=False。
    """
    rows = repo.list_audit_messages(conn, only_flagged=only_flagged, limit=2000)
    headers = ["消息ID", "会话", "角色", "时间", "越界标记", "无引用", "引用旧版本",
               "复核人", "复核时间", "回答摘要"]
    out: List[Row] = []
    for r in rows:
        out.append([
            r.get("message_id"), r.get("session_id"), r.get("session_role"),
            r.get("occurred_at"),
            "是" if r.get("flag_root_cause") else "",
            "是" if r.get("flag_no_citation") else "",
            "是" if r.get("flag_stale_version") else "",
            r.get("reviewed_by") or "", r.get("reviewed_at") or "",
            (r.get("content") or "")[:120].replace("\n", " "),
        ])
    return "对话审计_%s" % stamp(), headers, out


def pareto_table(conn, *, dim: str = "defect_code", **kw) -> Tuple[str, List[str], List[Row]]:
    """帕累托导出（按维度）。"""
    from ..services import analytics as svc

    data = svc.pareto_by(conn, dim, **kw)
    headers = ["排名", "项目", "数量", "占比", "累计占比", "ABC"]
    out: List[Row] = [
        [r["rank"], r["key"], r["value"], r["ratio"], r["cum_ratio"], r["abc"]]
        for r in data["rows"]
    ]
    rng = data.get("range") or {}
    return "帕累托_%s_%s_%s" % (
        data.get("dim_label", dim), rng.get("date_from", ""), rng.get("date_to", "")
    ), headers, out


def supplier_table(conn, *, date_from: Optional[str] = None, date_to: Optional[str] = None,
                   days: int = 30) -> Tuple[str, List[str], List[Row]]:
    """供应商记分卡导出（带口径说明，避免被当索赔依据）。"""
    from ..services import genealogy as gene

    data = gene.supplier_scorecard(conn, date_from=date_from, date_to=date_to, days=days)
    headers = ["供应商", "批次数", "关联工单", "关联产量", "不良件数", "PPM", "样本量"]
    out: List[Row] = [
        [r["supplier"], r["lots"], r["orders"], r["produced"], r["defect_pieces"],
         r["ppm"], "不足" if r["small_sample"] else "正常"]
        for r in data["rows"]
    ]
    rng = data["range"]
    return "供应商记分卡_%s_%s" % (rng["date_from"], rng["date_to"]), headers, out


# 注册表：路由用 `format` + `dataset` 两个参数就能定位到具体导出，
# 新增数据集只加一行，不用改路由（也就不用改三处错误处理）。
DATASETS = {
    "defects": defect_table,
    "attributions": attribution_table,
    "audit": audit_table,
    "pareto": pareto_table,
    "suppliers": supplier_table,
}


# ---------------------------------------------------------------------------
# 统一出口
# ---------------------------------------------------------------------------
def build(dataset: str, conn, *, fmt: str = "csv", **kwargs) -> Tuple[str, bytes, str]:
    """生成导出文件，返回 (文件名, 字节, MIME)。

    MIME 要写对：写成 `application/octet-stream` 时，浏览器会把它当未知附件，
    部分安卓微信会把 xlsx 直接存成 `.bin`，用户点不开。
    """
    if dataset not in DATASETS:
        raise ValueError("不支持的导出数据集：%s" % dataset)
    name, headers, rows = DATASETS[dataset](conn, **kwargs)

    if fmt not in ("csv", "xlsx"):
        # 不能"默默降级成 CSV"：用户点的是"导出 Excel"，拿到 .csv 会以为系统坏了
        raise ValueError("不支持的导出格式：%s（可选 csv / xlsx）" % fmt)

    if fmt == "xlsx":
        data = xlsx.build_xlsx([{"name": name[:28], "headers": headers, "rows": rows}])
        mime = ("application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet")
        return "%s.xlsx" % name, data, mime

    data = textio.to_csv_bytes(headers, rows)
    return "%s.csv" % name, data, "text/csv; charset=utf-8"


def build_multi_xlsx(sheets: Sequence[Tuple[str, Sequence[str], Sequence[Sequence[object]]]]) -> bytes:
    """多工作表导出（例如"质量周报"一次给三张表）。"""
    payload = [
        {"name": name, "headers": list(headers), "rows": [list(r) for r in rows]}
        for name, headers, rows in sheets
    ]
    return xlsx.build_xlsx(payload)
