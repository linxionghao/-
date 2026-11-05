# 部署与运行

> 本项目的"部署"有两个场景：**自己机器上跑通**（开发/答辩）和**同一局域网里让手机打开**（现场演示）。
> 生产级部署不在课程范围内，第 4 节列出了真正上生产前必须补的六件事。

---

## 1. 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | 3.9+（CI 覆盖 3.9 / 3.11） | `pyproject.toml` 的 `requires-python` |
| 依赖 | `pip install -r requirements.txt` | 必需依赖只有 8 个 |
| 可选 | `faiss-cpu`、`jieba`、`playwright` | 缺失自动降级，不影响启动 |
| 磁盘 | 约 50MB（含演示数据与索引） | SQLite 单文件 + 切片 JSON |
| 网络 | **不要求** | 无 API Key 时全链路可跑（检索正常、生成降级） |

---

## 2. 本地跑通（三步）

```bash
# 1) 装依赖
python -m pip install -r requirements.txt

# 2) 造演示数据（幂等，可反复执行）
python scripts/seed_demo_data.py --kb --audit

# 3) 起服务
bash scripts/run_server.sh          # Windows 用 scripts/run_server.ps1
```

打开：

| 地址 | 谁用 | 演示账号（密码 = 用户名） |
|---|---|---|
| http://127.0.0.1:8000/ | 质量工程师问答 | `qe001` |
| http://127.0.0.1:8000/admin/login | 质量主管后台 | `manager01` |
| http://127.0.0.1:8000/m | 班组长现场录入 | `leader01` |

最快的自检（不需要起服务）：

```bash
python scripts/demo_closed_loop.py
```

它在临时库上跑完"认证 → 检索 → 版本闸 → 录入 → 确认 → 归因 → 审批 → 审计"共 15 项断言，跑完即删，不污染演示数据。

---

## 3. 局域网演示（手机打开录入端）

班组长端是移动优先设计，用手机演示比缩窗口更有说服力。

```bash
# 1) 查本机内网 IP（Windows）
ipconfig | findstr IPv4
#   IPv4 地址 . . . . . . . . . . . . : 192.168.1.23

# 2) 让服务监听所有网卡（默认只监听 127.0.0.1）
python -m uvicorn quality_trace.api.app:create_app --factory \
    --host 0.0.0.0 --port 8000
```

手机浏览器打开 `http://192.168.1.23:8000/m`，用 `leader01 / leader01` 登录。

三个常见卡点：

| 现象 | 原因 | 解决 |
|---|---|---|
| 手机连不上 | 服务只监听 `127.0.0.1` | 按上面用 `--host 0.0.0.0` |
| 连得上但打不开 | Windows 防火墙拦了 8000 | 允许 Python 通过专用网络，或临时关防火墙 |
| 手机和电脑不在同一网段 | 手机走 4G/5G、电脑连公司 Wi-Fi | 手机连同一个 Wi-Fi（关流量） |

注意：`--host 0.0.0.0` 会把服务暴露给同网段所有人，且演示令牌密钥是公开的 `quality-trace-demo-secret`。**只在可信局域网、只在演示期间开。**

---

## 4. 上生产前必须补的六件事

课程项目明确不做，但要知道差在哪：

| # | 事项 | 现状 | 需要的动作 |
|---|---|---|---|
| 1 | 关掉演示旁路 | `auth.dev_header_bypass: true` | 改 false，否则任何人加 `X-Role: quality_manager` 就是主管 |
| 2 | 换 JWT 密钥 | 默认 `quality-trace-demo-secret` | 用 `QT_AUTH__SECRET` 注入随机值，并做轮换方案 |
| 3 | HTTPS | 全程 HTTP | 反代（Nginx/Caddy）终止 TLS；Cookie 加 `Secure` |
| 4 | 多进程/多实例 | 单进程 uvicorn | SQLite 换 PostgreSQL；`--workers` 会让每个进程各持一个 SQLite 连接，WAL 下写并发会锁 |
| 5 | 文件存储 | 本地 `data/uploads/` | 换对象存储，或至少加定期备份 |
| 6 | 审计与留存 | 表里有数据，无归档策略 | 按质量体系要求（通常 3 年）做归档与清理 |

另外两条容易被忽略：

- **口令策略**：演示账号密码 = 用户名，生产必须走企业账号（LDAP/OAuth）或至少强制复杂度 + 首次登录改密；
- **限流**：`chat.rate_limit_per_min` 只有单会话维度，生产需要按用户/IP 维度。

---

## 5. 配置说明

配置优先级（高 → 低）：

```
环境变量 QT_<SECTION>__<KEY>   >   config/local.yaml（不入库）   >   config/llm.yaml / default.yaml
```

例：`QT_LLM__MODEL=gpt-4o-mini`、`QT_AUTH__SECRET=xxx`、`QT_EMBEDDING__ENABLED=true`。

最常改的几项：

| 配置 | 默认 | 什么时候改 |
|---|---|---|
| `llm.base_url` / `llm.model` | 示例地址 / `demo-model` | 接真实供应商 |
| `llm.api_key_env` | `QT_LLM_API_KEY` | Key 放环境变量，**不要写进 yaml** |
| `embedding.enabled` | `false` | 有向量服务时打开（打开但调不通 = 50001 降级） |
| `chat.db_path` | `data/trace.db` | 换库位置 |
| `defect.upload_dir` | `data/uploads` | 换照片目录（相对路径按仓库根解析） |
| `auth.dev_header_bypass` | `true` | **上线前必须 false** |
| `auth.token_ttl_s` | 43200（12h） | 安全要求更严时缩短 |

`config/local.yaml` 已在 `.gitignore` 里——本地密钥、临时调试开关都放这里，不会被误提交。

---

## 6. 目录与运行时产物

```
data/
├── trace.db                 # SQLite（WAL，附带 -wal / -shm 两个临时文件）
├── uploads/
│   ├── tmp/                 # 预上传暂存的照片（32 位 hex token 命名）
│   └── photos/YYYYMM/       # 正式照片，按缺陷单号命名
├── phase1_index/
│   ├── chunks.json          # 切片（入库，随仓库一起走，便于零依赖启动）
│   └── *.faiss / *.npy      # 向量索引（运行时生成，不入库）
└── knowledge/*.md           # 示例语料
```

`.gitignore` 已排除 `trace.db*`、`uploads/`、索引二进制；`chunks.json` 特意保留入库——没有它，新机器上"检索 → 回答"这条链跑不起来。

---

## 7. 排错速查

| 现象 | 最可能的原因 | 怎么确认 / 怎么修 |
|---|---|---|
| `ModuleNotFoundError: quality_trace` | 直接 `python -m uvicorn` 没带 `PYTHONPATH=src` | `PYTHONPATH=src python -m uvicorn …`，或 `pip install -e .` |
| 页面能开，接口全 401 | 没登录 / token 过期 | 重新登录；`GET /api/v1/auth/me` 看 `code`（40101 无效、40103 过期） |
| 问答只有引用没有正文 | 没配 LLM Key（**设计如此**） | 看 `done` 事件里 `degraded.code = 50002`；配 `QT_LLM_API_KEY` 后恢复 |
| 检索一条都命中不了 | 索引文件缺失，或 `as_of` 落在所有生效区间之外 | 看 `data/phase1_index/chunks.json` 是否存在；换 `as_of` 试 |
| 上传文档 40903 | 同文档同版本已存在 | 后台先下架旧版本，或改版本号 |
| 班组长看不到自己的单 | 账号 `line_id` 与工单产线不一致 | 查 `sys_user.line_id` 与 `work_order.line_id` |
| 班组长所有接口 40302 | 账号没绑产线（**默认拒绝**，不是漏洞） | 给该账号补 `line_id` |
| 照片上传 40003 | 非 JPEG/PNG，或超过 2MB | 前端已压缩到 1280 长边/JPEG 0.7，仍失败就查原图格式 |
| 手机打不开 `/m` | 服务只监听 127.0.0.1 | 见第 3 节 `--host 0.0.0.0` |
| 后台登录后仍跳登录页 | Cookie 未写入（浏览器禁 Cookie / 跨域） | 用 `127.0.0.1` 而非 `localhost` 混用；检查 `qt_token` |
| `database is locked` | 多进程同时写 SQLite | 单进程运行；或换 PostgreSQL |
| 端口被占 | 上次的服务没退 | `netstat -ano | findstr :8000` 找到 PID 后 `taskkill /PID <pid> /F` |
