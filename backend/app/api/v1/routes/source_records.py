"""Source record ingestion and retrieval endpoints."""

from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_source_ingestion_service
from app.domain.source_records import SourceDocumentType
from app.schemas.source_record import (
    SourceRecordListResponse,
    SourceRecordResponse,
    SourceUrlImportRequest,
)
from app.services.source_ingestion_service import SourceIngestionService

router = APIRouter(tags=["source-records"])


def _iter_stream(stream) -> AsyncIterator[bytes]:
    try:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        stream.close()


@router.post(
    "/source-records/upload",
    response_model=SourceRecordResponse,
    status_code=201,
)
async def upload_source(
    response: Response,
    service: Annotated[SourceIngestionService, Depends(get_source_ingestion_service)],
    company_id: Annotated[UUID, Form()],
    provider_key: Annotated[str, Form()],
    document_type: Annotated[SourceDocumentType, Form()],
    title: Annotated[str, Form()],
    source_url: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    published_at: Annotated[datetime | None, Form()] = None,
    reporting_period_end: Annotated[date | None, Form()] = None,
    external_document_id: Annotated[str | None, Form()] = None,
) -> SourceRecordResponse:
    """Stream a user-uploaded PDF into the raw artifact store."""
    result = await service.ingest_upload(
        company_id=company_id,
        provider_key=provider_key,
        document_type=document_type,
        title=title,
        source_url=source_url,
        published_at=published_at,
        reporting_period_end=reporting_period_end,
        external_document_id=external_document_id,
        stream=file.file,
    )
    response.headers["Source-Replayed"] = "true" if result.replayed else "false"
    if result.replayed:
        response.status_code = 200
    return result.record


@router.post(
    "/source-records/import-url",
    response_model=SourceRecordResponse,
    status_code=201,
)
async def import_url_source(
    response: Response,
    payload: SourceUrlImportRequest,
    service: Annotated[SourceIngestionService, Depends(get_source_ingestion_service)],
) -> SourceRecordResponse:
    """Import an official PDF from a source-registry-approved URL."""
    result = await service.ingest_url(
        company_id=payload.company_id,
        provider_key=payload.provider_key,
        document_type=payload.document_type,
        title=payload.title,
        source_url=payload.source_url,
        published_at=payload.published_at,
        reporting_period_end=payload.reporting_period_end,
        external_document_id=payload.external_document_id,
    )
    response.headers["Source-Replayed"] = "true" if result.replayed else "false"
    if result.replayed:
        response.status_code = 200
    return result.record


@router.get("/source-records/{source_id}", response_model=SourceRecordResponse)
async def get_source_record(
    source_id: UUID,
    service: Annotated[SourceIngestionService, Depends(get_source_ingestion_service)],
) -> SourceRecordResponse:
    return await service.get_source(source_id)


@router.get(
    "/companies/{company_id}/source-records",
    response_model=SourceRecordListResponse,
)
async def list_company_source_records(
    company_id: UUID,
    service: Annotated[SourceIngestionService, Depends(get_source_ingestion_service)],
    document_type: Annotated[SourceDocumentType | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SourceRecordListResponse:
    return await service.list_company_sources(
        company_id=company_id,
        document_type=document_type,
        limit=limit,
        offset=offset,
    )


@router.get("/source-records/{source_id}/content")
async def download_source_content(
    source_id: UUID,
    service: Annotated[SourceIngestionService, Depends(get_source_ingestion_service)],
) -> StreamingResponse:
    record, stream = await service.open_source_content(source_id)
    headers = {
        "Content-Length": str(record.byte_size),
        "Content-Disposition": f'attachment; filename="source-{source_id}.pdf"',
    }
    return StreamingResponse(
        _iter_stream(stream),
        media_type="application/pdf",
        headers=headers,
    )
