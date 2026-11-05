"""导入导出包：文本/CSV 探测、零依赖 XLSX 写出、数据集导入与导出。

模块职责：

    textio.py    编码探测、分隔符探测、表头归一、CSV 读写、CSV 注入防护
    xlsx.py      零依赖 XLSX 写出（zip + 最小 OOXML）
    importer.py  四个数据集的导入（工单/缺陷码/物料批/实测值）+ 逐行错误报告
    exporter.py  五个数据集的导出（缺陷/归因/审计/帕累托/供应商）

依赖方向：`api → io`（路由直接调用）与 `io → repo`。
`io` **不依赖** `services`，唯一例外是 exporter 里延迟导入的两个分析函数
（pareto_table / supplier_table）——那两处如果提前 import 会形成
`io → services → analytics` 与 `services → io` 的循环，延迟导入是成本最低的解法。
"""

from __future__ import annotations

from . import exporter, importer, textio, xlsx

__all__ = ["textio", "xlsx", "importer", "exporter"]
