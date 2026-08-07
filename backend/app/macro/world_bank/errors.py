"""Stable error taxonomy for the World Bank macro provider.

错误消息不包含完整 query、响应正文、数据库 URL、allowed_domains 全集；
CLI 根据 error.code 映射退出码（3 = Provider 配置，4 = API/网络/响应）。
"""


class WorldBankError(Exception):
    """World Bank Provider 稳定错误基类。"""

    code = "world_bank_error"


class WorldBankProviderNotReady(WorldBankError):
    """Provider 未就绪：不存在 / 未启用 / 缺能力 / 需要 API Key 等配置问题。"""

    code = "provider_not_ready"


class WorldBankRequestFailed(WorldBankError):
    """请求传输失败：超时、连接错误、重定向违规或重定向超限。"""

    code = "request_failed"


class WorldBankResponseTooLarge(WorldBankError):
    """单响应正文超过 5 MiB 上限，未读取完整正文即拒绝。"""

    code = "response_too_large"


class WorldBankInvalidContentType(WorldBankError):
    """响应 Content-Type 不是 JSON。"""

    code = "invalid_content_type"


class WorldBankInvalidJson(WorldBankError):
    """响应正文 JSON 解析失败。"""

    code = "invalid_json"


class WorldBankApiError(WorldBankError):
    """API 层错误：非 2xx 状态码，或 2xx 但返回错误对象/错误数组。"""

    code = "api_error"


class WorldBankMalformedResponse(WorldBankError):
    """2xx 且 JSON 合法，但结构不符合契约（顶层/元数据/字段/年份越界等）。"""

    code = "malformed_response"


class WorldBankResponseConflict(WorldBankError):
    """跨页合并时同一 period 出现相互冲突的观测值或状态。"""

    code = "response_conflict"


class WorldBankRequestLimitExceeded(WorldBankError):
    """单次 MacroQuery 请求总数超过上限。"""

    code = "request_limit_exceeded"
