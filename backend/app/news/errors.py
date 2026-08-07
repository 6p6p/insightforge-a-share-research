"""Stable error taxonomy for news discovery (stage 2D.1).

错误消息不包含：完整 query_text、raw response 正文、DB URL、absolute path、
完整 candidate URL query。日志允许 engine / hostname / result_count / error_type。
"""


class NewsDiscoveryError(Exception):
    """News Discovery 稳定错误基类。"""

    code = "news_discovery_error"
    message = "news discovery error"

    def __init__(self, message: str | None = None) -> None:
        # 未传 message 时使用类级默认（稳定默认 message），str() 即返回该值；
        # 传了 message 时保留既有按位置传参的调用语义不变。
        super().__init__(message if message is not None else self.message)


class NewsDiscoveryInvalidQuery(NewsDiscoveryError):
    """NewsDiscoveryQuery 契约校验失败（空/超长 query、非法时间窗、max_results 越界等）。"""

    code = "news_discovery_invalid_query"


class NewsDiscoveryArtifactConflict(NewsDiscoveryError):
    """RawArtifact 引用冲突：既有 artifact 与期望的 JSON 内容/元数据不一致。

    默认 message 稳定且不含敏感信息（无 SHA 完整值 / storage absolute path /
    raw JSON / DB URL / query_text）。Service 发现冲突时直接 raise 本类
    （不传参），以保证调用方拿到的是这个稳定默认 message。
    """

    code = "news_discovery_artifact_conflict"
    message = "news discovery raw artifact metadata conflict"


class NewsDiscoveryIntegrityError(NewsDiscoveryError):
    """已存在 Discovery Run replay 时完整性检查失败（candidate 数与归档 result 数不一致等）。"""

    code = "news_discovery_integrity_error"


class NewsDiscoveryPersistenceFailed(NewsDiscoveryError):
    """持久化事务失败（DB 层错误，部分/整体回滚）。"""

    code = "news_discovery_persistence_failed"


class NewsCandidateNotFound(NewsDiscoveryError):
    """verify_candidate 时 Candidate 不存在（已删除或从未创建）。"""

    code = "news_candidate_not_found"


class NewsPublisherUnsupported(NewsDiscoveryError):
    """原始发布者解析失败：URL 非 https / 带 userinfo / 非默认端口 /
    IP 字面量 host / 不在 eligible 发布者 allowlist 内 / 无可匹配 Provider。"""

    code = "news_publisher_unsupported"


class NewsPublisherAmbiguous(NewsDiscoveryError):
    """normalized_url 同时命中多个 eligible Original Publisher，不自动挑选。"""

    code = "news_publisher_ambiguous"


class NewsOriginalFetchFailed(NewsDiscoveryError):
    """原文安全获取失败：DNS 解析/SSRF 预检、连接、超时、重定向违规、
    非 2xx、无正文等传输层失败。消息不含完整 URL / body。"""

    code = "news_original_fetch_failed"


class NewsOriginalContentRejected(NewsDiscoveryError):
    """原文内容被拒绝：Content-Type 非 text/html、超过 5 MiB 上限。"""

    code = "news_original_content_rejected"


class NewsOriginalArtifactConflict(NewsDiscoveryError):
    """RawArtifact 引用冲突：既有一行与本次 HTML 落盘描述不一致
    （media_type 非 text/html 或 content_sha256/byte_size/storage_key 不匹配）。

    默认 message 稳定且不含敏感信息（无 SHA 完整值 / storage absolute path /
    HTML body / DB URL / 完整 URL）。Service 冲突时直接 raise 本类（不传参）。
    """

    code = "news_original_artifact_conflict"
    message = "news original source raw artifact metadata conflict"


class NewsOriginalSourceIntegrityError(NewsDiscoveryError):
    """原来源完整性检查失败：normalized_url 重算 hostname 与 candidate.domain
    不一致、或 replay 时已存在 Verification 的元数据被篡改。"""

    code = "news_original_source_integrity_error"


class NewsOriginalPersistenceFailed(NewsDiscoveryError):
    """原来源持久化事务失败（DB 层错误，部分/整体回滚）。"""

    code = "news_original_persistence_failed"
