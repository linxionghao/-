# 工程约定

> 这份文档写给"下一个改这个仓库的人"——也包括三个月后的我自己。
> 里面每一条都对应一个真实踩过的坑，不是风格偏好清单。

---

## 1. 分层与依赖方向

```
api/            入口层：路由、依赖注入、SSE 格式化、页面渲染
  ↓ 只能向下
core/services/  业务层：编排（检索 → 提示词 → LLM）、状态机、权限判断
  ↓
core/repo/      数据层：SQL、建表、迁移
core/knowledge/ 检索层：分词、BM25、向量、融合
core/llm/       适配层：供应商无关的 call_llm / stream_chat / embed_texts
core/services/errors.py  错误码字典（全层共用，无依赖）
```

三条硬规则：

1. **不向上依赖**：`core` 里任何模块都不 import `fastapi`、不 import `api.*`。这条被 `pytest` 之外的方式守着——`core` 里的 `print` 会直接出现在 CLI 输出里，而在 Web 进程中会污染 stdout，所以 `core` 一律不 print，改由调用方决定怎么呈现。
2. **检索层与 LLM 层互不调用**（架构铁律）。检索只负责"给哪些片段"，LLM 只负责"怎么读这些片段"。让检索层去调 LLM 做查询改写，短期省事，长期会让"为什么召回这条"变成不可解释的。
3. **配置只在入口层读**。`core` 不读环境变量、不读 yaml——配置以参数形式传进来（`llm_cfg`、`api_key`、`retrieval_cfg`）。好处是单测可以随意构造配置，不用 monkeypatch 全局状态。

`core` 的三条禁区（写在每个模块 docstring 里）：

```python
【核心模块】core 约束：不 print、不读环境变量、不 import argparse。
```

---

## 2. 错误处理

### 2.1 一律抛 `ApiError`，不抛裸 `Exception`

```python
from quality_trace.core.services.errors import ApiError, ErrorCode

raise ApiError(ErrorCode.DOC_VERSION_CONFLICT)                  # 40903
raise ApiError(ErrorCode.BAD_REQUEST, "缺陷描述至少 4 个字")      # 40001 + 自定义文案
```

- 错误码集中在 `errors.py`（28 个），文案也是中文、面向用户，**不在路由里现编**；
- 全局异常处理器把 `ApiError` 转成统一包 `{code, message, data: null}`，HTTP 状态取错误码前三位（`40903 → 409`）；
- **禁止** `return {"code": 50001, ...}` 配 `status_code=200`——这是本项目修过的真实 bug：前端按 HTTP 状态判断成功，body 里的码没人看。

### 2.2 降级不是错误

`50001 / 50002 / 50401` 三个码的业务语义是"能力降级"，不是"失败"：

| 场景 | HTTP | body |
|---|---|---|
| LLM 不可用 | **200** | `code: 0` + `data.degraded = {code: 50002, message: "…"}` |
| 向量服务不可用（配置开启但调不通） | 200 | `degraded.code = 50001` |
| 流式超时（已产出部分正文） | 200 | `degraded.code = 50401` |
| 配置里本来就没开向量 | 200 | `degraded = null`（**配置关闭 ≠ 降级**） |

最后一行是最容易写错的：把"没开"当成"降级"，会让状态条永远挂着"降级中"，用户就不再相信这个标记了。

### 2.3 不吞异常

```python
try:
    ...conn.execute(sql)
except sqlite3.OperationalError:
    pass          # 只允许这种「幂等 DDL」场景，且必须写注释说明
```

其余场景一律捕获成具体的 `ApiError` 码，或在 `core` 层抛出后在服务层转换。吞掉异常 = 把一个可观测的失败变成一个不可观测的错数据。

---

## 3. 命名与结构

| 对象 | 约定 | 例子 |
|---|---|---|
| 表 / 列 | 小写下划线；时间列成对出现 | `defect_item.occurred_at` / `recorded_at` |
| 主键 | `<实体>_id`，前缀 id 便于日志 | `doc_id`、`attr-3f2a…`（`new_id("attr")`） |
| 错误码 | 五位：`类别(4/5) + 序号` | `40102 用户名或密码错误` |
| SSE 事件 | 小写单词，固定四个 + error | `status / sources / delta / done / error` |
| 前端 DOM id | 小写连字符 | `#btn-confirm`、`#as-of` |

模块命名按职责而非技术：`knowledge_admin.py`（知识库管理）、`normalize.py`（描述归一化），不是 `utils2.py`、`helper.py`。

---

## 4. 注释

**注释解释"为什么"，不解释"是什么"。** 判断标准：删掉这行注释，读者会不会写出错误的修改？

```python
# 好：解释了反直觉的选择
# 历史快照必须先于用户消息落库——否则当前问题会重复进上下文，
# 且会把刚写入的 partial assistant 空行当成历史。
history = self.build_history(sid)

# 坏：把代码翻译一遍
# 取历史
history = self.build_history(sid)
```

中文注释，全角标点；代码、SQL、字段名保持 ASCII。复杂 SQL 在函数 docstring 里写清"它在防什么"（见 `repo.list_chunks_as_of`）。

---

## 5. 提交规范

Conventional Commits，一个提交只做一件事：

```
<type>(<scope>): <subject>

type:  feat / fix / docs / test / refactor / chore / ci / perf
scope: api / chat / defects / knowledge / audit / attribution / repo / web / docs / seed / demo
```

| 好 | 坏 |
|---|---|
| `fix(auth): 会话接口补上归属校验` | `修复bug` |
| `docs(api): 补充现场录入接口与 curl 示例` | `更新文档` |
| `test(api): 新增 25 条 HTTP 层用例` | `加测试` |

要求：

- subject 用中文、不加句号、不超过 50 字；
- 一个提交能独立回滚（不出现"提交 A 依赖提交 B 才能编译"）；
- 提交前跑 `python -m pytest -q`，红了不提交——**测试通过是提交的前置条件，不是提交之后的事**。

---

## 6. 前端约定（原生 JS，无构建）

- 不用框架、不用打包器，直接 `<script>` 内联：课程项目的价值在"打开就能看"，不在工程化；
- 状态集中在几个全局对象里（`state`、`QUEUE_KEY`），不用观察者库；
- 所有 fetch 都走统一封装：先判 `res.ok`，再判 `body.code`，两个都过才算成功；
- 错误提示必须中文，且说清"下一步该做什么"（"照片超过 2MB，请压缩后重试"，不是"上传失败"）；
- 离线队列用 `localStorage`，键名 `qt_` 前缀，最多 20 条，超限丢最旧的并提示用户。

---

## 7. 提交前自查清单

改完一处，按这个顺序自查（顺序即"最容易漏"的排序）：

1. `python -m pytest -q` 全绿？
2. 新增的错误路径有对应的错误码，且中文文案通顺？
3. 新增字段有没有在 `public_view` 里考虑角色裁剪（班组长不能看到判定相关字段）？
4. 新增接口有没有挂 `Depends(require_roles(...))`？数据范围过滤在 SQL 层还是 Python 层？
5. 时间字段是否成对（`occurred_at` + `recorded_at`）？客户端能否伪造业务时间？
6. 改了表结构，`_MIGRATION_SQL` 里补了增列语句吗（旧库要能升级）？
7. 改了检索/提示词，`as_of` 视角还成立吗？
8. 文档（`docs/api.md` / `README.md`）里对应的描述同步了吗？

---

## 8. 明确不做的事

| 不做 | 为什么 |
|---|---|
| 引入 ORM（SQLAlchemy） | 12 张表、纯 SQL 可读性更好；换 ORM 会让"这个查询到底发了什么 SQL"变成需要额外知识 |
| 引入前端框架 | 6 个页面、无构建步骤，原生 JS 足够；引入框架后"打开文件就能看"的优势消失 |
| 引入 Alembic 迁移 | 只加列不改列，`_MIGRATION_SQL` 逐条 try 已足够；迁移框架的收益在多人协作的中长期 |
| 用 Redis 做缓存 | 单机课程项目，进程内 dict + 文件缓存够用；多实例部署时才需要 |
| 把 LLM 调用包成重试队列 | 降级路径已经保证"没有 LLM 也有引用可看"，加重试只会让响应更慢 |
