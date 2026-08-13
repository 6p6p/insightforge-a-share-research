"""Evaluation replay rehydrator（stage 7B.1.4B.1）。

Frozen Evaluation Bundle → 隔离 PostgreSQL + RawArtifactStore 的运行时复现。

关键隔离边界：`EvaluationReplayRehydrator` **只**依赖
`target_sessionmaker`（隔离 PG）+ `target_raw_artifact_store`（隔离 store root）+
`bundle_loader`（frozen bundle）。**结构上不可能**接触 source/live PG——没有任何
source/live sessionmaker / DEFAULT_PROVIDER registry 引用，replay 永远落在隔离
数据库上。

语义字段 vs 持久化脚手架：
- 语义字段（frozen bundle 携带、运行期 planner 读取）→ **frozen-exact**；
- persistence-only 字段（schema 要求 NOT NULL / FK / CHECK，但运行期不读取）→
  由 `replay_v1` 确定性 policy 补全（见 `app.eval.replay.contracts`）。

**不** seed 任何 derived artifact（ParsedSource / ParsedBlock / ChunkSet /
DocumentChunk / VectorIndex / EvidenceCard）——那些由 caller 在 rehydrate 之后
用 `SourceParsingService.parse_source` + `ChunkingService` 走真实 pipeline 重建。
"""

import hashlib
import io
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.companies.normalization import normalize_company_text
from app.core.errors import DomainError
from app.db.models.company import CompanyModel
from app.db.models.company_alias import CompanyAliasModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.domain.macro_persistence import MacroSnapshotStatus
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.contracts import (
    FrozenCompanyIdentity,
    FrozenDocumentSourceRef,
    FrozenMacroArtifactLinkRef,
    FrozenMacroObservationRef,
    FrozenMacroRawArtifactRef,
    FrozenMacroSeriesRef,
    FrozenMacroSnapshotRef,
    FrozenSourceProviderRef,
    FrozenSourceSnapshot,
)
from app.eval.errors import EvalReplayError, EvalReplayIntegrityError
from app.eval.fingerprints import compute_source_snapshot_fingerprint
from app.eval.replay.contracts import (
    REPLAY_ALIAS_TYPE,
    REPLAY_COMPANY_LISTING_STATUS,
    REPLAY_IDENTITY_SOURCE_URL,
    REPLAY_PROVIDER_ACQUISITION_METHODS,
    REPLAY_PROVIDER_ALLOWED_DOMAINS,
    REPLAY_PROVIDER_AUTHORITY_TIER,
    REPLAY_PROVIDER_CRITICAL_CLAIM_ELIGIBLE,
    REPLAY_PROVIDER_EXCHANGE_SCOPE,
    REPLAY_PROVIDER_HOMEPAGE_URL,
    REPLAY_PROVIDER_REQUIRES_API_KEY,
    REPLAY_PROVIDER_TYPE,
    REPLAY_SOURCE_ACQUISITION_METHOD,
    REPLAY_SOURCE_STATUS,
    RehydratedCase,
    RehydratedDocument,
)
from app.macro.contracts import MacroFrequency, MacroPeriodSemantics
from app.macro.errors import MacroSnapshotIntegrityError
from app.macro.fingerprint import (
    MACRO_SNAPSHOT_FINGERPRINT_VERSION,
    WORLD_BANK_NORMALIZATION_VERSION,
)
from app.repositories.company_repository import CompanyRepository
from app.repositories.macro_observation_repository import MacroObservationRepository
from app.repositories.macro_series_repository import MacroSeriesRepository
from app.repositories.macro_snapshot_repository import MacroSnapshotRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.macro_persistence_service import MacroPersistenceService
from app.storage.raw_store import LocalRawArtifactStore, StoredRawArtifact

_MEDIA_TYPE_PDF = "application/pdf"
_MEDIA_TYPE_JSON = "application/json"
_MEDIA_TYPE_HTML = "text/html"


class EvaluationReplayRehydrator:
    """Frozen Bundle → 隔离 PG + store 的运行时复现（只依赖隔离 target）。"""

    def __init__(
        self,
        target_sessionmaker: async_sessionmaker,
        target_raw_artifact_store: LocalRawArtifactStore,
        bundle_loader: EvaluationBundleLoader,
    ) -> None:
        self._sessionmaker = target_sessionmaker
        self._raw_store = target_raw_artifact_store
        self._loader = bundle_loader
        self._macro_service = MacroPersistenceService(
            target_sessionmaker, target_raw_artifact_store
        )

    async def rehydrate_case(self, case_id: str, case_version: int) -> RehydratedCase:
        case = self._loader.load_case(case_id, case_version)
        snapshot = self._loader.load_snapshot(case.source_snapshot_fingerprint)

        # bundle 自洽：case 引用的 snapshot fingerprint == snapshot 实际 fingerprint。
        if compute_source_snapshot_fingerprint(snapshot) != case.source_snapshot_fingerprint:
            raise EvalReplayIntegrityError("source snapshot fingerprint 与 case 引用不一致")

        providers_by_key = {p.provider_key: p for p in snapshot.source_providers}
        for doc in snapshot.document_sources:
            if doc.provider_key not in providers_by_key:
                raise EvalReplayIntegrityError(
                    "document provider_key 不在 source_providers（bundle 不自洽）"
                )

        # 阶段一：字节 SHA 校验 + 落盘（DB session 之外；content-addressed 幂等）。
        stored_docs: list[tuple[FrozenDocumentSourceRef, StoredRawArtifact]] = []
        for doc in snapshot.document_sources:
            blob = self._loader.read_document_blob(doc.content_sha256)
            if hashlib.sha256(blob).hexdigest() != doc.content_sha256:
                raise EvalReplayIntegrityError("document blob content_sha256 不匹配（篡改）")
            stored = self._write_blob(blob, doc.media_type)
            if stored.content_sha256 != doc.content_sha256:
                raise EvalReplayIntegrityError("落盘 content_sha256 与 frozen 不一致")
            stored_docs.append((doc, stored))

        stored_macros: list[tuple[FrozenMacroSnapshotRef, dict[UUID, StoredRawArtifact]]] = []
        for macro in snapshot.macro_snapshots:
            self._require_macro_closure(macro)
            stored_artifacts: dict[UUID, StoredRawArtifact] = {}
            for raw_ref in macro.raw_artifacts:
                blob = self._loader.read_document_blob(raw_ref.content_sha256)
                if hashlib.sha256(blob).hexdigest() != raw_ref.content_sha256:
                    raise EvalReplayIntegrityError(
                        "macro raw artifact blob content_sha256 不匹配（篡改）"
                    )
                stored = self._write_blob(blob, raw_ref.media_type)
                if stored.content_sha256 != raw_ref.content_sha256:
                    raise EvalReplayIntegrityError("落盘 content_sha256 与 frozen 不一致")
                stored_artifacts[raw_ref.artifact_id] = stored
            stored_macros.append((macro, stored_artifacts))

        # 阶段二：单 DB 事务精确 ID 复现（语义字段 frozen-exact + replay_v1 脚手架）。
        try:
            async with self._sessionmaker() as session:
                result = await self._persist(
                    session, case.company_id, case.company, snapshot, stored_docs, stored_macros
                )
                await session.commit()
                return result
        except IntegrityError as exc:
            raise EvalReplayIntegrityError(
                "rehydration 完整性破坏（frozen 值违反目标 schema 约束）"
            ) from exc
        except SQLAlchemyError as exc:
            raise EvalReplayError("rehydration 落库失败") from exc

    def _write_blob(self, blob: bytes, media_type: str) -> StoredRawArtifact:
        try:
            if media_type == _MEDIA_TYPE_PDF:
                return self._raw_store.put_pdf_stream(io.BytesIO(blob))
            if media_type == _MEDIA_TYPE_JSON:
                return self._raw_store.put_json_bytes(blob)
            if media_type == _MEDIA_TYPE_HTML:
                return self._raw_store.put_html_bytes(blob)
        except (DomainError, OSError) as exc:
            raise EvalReplayError("document blob 无法落盘到隔离 store") from exc
        raise EvalReplayError("不支持的 frozen media_type")

    async def _persist(
        self,
        session: AsyncSession,
        company_id: UUID,
        company: FrozenCompanyIdentity,
        snapshot: FrozenSourceSnapshot,
        stored_docs: list[tuple[FrozenDocumentSourceRef, StoredRawArtifact]],
        stored_macros: list[tuple[FrozenMacroSnapshotRef, dict[UUID, StoredRawArtifact]]],
    ) -> RehydratedCase:
        provider_keys = sorted(p.provider_key for p in snapshot.source_providers)
        if not provider_keys:
            raise EvalReplayError(
                "snapshot 无 provider，无法为 company 提供 identity_source_provider_key"
            )
        identity_provider_key = provider_keys[0]

        # 1. providers：load-or-insert + create-or-verify。禁用 upsert 覆盖：已存在 →
        #    逐 semantic + replay_v1 脚手架字段比对，完全一致 → replay，任何不一致 →
        #    EvalReplayIntegrityError（不覆盖、不静默改写）。
        provider_repo = SourceProviderRepository(session)
        for p in snapshot.source_providers:
            expected = self._provider_model(p)
            existing = await provider_repo.get_by_key(p.provider_key)
            if existing is None:
                await provider_repo.upsert(expected)
            else:
                self._verify_provider(existing, expected)

        # 2. company：load-or-insert + create-or-verify（精确 ID）。
        company_repo = CompanyRepository(session)
        expected_company = self._company_model(company_id, company, identity_provider_key)
        existing_company = await company_repo.get_by_id(company_id)
        if existing_company is None:
            await company_repo.create(expected_company)
        else:
            self._verify_company(existing_company, expected_company)

        # 3. aliases：exact 已存在 → replay；缺失 → create；同 normalized identity 但
        #    alias 语义不同 → reject；不产生重复。
        await self._replay_aliases(
            session, company_repo, company_id, company, identity_provider_key
        )

        # 4. raw artifacts：load-or-insert + create-or-verify（storage_key 不覆盖）。
        raw_repo = RawArtifactRepository(session)
        for doc, stored in stored_docs:
            expected_raw = self._raw_artifact_model(doc, stored)
            existing_raw = await raw_repo.get_by_id(doc.raw_artifact_id)
            if existing_raw is None:
                await raw_repo.insert(expected_raw)
            else:
                self._verify_raw_artifact(existing_raw, expected_raw)

        # 5. source records：load-or-insert + create-or-verify（不调用 SourceIngestionService）。
        source_repo = SourceRecordRepository(session)
        capabilities_by_key = {
            p.provider_key: list(p.capabilities) for p in snapshot.source_providers
        }
        documents: list[RehydratedDocument] = []
        for doc, stored in stored_docs:
            expected_source = self._source_record_model(
                doc, company_id, capabilities_by_key[doc.provider_key]
            )
            existing_source = await source_repo.get_by_id(doc.source_record_id)
            if existing_source is None:
                await source_repo.create(expected_source)
            else:
                self._verify_source_record(existing_source, expected_source)
            documents.append(
                RehydratedDocument(
                    source_record_id=doc.source_record_id,
                    raw_artifact_id=doc.raw_artifact_id,
                    content_sha256=stored.content_sha256,
                    storage_key=stored.storage_key,
                    byte_size=stored.byte_size,
                    media_type=stored.media_type,
                )
            )

        # 6. macro closure：RawArtifact 先行，再 series → snapshot → observation → link；
        #    最后在同一事务内调用 verify_snapshot_integrity 证明 fingerprint 一致
        #    （不复制 fingerprint 算法）。frozen UUID 原样复现，不回源 World Bank。
        series_repo = MacroSeriesRepository(session)
        snapshot_repo = MacroSnapshotRepository(session)
        observation_repo = MacroObservationRepository(session)
        macro_snapshot_ids: list[UUID] = []
        for macro, stored_artifacts in stored_macros:
            for raw_ref in macro.raw_artifacts:
                stored = stored_artifacts[raw_ref.artifact_id]
                expected_raw = self._macro_raw_artifact_model(raw_ref, stored)
                existing_raw = await raw_repo.get_by_id(raw_ref.artifact_id)
                if existing_raw is None:
                    await raw_repo.insert(expected_raw)
                else:
                    self._verify_raw_artifact(existing_raw, expected_raw)

            expected_series = self._macro_series_model(macro.series_id, macro.series)
            existing_series = await series_repo.get_by_id(macro.series_id)
            if existing_series is None:
                await series_repo.create(expected_series)
            else:
                self._verify_macro_series(existing_series, expected_series)

            expected_snapshot = self._macro_snapshot_model(macro)
            existing_snapshot = await snapshot_repo.get_by_id(macro.snapshot_id)
            if existing_snapshot is None:
                await snapshot_repo.create(expected_snapshot)
            else:
                self._verify_macro_snapshot(existing_snapshot, expected_snapshot)

            new_observations: list[MacroObservationModel] = []
            for obs_ref in macro.observations:
                expected_obs = self._macro_observation_model(macro.snapshot_id, obs_ref)
                existing_obs = await observation_repo.get_by_id(obs_ref.observation_id)
                if existing_obs is None:
                    new_observations.append(expected_obs)
                else:
                    self._verify_macro_observation(existing_obs, expected_obs)
            if new_observations:
                await observation_repo.bulk_create(new_observations)

            for link_ref in macro.artifact_links:
                expected_link = self._macro_artifact_link_model(macro.snapshot_id, link_ref)
                existing_link = await snapshot_repo.get_artifact_link_by_id(
                    link_ref.snapshot_artifact_id
                )
                if existing_link is None:
                    await snapshot_repo.add_artifact_link(expected_link)
                else:
                    self._verify_macro_artifact_link(existing_link, expected_link)

            try:
                await self._macro_service.verify_snapshot_integrity(session, macro.snapshot_id)
            except MacroSnapshotIntegrityError as exc:
                raise EvalReplayIntegrityError(
                    f"macro snapshot rehydration fingerprint 不一致（{macro.snapshot_id}）"
                ) from exc
            macro_snapshot_ids.append(macro.snapshot_id)

        return RehydratedCase(
            company_id=company_id,
            provider_keys=tuple(provider_keys),
            documents=tuple(documents),
            macro_snapshot_ids=tuple(macro_snapshot_ids),
        )

    # ------------------------------------------------------------- model builders

    @staticmethod
    def _provider_model(p: FrozenSourceProviderRef) -> SourceProviderModel:
        return SourceProviderModel(
            provider_key=p.provider_key,
            display_name=p.display_name,
            provider_type=REPLAY_PROVIDER_TYPE,
            authority_tier=REPLAY_PROVIDER_AUTHORITY_TIER,
            homepage_url=REPLAY_PROVIDER_HOMEPAGE_URL,
            allowed_domains=list(REPLAY_PROVIDER_ALLOWED_DOMAINS),
            capabilities=list(p.capabilities),
            acquisition_methods=list(REPLAY_PROVIDER_ACQUISITION_METHODS),
            exchange_scope=list(REPLAY_PROVIDER_EXCHANGE_SCOPE),
            requires_api_key=REPLAY_PROVIDER_REQUIRES_API_KEY,
            critical_claim_eligible=REPLAY_PROVIDER_CRITICAL_CLAIM_ELIGIBLE,
            enabled=p.enabled,
        )

    @staticmethod
    def _company_model(
        company_id: UUID,
        company: FrozenCompanyIdentity,
        identity_provider_key: str,
    ) -> CompanyModel:
        return CompanyModel(
            company_id=company_id,
            exchange=company.exchange,
            security_code=company.security_code,
            identity_key=f"{company.exchange}:{company.security_code}",
            board=company.board,
            official_name=company.official_name,
            short_name=company.short_name or company.security_code,
            listing_status=REPLAY_COMPANY_LISTING_STATUS,
            identity_source_provider_key=identity_provider_key,
            identity_source_url=REPLAY_IDENTITY_SOURCE_URL,
        )

    @staticmethod
    def _raw_artifact_model(
        doc: FrozenDocumentSourceRef, stored: StoredRawArtifact
    ) -> RawArtifactModel:
        return RawArtifactModel(
            artifact_id=doc.raw_artifact_id,
            content_sha256=stored.content_sha256,
            storage_key=stored.storage_key,
            byte_size=stored.byte_size,
            media_type=stored.media_type,
        )

    @staticmethod
    def _source_record_model(
        doc: FrozenDocumentSourceRef,
        company_id: UUID,
        capabilities: list[str],
    ) -> SourceRecordModel:
        return SourceRecordModel(
            source_id=doc.source_record_id,
            company_id=company_id,
            provider_key=doc.provider_key,
            artifact_id=doc.raw_artifact_id,
            document_type=doc.document_type,
            title=doc.title,
            published_at=doc.published_at,
            reporting_period_end=doc.reporting_period_end,
            source_url=doc.source_url,
            acquisition_method=REPLAY_SOURCE_ACQUISITION_METHOD,
            external_document_id=None,
            authority_tier_snapshot=doc.authority_tier_snapshot,
            critical_claim_eligible_snapshot=doc.critical_claim_eligible_snapshot,
            provider_capabilities_snapshot=capabilities,
            status=REPLAY_SOURCE_STATUS,
            acquired_at=doc.acquired_at,
        )

    # ------------------------------------------------------------- macro model builders

    @staticmethod
    def _require_macro_closure(macro: FrozenMacroSnapshotRef) -> None:
        """rehydrator 强制要求 macro closure（series + snapshot 行）；缺失 → reject。"""
        if macro.series is None or macro.snapshot is None:
            raise EvalReplayIntegrityError(
                f"macro snapshot 缺少 rehydration closure（{macro.snapshot_id}）"
            )

    @staticmethod
    def _macro_raw_artifact_model(
        raw_ref: FrozenMacroRawArtifactRef, stored: StoredRawArtifact
    ) -> RawArtifactModel:
        return RawArtifactModel(
            artifact_id=raw_ref.artifact_id,
            content_sha256=stored.content_sha256,
            storage_key=stored.storage_key,
            byte_size=stored.byte_size,
            media_type=stored.media_type,
        )

    @staticmethod
    def _macro_series_model(series_id: UUID, series: FrozenMacroSeriesRef) -> MacroSeriesModel:
        return MacroSeriesModel(
            series_id=series_id,
            provider_key=series.provider_key,
            source_id=series.source_id,
            external_indicator_id=series.external_indicator_id,
            geography_type=series.geography_type,
            geography_code=series.geography_code,
            frequency=series.frequency,
        )

    @staticmethod
    def _macro_snapshot_model(macro: FrozenMacroSnapshotRef) -> MacroDatasetSnapshotModel:
        detail = macro.snapshot
        assert detail is not None  # _require_macro_closure 已保证
        return MacroDatasetSnapshotModel(
            snapshot_id=macro.snapshot_id,
            series_id=macro.series_id,
            snapshot_fingerprint=macro.snapshot_fingerprint,
            fingerprint_version=MACRO_SNAPSHOT_FINGERPRINT_VERSION,
            normalization_version=WORLD_BANK_NORMALIZATION_VERSION,
            requested_country_code=detail.requested_country_code,
            query_start_year=detail.query_start_year,
            query_end_year=detail.query_end_year,
            source_id_snapshot=detail.source_id_snapshot,
            indicator_name=detail.indicator_name,
            indicator_unit=detail.indicator_unit,
            source_name=detail.source_name,
            source_note=detail.source_note,
            source_organization=detail.source_organization,
            topics_snapshot=[
                {"topic_id": topic.topic_id, "name": topic.name} for topic in detail.topics_snapshot
            ],
            provider_country_id=detail.provider_country_id,
            iso2_code=detail.iso2_code,
            iso3_code=detail.iso3_code,
            geography_name=detail.geography_name,
            region_name=detail.region_name,
            income_level_name=detail.income_level_name,
            page=detail.page,
            pages=detail.pages,
            per_page=detail.per_page,
            provider_total=detail.provider_total,
            provider_last_updated=detail.provider_last_updated,
            fetched_at=macro.fetched_at,
            request_count=detail.request_count,
            acquisition_method=detail.acquisition_method,
            authority_tier_snapshot=detail.authority_tier_snapshot,
            critical_claim_eligible_snapshot=detail.critical_claim_eligible_snapshot,
            provider_capabilities_snapshot=list(detail.provider_capabilities_snapshot),
            status=MacroSnapshotStatus.AVAILABLE.value,
        )

    @staticmethod
    def _macro_observation_model(
        snapshot_id: UUID, obs: FrozenMacroObservationRef
    ) -> MacroObservationModel:
        return MacroObservationModel(
            observation_id=obs.observation_id,
            snapshot_id=snapshot_id,
            period=obs.period,
            normalized_period_start=obs.normalized_period_start,
            period_semantics=MacroPeriodSemantics.PROVIDER_YEAR_LABEL.value,
            frequency=MacroFrequency.ANNUAL.value,
            value_numeric=obs.value_numeric,
            is_missing=obs.is_missing,
            observation_status=obs.observation_status,
            decimal_scale=obs.decimal_scale,
        )

    @staticmethod
    def _macro_artifact_link_model(
        snapshot_id: UUID, link: FrozenMacroArtifactLinkRef
    ) -> MacroSnapshotArtifactModel:
        return MacroSnapshotArtifactModel(
            snapshot_artifact_id=link.snapshot_artifact_id,
            snapshot_id=snapshot_id,
            artifact_id=link.artifact_id,
            role=link.role,
            page=link.page,
            response_status=link.response_status,
            final_hostname=link.final_hostname,
            content_type=link.content_type,
            fetched_at=link.fetched_at,
        )

    # ------------------------------------------------------------- create-or-verify

    @staticmethod
    def _verify_provider(existing: SourceProviderModel, expected: SourceProviderModel) -> None:
        if (
            existing.display_name != expected.display_name
            or existing.enabled != expected.enabled
            or list(existing.capabilities) != list(expected.capabilities)
            or existing.provider_type != expected.provider_type
            or existing.authority_tier != expected.authority_tier
            or existing.homepage_url != expected.homepage_url
            or list(existing.allowed_domains) != list(expected.allowed_domains)
            or list(existing.acquisition_methods) != list(expected.acquisition_methods)
            or list(existing.exchange_scope) != list(expected.exchange_scope)
            or existing.requires_api_key != expected.requires_api_key
            or existing.critical_claim_eligible != expected.critical_claim_eligible
        ):
            raise EvalReplayIntegrityError(
                f"provider 已存在但 semantic/脚手架字段不一致（{existing.provider_key}）"
            )

    @staticmethod
    def _verify_company(existing: CompanyModel, expected: CompanyModel) -> None:
        if (
            existing.exchange != expected.exchange
            or existing.security_code != expected.security_code
            or existing.identity_key != expected.identity_key
            or existing.board != expected.board
            or existing.official_name != expected.official_name
            or existing.short_name != expected.short_name
            or existing.listing_status != expected.listing_status
            or existing.identity_source_provider_key != expected.identity_source_provider_key
            or existing.identity_source_url != expected.identity_source_url
        ):
            raise EvalReplayIntegrityError(
                f"company 已存在但 semantic/脚手架字段不一致（{existing.company_id}）"
            )

    @staticmethod
    def _verify_raw_artifact(existing: RawArtifactModel, expected: RawArtifactModel) -> None:
        if (
            existing.artifact_id != expected.artifact_id
            or existing.content_sha256 != expected.content_sha256
            or existing.byte_size != expected.byte_size
            or existing.media_type != expected.media_type
            or existing.storage_key != expected.storage_key
        ):
            raise EvalReplayIntegrityError(
                f"raw_artifact 已存在但字段不一致（{existing.artifact_id}）"
            )

    @staticmethod
    def _verify_source_record(existing: SourceRecordModel, expected: SourceRecordModel) -> None:
        if (
            existing.company_id != expected.company_id
            or existing.provider_key != expected.provider_key
            or existing.artifact_id != expected.artifact_id
            or existing.document_type != expected.document_type
            or existing.title != expected.title
            or existing.source_url != expected.source_url
            or existing.published_at != expected.published_at
            or existing.acquired_at != expected.acquired_at
            or existing.reporting_period_end != expected.reporting_period_end
            or existing.authority_tier_snapshot != expected.authority_tier_snapshot
            or existing.critical_claim_eligible_snapshot
            != expected.critical_claim_eligible_snapshot
            or existing.acquisition_method != expected.acquisition_method
            or existing.status != expected.status
            or list(existing.provider_capabilities_snapshot)
            != list(expected.provider_capabilities_snapshot)
            or existing.external_document_id != expected.external_document_id
        ):
            raise EvalReplayIntegrityError(
                f"source_record 已存在但字段不一致（{existing.source_id}）"
            )

    @staticmethod
    def _verify_macro_series(existing: MacroSeriesModel, expected: MacroSeriesModel) -> None:
        if (
            existing.provider_key != expected.provider_key
            or existing.source_id != expected.source_id
            or existing.external_indicator_id != expected.external_indicator_id
            or existing.geography_type != expected.geography_type
            or existing.geography_code != expected.geography_code
            or existing.frequency != expected.frequency
        ):
            raise EvalReplayIntegrityError(
                f"macro series 已存在但身份字段不一致（{existing.series_id}）"
            )

    @staticmethod
    def _verify_macro_snapshot(
        existing: MacroDatasetSnapshotModel, expected: MacroDatasetSnapshotModel
    ) -> None:
        # semantic identity（snapshot_fingerprint）+ 冻结 fetched_at + replay 脚手架；
        # 其余行级字段的完整证明由 verify_snapshot_integrity 的 fingerprint 重算完成。
        if (
            existing.snapshot_fingerprint != expected.snapshot_fingerprint
            or existing.series_id != expected.series_id
            or existing.fetched_at != expected.fetched_at
            or existing.fingerprint_version != expected.fingerprint_version
            or existing.normalization_version != expected.normalization_version
            or existing.status != expected.status
        ):
            raise EvalReplayIntegrityError(
                f"macro snapshot 已存在但字段不一致（{existing.snapshot_id}）"
            )

    @staticmethod
    def _verify_macro_observation(
        existing: MacroObservationModel, expected: MacroObservationModel
    ) -> None:
        if (
            existing.period != expected.period
            or existing.normalized_period_start != expected.normalized_period_start
            or existing.period_semantics != expected.period_semantics
            or existing.frequency != expected.frequency
            or existing.value_numeric != expected.value_numeric
            or existing.is_missing != expected.is_missing
            or existing.observation_status != expected.observation_status
            or existing.decimal_scale != expected.decimal_scale
        ):
            raise EvalReplayIntegrityError(
                f"macro observation 已存在但字段不一致（{existing.observation_id}）"
            )

    @staticmethod
    def _verify_macro_artifact_link(
        existing: MacroSnapshotArtifactModel, expected: MacroSnapshotArtifactModel
    ) -> None:
        if (
            existing.artifact_id != expected.artifact_id
            or existing.role != expected.role
            or existing.page != expected.page
            or existing.response_status != expected.response_status
            or existing.final_hostname != expected.final_hostname
            or existing.content_type != expected.content_type
            or existing.fetched_at != expected.fetched_at
        ):
            raise EvalReplayIntegrityError(
                f"macro artifact link 已存在但字段不一致（{existing.snapshot_artifact_id}）"
            )

    async def _replay_aliases(
        self,
        session: AsyncSession,
        company_repo: CompanyRepository,
        company_id: UUID,
        company: FrozenCompanyIdentity,
        identity_provider_key: str,
    ) -> None:
        existing_aliases = (
            (
                await session.execute(
                    select(CompanyAliasModel).where(CompanyAliasModel.company_id == company_id)
                )
            )
            .scalars()
            .all()
        )
        by_alias = {a.alias: a for a in existing_aliases}
        by_normalized: dict[str, list[CompanyAliasModel]] = {}
        for a in existing_aliases:
            by_normalized.setdefault(a.normalized_alias, []).append(a)

        for alias in company.aliases:
            normalized = normalize_company_text(alias)
            if alias in by_alias:
                existing_a = by_alias[alias]
                if existing_a.normalized_alias != normalized:
                    raise EvalReplayIntegrityError(
                        f"alias 已存在但 normalized_alias 不一致（{alias}）"
                    )
                continue  # exact replay
            if normalized in by_normalized:
                raise EvalReplayIntegrityError(
                    f"alias normalized identity 冲突（语义不同，{alias}）"
                )
            await company_repo.add_alias(
                CompanyAliasModel(
                    company_id=company_id,
                    alias=alias,
                    normalized_alias=normalized,
                    alias_type=REPLAY_ALIAS_TYPE,
                    source_provider_key=identity_provider_key,
                    source_url=REPLAY_IDENTITY_SOURCE_URL,
                )
            )
