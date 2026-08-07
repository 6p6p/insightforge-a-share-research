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
