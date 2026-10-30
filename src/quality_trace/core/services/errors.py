"""统一错误码与 ApiError（docs/chat-api.md 第 1 节的实现）。

约定：
- code=0 成功；非零为失败，message 恒为中文，可直接展示给用户；
- 降级类（50001/50002/50401）不算失败：HTTP 仍为 200，降级信息放 data.degraded；
- core 约束：本模块不 print、不碰 HTTP，只负责错误语义。
"""

from __future__ import annotations

from typing import Dict, Optional


class ErrorCode:
    """错误码常量（5 位，前两位沿用 HTTP 语义便于定位）。"""

    OK = 0

    BAD_REQUEST = 40001        # 请求参数缺失或格式错误
    QUESTION_TOO_LONG = 40002  # 问题长度超限
    PHOTO_INVALID = 40003      # 图片解码失败或超过 2MB

    UNAUTHORIZED = 40101       # 未登录或身份无效
    LOGIN_FAILED = 40102       # 登录名或密码错误
    TOKEN_EXPIRED = 40103      # 登录已过期，请重新登录
    FORBIDDEN = 40301          # 角色无权（通用）
    LINE_FORBIDDEN = 40302     # 数据级越权：非本人产线

    SESSION_NOT_FOUND = 40401  # 会话不存在
    INDEX_NOT_FOUND = 40402    # 知识库索引不存在
    DOC_NOT_FOUND = 40403      # 知识库文档不存在
    DEFECT_NOT_FOUND = 40404   # 缺陷单不存在
    ATTR_NOT_FOUND = 40405     # 归因单不存在
    WORK_ORDER_NOT_FOUND = 40406  # 工单不存在或不属于当前产线

    SESSION_ARCHIVED = 40901   # 会话已归档
    STREAM_BUSY = 40902        # 上一条仍在生成
    DOC_VERSION_EXISTS = 40903 # 文档版本号冲突
    DEFECT_STATE_INVALID = 40905  # 缺陷单状态不允许此操作
    ATTR_STATE_INVALID = 40906   # 归因单状态不允许此操作

    FILE_TOO_LARGE = 41301     # 上传文件超过大小限制
    FILE_FORMAT = 41501        # 仅支持 Markdown 或 PDF

    RATE_LIMITED = 42901       # 请求过于频繁

    EMBED_DEGRADED = 50001     # 向量服务降级（非失败）
    LLM_DEGRADED = 50002       # 大模型不可用（非失败）
    INTERNAL_ERROR = 50003     # 服务内部错误
    EMBED_PENDING = 50004      # 向量化暂缺（非失败）：文档已入库但仅关键词检索
    STREAM_TIMEOUT = 50401     # 响应超时，回答截断（非失败）
    INDEX_UNAVAILABLE = 50301  # 索引加载失败


# ---- 中文文案表：保证同一错误码在任何接口文案一致 ----
MESSAGES: Dict[int, str] = {
    ErrorCode.OK: "成功",
    ErrorCode.BAD_REQUEST: "请求参数缺失或格式错误",
    ErrorCode.QUESTION_TOO_LONG: "问题长度超出限制（最多 1000 字）",
    ErrorCode.PHOTO_INVALID: "图片解码失败或超过 2MB",
    ErrorCode.UNAUTHORIZED: "未登录或身份无效",
    ErrorCode.LOGIN_FAILED: "登录名或密码错误",
    ErrorCode.TOKEN_EXPIRED: "登录已过期，请重新登录",
    ErrorCode.FORBIDDEN: "当前角色无权执行此操作",
    ErrorCode.LINE_FORBIDDEN: "无权访问其他产线的数据",
    ErrorCode.SESSION_NOT_FOUND: "会话不存在或已被删除",
    ErrorCode.INDEX_NOT_FOUND: "知识库索引不存在，请先执行入库",
    ErrorCode.DOC_NOT_FOUND: "文档不存在或已删除",
    ErrorCode.DEFECT_NOT_FOUND: "缺陷单不存在",
    ErrorCode.ATTR_NOT_FOUND: "归因单不存在",
    ErrorCode.WORK_ORDER_NOT_FOUND: "工单不存在或不属于当前产线",
    ErrorCode.SESSION_ARCHIVED: "会话已归档，无法继续提问",
    ErrorCode.STREAM_BUSY: "上一条回答仍在生成中，请稍候",
    ErrorCode.DOC_VERSION_EXISTS: "该文档的此版本号已存在",
    ErrorCode.DEFECT_STATE_INVALID: "缺陷单当前状态不允许此操作",
    ErrorCode.ATTR_STATE_INVALID: "当前仅允许审批未处理的归因单",
    ErrorCode.FILE_TOO_LARGE: "文件超过大小限制（10MB）",
    ErrorCode.FILE_FORMAT: "仅支持 Markdown 或 PDF 格式",
    ErrorCode.RATE_LIMITED: "请求过于频繁，请稍后再试",
    ErrorCode.EMBED_DEGRADED: "向量服务不可用，已切换关键词检索",
    ErrorCode.LLM_DEGRADED: "大模型不可用，仅返回检索依据",
    ErrorCode.INTERNAL_ERROR: "服务内部错误，请稍后重试",
    ErrorCode.EMBED_PENDING: "向量服务不可用，文档已入库但仅支持关键词检索",
    ErrorCode.STREAM_TIMEOUT: "大模型响应超时，回答可能不完整",
    ErrorCode.INDEX_UNAVAILABLE: "检索索引加载失败，请联系管理员",
}


class ApiError(Exception):
    """业务异常：携带错误码，由入口层转成 HTTP 响应或 SSE error 事件。"""

    def __init__(self, code: int, message: Optional[str] = None):
        self.code = code
        self.message = message or MESSAGES.get(code, "未知错误")
        super().__init__(self.message)
