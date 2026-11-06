# 0010. 手写 XLSX（zip + 最小 OOXML），不引入 openpyxl

- 状态：已采纳
- 日期：2026-11-06
- 影响范围：`core/io/xlsx.py`、`core/io/exporter.py`、`api/routers/io.py`

## 背景

导出功能要支持两种格式：CSV 和 XLSX。CSV 只需字符串拼接，
XLSX 是 zip 包里的 6 个 XML 文件。

项目已有的依赖约束是这样的：

- 运行环境是实验室/车间机器，可能是离线安装，也可能装不上编译型包；
- CI 里**故意不装 faiss 与 jieba**，用来验证降级路径——这条策略让
  "依赖越少越好"从一种偏好变成了项目的实际约束；
- 已经为了 JWT 手写过 HS256（ADR-0001），为了提示词不引模板引擎之外的库。

XLSX 的需求其实很窄：一张表，冻结表头，自动筛选，列宽按内容估一下，
数字写成数字而不是文本。不需要图表、公式、合并单元格、条件格式。

## 决策

手写一个最小 OOXML 写入器，`core/io/xlsx.py`，约 200 行。

一个 `.xlsx` 就是 zip 里的这些文件：

```
[Content_Types].xml        内容类型声明
_rels/.rels                包级关系（指向 workbook）
xl/workbook.xml            工作表清单
xl/_rels/workbook.xml.rels 工作表关系（指向 sheet 与 styles）
xl/styles.xml              样式表（表头加粗、数字格式）
xl/worksheets/sheet1.xml   实际数据
```

关键实现点：

1. **XML 非法字符必须剔除**。车间数据里常有从 Excel 复制出来的
   `\x00`–`\x08`、`\x0b`、`\x0c`、`\x0e`–`\x1f`，写进 XML 会让
   Excel 打不开文件并提示"内容有错误"。`_ILLEGAL_XML` 在写入前统一过滤；
2. **数字写成数字**（`<c><v>1.5</v></c>`），文本用 `t="inlineStr"`。
   全部写成文本时，用户点"求和"会得到 0——这是最常见的"导出看着正常但没法用"；
3. **列宽按字符数估算，中文按 2 个宽度**。不估的话所有列都是默认宽度，
   中文列被截断成一堆 `####`；
4. **表头冻结 + 自动筛选**：`<pane ySplit="1" topLeftCell="A2" state="frozen"/>`
   + `<autoFilter ref="A1:H200"/>`。这两条是"能直接用"和"还得手动调"的分界；
5. **工作表名清洗**：`/ \ ? * [ ] :` 是 Excel 的非法字符，长度上限 31。
   不处理会让 `openpyxl` 能生成但 Excel 打不开。

`build_xlsx(sheets, title)` 接收 `[(表名, headers, rows), ...]`，
返回 `bytes`。多 sheet 打包（`export/bundle`）复用它。

## 后果

- 好处：零依赖，离线可用；生成的文件约 5 KB（同内容 openpyxl 约 30 KB，
  因为它带完整样式表与主题）；不会因为 openpyxl 版本升级改行为。
- 代价：不支持公式、合并单元格、图片、图表。需要这些时只能换库——
  但本项目导出的是数据表，不是报表模板。
- 维护成本：OOXML 的 schema 很长，好在只用最小子集，
  6 个文件的内容都是常量字符串 + 数据拼接，改动面很小。
- 未来若要改：`build_xlsx()` 的签名可以保留，内部换成 openpyxl
  只需重写一个文件；调用方（`exporter.build`）不用动。

## 替代方案

| 方案 | 为什么不选 |
|---|---|
| `openpyxl` | 多一个依赖；纯 Python 但会带 `et-xmlfile`；本项目只用它 5% 的功能 |
| `xlsxwriter` | 同上，且它是"只能写不能读"，`openpyxl` 至少读写都行 |
| `pandas.to_excel` | 引入 pandas（几十 MB）+ openpyxl；为了一张表不值得 |
| 只导出 CSV | Excel 双击打开 UTF-8 无 BOM 的 CSV 会乱码；加 BOM 后中文正常，但数字列会被识别成文本，且无法多 sheet |

## 补充：CSV 侧的两个坑

手写 XLSX 的同时，CSV 侧也处理了两个容易忽略的问题：

1. **BOM**：导出带 `\ufeff`，否则 Excel 双击打开中文全是乱码。
   导入时 `utf-8-sig` 优先，这样带 BOM 与不带 BOM 的文件都能读；
2. **公式注入**：单元格以 `=` `+` `-` `@` 开头时前置单引号（`'`）。
   这是 `=cmd|'/c calc'!A1` 这类载荷的标准缓解方式。
   牺牲一个可见字符，换掉一个可执行载荷。
