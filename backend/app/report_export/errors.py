"""Report export domain errors (stage 6C).

导出是 immutable research artifact：失败路径全部走稳定 DomainError 信封，
不泄漏 SQL / stack / 原始异常 / 绝对存储路径。
"""

from app.core.errors import DomainError


class ReportExportError(DomainError):
    """导出领域错误基类（http_status 默认 409 由子类覆盖）。"""

    code = "report_export_error"
    http_status = 409
    message = "报告导出失败"


class ReportNotExportable(ReportExportError):
    """当前报告尚未达到可导出状态（spec H：running / waiting_human /
    rewrite / research / cancelled / failed / check fail / audit rewrite /
    research 都不可导出；只有 finalize 路径可导出）。"""

    code = "report_not_exportable"
    http_status = 409
    message = "当前报告尚未达到可导出状态"


class ReportExportNotFound(ReportExportError):
    """指定 export_id 不存在或不属于该 task（task-scoped 404）。"""

    code = "report_export_not_found"
    http_status = 404
    message = "报告导出不存在"


class ReportExportIntegrityError(ReportExportError):
    """导出完整性校验失败（spec N）：引用产物 / 指纹 / 归档字节任一不一致。

    只验证、不 repair——下载前必须校验通过；损坏 → 409，不静默降级。
    """

    code = "report_export_integrity"
    http_status = 409
    message = "报告导出完整性校验失败"


class ExportStorageUnavailable(ReportExportError):
    """导出字节存储不可用（不可写 / 探测失败）。"""

    code = "export_storage_unavailable"
    http_status = 503
    message = "导出存储不可用"


class ExportArtifactNotFound(ReportExportError):
    """存储中的归档字节缺失（storage_key 不存在或超出根目录）。"""

    code = "export_artifact_not_found"
    http_status = 404
    message = "导出字节不存在"
