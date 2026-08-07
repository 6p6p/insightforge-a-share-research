"""Stable error taxonomy for macro capture and persistence (stage 2C.2B).

错误消息不包含：raw JSON body、storage 绝对路径、DB URL、完整 URL/query、
Provider allowed_domains 全集。日志允许 provider_key / series identity /
snapshot fingerprint 前 12 位 / role / page / error_type。
"""


class MacroPersistenceError(Exception):
    """Macro 捕获/持久化稳定错误基类。"""

    code = "macro_persistence_error"


class MacroCaptureInvalid(MacroPersistenceError):
    """CapturedMacroFetch 完整性校验失败（缺页、重复、hostname/content-type 不符等）。"""

    code = "macro_capture_invalid"


class MacroArtifactConflict(MacroPersistenceError):
    """RawArtifact 引用冲突：既有 artifact 与期望的 JSON 内容/元数据不一致。"""

    code = "macro_artifact_conflict"


class MacroSnapshotIntegrityError(MacroPersistenceError):
    """已存在 Snapshot replay 时完整性检查失败（series/fingerprint/version/link 数/观测数）。"""

    code = "macro_snapshot_integrity_error"


class MacroPersistenceFailed(MacroPersistenceError):
    """持久化事务失败（DB 层错误，部分/整体回滚）。"""

    code = "macro_persistence_failed"
