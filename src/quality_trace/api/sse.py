"""SSE 事件格式化（Server-Sent Events）。

报文格式：
    event: delta
    data: {"text": "…"}

    每行以 \n 结尾，事件之间用空行分隔；`: ping` 为心跳注释行，
    防止反向代理/浏览器因空闲掐断长连接。
"""

from __future__ import annotations

import json
from typing import Dict

# 心跳间隔（秒）：由前端/网关读取，后端每 15 秒发一次注释行
HEARTBEAT_SECONDS = 15


def format_sse(event: str, data: Dict) -> str:
    """把一个事件格式化为 SSE 报文段（data 以 JSON 单行输出，避免换行破坏协议）。"""
    payload = json.dumps(data, ensure_ascii=False).replace("\n", "\\n")
    return f"event: {event}\ndata: {payload}\n\n"


def heartbeat() -> str:
    """心跳注释行（客户端忽略，仅用于保活）。"""
    return ": ping\n\n"
