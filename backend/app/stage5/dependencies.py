"""Stage 5 report control workflow dependencies (DI container, spec D/O).

`Stage5WorkflowDependencies` 集中持有既有 Services；graph nodes 通过它 dispatch，
**不**在 node 内重新初始化 model factory。

装配链（与既有调用方 / smoke 脚本一致）：
    ReportOutlineService → DraftSectionService
        → ReportService(revision_service) → ReportCheckService → ReportAuditService
        → ReviewActionService → RevisionService(draft, check, review)

**循环依赖**：`ReportService` 装配含修订输出（writer_version=1）的 Report 需要
`RevisionService`（spec N），而 `RevisionService` 又需要 `ReportCheckService` /
`ReviewActionService`（其上游都要 `ReportService`）。断环点选在
`ReportService._revision_service`——该属性本就是可选注入（纯 v2 装配不需要），
构造 `report_service` 后再绑定 `revision_service`，同一次实例同时被 graph 的
report_service 与 check_service 引用（check 验证含修订的 Report 走同一入口）。

model factory 只在 `create_stage5_dependencies` 调用一次（runner 持有）；自动
测试一律用 Fake models + 现有 Services（不访问真实 LLM）。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.audit.service import ReportAuditService
from app.core.config import Settings
from app.draft_section.service import DraftSectionService
from app.report.check_service import ReportCheckService
from app.report.service import ReportService
from app.report_outline.service import ReportOutlineService
from app.review.service import ReviewActionService
from app.revision.service import RevisionService


@dataclass(frozen=True)
class Stage5WorkflowDependencies:
    """一次 Stage 5 报告控制流所需的全部 Application Services。"""

    sessionmaker: async_sessionmaker
    report_outline_service: ReportOutlineService
    draft_section_service: DraftSectionService
    report_service: ReportService
    report_check_service: ReportCheckService
    report_audit_service: ReportAuditService
    review_action_service: ReviewActionService
    revision_service: RevisionService


def create_stage5_dependencies(
    settings: Settings,
    sessionmaker: async_sessionmaker,
) -> Stage5WorkflowDependencies:
    """生产 factory：Settings → 现有 model factories → Services → deps。

    只在 graph 之外构建一次（runner 持有）；node 内不重新初始化 model。
    """
    from app.audit.adapters import DeepSeekAuditModel
    from app.draft_section.factory import create_draft_section_model
    from app.revision.factory import create_revision_writer_model

    outline_service = ReportOutlineService(sessionmaker)
    draft_section_service = DraftSectionService(sessionmaker, create_draft_section_model(settings))
    # report_service 先不带 revision 构造；check→audit→review→revision 链完成后
    # 再绑定（唯一断环点，见模块 docstring）。
    report_service = ReportService(sessionmaker, draft_section_service)
    check_service = ReportCheckService(sessionmaker, report_service)
    audit_service = ReportAuditService(sessionmaker, DeepSeekAuditModel(settings), check_service)
    review_action_service = ReviewActionService(sessionmaker, audit_service)
    revision_service = RevisionService(
        sessionmaker,
        model=create_revision_writer_model(settings),
        draft_section_service=draft_section_service,
        check_service=check_service,
        review_action_service=review_action_service,
    )
    report_service._revision_service = revision_service  # noqa: SLF001 — DI 断环

    return Stage5WorkflowDependencies(
        sessionmaker=sessionmaker,
        report_outline_service=outline_service,
        draft_section_service=draft_section_service,
        report_service=report_service,
        report_check_service=check_service,
        report_audit_service=audit_service,
        review_action_service=review_action_service,
        revision_service=revision_service,
    )
