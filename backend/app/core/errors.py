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


class CompanyIdentityMismatch(DomainError):
    """P3.3 「名称+代码」组合查询：名称解析与代码解析指向不同公司（或任一侧
    不唯一），或名称解析结果与所给证券代码不一致 → 无法确认同一身份。"""

    code = "company_identity_mismatch"
    http_status = 409
    message = "公司名称与证券代码不匹配"


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
    message = "任务未提供核心研究问题，无法启动研究"


class ResearchExecutionRequiresSingleQuestion(DomainError):
    """任务提供多个研究问题时不能启动真实研究（Stage 6A 不实现 multi-question 编排）。

    execute 只消费 task.questions[0]；若存在多个 question，静默忽略会丢失
    用户意图 → 明确 422 拒绝，要求调用方收敛为单个问题。
    """

    code = "research_execution_requires_single_question"
    http_status = 422
    message = "一次研究任务需要且仅支持一个核心研究问题"


class WorkflowActionInvalid(DomainError):
    """该 action 对当前 run 的图/状态不合法（graph_name 不匹配等）。"""

    code = "workflow_action_invalid"
    http_status = 409
    message = "该操作对当前工作流运行不适用"


class TaskArtifactIntegrityError(DomainError):
    """任务产物完整性校验失败（Stage 6B.1 spec D）。

    锚定 checkpoint 引用了某个 artifact ID（synthesis / report / check / audit /
    review action / human review / research backflow），但对应
    `verify_*_integrity` 无法完整重建（行缺失 / 指纹不一致 / 上游损坏）。**不
    repair / 不降级为空**——读路径必须暴露损坏，而不是静默返回空。HTTP 409，
    统一 `{error:{code,message,request_id}}` 信封，不泄漏 SQL / stack / 原始异常。
    """

    code = "task_artifact_integrity"
    http_status = 409
    message = "任务产物完整性校验失败"


class CitationNotFound(DomainError):
    """Citation 目标不存在 / 不属于当前任务（Stage 6B.2 spec J）。

    Evidence / Claim 必须属于该 task 的 canonical lineage scope；不属于 → 404。
    **不要通过响应暴露「这个 UUID 在别的 task 存在」**——与本任务内不存在返回
    完全相同（不区分 UUID 存在性，统一 404）。
    """

    code = "citation_not_found"
    http_status = 404
    message = "引用不存在"
