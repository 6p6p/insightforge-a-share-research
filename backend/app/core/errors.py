"""Domain errors mapped to stable API error responses."""


class DomainError(Exception):
    code: str = "domain_error"
    http_status: int = 500
    message: str = "domain error"


class TaskNotFound(DomainError):
    code = "task_not_found"
    http_status = 404
    message = "研究任务不存在"


class IdempotencyConflict(DomainError):
    code = "idempotency_conflict"
    http_status = 409
    message = "幂等键已用于不同的请求内容"


class InvalidIdempotencyKey(DomainError):
    code = "invalid_idempotency_key"
    http_status = 400
    message = "Idempotency-Key 格式非法"


class WorkflowRunNotFound(DomainError):
    code = "workflow_run_not_found"
    http_status = 404
    message = "工作流运行不存在"


class ActiveWorkflowRunExists(DomainError):
    code = "active_workflow_run_exists"
    http_status = 409
    message = "该任务已存在进行中的工作流运行"


class WorkflowRunAlreadyFinished(DomainError):
    code = "workflow_run_already_finished"
    http_status = 409
    message = "工作流运行已结束，不能重复执行"


class WorkflowRunAlreadyStarted(DomainError):
    code = "workflow_run_already_started"
    http_status = 409
    message = "工作流运行已经开始执行"


class InvalidLastEventId(DomainError):
    code = "invalid_last_event_id"
    http_status = 400
    message = "Last-Event-ID 必须是大于等于 0 的整数"


class InvalidCompanyQuery(DomainError):
    code = "invalid_company_query"
    http_status = 400
    message = "公司查询格式非法"


class CompanyIdentityNotFound(DomainError):
    code = "company_identity_not_found"
    http_status = 404
    message = "未找到匹配的公司身份"


class CompanyIdentityAmbiguous(DomainError):
    code = "company_identity_ambiguous"
    http_status = 409
    message = "公司查询存在多个匹配"


class SourceProviderNotFound(DomainError):
    code = "source_provider_not_found"
    http_status = 404
    message = "来源 Provider 不存在"


class SourceUrlNotAllowed(DomainError):
    code = "source_url_not_allowed"
    http_status = 400
    message = "来源 URL 不在允许域名范围内"


class RawArtifactNotFound(DomainError):
    code = "raw_artifact_not_found"
    http_status = 404
    message = "原始文件不存在"


class SourceRecordNotFound(DomainError):
    code = "source_record_not_found"
    http_status = 404
    message = "来源记录不存在"


class SourceFileTooLarge(DomainError):
    code = "source_file_too_large"
    http_status = 413
    message = "文件大小超过限制"


class InvalidPdfFile(DomainError):
    code = "invalid_pdf_file"
    http_status = 400
    message = "不是有效的 PDF 文件"


class InvalidJsonFile(DomainError):
    code = "invalid_json_file"
    http_status = 400
    message = "不是有效的 JSON 文件"


class InvalidHtmlFile(DomainError):
    code = "invalid_html_file"
    http_status = 400
    message = "不是有效的 HTML 文件"


class SourceProviderDisabled(DomainError):
    code = "source_provider_disabled"
    http_status = 409
    message = "来源 Provider 未启用"


class SourceCapabilityNotAllowed(DomainError):
    code = "source_capability_not_allowed"
    http_status = 400
    message = "来源 Provider 不支持公司文件能力"


class SourceDownloadFailed(DomainError):
    code = "source_download_failed"
    http_status = 502
    message = "来源下载失败"


class SourceRedirectNotAllowed(DomainError):
    code = "source_redirect_not_allowed"
    http_status = 400
    message = "来源重定向不符合安全策略"


class SourceStorageUnavailable(DomainError):
    code = "source_storage_unavailable"
    http_status = 503
    message = "原始文件存储不可用"


class SourceContentUnsupportedMediaType(DomainError):
    """内容下载端点只支持 PDF；HTML/JSON 等媒体类型一律 415 拒绝。

    news_article 的 raw HTML 归档不可通过本端点下载（2D.2A §二十一），
    后续阶段如需浏览器查看 HTML 应新增专用端点而非放开本端点。
    """

    code = "source_content_unsupported_media_type"
    http_status = 415
    message = "该来源媒体类型不支持内容下载"


class NewsArticleIngestionNotAllowed(DomainError):
    """news_article 不能通过 upload / import-url 注入。

    新闻来源记录只能由 NewsOriginalSourceService 走原创发布者验证链路创建
    （acquisition_method=public_html）；上传/导入边界仅服务公司文件类
    document_type（2D.2A §二十）。
    """

    code = "news_article_ingestion_not_allowed"
    http_status = 400
    message = "news_article 只能通过原创发布者验证流程创建"


class MissingResearchQuestion(DomainError):
    """启动真实研究时任务未提供研究问题（questions 为空）。

    Stage 6A 以 ResearchTask 为研究问题来源：execute 请求只携带 work plan，
    research_question 派生自 task.questions[0]；为空时不能假装自动生成。
    """

    code = "missing_research_question"
    http_status = 422
    message = "任务未提供研究问题（questions 为空），无法启动真实研究"


class WorkflowActionInvalid(DomainError):
    """该 action 对当前 run 的图/状态不合法（graph_name 不匹配等）。"""

    code = "workflow_action_invalid"
    http_status = 409
    message = "该操作对当前工作流运行不适用"
