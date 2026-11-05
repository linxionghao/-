# 0002. 问答流式用 SSE，不用 WebSocket

- 状态：已采纳
- 日期：2026-10-30
- 影响范围：`api/routers/chat.py`、`api/sse.py`、`core/services/chat.py`、`web/chat.html`

## 背景

质量工程师提问后要等模型逐字生成，全量返回会让人盯着空白页 5–15 秒。需要流式。候选是 WebSocket 与 SSE。

数据流是**单向的**：客户端发一次问题，服务端持续推文本。没有"边生成边让客户端插话"的需求。

## 决策

用 SSE（`text/event-stream`），事件序列固定为：

```
status  → "正在检索依据…"        （前端状态条）
sources → 引用列表（先于正文）    （前端先渲染引用卡片）
delta   → 正文增量（多次）
done    → {message_id, degraded, latency_ms, citations}
error   → {code, message}         （流中异常，HTTP 码已发出无法再改）
```

两个实现要点：

1. **`sources` 必须在正文之前发**。如果先发正文再发引用，客户端断线或用户点"停止"后，正文在、引用没了——回答变成一段没有出处的断言，恰好破坏本项目最想保证的可溯源性。
2. **生产者线程 + `queue.Queue` + 异步消费者**。`stream_chat()` 是同步生成器（依赖 `requests`），而 FastAPI 端点是异步的；用线程把它跑起来、把事件塞进队列，异步侧 `await queue.get()` 消费，避免阻塞事件循环。15 秒一次心跳（`: hb\n\n`）防中间层断开空闲连接。

前端用 `fetch` + `ReadableStream` 手工解析 SSE，不用 `EventSource`——因为 `EventSource` 只支持 GET，而问答要 POST 带 body（问题可能很长，且不该出现在 URL 与访问日志里）。

## 后果

好处：

- 协议简单：一个 HTTP 请求，能直接 `curl -N` 调试，能穿过所有代理与浏览器；
- 断线行为明确：HTTP 连接断了就是断了，不需要处理重连后的状态同步；
- 与现有鉴权天然兼容（`Authorization` 头照常用）。

代价：

- 不能双向：将来若要"生成中打断并追问"，SSE 需要改造成 WebSocket 或加一个 `POST /chat/{id}/interrupt` 旁路；
- 部分中间层会缓冲 SSE（需 `X-Accel-Buffering: no`），部署到 Nginx 时要显式关掉；
- 需要自己维护心跳与"流中异常"的表达（HTTP 状态码已经发出，只能靠 `error` 事件）。

未来若要改：`core/services/chat.py` 的 `stream_answer()` 产出的是**事件字典**而非字节流，换传输层只需改 `api/` 的格式化部分。

## 替代方案

| 方案 | 为什么不选 |
|---|---|
| WebSocket | 双向能力用不上；要自己实现鉴权握手、心跳、重连、消息编号，成本与收益不匹配 |
| 一次性返回 + 前端假打字机 | 首字延迟仍然存在（用户等 10 秒才看到"打字"），体验没解决 |
| 长轮询（多次请求拉增量） | 每次请求都要重建上下文，服务端要维护"生成到哪了"的状态，比 SSE 复杂得多 |
| 第三方流式 SDK | 绑定供应商，违背"LLM 可替换"的硬约束 |
