"""Stable error taxonomy for news discovery (stage 2D.1).

错误消息不包含：完整 query_text、raw response 正文、DB URL、absolute path、
完整 candidate URL query。日志允许 engine / hostname / result_count / error_type。
"""


class NewsDiscoveryError(Exception):
    """News Discovery 稳定错误基类。"""

    code = "news_discovery_error"


class NewsDiscoveryInvalidQuery(NewsDiscoveryError):
    """NewsDiscoveryQuery 契约校验失败（空/超长 query、非法时间窗、max_results 越界等）。"""

    code = "news_discovery_invalid_query"


class NewsDiscoveryArtifactConflict(NewsDiscoveryError):
    """RawArtifact 引用冲突：既有 artifact 与期望的 JSON 内容/元数据不一致。"""

    code = "news_discovery_artifact_conflict"


class NewsDiscoveryIntegrityError(NewsDiscoveryError):
    """已存在 Discovery Run replay 时完整性检查失败（candidate 数与归档 result 数不一致等）。"""

    code = "news_discovery_integrity_error"


class NewsDiscoveryPersistenceFailed(NewsDiscoveryError):
    """持久化事务失败（DB 层错误，部分/整体回滚）。"""

    code = "news_discovery_persistence_failed"
