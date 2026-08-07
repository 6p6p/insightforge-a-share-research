"""News original source verification service (stage 2D.2A).

verify_candidate(candidate_id) 实现确定性链路：

    Candidate → Original Publisher（Resolver）→ SafeHtmlFetcher →
    RawArtifact(text/html 内容寻址归档) → SourceRecord → Verification →
    candidate.verification_status=verified

verified 语义（ADR-0015 不变量 D）严格限定为：原始发布网页属于登记的原创
媒体、公开 HTML 被安全获取并不可变归档、Candidate → SourceRecord 溯源已
建立；**不代表新闻内容为真、不代表已交叉验证、不是 Evidence**。

顺序约束：
1. 短 DB session 加载 Candidate + Run（派生 company_id），检查既有
   Verification（replay → 完整性校验 → 返回 replayed=true，无网络请求）；
2. 网络 I/O（Resolver + SafeHtmlFetcher）期间**绝不持有 AsyncSession**；
3. 文件 I/O（put_html_bytes）在 DB transaction 之前；
4. 短 DB transaction：RawArtifact get_or_create + 四元强一致 → SourceRecord
   dedupe（provider_key+final_url+artifact_id）→ Verification（每 candidate
   唯一）→ candidate 置 verified → commit；DB 异常 rollback 抛
   NewsOriginalPersistenceFailed。

company_id 一律来自 NewsDiscoveryRun.company_id（不变量 §八），source_url
使用 final URL（不变量 H），published_at 恒为 NULL（不变量 F）。
"""

import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.acquisition.html_fetcher import SafeHtmlFetcher
from app.db.models.news_discovery_candidate import NewsDiscoveryCandidateModel
from app.db.models.news_source_verification import NewsSourceVerificationModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.domain.news_discovery import NewsCandidateVerificationStatus
from app.domain.source_records import SourceRecordStatus
from app.news.errors import (
    NewsCandidateNotFound,
    NewsOriginalArtifactConflict,
    NewsOriginalPersistenceFailed,
    NewsOriginalSourceIntegrityError,
)
from app.news.publisher_resolver import OriginalPublisherResolver
from app.repositories.news_discovery_candidate_repository import (
    NewsDiscoveryCandidateRepository,
)
from app.repositories.news_discovery_run_repository import NewsDiscoveryRunRepository
from app.repositories.news_source_verification_repository import (
    NewsSourceVerificationRepository,
)
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.storage.raw_store import LocalRawArtifactStore

_MEDIA_TYPE_HTML = "text/html"
_DOCUMENT_TYPE_NEWS_ARTICLE = "news_article"
_ACQUISITION_METHOD_PUBLIC_HTML = "public_html"
_TITLE_ORIGIN_DISCOVERY_CANDIDATE = "discovery_candidate"
_SOURCE_RECORD_TITLE_MAX = 500  # source_records.title VARCHAR(500)（display 元数据边界）


@dataclass(frozen=True)
class NewsOriginalSourceVerificationResult:
    """一次 verify_candidate 的结果摘要（不含任何 HTML 正文）。"""

    candidate_id: UUID
    source_id: UUID
    verification_id: UUID
    artifact_id: UUID
    provider_key: str
    final_url: str
    status_code: int
    content_type: str
    redirect_count: int
    replayed: bool


class NewsOriginalSourceService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        raw_store: LocalRawArtifactStore,
        fetcher: SafeHtmlFetcher,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store
        self._fetcher = fetcher

    async def verify_candidate(
        self,
        candidate_id: UUID,
    ) -> NewsOriginalSourceVerificationResult:
        # 1. 短 DB session：加载 Candidate + Run + 检查 replay。
        async with self._sessionmaker() as session:
            candidate_repo = NewsDiscoveryCandidateRepository(session)
            run_repo = NewsDiscoveryRunRepository(session)
            verification_repo = NewsSourceVerificationRepository(session)
            provider_repo = SourceProviderRepository(session)

            candidate = await candidate_repo.get_by_id(candidate_id)
            if candidate is None:
                raise NewsCandidateNotFound()
            self._check_candidate_integrity(candidate)
            run = await run_repo.get_by_id(candidate.discovery_run_id)
            if run is None:
                raise NewsOriginalSourceIntegrityError("discovery run missing")
            company_id = run.company_id
            # 只取 eligible Original Publishers；Resolver 内部仍独立复核资格。
            providers = await provider_repo.list_original_publishers()

            # replay：既有 Verification 存在 → 完整性校验 → 返回，无网络请求。
            existing = await verification_repo.get_by_candidate_id(candidate_id)
            if existing is not None:
                return await self._replay_result(session, candidate, existing)

        # 2. 网络 I/O（Resolver + SafeHtmlFetcher），不持有 Session。
        publisher = OriginalPublisherResolver.resolve(
            candidate.normalized_url,
            providers,
        )
        page = await self._fetcher.fetch(
            candidate.normalized_url,
            publisher.provider_key,
            list(publisher.allowed_domains),
        )

        # 3. 文件 I/O：HTML 原始字节先内容寻址落盘（DB transaction 之前）。
        stored = self._raw_store.put_html_bytes(page.raw_bytes)

        # 4. 短 DB transaction：artifact → source record → verification →
        #    candidate verified → commit。
        async with self._sessionmaker() as session:
            try:
                candidate_repo = NewsDiscoveryCandidateRepository(session)
                artifact_repo = RawArtifactRepository(session)
                source_repo = SourceRecordRepository(session)
                verification_repo = NewsSourceVerificationRepository(session)

                # 重新加载 candidate：session 1 已关闭，detached 对象写入不会
                # 进入本事务；加载失败视为完整性错误。
                candidate = await candidate_repo.get_by_id(candidate_id)
                if candidate is None:
                    raise NewsOriginalSourceIntegrityError("candidate missing on persist")

                artifact, _ = await artifact_repo.get_or_create(
                    RawArtifactModel(
                        content_sha256=stored.content_sha256,
                        storage_key=stored.storage_key,
                        byte_size=stored.byte_size,
                        media_type=_MEDIA_TYPE_HTML,
                    )
                )
                # 内容寻址强一致（§十二）：既有一行必须 media_type=text/html 且
                # 四个元数据与本次落盘完全一致，否则同一 SHA 被其他类型占用或
                # 被篡改，抛 NewsOriginalArtifactConflict（稳定默认 message）。
                if (
                    artifact.media_type != _MEDIA_TYPE_HTML
                    or artifact.content_sha256 != stored.content_sha256
                    or artifact.byte_size != stored.byte_size
                    or artifact.storage_key != stored.storage_key
                ):
                    raise NewsOriginalArtifactConflict()

                source_record, _ = await source_repo.create_or_get(
                    self._build_source_record(
                        company_id=company_id,
                        publisher=publisher,
                        candidate=candidate,
                        artifact=artifact,
                        final_url=page.final_url,
                        fetched_at=page.fetched_at,
                    )
                )
                source_id = source_record.source_id

                verification, _ = await verification_repo.create_or_get_by_candidate(
                    self._build_verification(
                        candidate_id=candidate_id,
                        source_id=source_id,
                        publisher_key=publisher.provider_key,
                        requested_url=candidate.normalized_url,
                        page=page,
                    )
                )
                verification_id = verification.verification_id

                candidate.verification_status = NewsCandidateVerificationStatus.VERIFIED.value
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise NewsOriginalPersistenceFailed() from exc

        return NewsOriginalSourceVerificationResult(
            candidate_id=candidate_id,
            source_id=source_id,
            verification_id=verification_id,
            artifact_id=artifact.artifact_id,
            provider_key=publisher.provider_key,
            final_url=page.final_url,
            status_code=page.status_code,
            content_type=page.content_type,
            redirect_count=page.redirect_count,
            replayed=False,
        )

    # ------------------------------------------------------------------ 内部

    async def _replay_result(
        self,
        session,
        candidate: NewsDiscoveryCandidateModel,
        verification: NewsSourceVerificationModel,
    ) -> NewsOriginalSourceVerificationResult:
        """replay 完整性校验：source 必须存在且 artifact 仍是 text/html。"""
        source_repo = SourceRecordRepository(session)
        artifact_repo = RawArtifactRepository(session)
        source = await source_repo.get_by_id(verification.source_id)
        if source is None:
            raise NewsOriginalSourceIntegrityError("source record missing on replay")
        artifact = await artifact_repo.get_by_id(source.artifact_id)
        if artifact is None or artifact.media_type != _MEDIA_TYPE_HTML:
            raise NewsOriginalSourceIntegrityError("artifact tampered on replay")
        return NewsOriginalSourceVerificationResult(
            candidate_id=candidate.candidate_id,
            source_id=source.source_id,
            verification_id=verification.verification_id,
            artifact_id=artifact.artifact_id,
            provider_key=verification.publisher_provider_key,
            final_url=verification.final_url,
            status_code=verification.http_status,
            content_type=verification.content_type,
            redirect_count=verification.redirect_count,
            replayed=True,
        )

    @staticmethod
    def _check_candidate_integrity(candidate: NewsDiscoveryCandidateModel) -> None:
        """§八：从 normalized_url 重算 hostname，必须等于 candidate.domain。"""
        hostname = urlsplit(candidate.normalized_url).hostname or ""
        if hostname != candidate.domain:
            raise NewsOriginalSourceIntegrityError("candidate domain mismatch")

    @staticmethod
    def _build_source_record(
        *,
        company_id: UUID,
        publisher: SourceProviderModel,
        candidate: NewsDiscoveryCandidateModel,
        artifact: RawArtifactModel,
        final_url: str,
        fetched_at,
    ) -> SourceRecordModel:
        capabilities = sorted(str(c) for c in publisher.capabilities)
        return SourceRecordModel(
            company_id=company_id,
            provider_key=publisher.provider_key,
            artifact_id=artifact.artifact_id,
            document_type=_DOCUMENT_TYPE_NEWS_ARTICLE,
            title=candidate.title[:_SOURCE_RECORD_TITLE_MAX],
            published_at=None,
            reporting_period_end=None,
            source_url=final_url,
            acquisition_method=_ACQUISITION_METHOD_PUBLIC_HTML,
            external_document_id=None,
            authority_tier_snapshot=int(publisher.authority_tier),
            critical_claim_eligible_snapshot=bool(publisher.critical_claim_eligible),
            provider_capabilities_snapshot=capabilities,
            status=SourceRecordStatus.AVAILABLE.value,
            acquired_at=fetched_at,
        )

    @staticmethod
    def _build_verification(
        *,
        candidate_id: UUID,
        source_id: UUID,
        publisher_key: str,
        requested_url: str,
        page,
    ) -> NewsSourceVerificationModel:
        return NewsSourceVerificationModel(
            verification_id=uuid.uuid4(),
            candidate_id=candidate_id,
            source_id=source_id,
            publisher_provider_key=publisher_key,
            requested_url=requested_url,
            final_url=page.final_url,
            final_hostname=page.final_hostname,
            http_status=page.status_code,
            content_type=page.content_type,
            redirect_count=page.redirect_count,
            title_origin=_TITLE_ORIGIN_DISCOVERY_CANDIDATE,
            verified_at=page.fetched_at,
        )
