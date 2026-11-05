"""统计分析包（纯计算，无 I/O）。

模块划分与职责边界：

    descriptive.py  单变量描述统计：均值/标准差/分位/直方图/偏度峰度/线性拟合
    spc.py          控制图：X̄-R、I-MR、P、U + Nelson 8 条判异规则
    capability.py   过程能力：Cp/Cpk/Pp/Ppk/Ca/Cr/PPM + 判级与改善方向提示
    pareto.py       帕累托：ABC 分类、两期对比
    trend.py        时间聚合：按天/周/月分桶、不良率序列、趋势检验、星期效应

为什么这个包不 import repo、不 import services：
依赖方向必须是 `api → services → repo → (无)` 与 `services → analytics → (无)`。
反过来的话，统计计算就会被数据库结构绑架，改一张表要动算法，写单测要建库。
这是全项目唯一"零依赖"的包，请保持。
"""

from __future__ import annotations

from . import capability, descriptive, pareto, spc, trend

__all__ = ["descriptive", "spc", "capability", "pareto", "trend"]
