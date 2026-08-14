"""Evaluation snapshot materializer (stage 7B.1.1B).

PG + RawArtifactStore → frozen Evaluation Bundle。从真实 PG rows + content-addressed
raw artifact bytes 加载三路 frozen input，逐条校验后投影为 frozen contracts +
source payloads，交给 7B.1.1A 的 `EvaluationBundleWriter` 落盘。

- **不用 Chroma**：Chroma 是 derived index，不是 frozen source of truth；本阶段
  只冻结原始 source（document bytes / macro snapshot / structured artifact）。
- 0 LLM / 0 Chroma / 0 network / 0 Alembic / 0 token capture / 0 variant runner。
- 校验边界：
  - document：company 归属 + no-lookahead（复用 `resolve_availability`，
    绝不用 reporting_period_end）+ raw bytes 重新 SHA-256（防篡改）；
  - macro：`MacroPersistenceService.verify_snapshot_integrity`（结构不变量 +
    fingerprint 重算防篡改）+ no-lookahead（fetched_at <= analysis_as_of）；
  - financial / valuation observation：重算 fingerprint 并比对 persisted 列
    （防篡改）+ company 归属；
  - valuation comparison：`RelativeValuationComparisonService.
    verify_comparison_integrity`（deep closure，不复制 formula）+ company 归属。
- raw bytes 在 DB session 关闭后重新 SHA-256 校验，再交给 writer（fail-fast）。
"""

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.claims.macro_policy import resolve_availability
from app.core.errors import RawArtifactNotFound
from app.db.models.company_alias import CompanyAliasModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.eval.bundle.writer import EvaluationBundleWriter
from app.eval.contracts import (
    EvalCase,
    EvalDatasetCaseRef,
    EvalDatasetManifest,
    FrozenCompanyIdentity,
    FrozenDocumentSourceRef,
    FrozenMacroArtifactLinkRef,
    FrozenMacroObservationRef,
    FrozenMacroRawArtifactRef,
    FrozenMacroSeriesRef,
    FrozenMacroSnapshotDetail,
    FrozenMacroSnapshotRef,
    FrozenMacroTopicRef,
    FrozenSourceProviderRef,
    FrozenSourceSnapshot,
    FrozenStructuredArtifactRef,
    StructuredArtifactType,
)
from app.eval.errors import EvalMaterializationError
from app.eval.fingerprints import (
    compute_eval_case_fingerprint,
    compute_source_snapshot_fingerprint,
)
from app.eval.materialization.contracts import (
    EvalCaseMaterializationSpec,
    MaterializedEvalCase,
    StructuredArtifactSelection,
)
from app.eval.materialization.projections import (
    build_comparison_payload,
    build_financial_metric_payload,
    build_macro_payload,
    build_valuation_observation_payload,
    payload_sha256,
)
from app.eval.materialization.provenance import (
    build_evidence_match,
    build_observation_provenance,
    build_observation_source_provenance,
    build_replay_evidence,
)
from app.evidence.contracts import EvidenceOrigin
from app.financial.contracts import compute_metric_fingerprint
from app.financial.number_parser import normalize_value_cny, parse_financial_number
from app.macro.errors import MacroSnapshotIntegrityError
from app.repositories.company_repository import CompanyRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.financial_metric_observation_repository import (
    FinancialMetricObservationRepository,
)
from app.repositories.macro_observation_repository import MacroObservationRepository
from app.repositories.macro_series_repository import MacroSeriesRepository
from app.repositories.macro_snapshot_repository import MacroSnapshotRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.repositories.valuation_metric_observation_repository import (
    ValuationMetricObservationRepository,
)
from app.services.macro_persistence_service import MacroPersistenceService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.comparison_service import (
    RelativeValuationComparisonService,
    VerifiedComparison,
)
from app.valuation.contracts import compute_valuation_observation_fingerprint
from app.valuation.errors import ValuationIntegrityError
from app.valuation.number_parser import parse_valuation_number


class EvaluationSnapshotMaterializer:
    """PG + RawArtifactStore → frozen Evaluation Bundle。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        raw_store: LocalRawArtifactStore,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store
        self._macro_service = MacroPersistenceService(sessionmaker, raw_store)
        self._comparison_service = RelativeValuationComparisonService(sessionmaker)

    async def materialize_case(self, spec: EvalCaseMaterializationSpec) -> MaterializedEvalCase:
        # 阶段一：单 DB session 内加载 + 校验 + 投影 payload（不碰文件）。
        async with self._sessionmaker() as session:
            company_identity = await self._materialize_company(session, spec)
            provider_refs = await self._materialize_providers(session)
            document_refs, blob_sources = await self._materialize_documents(session, spec)
            macro_refs, macro_payloads, macro_blob_sources = await self._materialize_macros(
                session, spec
            )
            structured_refs, structured_payloads = await self._materialize_structured(session, spec)

        # 阶段二：DB session 关闭后，重新 SHA-256 校验 raw bytes（防篡改）。
        document_blobs = self._read_blobs(blob_sources)
        macro_raw_blobs = self._read_blobs(macro_blob_sources)

        # 阶段三：组装 frozen contracts（duplicate identity 显式拒绝，稳定错误）。
        self._reject_duplicates(document_refs, macro_refs, structured_refs)
        snapshot = FrozenSourceSnapshot(
            document_sources=tuple(document_refs),
            macro_snapshots=tuple(macro_refs),
            structured_artifacts=tuple(structured_refs),
            source_providers=tuple(provider_refs),
        )
        case = EvalCase(
            case_id=spec.case_id,
            case_version=spec.case_version,
            company_id=spec.company_id,
            company=company_identity,
            research_question=spec.research_question,
            analysis_as_of=spec.analysis_as_of,
            tags=spec.tags,
            source_snapshot_fingerprint=compute_source_snapshot_fingerprint(snapshot),
            human_label_fingerprint=spec.human_label_fingerprint,
        )
        return MaterializedEvalCase(
            case=case,
            snapshot=snapshot,
            document_blobs=document_blobs,
            macro_payloads=macro_payloads,
            macro_raw_blobs=macro_raw_blobs,
            structured_payloads=structured_payloads,
        )

    # ------------------------------------------------------------ company / provider 路

    async def _materialize_company(
        self, session: AsyncSession, spec: EvalCaseMaterializationSpec
    ) -> FrozenCompanyIdentity:
        company = await CompanyRepository(session).get_by_id(spec.company_id)
        if company is None:
            raise EvalMaterializationError("company not found")
        if company.security_code != spec.security_code:
            raise EvalMaterializationError("company security_code mismatch")
        result = await session.execute(
            select(CompanyAliasModel).where(CompanyAliasModel.company_id == spec.company_id)
        )
        aliases = sorted({row.alias for row in result.scalars().all()})
        return FrozenCompanyIdentity(
            security_code=company.security_code,
            official_name=company.official_name,
            short_name=company.short_name,
            exchange=company.exchange,
            board=company.board,
            aliases=tuple(aliases),
        )

    async def _materialize_providers(self, session: AsyncSession) -> list[FrozenSourceProviderRef]:
        providers = await SourceProviderRepository(session).list_providers(
            authority_tier=None,
            capability=None,
            acquisition_method=None,
            exchange=None,
            enabled_only=False,
        )
        return [
            FrozenSourceProviderRef(
                provider_key=p.provider_key,
                display_name=p.display_name,
                enabled=p.enabled,
                capabilities=tuple(p.capabilities),
            )
            for p in providers
        ]

    # ------------------------------------------------------------ document 路

    async def _materialize_documents(
        self, session: AsyncSession, spec: EvalCaseMaterializationSpec
    ) -> tuple[list[FrozenDocumentSourceRef], list[tuple[str, str]]]:
        refs: list[FrozenDocumentSourceRef] = []
        blob_sources: list[tuple[str, str]] = []
        for source_id in spec.document_source_ids:
            source = await SourceRecordRepository(session).get_by_id(source_id)
            if source is None:
                raise EvalMaterializationError("document source not found")
            if source.company_id != spec.company_id:
                raise EvalMaterializationError("document source company mismatch")
            artifact = await RawArtifactRepository(session).get_by_id(source.artifact_id)
            if artifact is None:
                raise EvalMaterializationError("document raw artifact not found")
            availability = resolve_availability(
                origin_type=EvidenceOrigin.DOCUMENT_CHUNK.value,
                snapshot_fetched_at=None,
                source_published_at=source.published_at,
                source_acquired_at=source.acquired_at,
            )
            if availability is None:
                raise EvalMaterializationError(
                    "document availability missing (no published/acquired time)"
                )
            if availability.date() > spec.analysis_as_of.date():
                raise EvalMaterializationError("document is future evidence (no-lookahead)")
            refs.append(
                FrozenDocumentSourceRef(
                    source_record_id=source.source_id,
                    raw_artifact_id=artifact.artifact_id,
                    content_sha256=artifact.content_sha256,
                    provider_key=source.provider_key,
                    document_type=source.document_type,
                    media_type=artifact.media_type,
                    title=source.title,
                    source_url=source.source_url,
                    acquired_at=source.acquired_at,
                    authority_tier_snapshot=source.authority_tier_snapshot,
                    critical_claim_eligible_snapshot=source.critical_claim_eligible_snapshot,
                    published_at=source.published_at,
                    reporting_period_start=None,
                    reporting_period_end=source.reporting_period_end,
                )
            )
            blob_sources.append((artifact.content_sha256, artifact.storage_key))
        return refs, blob_sources

    # ------------------------------------------------------------ macro 路

    async def _materialize_macros(
        self, session: AsyncSession, spec: EvalCaseMaterializationSpec
    ) -> tuple[list[FrozenMacroSnapshotRef], dict[str, dict], list[tuple[str, str]]]:
        refs: list[FrozenMacroSnapshotRef] = []
        payloads: dict[str, dict] = {}
        macro_blob_sources: list[tuple[str, str]] = []
        snapshot_repo = MacroSnapshotRepository(session)
        raw_repo = RawArtifactRepository(session)
        for snapshot_id in spec.macro_snapshot_ids:
            try:
                snapshot = await self._macro_service.verify_snapshot_integrity(session, snapshot_id)
            except MacroSnapshotIntegrityError as exc:
                raise EvalMaterializationError(
                    "macro snapshot integrity verification failed"
                ) from exc
            if snapshot is None:
                raise EvalMaterializationError("macro snapshot not found")
            if snapshot.fetched_at.date() > spec.analysis_as_of.date():
                raise EvalMaterializationError("macro snapshot is future evidence (no-lookahead)")
            series = await MacroSeriesRepository(session).get_by_id(snapshot.series_id)
            if series is None:
                raise EvalMaterializationError("macro series not found")
            observations = await MacroObservationRepository(session).list_for_snapshot(
                snapshot.snapshot_id
            )
            links = await snapshot_repo.list_artifact_links(snapshot.snapshot_id)
            raw_rows: dict[UUID, RawArtifactModel] = {}
            for link in links:
                artifact = await raw_repo.get_by_id(link.artifact_id)
                if artifact is None:
                    raise EvalMaterializationError("macro raw artifact not found")
                raw_rows[link.artifact_id] = artifact
            payload = build_macro_payload(snapshot, series, observations)
            fingerprint = snapshot.snapshot_fingerprint
            refs.append(
                FrozenMacroSnapshotRef(
                    snapshot_id=snapshot.snapshot_id,
                    series_id=snapshot.series_id,
                    snapshot_fingerprint=fingerprint,
                    payload_sha256=payload_sha256(payload),
                    fetched_at=snapshot.fetched_at,
                    series=self._project_macro_series(series),
                    snapshot=self._project_macro_snapshot_detail(snapshot),
                    observations=tuple(self._project_macro_observation(o) for o in observations),
                    artifact_links=tuple(self._project_macro_artifact_link(link) for link in links),
                    raw_artifacts=tuple(
                        FrozenMacroRawArtifactRef(
                            artifact_id=artifact.artifact_id,
                            content_sha256=artifact.content_sha256,
                            media_type=artifact.media_type,
                            byte_size=artifact.byte_size,
                        )
                        for artifact in raw_rows.values()
                    ),
                )
            )
            payloads[fingerprint] = payload
            for artifact in raw_rows.values():
                macro_blob_sources.append((artifact.content_sha256, artifact.storage_key))
        return refs, payloads, macro_blob_sources

    # ------------------------------------------------------------ macro 投影

    @staticmethod
    def _project_macro_series(series: MacroSeriesModel) -> FrozenMacroSeriesRef:
        return FrozenMacroSeriesRef(
            provider_key=series.provider_key,
            source_id=series.source_id,
            external_indicator_id=series.external_indicator_id,
            geography_type=series.geography_type,
            geography_code=series.geography_code,
            frequency=series.frequency,
        )

    @staticmethod
    def _project_macro_snapshot_detail(
        snapshot: MacroDatasetSnapshotModel,
    ) -> FrozenMacroSnapshotDetail:
        return FrozenMacroSnapshotDetail(
            requested_country_code=snapshot.requested_country_code,
            query_start_year=snapshot.query_start_year,
            query_end_year=snapshot.query_end_year,
            source_id_snapshot=snapshot.source_id_snapshot,
            indicator_name=snapshot.indicator_name,
            indicator_unit=snapshot.indicator_unit,
            source_name=snapshot.source_name,
            source_note=snapshot.source_note,
            source_organization=snapshot.source_organization,
            topics_snapshot=tuple(
                FrozenMacroTopicRef(topic_id=topic["topic_id"], name=topic["name"])
                for topic in snapshot.topics_snapshot
            ),
            provider_country_id=snapshot.provider_country_id,
            iso2_code=snapshot.iso2_code,
            iso3_code=snapshot.iso3_code,
            geography_name=snapshot.geography_name,
            region_name=snapshot.region_name,
            income_level_name=snapshot.income_level_name,
            page=snapshot.page,
            pages=snapshot.pages,
            per_page=snapshot.per_page,
            provider_total=snapshot.provider_total,
            provider_last_updated=snapshot.provider_last_updated,
            request_count=snapshot.request_count,
            acquisition_method=snapshot.acquisition_method,
            authority_tier_snapshot=snapshot.authority_tier_snapshot,
            critical_claim_eligible_snapshot=snapshot.critical_claim_eligible_snapshot,
            provider_capabilities_snapshot=tuple(snapshot.provider_capabilities_snapshot),
            fingerprint_version=snapshot.fingerprint_version,
            normalization_version=snapshot.normalization_version,
            status=snapshot.status,
        )

    @staticmethod
    def _project_macro_observation(
        observation: MacroObservationModel,
    ) -> FrozenMacroObservationRef:
        return FrozenMacroObservationRef(
            observation_id=observation.observation_id,
            period=observation.period,
            normalized_period_start=observation.normalized_period_start,
            value_numeric=observation.value_numeric,
            is_missing=observation.is_missing,
            decimal_scale=observation.decimal_scale,
            observation_status=observation.observation_status,
            period_semantics=observation.period_semantics,
            frequency=observation.frequency,
        )

    @staticmethod
    def _project_macro_artifact_link(
        link: MacroSnapshotArtifactModel,
    ) -> FrozenMacroArtifactLinkRef:
        return FrozenMacroArtifactLinkRef(
            snapshot_artifact_id=link.snapshot_artifact_id,
            artifact_id=link.artifact_id,
            role=link.role,
            page=link.page,
            response_status=link.response_status,
            final_hostname=link.final_hostname,
            content_type=link.content_type,
            fetched_at=link.fetched_at,
        )

    # ------------------------------------------------------------ structured 路

    async def _materialize_structured(
        self, session: AsyncSession, spec: EvalCaseMaterializationSpec
    ) -> tuple[list[FrozenStructuredArtifactRef], dict[tuple[StructuredArtifactType, str], dict]]:
        refs: list[FrozenStructuredArtifactRef] = []
        payloads: dict[tuple[StructuredArtifactType, str], dict] = {}
        for sel in spec.structured_artifacts:
            if sel.artifact_type == StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION:
                ref, payload = await self._materialize_financial(session, spec, sel)
            elif sel.artifact_type == StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION:
                ref, payload = await self._materialize_valuation_observation(session, spec, sel)
            else:
                ref, payload = await self._materialize_comparison(session, spec, sel)
            refs.append(ref)
            payloads[(ref.artifact_type, ref.artifact_fingerprint)] = payload
        return refs, payloads

    async def _materialize_financial(
        self,
        session: AsyncSession,
        spec: EvalCaseMaterializationSpec,
        sel: StructuredArtifactSelection,
    ) -> tuple[FrozenStructuredArtifactRef, dict]:
        row = await FinancialMetricObservationRepository(session).get_by_id(sel.artifact_id)
        if row is None:
            raise EvalMaterializationError("financial metric observation not found")
        if row.company_id != spec.company_id:
            raise EvalMaterializationError("financial metric observation company mismatch")
        # 从 source_value_text + raw_unit 重算（而非直接用 NUMERIC(38,12) 列）：
        # 指纹在写入时基于 parse_financial_number 的 Decimal（无尾零），而 DB
        # round-trip 会补足 12 位 scale，直接用列值会破坏 fingerprint 一致性。
        raw_value = parse_financial_number(row.source_value_text)
        normalized_value_cny = normalize_value_cny(raw_value, row.raw_unit)
        fingerprint = compute_metric_fingerprint(
            metric_schema_version=row.metric_schema_version,
            company_id=row.company_id,
            source_evidence_card_id=row.source_evidence_card_id,
            metric_code=row.metric_code,
            statement_scope=row.statement_scope,
            period_start=row.period_start,
            period_end=row.period_end,
            period_kind=row.period_kind,
            source_value_text=row.source_value_text,
            raw_value=raw_value,
            raw_unit=row.raw_unit,
            normalized_value_cny=normalized_value_cny,
        )
        if fingerprint != row.metric_fingerprint:
            raise EvalMaterializationError(
                "financial metric observation fingerprint mismatch (tampered)"
            )
        provenance = build_observation_source_provenance(
            build_evidence_match(
                *await self._load_evidence_context(session, row.source_evidence_card_id)
            )
        )
        payload = build_financial_metric_payload(row, fingerprint, provenance)
        ref = FrozenStructuredArtifactRef(
            artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
            artifact_id=row.metric_observation_id,
            artifact_fingerprint=fingerprint,
            payload_sha256=payload_sha256(payload),
        )
        return ref, payload

    async def _materialize_valuation_observation(
        self,
        session: AsyncSession,
        spec: EvalCaseMaterializationSpec,
        sel: StructuredArtifactSelection,
    ) -> tuple[FrozenStructuredArtifactRef, dict]:
        row = await ValuationMetricObservationRepository(session).get_by_id(sel.artifact_id)
        if row is None:
            raise EvalMaterializationError("valuation observation not found")
        if row.company_id != spec.company_id:
            raise EvalMaterializationError("valuation observation company mismatch")
        # 同 financial：从 source_value_text 重算 Decimal，避免 NUMERIC(38,12)
        # scale 补足破坏 fingerprint 一致性。
        metric_value = parse_valuation_number(row.source_value_text)
        fingerprint = compute_valuation_observation_fingerprint(
            valuation_observation_schema_version=row.valuation_observation_schema_version,
            company_id=row.company_id,
            source_evidence_card_id=row.source_evidence_card_id,
            metric_code=row.metric_code,
            metric_as_of=row.metric_as_of,
            source_value_text=row.source_value_text,
            metric_value=metric_value,
        )
        if fingerprint != row.valuation_observation_fingerprint:
            raise EvalMaterializationError("valuation observation fingerprint mismatch (tampered)")
        provenance = build_observation_source_provenance(
            build_evidence_match(
                *await self._load_evidence_context(session, row.source_evidence_card_id)
            )
        )
        payload = build_valuation_observation_payload(row, fingerprint, provenance)
        ref = FrozenStructuredArtifactRef(
            artifact_type=StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION,
            artifact_id=row.valuation_observation_id,
            artifact_fingerprint=fingerprint,
            payload_sha256=payload_sha256(payload),
        )
        return ref, payload

    async def _materialize_comparison(
        self,
        session: AsyncSession,
        spec: EvalCaseMaterializationSpec,
        sel: StructuredArtifactSelection,
    ) -> tuple[FrozenStructuredArtifactRef, dict]:
        try:
            verified = await self._comparison_service.verify_comparison_integrity(
                session, sel.artifact_id
            )
        except ValuationIntegrityError as exc:
            raise EvalMaterializationError(
                "valuation comparison integrity verification failed"
            ) from exc
        if verified is None:
            raise EvalMaterializationError("valuation comparison not found")
        if verified.target_company_id != spec.company_id:
            raise EvalMaterializationError("valuation comparison company mismatch")
        provenance = await self._comparison_provenance(session, verified)
        payload = build_comparison_payload(verified, provenance)
        ref = FrozenStructuredArtifactRef(
            artifact_type=StructuredArtifactType.RELATIVE_VALUATION_COMPARISON,
            artifact_id=verified.comparison_id,
            artifact_fingerprint=verified.comparison_fingerprint,
            payload_sha256=payload_sha256(payload),
        )
        return ref, payload

    # ------------------------------------------------------------ structured provenance

    async def _load_evidence_context(
        self, session: AsyncSession, evidence_card_id: UUID
    ) -> tuple[EvidenceCardModel, SourceRecordModel, RawArtifactModel]:
        """加载 EvidenceCard + SourceRecord + RawArtifact（structured provenance 用）。

        structured observation 的 source evidence 必须是 document-origin（创建时
        锁定）；缺失 / 非 document → `EvalMaterializationError`（不能构建语义
        provenance，fail-fast 而非冻结不完整引用）。
        """
        card = await EvidenceCardRepository(session).get_by_id(evidence_card_id)
        if card is None or card.source_id is None:
            raise EvalMaterializationError(
                "structured artifact source evidence card missing or not document-origin"
            )
        source = await SourceRecordRepository(session).get_by_id(card.source_id)
        if source is None:
            raise EvalMaterializationError("structured artifact source record missing")
        artifact = await RawArtifactRepository(session).get_by_id(source.artifact_id)
        if artifact is None:
            raise EvalMaterializationError("structured artifact source raw artifact missing")
        return card, source, artifact

    async def _comparison_provenance(
        self, session: AsyncSession, verified: VerifiedComparison
    ) -> dict:
        """comparison provenance：target observation（match）+ peer observations
        （replay，含 peer 公司身份）+ peer 公司语义身份。"""
        obs_repo = ValuationMetricObservationRepository(session)
        target_obs = await obs_repo.get_by_id(verified.target_observation_id)
        if target_obs is None:
            raise EvalMaterializationError("comparison target observation not found")
        target_evidence = build_evidence_match(
            *await self._load_evidence_context(session, target_obs.source_evidence_card_id)
        )
        target_provenance = build_observation_provenance(
            metric_code=target_obs.metric_code,
            metric_as_of=target_obs.metric_as_of.isoformat(),
            source_value_text=target_obs.source_value_text,
            metric_value=str(target_obs.metric_value),
            evidence=target_evidence,
        )
        peer_provenances: list[dict] = []
        peer_company_refs: list[dict] = []
        for obs_id in verified.peer_observation_ids:
            peer_obs = await obs_repo.get_by_id(obs_id)
            if peer_obs is None:
                raise EvalMaterializationError("comparison peer observation not found")
            card, source, artifact = await self._load_evidence_context(
                session, peer_obs.source_evidence_card_id
            )
            company = await CompanyRepository(session).get_by_id(peer_obs.company_id)
            if company is None:
                raise EvalMaterializationError("comparison peer company not found")
            replay = build_replay_evidence(card, source, artifact, company)
            peer_provenances.append(
                build_observation_provenance(
                    metric_code=peer_obs.metric_code,
                    metric_as_of=peer_obs.metric_as_of.isoformat(),
                    source_value_text=peer_obs.source_value_text,
                    metric_value=str(peer_obs.metric_value),
                    evidence=replay,
                )
            )
            peer_company_refs.append(
                {
                    "exchange": company.exchange,
                    "security_code": company.security_code,
                    "official_name": company.official_name,
                    "short_name": company.short_name,
                    "board": company.board,
                }
            )
        return {
            "schema_version": 1,
            "target_observation": target_provenance,
            "peer_observations": tuple(
                sorted(peer_provenances, key=lambda item: item["source_value_text"])
            ),
            "peer_companies": tuple(
                sorted(peer_company_refs, key=lambda item: item["security_code"])
            ),
        }

    # ------------------------------------------------------------ 字节校验

    def _read_blobs(self, blob_sources: list[tuple[str, str]]) -> dict[str, bytes]:
        """content_sha256 → raw bytes（content-addressed 去重；重新 SHA-256 防篡改）。"""
        blobs: dict[str, bytes] = {}
        for content_sha256, storage_key in blob_sources:
            if content_sha256 in blobs:
                continue
            try:
                with self._raw_store.open(storage_key) as handle:
                    content = handle.read()
            except (RawArtifactNotFound, OSError) as exc:
                raise EvalMaterializationError("raw bytes unreadable") from exc
            if hashlib.sha256(content).hexdigest() != content_sha256:
                raise EvalMaterializationError("raw bytes tampered (content_sha256 mismatch)")
            blobs[content_sha256] = content
        return blobs

    # ------------------------------------------------------------ 组装 / 落盘

    @staticmethod
    def _reject_duplicates(
        document_refs: list[FrozenDocumentSourceRef],
        macro_refs: list[FrozenMacroSnapshotRef],
        structured_refs: list[FrozenStructuredArtifactRef],
    ) -> None:
        seen_doc: set[str] = set()
        for ref in document_refs:
            if ref.content_sha256 in seen_doc:
                raise EvalMaterializationError("duplicate document content_sha256 in selection")
            seen_doc.add(ref.content_sha256)
        seen_macro: set[str] = set()
        for ref in macro_refs:
            if ref.snapshot_fingerprint in seen_macro:
                raise EvalMaterializationError("duplicate macro snapshot_fingerprint in selection")
            seen_macro.add(ref.snapshot_fingerprint)
        seen_art: set[tuple[StructuredArtifactType, str]] = set()
        for ref in structured_refs:
            key = (ref.artifact_type, ref.artifact_fingerprint)
            if key in seen_art:
                raise EvalMaterializationError("duplicate structured artifact in selection")
            seen_art.add(key)

    @staticmethod
    def write_materialized(
        materialized: MaterializedEvalCase, writer: EvaluationBundleWriter
    ) -> None:
        """把 materialize_case 的产物写入 Evaluation Bundle（不查 DB / 不碰文件 store）。"""
        writer.write_case(materialized.case)
        writer.write_snapshot(materialized.snapshot)
        for ref in materialized.snapshot.document_sources:
            writer.write_document_blob(
                ref.content_sha256, materialized.document_blobs[ref.content_sha256]
            )
        for ref in materialized.snapshot.macro_snapshots:
            writer.write_macro_payload(ref, materialized.macro_payloads[ref.snapshot_fingerprint])
            for raw_ref in ref.raw_artifacts:
                writer.write_document_blob(
                    raw_ref.content_sha256, materialized.macro_raw_blobs[raw_ref.content_sha256]
                )
        for ref in materialized.snapshot.structured_artifacts:
            writer.write_structured_payload(
                ref,
                materialized.structured_payloads[(ref.artifact_type, ref.artifact_fingerprint)],
            )

    @staticmethod
    def assemble_dataset_manifest(
        dataset_id: str,
        dataset_version: int,
        cases: list[MaterializedEvalCase],
        description: str | None = None,
    ) -> EvalDatasetManifest:
        """从 materialized cases 组装 dataset manifest（case_fingerprint 由 EvalCase 派生）。"""
        return EvalDatasetManifest(
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            cases=tuple(
                EvalDatasetCaseRef(
                    case_id=m.case.case_id,
                    case_version=m.case.case_version,
                    case_fingerprint=compute_eval_case_fingerprint(m.case),
                )
                for m in cases
            ),
            description=description,
        )
