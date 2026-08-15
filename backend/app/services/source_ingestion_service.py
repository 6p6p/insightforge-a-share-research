"""Source ingestion service for uploads and safe URL imports."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import BinaryIO
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.acquisition.http_fetcher import SafePdfFetcher
from app.core.errors import (
    CompanyIdentityNotFound,
    NewsArticleIngestionNotAllowed,
    RawArtifactNotFound,
    SourceCapabilityNotAllowed,
    SourceProviderDisabled,
    SourceProviderNotFound,
    SourceRecordNotFound,
    SourceUrlNotAllowed,
)
from app.core.logging import get_logger
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.domain.source_records import (
    RawArtifactMediaType,
    SourceDocumentType,
    SourceRecordStatus,
)
from app.domain.sources import AcquisitionMethod
from app.repositories.company_repository import CompanyRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.schemas.source_record import (
    SourceRecordListResponse,
    SourceRecordResponse,
)
from app.source_registry.url_policy import is_url_allowed
from app.storage.raw_store import LocalRawArtifactStore, StoredRawArtifact

logger = get_logger("app.source_ingestion")

_COMPANY_CAPABILITIES = frozenset({"company_announcement", "issuer_ir", "document_download"})


@dataclass(frozen=True)
class IngestionResult:
    record: SourceRecordResponse
    replayed: bool


class SourceIngestionService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        raw_store: LocalRawArtifactStore,
        fetcher: SafePdfFetcher | None = None,
        max_bytes: int = 104857600,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store
        self._fetcher = fetcher or SafePdfFetcher()
        self._max_bytes = max_bytes

    # ------------------------------------------------------------------ ingest

    async def ingest_upload(
        self,
        *,
        company_id: UUID,
        provider_key: str,
        document_type: SourceDocumentType,
        title: str,
        source_url: str | None,
        published_at: datetime | None,
        reporting_period_end: date | None,
        external_document_id: str | None,
        stream: BinaryIO,
    ) -> IngestionResult:
        """用户上传 PDF 落库。

        V1.1 closure：`source_url` 允许 None（本地 PDF 无官方 URL 时不伪造
        URL；provider 能力/启用校验保留，URL allowlist 校验仅在提供 URL 时
        执行）。acquisition_method=user_upload。
        """
        self._ensure_not_news_article(document_type)
        provider = await self._load_company_and_provider(company_id, provider_key, source_url)
        stored = self._raw_store.put_pdf_stream(stream)
        return await self._persist(
            company_id=company_id,
            provider_key=provider_key,
            document_type=document_type,
            title=title,
            source_url=source_url,
            published_at=published_at,
            reporting_period_end=reporting_period_end,
            external_document_id=external_document_id,
            stored=stored,
            acquisition_method="user_upload",
            provider=provider,
        )

    async def ingest_url(
        self,
        *,
        company_id: UUID,
        provider_key: str,
        document_type: SourceDocumentType,
        title: str,
        source_url: str,
        published_at: datetime | None,
        reporting_period_end: date | None,
        external_document_id: str | None,
    ) -> IngestionResult:
        self._ensure_not_news_article(document_type)
        provider = await self._load_company_and_provider(company_id, provider_key, source_url)
        pdf = await self._fetcher.fetch(
            source_url,
            provider.allowed_domains,
            self._max_bytes,
        )
        try:
            stored = self._raw_store.put_pdf_stream(pdf.content_stream)
        finally:
            pdf.close()
        return await self._persist(
            company_id=company_id,
            provider_key=provider_key,
            document_type=document_type,
            title=title,
            source_url=source_url,
            published_at=published_at,
            reporting_period_end=reporting_period_end,
            external_document_id=external_document_id,
            stored=stored,
            acquisition_method=AcquisitionMethod.USER_PROVIDED_URL.value,
            provider=provider,
        )

    async def ingest_discovered(
        self,
        *,
        company_id: UUID,
        provider_key: str,
        document_type: SourceDocumentType,
        title: str,
        source_url: str,
        published_at: datetime | None,
        reporting_period_end: date | None,
        external_document_id: str | None,
    ) -> IngestionResult:
        """自动发现来源落库（V1.1 closure）：acquisition_method=automatic_discovery。

        与 ingest_url 同一安全边界（provider allowlist + SafePdfFetcher）；仅
        acquisition_method 不同（来源方式诚实记录为受控自动发现）。
        """
        self._ensure_not_news_article(document_type)
        provider = await self._load_company_and_provider(company_id, provider_key, source_url)
        pdf = await self._fetcher.fetch(
            source_url,
            provider.allowed_domains,
            self._max_bytes,
        )
        try:
            stored = self._raw_store.put_pdf_stream(pdf.content_stream)
        finally:
            pdf.close()
        return await self._persist(
            company_id=company_id,
            provider_key=provider_key,
            document_type=document_type,
            title=title,
            source_url=source_url,
            published_at=published_at,
            reporting_period_end=reporting_period_end,
            external_document_id=external_document_id,
            stored=stored,
            acquisition_method=AcquisitionMethod.AUTOMATIC_DISCOVERY.value,
            provider=provider,
        )

    async def ingest_discovered_bytes(
        self,
        *,
        company_id: UUID,
        provider_key: str,
        document_type: SourceDocumentType,
        title: str,
        source_url: str,
        published_at: datetime | None,
        reporting_period_end: date | None,
        external_document_id: str | None,
        pdf_bytes: bytes,
    ) -> IngestionResult:
        """自动发现落库（字节路径，V1.1 closure）：反爬握手下载的 PDF 直接入库。

        与 ingest_discovered 同一语义（acquisition_method=automatic_discovery）；
        字节已由调用方完成 allowlist 校验与 %PDF 校验，这里仍走 raw_store 的
        PDF 头校验（双重防线）。
        """
        self._ensure_not_news_article(document_type)
        provider = await self._load_company_and_provider(company_id, provider_key, source_url)
        import io

        stored = self._raw_store.put_pdf_stream(io.BytesIO(pdf_bytes))
        return await self._persist(
            company_id=company_id,
            provider_key=provider_key,
            document_type=document_type,
            title=title,
            source_url=source_url,
            published_at=published_at,
            reporting_period_end=reporting_period_end,
            external_document_id=external_document_id,
            stored=stored,
            acquisition_method=AcquisitionMethod.AUTOMATIC_DISCOVERY.value,
            provider=provider,
        )

    # ----------------------------------------------------------------- queries

    async def get_source(self, source_id: UUID) -> SourceRecordResponse:
        async with self._sessionmaker() as session:
            record = await SourceRecordRepository(session).get_by_id(source_id)
            if record is None:
                raise SourceRecordNotFound()
            artifact = await RawArtifactRepository(session).get_by_id(record.artifact_id)
        if artifact is None:
            raise RawArtifactNotFound()
        return self._build_response(record, artifact)

    async def list_company_sources(
        self,
        company_id: UUID,
        document_type: SourceDocumentType | None,
        limit: int,
        offset: int,
    ) -> SourceRecordListResponse:
        document_type_value = document_type.value if document_type is not None else None
        async with self._sessionmaker() as session:
            repo = SourceRecordRepository(session)
            rows = await repo.list_for_company(company_id, document_type_value, limit, offset)
            total = await repo.count_for_company(company_id, document_type_value)
            artifacts = await self._load_artifacts(session, [row.artifact_id for row in rows])
        items = [self._build_response(row, artifacts[row.artifact_id]) for row in rows]
        return SourceRecordListResponse(items=items, total=total, limit=limit, offset=offset)

    async def open_source_content(self, source_id: UUID) -> tuple[SourceRecordResponse, BinaryIO]:
        async with self._sessionmaker() as session:
            record = await SourceRecordRepository(session).get_by_id(source_id)
            if record is None:
                raise SourceRecordNotFound()
            artifact = await RawArtifactRepository(session).get_by_id(record.artifact_id)
        if artifact is None:
            raise RawArtifactNotFound()
        stream = self._raw_store.open(artifact.storage_key)
        return self._build_response(record, artifact), stream

    # ------------------------------------------------------------- internals

    @staticmethod
    def _ensure_not_news_article(document_type: SourceDocumentType) -> None:
        """§二十：news_article 只能由原创发布者验证链路创建。

        DB CHECK（document_type / acquisition_method 相互独立）不会拦截
        news_article + user_upload 的组合，必须在服务层显式拒绝，防止新闻
        来源绕过 Original Publisher → SafeHtmlFetcher → 归档验证流程注入。
        """
        if document_type == SourceDocumentType.NEWS_ARTICLE:
            raise NewsArticleIngestionNotAllowed()

    async def _load_company_and_provider(
        self,
        company_id: UUID,
        provider_key: str,
        source_url: str | None,
    ) -> SourceProviderModel:
        """短会话只读 Company/Provider；不跨网络 I/O 持有 AsyncSession。

        source_url=None（本地上传无官方 URL）时跳过 allowlist 校验（DB CHECK
        已允许 NULL）；provider 存在/启用/能力校验始终执行。
        """
        async with self._sessionmaker() as session:
            company = await CompanyRepository(session).get_by_id(company_id)
            if company is None:
                raise CompanyIdentityNotFound()
            provider = await SourceProviderRepository(session).get_by_key(provider_key)
        if provider is None:
            raise SourceProviderNotFound()
        if not provider.enabled:
            raise SourceProviderDisabled()
        if not set(provider.capabilities) & _COMPANY_CAPABILITIES:
            raise SourceCapabilityNotAllowed()
        if source_url is not None and not is_url_allowed(source_url, provider.allowed_domains):
            raise SourceUrlNotAllowed()
        return provider

    async def _persist(
        self,
        *,
        company_id: UUID,
        provider_key: str,
        document_type: SourceDocumentType,
        title: str,
        source_url: str,
        published_at: datetime | None,
        reporting_period_end: date | None,
        external_document_id: str | None,
        stored: StoredRawArtifact,
        acquisition_method: str,
        provider: SourceProviderModel,
    ) -> IngestionResult:
        async with self._sessionmaker() as session:
            artifact_repo = RawArtifactRepository(session)
            artifact = await artifact_repo.get_by_sha256(stored.content_sha256)
            if artifact is None:
                created = await artifact_repo.create(
                    RawArtifactModel(
                        content_sha256=stored.content_sha256,
                        storage_key=stored.storage_key,
                        byte_size=stored.byte_size,
                        media_type=RawArtifactMediaType.PDF.value,
                    )
                )
                if created is None:
                    # 并发竞争：另一事务已插入相同 sha256
                    artifact = await artifact_repo.get_by_sha256(stored.content_sha256)
                else:
                    artifact = created

            source_repo = SourceRecordRepository(session)
            existing = await source_repo.find_existing(
                provider_key, source_url, artifact.artifact_id
            )
            if existing is not None:
                # rollback 会 expire 所有 ORM 对象；先在回滚前构造响应，
                # 否则 _build_response 访问过期属性会触发 lazy load（MissingGreenlet）。
                result = IngestionResult(self._build_response(existing, artifact), replayed=True)
                await session.rollback()
                return result

            record = SourceRecordModel(
                company_id=company_id,
                provider_key=provider_key,
                artifact_id=artifact.artifact_id,
                document_type=document_type.value,
                title=title,
                published_at=published_at,
                reporting_period_end=reporting_period_end,
                source_url=source_url,
                acquisition_method=acquisition_method,
                external_document_id=external_document_id,
                authority_tier_snapshot=int(provider.authority_tier),
                critical_claim_eligible_snapshot=provider.critical_claim_eligible,
                # 保存获取当时的 Provider 能力完整列表；稳定排序后不可变快照。
                provider_capabilities_snapshot=sorted(provider.capabilities),
                status=SourceRecordStatus.AVAILABLE.value,
                acquired_at=datetime.now(UTC),
            )
            try:
                await source_repo.create(record)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # rollback 后 artifact 已 expire；重新按 sha256 查询，避免访问过期属性。
                artifact = await artifact_repo.get_by_sha256(stored.content_sha256)
                if artifact is None:
                    # 文件已归档但数据库写入失败：不删除内容寻址文件，
                    # 记录脱敏 orphan 提示，等待后续 GC。
                    logger.warning(
                        "source_record_persist_failed",
                        provider_key=provider_key,
                        reason="unexpected_write_failure",
                    )
                    raise
                existing = await source_repo.find_existing(
                    provider_key, source_url, artifact.artifact_id
                )
                if existing is None:
                    logger.warning(
                        "source_record_persist_failed",
                        provider_key=provider_key,
                        reason="unexpected_write_failure",
                    )
                    raise
                return IngestionResult(self._build_response(existing, artifact), replayed=True)
            # created_at 由数据库 now() 生成，commit 后重新读取完整行
            persisted = await source_repo.get_by_id(record.source_id)
            if persisted is None:
                raise SourceRecordNotFound()
            return IngestionResult(self._build_response(persisted, artifact), replayed=False)

    @staticmethod
    async def _load_artifacts(session, artifact_ids: list[UUID]) -> dict[UUID, RawArtifactModel]:
        if not artifact_ids:
            return {}
        result = await session.execute(
            select(RawArtifactModel).where(RawArtifactModel.artifact_id.in_(artifact_ids))
        )
        return {artifact.artifact_id: artifact for artifact in result.scalars()}

    @staticmethod
    def _build_response(
        record: SourceRecordModel,
        artifact: RawArtifactModel,
    ) -> SourceRecordResponse:
        return SourceRecordResponse(
            source_id=record.source_id,
            company_id=record.company_id,
            provider_key=record.provider_key,
            artifact_id=record.artifact_id,
            document_type=SourceDocumentType(record.document_type),
            title=record.title,
            published_at=record.published_at,
            reporting_period_end=record.reporting_period_end,
            source_url=record.source_url,
            acquisition_method=record.acquisition_method,
            external_document_id=record.external_document_id,
            authority_tier_snapshot=record.authority_tier_snapshot,
            critical_claim_eligible_snapshot=record.critical_claim_eligible_snapshot,
            provider_capabilities_snapshot=record.provider_capabilities_snapshot,
            status=record.status,
            acquired_at=record.acquired_at,
            created_at=record.created_at,
            content_sha256=artifact.content_sha256,
            byte_size=artifact.byte_size,
            media_type=artifact.media_type,
        )
