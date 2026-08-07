"""GDELT DOC 2.0 client/parser errors (stage 2D.1).

错误消息遵循新闻发现统一脱敏规则：不含完整 query_text、不含 raw response
正文、不含完整请求 URL query、不含 DB URL / absolute path。
"""


class GdeltDiscoveryError(Exception):
    """GDELT discovery 稳定错误基类。"""

    code = "gdelt_discovery_error"


class GdeltRequestFailed(GdeltDiscoveryError):
    """HTTP 请求失败：连接错误、超时、redirect 违规、非 2xx、429/5xx。"""

    code = "gdelt_request_failed"


class GdeltResponseTooLarge(GdeltDiscoveryError):
    """响应体超过 5 MiB 上限。"""

    code = "gdelt_response_too_large"


class GdeltInvalidContentType(GdeltDiscoveryError):
    """响应 Content-Type 不是 application/json。"""

    code = "gdelt_invalid_content_type"


class GdeltInvalidJson(GdeltDiscoveryError):
    """响应不是合法 JSON，或包含 NaN/Infinity。"""

    code = "gdelt_invalid_json"


class GdeltMalformedResponse(GdeltDiscoveryError):
    """JSON 合法但结构不符合 DOC 2.0 artlist 契约。"""

    code = "gdelt_malformed_response"
