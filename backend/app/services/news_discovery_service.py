"""News discovery persistence service (stage 2D.1).

discover_and_persist 严格遵循写入顺序 A-H：

A. provider.discover(query)：网络 I/O，期间绝不持有 AsyncSession；
B. raw_store.put_json_bytes(result.raw_response.raw_bytes)：原始响应先写
   内容寻址文件（文件 I/O 在 DB transaction 之前；orphan 文件保留不删，
   等待后续 GC）；
C. 短 DB transaction：RawArtifact get_or_create，media_type 必须
   application/json，否则抛 NewsDiscoveryArtifactConflict；
D. build_query_fingerprint：engine + company_id + query_text + UTC ISO
   时间窗 + max_results + raw_content_sha256（不含 fetched_at / request_count
   / IDs / storage_key）；
E. Run create-or-get by query_fingerprint（ON CONFLICT DO NOTHING，只有
   赢得 insert 的事务 created=True）；
F. 若 replay：校验 candidate_count == result_count，不一致抛
   NewsDiscoveryIntegrityError，不自动修复；
G. 若新 Run：bulk insert Candidates（含 url_sha256 与
   verification_status=unverified）；
H. commit；任何 DB 层异常回滚并抛 NewsDiscoveryPersistenceFailed。

重复完全相同的 discovery response → 相同 run_id，replayed=true。
"""

import uuid
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.news_discovery_candidate import NewsDiscoveryCandidateModel
from app.db.models.news_discovery_run import NewsDiscoveryRunModel
from app.db.models.raw_artifact import RawArtifactModel
from app.domain.news_discovery import (
    NewsCandidateVerificationStatus,
    NewsDiscoveryStatus,
)
from app.news.contracts import NewsDiscoveryCandidate, NewsDiscoveryQuery
from app.news.errors import (
    NewsDiscoveryArtifactConflict,
    NewsDiscoveryIntegrityError,
    NewsDiscoveryPersistenceFailed,
)
from app.news.fingerprint import build_query_fingerprint, build_url_sha256
from app.news.provider import NewsDiscoveryResult
from app.repositories.news_discovery_candidate_repository import (
    NewsDiscoveryCandidateRepository,
)
from app.repositories.news_discovery_run_repository import NewsDiscoveryRunRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.storage.raw_store import LocalRawArtifactStore

_MEDIA_TYPE_JSON = "application/json"


@dataclass(frozen=True)
class NewsDiscoveryPersistenceResult:
    """一次 discover_and_persist 的结果摘要（不含任何原始响应正文）。"""

    discovery_run_id: UUID
    query_fingerprint: str
    replayed: bool
    result_count: int
    candidate_count: int
    artifact_id: UUID


class NewsDiscoveryPersistenceService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        raw_store: LocalRawArtifactStore,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store

    async def discover_and_persist(
        self,
        provider: object,
        query: NewsDiscoveryQuery,
    ) -> NewsDiscoveryPersistenceResult:
        # A. 网络 I/O：provider.discover(query)，期间不持有 DB Session。
        result: NewsDiscoveryResult = await provider.discover(query)

        # B. 原始响应先写内容寻址文件；文件 I/O 在 DB transaction 之前。
        stored = self._raw_store.put_json_bytes(result.raw_response.raw_bytes)

        # C-H. 短 DB transaction（无网络 I/O，可持有 Session）。
        async with self._sessionmaker() as session:
            try:
                artifact_repo = RawArtifactRepository(session)
                run_repo = NewsDiscoveryRunRepository(session)
                candidate_repo = NewsDiscoveryCandidateRepository(session)

                # C. raw artifact rows get_or_create；media_type 必须 JSON。
                artifact, _ = await artifact_repo.get_or_create(
                    RawArtifactModel(
                        content_sha256=stored.content_sha256,
                        storage_key=stored.storage_key,
                        byte_size=stored.byte_size,
                        media_type=_MEDIA_TYPE_JSON,
                    )
                )
                if (
                    artifact.media_type != _MEDIA_TYPE_JSON
                    or artifact.storage_key != stored.storage_key
                    or artifact.byte_size != stored.byte_size
                ):
                    raise NewsDiscoveryArtifactConflict("existing raw artifact metadata mismatch")

                # D. query fingerprint（仅归档后的内容寻址，不含过程元数据）。
                fingerprint = build_query_fingerprint(
                    provider.engine,
                    query,
                    stored.content_sha256,
                )

                # E. Run create-or-get by fingerprint。
                run = self._build_run_model(
                    provider,
                    query,
                    result,
                    artifact,
                    fingerprint,
                    stored.content_sha256,
                )
                run, created = await run_repo.create_or_get_by_fingerprint(run)
                # 立即捕获标识：commit 后 returning 行可能被 expire。
                run_id = run.discovery_run_id
                query_fingerprint = run.query_fingerprint
                result_count = run.result_count

                # F. replay 完整性检查（不自动修复）。
                if not created:
                    candidate_count = await candidate_repo.count_for_run(run_id)
                    if candidate_count != result_count:
                        raise NewsDiscoveryIntegrityError("replay candidate count mismatch")
                    return NewsDiscoveryPersistenceResult(
                        discovery_run_id=run_id,
                        query_fingerprint=query_fingerprint,
                        replayed=True,
                        result_count=result_count,
                        candidate_count=candidate_count,
                        artifact_id=artifact.artifact_id,
                    )

                # G. 仅赢家写 Candidates。
                candidate_count = 0
                if result.candidates:
                    candidate_models = [
                        self._build_candidate_model(run_id, candidate)
                        for candidate in result.candidates
                    ]
                    candidate_count = await candidate_repo.bulk_create(candidate_models)

                # H. flush + commit。
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise NewsDiscoveryPersistenceFailed() from exc

            return NewsDiscoveryPersistenceResult(
                discovery_run_id=run_id,
                query_fingerprint=query_fingerprint,
                replayed=False,
                result_count=result_count,
                candidate_count=candidate_count,
                artifact_id=artifact.artifact_id,
            )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _build_run_model(
        provider: object,
        query: NewsDiscoveryQuery,
        result: NewsDiscoveryResult,
        artifact: RawArtifactModel,
        fingerprint: str,
        raw_content_sha256: str,
    ) -> NewsDiscoveryRunModel:
        raw = result.raw_response
        return NewsDiscoveryRunModel(
            discovery_run_id=uuid.uuid4(),
            company_id=query.company_id,
            engine=provider.engine.value,
            query_text=query.query_text,
            query_start_at=query.start_at,
            query_end_at=query.end_at,
            max_results=query.max_results,
            raw_artifact_id=artifact.artifact_id,
            raw_content_sha256=raw_content_sha256,
            result_count=len(result.candidates),
            request_count=result.request_count,
            response_status=raw.response_status,
            final_hostname=raw.final_hostname,
            content_type=raw.content_type,
            query_fingerprint=fingerprint,
            status=NewsDiscoveryStatus.AVAILABLE.value,
            fetched_at=result.fetched_at,
        )

    @staticmethod
    def _build_candidate_model(
        discovery_run_id: UUID,
        candidate: NewsDiscoveryCandidate,
    ) -> NewsDiscoveryCandidateModel:
        return NewsDiscoveryCandidateModel(
            candidate_id=uuid.uuid4(),
            discovery_run_id=discovery_run_id,
            rank=candidate.rank,
            title=candidate.title,
            discovered_url=candidate.discovered_url,
            normalized_url=candidate.normalized_url,
            url_sha256=build_url_sha256(candidate.normalized_url),
            domain=candidate.domain,
            seen_at=candidate.seen_at,
            source_language=candidate.source_language,
            source_country=candidate.source_country,
            verification_status=NewsCandidateVerificationStatus.UNVERIFIED.value,
        )
