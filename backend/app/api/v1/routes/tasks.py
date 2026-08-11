"""Research task API endpoints."""

import asyncio
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.api.dependencies import (
    get_report_export_service,
    get_research_execution_service,
    get_task_artifact_service,
    get_task_citation_service,
    get_task_service,
    get_task_workspace_service,
    get_workflow_service,
)
from app.core.errors import InvalidIdempotencyKey
from app.domain.tasks import TaskStatus
from app.report_export.service import ReportExportService
from app.schemas.artifact import (
    AnalysisArtifactResponse,
    EvidenceArtifactListResponse,
    ReportArtifactResponse,
    ReviewsArtifactResponse,
    SourceArtifactListResponse,
)
from app.schemas.citation import ClaimCitationResponse, EvidenceCitationResponse
from app.schemas.export import (
    ExportCreateRequest,
    ExportCreateResponse,
    ExportMetadataResponse,
)
from app.schemas.research_execution import (
    ResearchExecutionRequest,
    TaskWorkspaceResponse,
)
from app.schemas.task import TaskCreateRequest, TaskListResponse, TaskResponse
from app.schemas.workflow import WorkflowRunResponse
from app.services.research_execution_service import ResearchExecutionService
from app.services.sse_service import format_sse_event, parse_last_event_id
from app.services.task_artifact_service import TaskArtifactService
from app.services.task_citation_service import TaskCitationService
from app.services.task_service import TaskService
from app.services.task_workspace_service import TaskWorkspaceService
from app.services.workflow_service import WorkflowService

router = APIRouter(tags=["tasks"], prefix="/tasks")

_MAX_IDEMPOTENCY_KEY_LENGTH = 128

_POLL_INTERVAL_SECONDS = 1.0
_KEEPALIVE_INTERVAL_SECONDS = 15.0
_MAX_EVENTS_PER_POLL = 100


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or len(value) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidIdempotencyKey()
    for char in value:
        if not 32 <= ord(char) <= 126:
            raise InvalidIdempotencyKey()
    return value


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreateRequest,
    response: Response,
    service: Annotated[TaskService, Depends(get_task_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskResponse:
    key = _normalize_idempotency_key(idempotency_key)
    result = await service.create_task(payload, key)
    if result.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    else:
        response.headers["Idempotent-Replayed"] = "false"
    return result.task


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: UUID,
    service: Annotated[TaskService, Depends(get_task_service)],
) -> TaskResponse:
    return await service.get_task(task_id)


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    service: Annotated[TaskService, Depends(get_task_service)],
    status: Annotated[TaskStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TaskListResponse:
    return await service.list_tasks(status=status, limit=limit, offset=offset)


@router.get("/{task_id}/workspace", response_model=TaskWorkspaceResponse)
async def get_task_workspace(
    task_id: UUID,
    service: Annotated[TaskWorkspaceService, Depends(get_task_workspace_service)],
) -> TaskWorkspaceResponse:
    """Task workspace projection（spec E）：task + 解析公司 + 当前 run + 任务级产物计数。"""
    return await service.get_workspace(task_id)


@router.get("/{task_id}/sources", response_model=SourceArtifactListResponse)
async def get_task_sources(
    task_id: UUID,
    service: Annotated[TaskArtifactService, Depends(get_task_artifact_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SourceArtifactListResponse:
    """任务引用的 source 列表（任务级 scoped，从 checkpoint 恢复精确 ID 集）。"""
    return await service.get_sources(task_id, limit=limit, offset=offset)


@router.get("/{task_id}/evidence", response_model=EvidenceArtifactListResponse)
async def get_task_evidence(
    task_id: UUID,
    service: Annotated[TaskArtifactService, Depends(get_task_artifact_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EvidenceArtifactListResponse:
    """任务引用的 evidence card 列表（任务级 scoped，分页）。"""
    return await service.get_evidence(task_id, limit=limit, offset=offset)


@router.get("/{task_id}/analysis", response_model=AnalysisArtifactResponse)
async def get_task_analysis(
    task_id: UUID,
    service: Annotated[TaskArtifactService, Depends(get_task_artifact_service)],
) -> AnalysisArtifactResponse:
    """任务 Stage 4 分析视图：work items + claims + synthesis 摘要。"""
    return await service.get_analysis(task_id)


@router.get("/{task_id}/report", response_model=ReportArtifactResponse)
async def get_task_report(
    task_id: UUID,
    service: Annotated[TaskArtifactService, Depends(get_task_artifact_service)],
) -> ReportArtifactResponse:
    """任务最新报告投影（verify_report_integrity read-side）。"""
    return await service.get_report(task_id)


@router.get("/{task_id}/reviews", response_model=ReviewsArtifactResponse)
async def get_task_reviews(
    task_id: UUID,
    service: Annotated[TaskArtifactService, Depends(get_task_artifact_service)],
) -> ReviewsArtifactResponse:
    """任务最新审核视图：audit 摘要 + issues。"""
    return await service.get_reviews(task_id)


@router.post(
    "/{task_id}/export",
    response_model=ExportCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_export(
    task_id: UUID,
    payload: ExportCreateRequest,
    response: Response,
    service: Annotated[ReportExportService, Depends(get_report_export_service)],
) -> ExportCreateResponse:
    """确定性导出（Stage 6C spec P）。

    资格判定（spec H：check pass + (audit pass/route pass) 或 (audit fail/route
    human_review + 人工 approve)）→ 生成 / replay。同输入（指纹相同）→ 200 +
    `X-Export-Replayed: true`；新建 → 201 + `X-Export-Replayed: false`。
    不可导出 → `ReportNotExportable` 409。**0 LLM / 0 Retrieval / 0 Chroma /
    0 Web**。
    """
    result = await service.create_or_get_export(task_id, payload.format)
    response.headers["X-Export-Replayed"] = "true" if result.replayed else "false"
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return ExportCreateResponse(
        export_id=result.export_id,
        format=result.format,
        file_name=result.file_name,
        media_type=result.media_type,
        byte_size=result.byte_size,
        replayed=result.replayed,
        created_at=result.created_at,
    )


@router.get(
    "/{task_id}/exports/{export_id}",
    response_model=ExportMetadataResponse,
)
async def get_export_metadata(
    task_id: UUID,
    export_id: UUID,
    service: Annotated[ReportExportService, Depends(get_report_export_service)],
) -> ExportMetadataResponse:
    """导出 metadata（task-scoped 404；不属于该 task → `ReportExportNotFound`）。"""
    record = await service.get_export(task_id, export_id)
    return ExportMetadataResponse(
        export_id=record.export_id,
        task_id=record.task_id,
        report_id=record.report_id,
        format=record.format,
        file_name=record.file_name,
        media_type=record.media_type,
        byte_size=record.byte_size,
        content_sha256=record.content_sha256,
        created_at=record.created_at,
    )


@router.get("/{task_id}/exports/{export_id}/content")
async def get_export_content(
    task_id: UUID,
    export_id: UUID,
    service: Annotated[ReportExportService, Depends(get_report_export_service)],
) -> Response:
    """导出字节下载（spec N/P）。

    下载前必须 `verify_export_integrity`；校验失败 → `ReportExportIntegrityError`
    409，字节缺失 → `ExportArtifactNotFound` 404。Content-Disposition attachment
    + 正确 MIME（text/markdown / docx / pdf）。
    """
    record, stream = await service.get_export_content(task_id, export_id)
    try:
        content = stream.read()
    finally:
        stream.close()
    return Response(
        content=content,
        media_type=record.media_type,
        headers={"Content-Disposition": f'attachment; filename="{record.file_name}"'},
    )


@router.get(
    "/{task_id}/citations/evidence/{evidence_card_id}",
    response_model=EvidenceCitationResponse,
)
async def get_evidence_citation(
    task_id: UUID,
    evidence_card_id: UUID,
    service: Annotated[TaskCitationService, Depends(get_task_citation_service)],
) -> EvidenceCitationResponse:
    """Evidence citation（Stage 6B.2 spec K）。

    Report → click citation → Evidence（头部 + canonical Claim relations +
    verified Document / Macro provenance）。task-scoped（spec J）；Document /
    Macro 全链 integrity 失败 → 409。
    """
    return await service.get_evidence_citation(task_id, evidence_card_id)


@router.get(
    "/{task_id}/citations/claims/{claim_id}",
    response_model=ClaimCitationResponse,
)
async def get_claim_citation(
    task_id: UUID,
    claim_id: UUID,
    service: Annotated[TaskCitationService, Depends(get_task_citation_service)],
) -> ClaimCitationResponse:
    """Claim citation（Stage 6B.2 spec L）。

    只允许 canonical synthesis input claim；返回 claim 元数据 + evidence
    relation list（relation 保留 supports / contradicts / context）。
    """
    return await service.get_claim_citation(task_id, claim_id)


@router.post(
    "/{task_id}/execute",
    response_model=WorkflowRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def execute_research(
    task_id: UUID,
    payload: ResearchExecutionRequest,
    execution: Annotated[ResearchExecutionService, Depends(get_research_execution_service)],
) -> WorkflowRunResponse:
    """启动真实研究执行（Stage 6A spec C/D）。

    请求体是显式 Stage 4 work plan；research_question / analysis_as_of 由
    ResearchExecutionService 从 ResearchTask 派生。返回 Stage 4 run（202）。
    """
    return await execution.start(task_id, payload)


@router.get("/{task_id}/events")
async def stream_task_events(
    task_id: UUID,
    request: Request,
    service: Annotated[WorkflowService, Depends(get_workflow_service)],
    task_service: Annotated[TaskService, Depends(get_task_service)],
    execution: Annotated[ResearchExecutionService, Depends(get_research_execution_service)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    # StreamingResponse 创建前确认 task 存在并校验 Last-Event-ID。
    await task_service.get_task(task_id)
    cursor = parse_last_event_id(last_event_id)

    async def event_generator():
        current = cursor
        last_keepalive = time.monotonic()
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            events = await service.list_events_after_for_task(
                task_id, current, _MAX_EVENTS_PER_POLL
            )
            for event in events:
                yield format_sse_event(event)
                current = event.event_id
            if events:
                last_keepalive = time.monotonic()
                continue
            terminal = await service.is_task_terminal(task_id)
            # 无 active run 且无后台链时才算任务终态（避免 Stage4→Stage5 空窗提前断流）。
            if terminal and not execution.is_running(task_id):
                events = await service.list_events_after_for_task(
                    task_id, current, _MAX_EVENTS_PER_POLL
                )
                for event in events:
                    yield format_sse_event(event)
                    current = event.event_id
                break
            if time.monotonic() - last_keepalive >= _KEEPALIVE_INTERVAL_SECONDS:
                yield ": keep-alive\n\n"
                last_keepalive = time.monotonic()
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
