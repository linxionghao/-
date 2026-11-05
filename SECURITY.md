# 安全说明

本项目是创新创业训练的课程作业，默认以**本机演示**方式运行。下面写清"哪些设计是认真做的"和"哪些是演示妥协"，避免被误当成可上线的安全实现。

## 1. 已经做的（可核对）

| 面 | 措施 | 代码位置 |
|---|---|---|
| 口令存储 | PBKDF2 加盐哈希，不存明文 | `core/services/auth.py` |
| 令牌 | 手写 HS256；签名比较用常数时间；算法在校验前固定（不信任 header 里的 `alg`） | `core/services/auth.py` |
| 登录失败 | 用户名不存在与密码错误返回**同一个码** 40102，不透出差异 | `core/services/auth.py` |
| 令牌过期 | 与无效分开（40103 / 40101），便于前端提示重新登录 | `core/services/auth.py` |
| 越权 | 路由级角色白名单（40301）+ SQL 层数据过滤 + 响应字段白名单，三层独立 | `api/deps.py`、`core/repo/db.py`、`core/services/defects.py` |
| 班组长未绑产线 | 一律 40302 **拒绝**，不降级为全量可见 | `core/services/defects.py::line_scope` |
| 越权探测 | 跨产线取单返回 404（不是 403），不让单号成为枚举器 | `core/services/defects.py` |
| 会话归属 | 读取/写入/删除会话都校验 `user_id`，他人会话按"不存在"处理 | `api/routers/chat.py`、`core/services/chat.py` |
| 上传校验 | 服务端按魔数判断 JPEG/PNG（不信任扩展名与 `Content-Type`），2MB 上限 | `core/services/photo.py` |
| 路径穿越 | 预上传 token 严格匹配 `^[0-9a-f]{32}$`，文件名由服务端生成 | `core/services/photo.py::resolve_temp` |
| SQL 注入 | 全部查询参数化；唯一拼接是 `WHERE` 片段，值仍走参数 | `core/repo/db.py` |
| XSS | 前端渲染用户内容用 `textContent`；SSR 模板由 Jinja2 默认转义 | `web/*.html`、`web/admin/*.html` |
| CSRF | 后台 Cookie 设 `HttpOnly` + `SameSite=Lax` | `api/routers/admin_pages.py` |
| 密钥不入库 | API Key 只从环境变量读；`config/local.yaml` 在 `.gitignore` 里 | `core/llm/client.py`、`.gitignore` |
| 越界输出 | 三条审计规则把"无引用却称完成""引用废止版本""直接下根因结论"标记出来 | `core/services/audit.py` |

## 2. 演示妥协（上线前必须改）

| 项 | 现状 | 风险 | 改法 |
|---|---|---|---|
| `auth.dev_header_bypass` | **默认 true** | 任何人加 `X-Role: quality_manager` 即获得主管权限 | 置 false（`docs/deployment.md` 第 4 节第 1 条） |
| JWT 密钥 | `quality-trace-demo-secret` 明文写在 `config/llm.yaml` | 可伪造任意角色的令牌 | 用 `QT_AUTH__SECRET` 注入随机值并定期轮换 |
| 传输 | 全程 HTTP | 令牌与密码可被同网段嗅探 | 反代终止 TLS；Cookie 加 `Secure` |
| 演示口令 | 密码 = 用户名 | 撞库即入 | 接企业账号或强制改密 |
| 限流 | 仅单会话维度 | 可被刷 | 按用户/IP 加限流 |
| 多实例 | 单进程 SQLite | `--workers` 下写并发锁 | 换 PostgreSQL |
| 审计留存 | 无归档策略 | 不满足质量体系留存要求 | 定期归档 + 只读存储 |

## 3. 已知的、不打算在课程范围内修的问题

- **SSE 端点没有独立的 CSRF token**：`POST /chat` 走 `Authorization` 头（非 Cookie），浏览器不会自动携带，因此不受传统 CSRF 影响；但后台页面用 Cookie，其写操作接口只接受 JSON（`Content-Type: application/json`），跨站表单无法伪造。
- **照片 EXIF 未剥离**：手机原图可能带 GPS。前端压缩后通常已丢失 EXIF，但服务端未做二次剥离。
- **无审计日志文件**：操作痕迹落在数据库表里（`reviewed_by`、`decided_by` 等），没有独立的 append-only 日志。
- **依赖未做供应链校验**：没有 lock 文件与哈希校验，`pip install` 直接取最新兼容版本。

## 4. 报告问题

课程作业不设漏洞奖励。若在评审中发现安全问题，直接在仓库开 issue 或在答辩时指出即可——**特别是第 2 节里"演示妥协"之外的**，那说明设计本身有洞。
