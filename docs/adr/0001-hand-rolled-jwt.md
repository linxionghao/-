# 0001. 手写 HS256 JWT，不引入 PyJWT

- 状态：已采纳
- 日期：2026-10-30
- 影响范围：`core/services/auth.py`、`api/deps.py`、`api/routers/admin_pages.py`

## 背景

阶段 3 要同时支持两个入口的登录：API 客户端（`Authorization: Bearer`）和 SSR 后台页面（HttpOnly Cookie）。需要签发与校验令牌，候选是引入 `PyJWT` 或自己实现。

硬约束是"换台机器能跑"——答辩现场从零装依赖，任何一个装不上的包都可能让演示翻车。

## 决策

用标准库 `hmac` + `base64` + `json` 手写 HS256 签发与校验，约 80 行：

```python
def create_token(payload, secret, ttl_seconds):
    header = {"alg": "HS256", "typ": "JWT"}
    body = {**payload, "iat": now, "exp": now + ttl_seconds}
    signing_input = b64(header) + "." + b64(body)
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + b64(sig)

def decode_token(token, secret):
    # 校验：三段结构 → 签名（常数时间比较）→ exp
    # 失败分别抛 40101（无效）/ 40103（过期）
```

关键实现细节：

- 签名比较用 `hmac.compare_digest`，防时序侧信道；
- `line_id` 写进令牌，成为数据层过滤的唯一权威来源（不信任任何客户端传值）；
- 只允许 `alg=HS256`，**在校验前就固定算法**——这是 JWT 最经典的漏洞（`alg: none` 或 RS256→HS256 混淆）的根因，手写的好处是这条检查不可能被"默认配置"漏掉。

## 后果

好处：

- 零新增依赖，`requirements.txt` 更短，答辩机器上少一个变量；
- 令牌内容一目了然，讲清"为什么 `line_id` 在令牌里"比讲清 PyJWT 的 claim 体系更快；
- 算法固定、校验顺序显式，安全要点在新人读 80 行代码时就能看懂。

代价：

- 不支持 RS256/ES256、JWKS、密钥轮换等企业能力——真上生产要换成成熟库；
- 需要自己处理 `exp` 时区、`iat` 时钟偏移等细节（已按秒级本地时间统一）。

未来若要改：只需替换 `create_token` / `decode_token` 两个函数的实现，调用点（`api/deps.py` 的 `get_current_user`、`admin_pages` 的 Cookie 读取）不变。

## 替代方案

| 方案 | 为什么不选 |
|---|---|
| PyJWT | 多一个依赖；其默认参数历史上多次出现算法混淆问题，需要额外配置才能安全 |
| `itsdangerous`（签名 Cookie） | 面向 Cookie 场景，不适合 API 的 Bearer 令牌；且同样多一个依赖 |
| Session + 服务端会话表 | 需要一张会话表 + 清理任务；课程项目里令牌自带的 `exp` 已够用 |
| 不鉴权（只靠 `X-Role` 头） | 演示能跑，但"权限"就成了装饰品，与需求里的三角色权限矩阵直接冲突 |
