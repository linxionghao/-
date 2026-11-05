"""描述统计：一组数进去，一组数出来，不碰数据库、不碰网络。

为什么单独成包、而不是写在服务层里：

1. **可测**。这些函数是全项目里唯一可以"给数就能断言"的部分，单测不需要建库、
   不需要起服务，几十毫秒跑完，因此可以覆盖得比业务层更密；
2. **可复用**。控制图（spc）、过程能力（capability）、趋势（trend）三处都要算
   均值与标准差，如果各写一遍，迟早在"除以 n 还是 n-1"上分叉，出现
   "仪表盘说 Cpk=1.12、报表说 Cpk=1.09"这种自己都解释不清的差异；
3. **无副作用**。本包全部函数**不接收 conn、不写日志、不抛业务异常**，
   输入非法时返回 None 或空列表，由调用方决定是"降级展示"还是"报错"。

统计口径上的两个约定（写在代码里，也写在 docs/analytics.md）：

- 样本方差一律用 **n-1**（贝塞尔校正）。质量控制拿的是抽样数据，不是全集，
  用 n 会系统性低估波动，导致控制限偏窄、误报变多；
- 分位数一律用 **线性插值**（与 Excel 的 PERCENTILE.INC、numpy 的 linear 一致）。
  车间用 Excel 核对我们给的 P95，口径必须能对上，否则每次对数都要吵一次。
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Number = float


# ---------------------------------------------------------------------------
# 基础：清洗与求和
# ---------------------------------------------------------------------------
def clean(values: Optional[Iterable[Number]]) -> List[float]:
    """把输入变成"干净的浮点列表"。

    做三件事：丢掉 None、丢掉 NaN 与 inf、其余转 float。
    为什么不直接 `[float(v) for v in values]`：数据库里 `qty` 可能是 NULL，
    Excel 导入会带空字符串，JSON 里可能有 `null`——这些都会在 float() 上炸掉，
    而它们的语义都是"这条没有值"，不是"这条是 0"。把 0 混进来会直接拉低均值，
    是统计类 bug 里最常见也最难被发现的一种（数字看起来完全正常）。
    """
    if not values:
        return []
    out: List[float] = []
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        out.append(f)
    return out


def total(values: Optional[Iterable[Number]]) -> float:
    """求和（空列表返回 0.0，而不是抛异常）。"""
    return float(sum(clean(values)))


def mean(values: Optional[Iterable[Number]]) -> Optional[float]:
    """算术平均；无有效数据返回 None。

    返回 None 而不是 0：0 是一个合法均值（比如全部不良数为 0 的那天），
    None 表示"没有数据"。前端据此显示 "—" 而不是 "0"，两者的业务含义完全不同。
    """
    data = clean(values)
    if not data:
        return None
    return sum(data) / len(data)


def median(values: Optional[Iterable[Number]]) -> Optional[float]:
    """中位数。偶数个取中间两个的平均。"""
    data = sorted(clean(values))
    if not data:
        return None
    n = len(data)
    mid = n // 2
    if n % 2 == 1:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2.0


def variance(values: Optional[Iterable[Number]], sample: bool = True) -> Optional[float]:
    """方差。sample=True 用 n-1（默认），否则用 n。

    n=1 且 sample=True 时无法估计方差，返回 None——这是"自由度不够"，
    不是"方差为 0"。控制图在子组大小为 1 时改用移动极差，正是同一个原因。
    """
    data = clean(values)
    n = len(data)
    if n == 0:
        return None
    if sample and n < 2:
        return None
    mu = sum(data) / n
    ss = sum((x - mu) ** 2 for x in data)
    return ss / (n - 1 if sample else n)


def stdev(values: Optional[Iterable[Number]], sample: bool = True) -> Optional[float]:
    """标准差 = 方差的平方根。"""
    var = variance(values, sample=sample)
    if var is None:
        return None
    return math.sqrt(var)


def minimum(values: Optional[Iterable[Number]]) -> Optional[float]:
    data = clean(values)
    return min(data) if data else None


def maximum(values: Optional[Iterable[Number]]) -> Optional[float]:
    data = clean(values)
    return max(data) if data else None


def value_range(values: Optional[Iterable[Number]]) -> Optional[float]:
    """极差 R = max - min。控制图里 R 图用的就是它。"""
    data = clean(values)
    if not data:
        return None
    return max(data) - min(data)


def quantile(values: Optional[Iterable[Number]], q: float) -> Optional[float]:
    """分位数（线性插值，与 Excel PERCENTILE.INC 同口径）。

    实现的是 R-7 / numpy linear 定义：
        pos = (n - 1) * q
        lo = floor(pos), hi = ceil(pos)
        result = x[lo] + (pos - lo) * (x[hi] - x[lo])

    为什么不用"取排序后第 k 个"的简单法：样本量小的时候（车间一天可能只有 5 条），
    简单法会把 P95 直接退化成年最大值，同一批数据在 n=5 与 n=50 下口径不一致，
    看起来像"系统算错了"。
    """
    data = sorted(clean(values))
    if not data:
        return None
    if q <= 0:
        return data[0]
    if q >= 1:
        return data[-1]
    pos = (len(data) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return data[lo]
    frac = pos - lo
    return data[lo] + frac * (data[hi] - data[lo])


def iqr(values: Optional[Iterable[Number]]) -> Optional[float]:
    """四分位距 IQR = P75 - P25。"""
    q1 = quantile(values, 0.25)
    q3 = quantile(values, 0.75)
    if q1 is None or q3 is None:
        return None
    return q3 - q1


def outliers(values: Optional[Iterable[Number]], k: float = 1.5) -> List[float]:
    """按 Tukey 围栏（Q1 - k·IQR, Q3 + k·IQR）挑出离群点，返回原始值。

    k=1.5 是"疑似异常"，k=3.0 是"极端异常"。质量数据里更常用后者：
    单件尺寸的分布本来就是厚尾的，用 1.5 会把正常的边界件全标成异常。
    """
    data = clean(values)
    if len(data) < 4:
        return []
    q1 = quantile(data, 0.25)
    q3 = quantile(data, 0.75)
    assert q1 is not None and q3 is not None
    span = q3 - q1
    low = q1 - k * span
    high = q3 + k * span
    return [x for x in data if x < low or x > high]


def zscores(values: Optional[Iterable[Number]]) -> List[Optional[float]]:
    """逐点标准分 (x - μ) / σ；σ 为 0 或样本不足时对应位置给 None。

    保持与输入等长（None 占位），因为调用方通常要拿它和原始点一一对应画图，
    过滤掉就会错位——这类错位在图表上表现为"标记跑到隔壁点上"，极难排查。
    """
    data = clean(values)
    mu = mean(data)
    sd = stdev(data)
    if mu is None or not sd:
        return [None] * len(data)
    return [(x - mu) / sd for x in data]


# ---------------------------------------------------------------------------
# 分布形状
# ---------------------------------------------------------------------------
def histogram(
    values: Optional[Iterable[Number]], bins: Optional[int] = None
) -> Dict[str, object]:
    """直方图。bins 为空时用 Sturges 公式 ceil(log2(n)) + 1。

    返回 {bins: [{low, high, count, ratio}], min, max, n, bin_width}，
    结构直接可喂给前端画柱状图，不需要前端再算一遍分箱（两端各算一次必然对不上）。
    """
    data = clean(values)
    if not data:
        return {"bins": [], "min": None, "max": None, "n": 0, "bin_width": None}

    lo, hi = min(data), max(data)
    if bins is None:
        bins = max(1, int(math.ceil(math.log2(len(data)))) + 1) if len(data) > 1 else 1
    bins = max(1, int(bins))

    if hi == lo:
        # 全部相同：给一个宽度为 1 的桶，避免除零
        return {
            "bins": [{"low": lo, "high": lo, "count": len(data), "ratio": 1.0}],
            "min": lo,
            "max": hi,
            "n": len(data),
            "bin_width": 0.0,
        }

    width = (hi - lo) / bins
    edges = [lo + width * i for i in range(bins + 1)]
    counts = [0] * bins
    for x in data:
        idx = int((x - lo) / width)
        if idx >= bins:  # 最大值落在最后一个桶
            idx = bins - 1
        if idx < 0:
            idx = 0
        counts[idx] += 1

    n = len(data)
    return {
        "bins": [
            {
                "low": round(edges[i], 6),
                "high": round(edges[i + 1], 6),
                "count": counts[i],
                "ratio": round(counts[i] / n, 6),
            }
            for i in range(bins)
        ],
        "min": lo,
        "max": hi,
        "n": n,
        "bin_width": round(width, 6),
    }


def skewness(values: Optional[Iterable[Number]]) -> Optional[float]:
    """偏度（Fisher-Pearson 标准化矩，样本校正版）。

    质量场景里偏度的用途：尺寸类数据若明显右偏（> +1），通常意味着
    "刀具磨损方向单边"——不是随机波动，而是过程在漂移，这时该看趋势图而不是控制图。
    """
    data = clean(values)
    n = len(data)
    if n < 3:
        return None
    mu = sum(data) / n
    m2 = sum((x - mu) ** 2 for x in data) / n
    if m2 == 0:
        return None
    m3 = sum((x - mu) ** 3 for x in data) / n
    g1 = m3 / (m2 ** 1.5)
    # 小样本校正，n 越大越接近 g1
    return g1 * math.sqrt(n * (n - 1)) / (n - 2)


def kurtosis(values: Optional[Iterable[Number]]) -> Optional[float]:
    """超额峰度（正态分布为 0）。

    峰度 > 0 说明"尖峰厚尾"——多数点紧贴中心，但偶尔出现远离中心的点。
    现场interpretation：这是"混批"的典型形状（两个不同批次的料混在一起测），
    单看均值和标准差都正常，只有峰度会露馅。
    """
    data = clean(values)
    n = len(data)
    if n < 4:
        return None
    mu = sum(data) / n
    m2 = sum((x - mu) ** 2 for x in data) / n
    if m2 == 0:
        return None
    m4 = sum((x - mu) ** 4 for x in data) / n
    g2 = m4 / (m2 * m2) - 3.0
    return ((n - 1) * ((n + 1) * g2 + 6)) / ((n - 2) * (n - 3))


def summary(values: Optional[Iterable[Number]]) -> Dict[str, Optional[float]]:
    """一次给出前端仪表盘要的全部标量。"""
    data = clean(values)
    return {
        "n": len(data),
        "sum": round(total(data), 6) if data else 0.0,
        "mean": _round(mean(data)),
        "median": _round(median(data)),
        "stdev": _round(stdev(data)),
        "min": minimum(data),
        "max": maximum(data),
        "range": value_range(data),
        "p25": _round(quantile(data, 0.25)),
        "p75": _round(quantile(data, 0.75)),
        "p95": _round(quantile(data, 0.95)),
        "skewness": _round(skewness(data)),
        "kurtosis": _round(kurtosis(data)),
    }


def _round(v: Optional[float], digits: int = 4) -> Optional[float]:
    """统一保留 4 位。返回给前端前一定要收口，否则 JSON 里会出现
    1.7999999999999998 这种数字，用户第一反应是"这系统算得不准"。"""
    if v is None:
        return None
    return round(v, digits)


# ---------------------------------------------------------------------------
# 平滑与拟合：趋势分析的地基
# ---------------------------------------------------------------------------
def moving_average(
    values: Optional[Iterable[Number]], window: int = 3
) -> List[Optional[float]]:
    """简单移动平均。前 window-1 个位置给 None（不足一个窗口）。

    注意是"居中"还是"滞后"：这里用**滞后**（第 i 位是 i-window+1..i 的均值），
    因为车间看的是"最近 3 天平均不良率"，不是"以今天为中心的 3 天"——
    后者会用到未来数据，在日报场景里根本拿不到。
    """
    data = clean(values)
    window = max(1, int(window))
    out: List[Optional[float]] = []
    acc = 0.0
    for i, x in enumerate(data):
        acc += x
        if i >= window:
            acc -= data[i - window]
        if i >= window - 1:
            out.append(acc / window)
        else:
            out.append(None)
    return out


def ewma(
    values: Optional[Iterable[Number]], alpha: float = 0.3
) -> List[Optional[float]]:
    """指数加权移动平均 EWMA。alpha 越大越贴近原始序列。

    EWMA 控制图（ Montgomery 建议 lambda=0.2、L=3 ）对"小幅度持续漂移"比
    X-bar 图敏感得多——刀具缓慢磨损就是这种模式，X-bar 图往往要等废品出来才报警。
    """
    data = clean(values)
    if not data:
        return []
    alpha = min(max(float(alpha), 0.01), 1.0)
    out: List[Optional[float]] = [data[0]]
    prev = data[0]
    for x in data[1:]:
        prev = alpha * x + (1 - alpha) * prev
        out.append(prev)
    return out


def linear_fit(
    values: Optional[Iterable[Number]],
) -> Dict[str, Optional[float]]:
    """对 (下标, 值) 做最小二乘直线拟合，返回斜率/截距/R²。

    斜率是最有用的那个数：单位时间的变化量。尺寸类数据的斜率若显著不为 0
    （|t| > 2），说明过程存在系统性漂移，此时算 Cpk 是没有意义的——
    Cpk 的前提是过程稳定，不稳定过程的 Cpk 只是一个好看的平均数。
    """
    data = clean(values)
    n = len(data)
    if n < 2:
        return {"slope": None, "intercept": None, "r2": None, "n": n, "t_stat": None,
                "perfect_fit": False}

    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(data) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((xs[i] - mx) * (data[i] - my) for i in range(n))
    if sxx == 0:
        return {"slope": None, "intercept": None, "r2": None, "n": n, "t_stat": None,
                "perfect_fit": False}

    slope = sxy / sxx
    intercept = my - slope * mx

    ss_tot = sum((y - my) ** 2 for y in data)
    ss_res = sum((data[i] - (slope * xs[i] + intercept)) ** 2 for i in range(n))
    r2 = 1.0 - ss_res / ss_tot if ss_tot else None

    t_stat = None
    perfect_fit = bool(ss_tot) and ss_res <= 1e-12 * max(1.0, ss_tot)
    if n > 2 and ss_tot and not perfect_fit:
        se = math.sqrt(ss_res / (n - 2) / sxx)
        if se:
            t_stat = slope / se

    return {
        "slope": _round(slope, 6),
        "intercept": _round(intercept, 6),
        "r2": _round(r2),
        "n": n,
        "t_stat": _round(t_stat),
        # 完美线性拟合（残差为 0）时标准误为 0，t 统计量在数学上是无穷大，
        # 但无穷大没法进 JSON。因此单独给一个布尔位，让调用方知道"这条线是确定的"，
        # 而不是把 None 当成"检验失败"——两者结论正好相反。
        "perfect_fit": perfect_fit,
    }


def describe_change(current: Optional[float], previous: Optional[float]) -> Dict[str, object]:
    """环比描述：绝对变化 + 百分比 + 方向。

    除零处理：上期为 0、本期也为 0 → 变化 0%；上期为 0、本期非 0 → 百分比记 None
    但 direction 标 "up"。硬算成 +∞ 或 100% 都会误导——从 0 到 1 的"增长率"
    在数学上不存在，报表里写 "—" 比写 "100%" 诚实。
    """
    if current is None or previous is None:
        return {"delta": None, "pct": None, "direction": "unknown"}
    delta = current - previous
    if previous == 0:
        pct = 0.0 if current == 0 else None
    else:
        pct = delta / previous
    direction = "flat" if abs(delta) < 1e-12 else ("up" if delta > 0 else "down")
    return {
        "delta": _round(delta),
        "pct": _round(pct) if pct is not None else None,
        "direction": direction,
    }
