"""Structured evidence remap service (stage 7B.1.4C.3).

Frozen structured artifact → **当前 attempt** 的 deterministic structured state：

1. **envelope 校验**：frozen payload 的 artifact_type / artifact_fingerprint 与
   ref 逐字节一致（篡改 → `EvalRemapError`）；
2. **evidence 匹配**（target-company 观测）：按 frozen semantic provenance
   （source document content_sha256 + evidence statement + quote）定位 attempt
   重新生成的 EvidenceCard——**不 seed 历史卡**；0 命中 / 歧义 → 稳定 fail-fast；
3. **observation 重建**：frozen source_value_text + raw_unit → Decimal →
   fingerprint 重算（只换新 evidence card id）→ create_or_get（fingerprint
   replay 幂等）；
4. **comparison 重建**：target observation 走 evidence 匹配；peer observation
   走 **replay scaffold**（peer 公司不在 frozen snapshot，无文档可重新提取——
   用 frozen 完整证据内容 + 确定性脚手架重建，`extractor_name=eval_replay_peer_v1`
   明确身份，仅供 comparison 校验引用，不进入 target 证据链）；peer 公司按
   (exchange, security_code) create-or-get。

所有新 ID 用 `uuid5` 从语义字段确定性派生（同语义 → 同 ID → 重放稳定）；
全部 fingerprint 用生产 domain 函数重算（不复制算法）。
"""

import hashlib
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.chunk_set import ChunkSetModel
from app.db.models.company import CompanyModel
from app.db.models.document_chunk import DocumentChunkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.relative_valuation_comparison import RelativeValuationComparisonModel
from app.db.models.relative_valuation_comparison_peer import (
    RelativeValuationComparisonPeerModel,
)
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.db.models.valuation_metric_observation import ValuationMetricObservationModel
from app.eval.bundle.loader import EvaluationBundleLoader, LoadedEvalExecutionCase
from app.eval.contracts import (
    StructuredArtifactType,
    _is_sha256_hex,
)
from app.eval.errors import EvalRemapError
from app.eval.remap.contracts import (
    RemappedObservation,
    StructuredRemapResult,
    _RemapAccumulator,
)
from app.eval.replay.contracts import (
    REPLAY_COMPANY_LISTING_STATUS,
    REPLAY_IDENTITY_SOURCE_URL,
    REPLAY_SOURCE_ACQUISITION_METHOD,
    REPLAY_SOURCE_STATUS,
)
from app.financial.contracts import compute_metric_fingerprint
from app.financial.number_parser import normalize_value_cny, parse_financial_number
from app.repositories.company_repository import CompanyRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.financial_metric_observation_repository import (
    FinancialMetricObservationRepository,
)
from app.repositories.relative_valuation_comparison_peer_repository import (
    RelativeValuationComparisonPeerRepository,
)
from app.repositories.relative_valuation_comparison_repository import (
    RelativeValuationComparisonRepository,
)
from app.repositories.valuation_metric_observation_repository import (
    ValuationMetricObservationRepository,
)
from app.valuation.contracts import (
    RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION,
    VALUATION_OBSERVATION_SCHEMA_VERSION,
    compute_comparison_fingerprint,
    compute_valuation_observation_fingerprint,
)

# replay scaffold 的身份标记（peer evidence 是确定性重建，非 attempt 提取）。
_REPLAY_EXTRACTOR_NAME = "eval_replay_peer_v1"
_REPLAY_PARSER_NAME = "eval_replay_peer_v1"
_REPLAY_CHUNKER_NAME = "eval_replay_peer_v1"


def _replay_uuid(scope: str, *parts: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, "|".join((scope, *parts)))


def _replay_sha256(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value is not None else None


class StructuredEvidenceRemapService:
    """frozen structured payload → attempt PG（evidence 匹配 + deterministic 重建）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        bundle_loader: EvaluationBundleLoader,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._loader = bundle_loader

    async def remap_case(self, execution_case: LoadedEvalExecutionCase) -> StructuredRemapResult:
        """remap 本 case 的全部 frozen structured artifact 到 attempt PG。"""
        refs = execution_case.snapshot.structured_artifacts
        if not refs:
            return StructuredRemapResult()
        payloads: list[tuple[Any, dict]] = []
        for ref in refs:
            payload = self._loader.load_structured_payload(
                ref.artifact_type, ref.artifact_fingerprint
            )
            self._validate_envelope(ref, payload)
            payloads.append((ref, payload))

        accumulator = _RemapAccumulator()
        # 本 attempt 内 observation 去重（comparison 的 target/peer 可能独立出现）。
        observation_ids: dict[tuple[str, str, str], tuple[UUID, str]] = {}
        async with self._sessionmaker() as session:
            # pass 1：financial + valuation observations（evidence 匹配 attempt 新卡）。
            for ref, payload in payloads:
                if ref.artifact_type == StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION:
                    await self._remap_financial(session, ref, payload, accumulator)
                elif ref.artifact_type == StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION:
                    await self._remap_valuation_observation(
                        session, ref, payload, accumulator, observation_ids
                    )
            # pass 2：comparison（依赖 pass 1 的 target observation）。
            for ref, payload in payloads:
                if ref.artifact_type == StructuredArtifactType.RELATIVE_VALUATION_COMPARISON:
                    await self._remap_comparison(
                        session, ref, payload, accumulator, observation_ids
                    )
            await session.commit()
        return accumulator.result()

    # ------------------------------------------------------------ envelope / evidence 匹配

    @staticmethod
    def _validate_envelope(ref, payload: dict) -> None:
        if payload.get("artifact_type") != ref.artifact_type.value:
            raise EvalRemapError("structured payload envelope artifact_type 不匹配")
        if payload.get("artifact_fingerprint") != ref.artifact_fingerprint:
            raise EvalRemapError("structured payload envelope artifact_fingerprint 不匹配")
        if "provenance" not in payload:
            raise EvalRemapError("structured payload 缺 provenance（请用当前版本重新 materialize）")

    async def _resolve_evidence_card(self, session: AsyncSession, match: dict) -> UUID:
        """按 frozen semantic provenance 定位 attempt 重新生成的 EvidenceCard。

        主键 = source document content_sha256（与 snapshot 同语义）；evidence
        statement / quote 用于消歧（同一 source 多张卡时精确命中）。0 命中 →
        `EvalRemapError`（frozen 观测的 source 未在本 attempt 重建）；歧义 →
        `EvalRemapError`（不静默选第一张）。
        """
        content_sha256 = match.get("content_sha256")
        if not content_sha256 or not _is_sha256_hex(content_sha256):
            raise EvalRemapError("provenance content_sha256 缺失或非法")
        stmt = (
            select(EvidenceCardModel.evidence_card_id)
            .join(SourceRecordModel, SourceRecordModel.source_id == EvidenceCardModel.source_id)
            .join(RawArtifactModel, RawArtifactModel.artifact_id == SourceRecordModel.artifact_id)
            .where(RawArtifactModel.content_sha256 == content_sha256)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            raise EvalRemapError(
                "无法定位 attempt 重新生成的 evidence card"
                f"（content_sha256={content_sha256[:12]}…）"
            )
        if len(rows) == 1:
            return rows[0]
        # 多卡：按 evidence_statement 精确消歧。
        statement = match.get("evidence_statement")
        quote = match.get("quote_text")
        candidates = rows
        if statement:
            stmt2 = select(EvidenceCardModel.evidence_card_id).where(
                EvidenceCardModel.evidence_card_id.in_(rows),
                EvidenceCardModel.evidence_statement == statement,
            )
            narrowed = list((await session.execute(stmt2)).scalars().all())
            if narrowed:
                candidates = narrowed
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1 and quote:
            stmt3 = select(EvidenceCardModel.evidence_card_id).where(
                EvidenceCardModel.evidence_card_id.in_(candidates),
                EvidenceCardModel.quote_text == quote,
            )
            narrowed = list((await session.execute(stmt3)).scalars().all())
            if len(narrowed) == 1:
                return narrowed[0]
        raise EvalRemapError(
            f"evidence card 匹配歧义（source 多张卡无法按 provenance 消歧：{content_sha256[:12]}…）"
        )

    # ------------------------------------------------------------ financial observation

    async def _remap_financial(
        self,
        session: AsyncSession,
        ref,
        payload: dict,
        acc: _RemapAccumulator,
    ) -> None:
        provenance = payload["provenance"]
        match = provenance.get("source_evidence")
        if not isinstance(match, dict):
            raise EvalRemapError("financial observation provenance.source_evidence 缺失")
        card_id = await self._resolve_evidence_card(session, match)
        raw_value = parse_financial_number(payload["source_value_text"])
        normalized_value_cny = normalize_value_cny(raw_value, payload["raw_unit"])
        fingerprint = compute_metric_fingerprint(
            metric_schema_version=int(payload["metric_schema_version"]),
            company_id=UUID(payload["company_id"]),
            source_evidence_card_id=card_id,
            metric_code=payload["metric_code"],
            statement_scope=payload["statement_scope"],
            period_start=_parse_date(payload.get("period_start")),
            period_end=_parse_date(payload["period_end"]),
            period_kind=payload["period_kind"],
            source_value_text=payload["source_value_text"],
            raw_value=raw_value,
            raw_unit=payload["raw_unit"],
            normalized_value_cny=normalized_value_cny,
        )
        row, created = await FinancialMetricObservationRepository(session).create_or_get(
            FinancialMetricObservationModel(
                metric_observation_id=uuid.uuid4(),
                company_id=UUID(payload["company_id"]),
                source_evidence_card_id=card_id,
                metric_code=payload["metric_code"],
                statement_scope=payload["statement_scope"],
                period_start=_parse_date(payload.get("period_start")),
                period_end=_parse_date(payload["period_end"]),
                period_kind=payload["period_kind"],
                source_value_text=payload["source_value_text"],
                raw_value=raw_value,
                raw_unit=payload["raw_unit"],
                normalized_value_cny=normalized_value_cny,
                metric_schema_version=int(payload["metric_schema_version"]),
                metric_fingerprint=fingerprint,
            )
        )
        acc.financial.append(
            RemappedObservation(
                artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                semantic_key=_semantic_key(
                    payload["metric_code"], payload["period_end"], payload["source_value_text"]
                ),
                observation_id=row.metric_observation_id,
                fingerprint=row.metric_fingerprint,
                source_evidence_card_id=row.source_evidence_card_id,
                replayed=not created,
            )
        )

    # ------------------------------------------------------------ valuation observation

    async def _remap_valuation_observation(
        self,
        session: AsyncSession,
        ref,
        payload: dict,
        acc: _RemapAccumulator,
        observation_ids: dict[tuple[str, str, str], tuple[UUID, str]],
    ) -> tuple[UUID, str]:
        provenance = payload["provenance"]
        match = provenance.get("source_evidence")
        if not isinstance(match, dict):
            raise EvalRemapError("valuation observation provenance.source_evidence 缺失")
        return await self._create_valuation_observation(
            session,
            company_id=UUID(payload["company_id"]),
            metric_code=payload["metric_code"],
            metric_as_of=_parse_date(payload["metric_as_of"]),
            source_value_text=payload["source_value_text"],
            metric_value=Decimal(payload["metric_value"]),
            schema_version=int(payload["valuation_observation_schema_version"]),
            evidence=match,
            replay=False,
            acc=acc,
            observation_ids=observation_ids,
        )

    async def _create_valuation_observation(
        self,
        session: AsyncSession,
        *,
        company_id: UUID,
        metric_code: str,
        metric_as_of: date,
        source_value_text: str,
        metric_value: Decimal,
        schema_version: int,
        evidence: dict,
        replay: bool,
        acc: _RemapAccumulator,
        observation_ids: dict[tuple[str, str, str], tuple[UUID, str]],
    ) -> tuple[UUID, str]:
        key = _semantic_key(metric_code, metric_as_of.isoformat(), source_value_text)
        if key in observation_ids:
            return observation_ids[key]
        if replay:
            card_id = await self._replay_evidence_card(session, evidence, acc)
        else:
            card_id = await self._resolve_evidence_card(session, evidence)
        fingerprint = compute_valuation_observation_fingerprint(
            valuation_observation_schema_version=schema_version,
            company_id=company_id,
            source_evidence_card_id=card_id,
            metric_code=metric_code,
            metric_as_of=metric_as_of,
            source_value_text=source_value_text,
            metric_value=metric_value,
        )
        row, created = await ValuationMetricObservationRepository(session).create_or_get(
            ValuationMetricObservationModel(
                valuation_observation_id=uuid.uuid4(),
                company_id=company_id,
                source_evidence_card_id=card_id,
                metric_code=metric_code,
                metric_as_of=metric_as_of,
                source_value_text=source_value_text,
                metric_value=metric_value,
                valuation_observation_schema_version=schema_version,
                valuation_observation_fingerprint=fingerprint,
            )
        )
        observation_ids[key] = (row.valuation_observation_id, row.valuation_observation_fingerprint)
        acc.valuation.append(
            RemappedObservation(
                artifact_type=StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION,
                semantic_key=key,
                observation_id=row.valuation_observation_id,
                fingerprint=row.valuation_observation_fingerprint,
                source_evidence_card_id=row.source_evidence_card_id,
                replayed=not created,
            )
        )
        return row.valuation_observation_id, row.valuation_observation_fingerprint

    # ------------------------------------------------------------ comparison

    async def _remap_comparison(
        self,
        session: AsyncSession,
        ref,
        payload: dict,
        acc: _RemapAccumulator,
        observation_ids: dict[tuple[str, str, str], tuple[UUID, str]],
    ) -> None:
        provenance = payload["provenance"]
        target_prov = provenance.get("target_observation")
        if not isinstance(target_prov, dict):
            raise EvalRemapError("comparison provenance.target_observation 缺失")
        # target observation：evidence 匹配 attempt 新卡（target 公司文档已 rehydrate）。
        target_obs_id, target_fp = await self._create_valuation_observation(
            session,
            company_id=UUID(payload["target_company_id"]),
            metric_code=target_prov["metric_code"],
            metric_as_of=_parse_date(target_prov["metric_as_of"]),
            source_value_text=target_prov["source_value_text"],
            metric_value=Decimal(target_prov["metric_value"]),
            schema_version=VALUATION_OBSERVATION_SCHEMA_VERSION,
            evidence=target_prov["evidence"],
            replay=False,
            acc=acc,
            observation_ids=observation_ids,
        )
        # peer observations：replay scaffold（peer 公司不在 frozen snapshot）。
        peer_entries: list[dict] = []
        for peer_prov in provenance.get("peer_observations", ()):
            peer_company = peer_prov["evidence"]["company"]
            company_id = await self._create_peer_company(session, peer_company, acc)
            peer_obs_id, peer_fp = await self._create_valuation_observation(
                session,
                company_id=company_id,
                metric_code=peer_prov["metric_code"],
                metric_as_of=_parse_date(peer_prov["metric_as_of"]),
                source_value_text=peer_prov["source_value_text"],
                metric_value=Decimal(peer_prov["metric_value"]),
                schema_version=VALUATION_OBSERVATION_SCHEMA_VERSION,
                evidence=peer_prov["evidence"],
                replay=True,
                acc=acc,
                observation_ids=observation_ids,
            )
            peer_entries.append(
                {
                    "peer_company_id": str(company_id),
                    "peer_observation_id": str(peer_obs_id),
                    "observation_fingerprint": peer_fp,
                }
            )
        peer_entries.sort(key=lambda entry: entry["peer_company_id"])
        fingerprint = compute_comparison_fingerprint(
            comparison_schema_version=RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION,
            formula_version=int(payload["formula_version"]),
            comparison_method=payload["comparison_method"],
            target_company_id=UUID(payload["target_company_id"]),
            target_observation_id=target_obs_id,
            target_observation_fingerprint=target_fp,
            metric_code=payload["metric_code"],
            metric_as_of=_parse_date(payload["metric_as_of"]),
            analysis_as_of=_parse_date(payload["analysis_as_of"]),
            peers=peer_entries,
            peer_median=Decimal(payload["peer_median"]),
            peer_min=Decimal(payload["peer_min"]),
            peer_max=Decimal(payload["peer_max"]),
            premium_discount_to_median=Decimal(payload["premium_discount_to_median"]),
        )
        row, created = await RelativeValuationComparisonRepository(session).create_or_get(
            RelativeValuationComparisonModel(
                comparison_id=uuid.uuid4(),
                target_company_id=UUID(payload["target_company_id"]),
                target_observation_id=target_obs_id,
                metric_code=payload["metric_code"],
                metric_as_of=_parse_date(payload["metric_as_of"]),
                analysis_as_of=_parse_date(payload["analysis_as_of"]),
                comparison_method=payload["comparison_method"],
                peer_count=int(payload["peer_count"]),
                peer_median=Decimal(payload["peer_median"]),
                peer_min=Decimal(payload["peer_min"]),
                peer_max=Decimal(payload["peer_max"]),
                premium_discount_to_median=Decimal(payload["premium_discount_to_median"]),
                comparison_schema_version=RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION,
                formula_version=int(payload["formula_version"]),
                comparison_fingerprint=fingerprint,
            )
        )
        if created:
            await RelativeValuationComparisonPeerRepository(session).bulk_insert(
                [
                    RelativeValuationComparisonPeerModel(
                        comparison_id=row.comparison_id,
                        peer_company_id=UUID(entry["peer_company_id"]),
                        peer_observation_id=UUID(entry["peer_observation_id"]),
                    )
                    for entry in peer_entries
                ]
            )
        acc.comparisons.append((row.comparison_id, row.comparison_fingerprint))

    async def _create_peer_company(
        self, session: AsyncSession, company: dict, acc: _RemapAccumulator
    ) -> UUID:
        """peer 公司 create-or-get（(exchange, security_code) 语义身份）。

        `identity_source_provider_key` 复用 attempt DB 已 rehydrate 的 provider
        （peer 公司没有自己的 provider 注册；companies 表 FK 要求 provider 存在）。
        """
        exchange = company["exchange"]
        security_code = company["security_code"]
        identity_key = f"{exchange}:{security_code}"
        repo = CompanyRepository(session)
        existing = await repo.get_by_identity_key(identity_key)
        if existing is not None:
            return existing.company_id
        provider_key = await self._any_provider_key(session)
        row = await repo.create(
            CompanyModel(
                company_id=_replay_uuid("peer-company", identity_key),
                exchange=exchange,
                security_code=security_code,
                identity_key=identity_key,
                board=company.get("board") or "unknown",
                official_name=company.get("official_name") or security_code,
                short_name=company.get("short_name") or security_code,
                listing_status=REPLAY_COMPANY_LISTING_STATUS,
                identity_source_provider_key=provider_key,
                identity_source_url=REPLAY_IDENTITY_SOURCE_URL,
            )
        )
        acc.peer_companies.append(row.company_id)
        return row.company_id

    @staticmethod
    async def _any_provider_key(session: AsyncSession) -> str:
        """attempt DB 中任意已 rehydrate 的 provider_key（确定性：最小 key）。"""
        result = await session.execute(
            select(SourceProviderModel.provider_key).order_by(SourceProviderModel.provider_key)
        )
        rows = result.scalars().all()
        if not rows:
            raise EvalRemapError("attempt DB 无 provider（peer company 无法建立 identity）")
        return rows[0]

    async def _replay_evidence_card(
        self, session: AsyncSession, evidence: dict, acc: _RemapAccumulator
    ) -> UUID:
        """peer evidence replay：从 frozen 完整内容确定性重建 EvidenceCard 链。

        peer 公司不在 frozen snapshot（无文档可重新提取），因此用 frozen 真实
        证据内容（statement / quote / extractor 身份）+ 确定性脚手架重建
        source → parsed → chunk → card 链。`extractor_name=eval_replay_peer_v1`
        明确标记 replay 身份。卡只被 comparison 校验引用（peer_observation →
        card → source 的 published/acquired availability），不进入 target 证据链。
        """
        company = evidence["company"]
        source_prov = evidence["source"]
        card_prov = evidence["evidence"]
        company_id = await self._create_peer_company(session, company, acc)
        content_sha256 = card_prov["content_sha256"]
        quote_text = card_prov.get("quote_text") or card_prov["evidence_statement"]
        raw_artifact_id = _replay_uuid("peer-raw", content_sha256)
        source_id = _replay_uuid("peer-source", content_sha256, source_prov["title"])
        parsed_id = _replay_uuid("peer-parsed", content_sha256, source_prov["title"])
        chunk_set_id = _replay_uuid("peer-chunkset", content_sha256, source_prov["title"])
        chunk_id = _replay_uuid("peer-chunk", content_sha256, source_prov["title"], "1")
        parse_fingerprint = _replay_sha256("peer-parse", content_sha256)
        chunk_set_fingerprint = _replay_sha256("peer-chunkset-fp", content_sha256)
        acquired_at = _parse_dt(source_prov["acquired_at"])
        if acquired_at is None:
            raise EvalRemapError("peer replay source acquired_at 缺失")

        if await session.get(RawArtifactModel, raw_artifact_id) is None:
            session.add(
                RawArtifactModel(
                    artifact_id=raw_artifact_id,
                    content_sha256=content_sha256,
                    storage_key=f"replay/peer/{content_sha256}",
                    byte_size=max(1, len(quote_text)),
                    media_type=source_prov.get("media_type") or "application/pdf",
                )
            )
        if await session.get(SourceRecordModel, source_id) is None:
            session.add(
                SourceRecordModel(
                    source_id=source_id,
                    company_id=company_id,
                    provider_key=source_prov["provider_key"],
                    artifact_id=raw_artifact_id,
                    document_type=source_prov["document_type"],
                    title=source_prov["title"],
                    published_at=_parse_dt(source_prov.get("published_at")),
                    reporting_period_end=_parse_date(source_prov.get("reporting_period_end")),
                    source_url=source_prov["source_url"],
                    acquisition_method=REPLAY_SOURCE_ACQUISITION_METHOD,
                    external_document_id=None,
                    authority_tier_snapshot=int(source_prov["authority_tier_snapshot"]),
                    critical_claim_eligible_snapshot=bool(
                        source_prov["critical_claim_eligible_snapshot"]
                    ),
                    provider_capabilities_snapshot=list(
                        source_prov.get("provider_capabilities_snapshot", [])
                    ),
                    status=REPLAY_SOURCE_STATUS,
                    acquired_at=acquired_at,
                )
            )
        if await session.get(ParsedSourceModel, parsed_id) is None:
            session.add(
                ParsedSourceModel(
                    parsed_source_id=parsed_id,
                    source_id=source_id,
                    artifact_id=raw_artifact_id,
                    parser_name=_REPLAY_PARSER_NAME,
                    parser_version=1,
                    raw_content_sha256=content_sha256,
                    parse_fingerprint=parse_fingerprint,
                    extracted_title=source_prov["title"],
                    block_count=1,
                    parsed_at=acquired_at,
                )
            )
        if await session.get(ChunkSetModel, chunk_set_id) is None:
            session.add(
                ChunkSetModel(
                    chunk_set_id=chunk_set_id,
                    parsed_source_id=parsed_id,
                    chunker_name=_REPLAY_CHUNKER_NAME,
                    chunker_version=1,
                    source_parse_fingerprint=parse_fingerprint,
                    chunk_count=1,
                    chunk_set_fingerprint=chunk_set_fingerprint,
                )
            )
        if await session.get(DocumentChunkModel, chunk_id) is None:
            session.add(
                DocumentChunkModel(
                    chunk_id=chunk_id,
                    chunk_set_id=chunk_set_id,
                    ordinal=1,
                    text=quote_text,
                    text_sha256=hashlib.sha256(quote_text.encode("utf-8")).hexdigest(),
                    char_count=len(quote_text),
                    locator_refs=list(card_prov.get("locator_refs") or []),
                )
            )
        card_id = _replay_uuid("peer-card", content_sha256, card_prov["evidence_fingerprint"])
        existing = await session.get(EvidenceCardModel, card_id)
        if existing is None:
            repo = EvidenceCardRepository(session)
            card, _ = await repo.create_or_get(
                EvidenceCardModel(
                    evidence_card_id=card_id,
                    origin_type="document_chunk",
                    company_id=company_id,
                    source_id=source_id,
                    parsed_source_id=parsed_id,
                    chunk_set_id=chunk_set_id,
                    chunk_id=chunk_id,
                    research_question=card_prov["research_question"],
                    research_question_sha256=card_prov["research_question_sha256"],
                    evidence_statement=card_prov["evidence_statement"],
                    evidence_type=card_prov["evidence_type"],
                    quote_start=card_prov.get("quote_start"),
                    quote_end=card_prov.get("quote_end"),
                    quote_text=card_prov.get("quote_text"),
                    quote_sha256=card_prov.get("quote_sha256"),
                    locator_refs=list(card_prov.get("locator_refs") or []),
                    provider_key=card_prov["provider_key"],
                    source_published_at=_parse_dt(card_prov.get("source_published_at")),
                    reporting_period_end=_parse_date(card_prov.get("reporting_period_end")),
                    authority_tier_snapshot=int(card_prov["authority_tier_snapshot"]),
                    critical_claim_eligible_snapshot=bool(
                        card_prov["critical_claim_eligible_snapshot"]
                    ),
                    extractor_name=_REPLAY_EXTRACTOR_NAME,
                    extractor_version=1,
                    extractor_model_id=card_prov.get("extractor_model_id"),
                    extractor_confidence=card_prov["extractor_confidence"],
                    evidence_schema_version=int(card_prov["evidence_schema_version"]),
                    evidence_fingerprint=card_prov["evidence_fingerprint"],
                )
            )
            return card.evidence_card_id
        return existing.evidence_card_id


def _semantic_key(metric_code: str, as_of: str, source_value_text: str) -> str:
    """observation 语义键（跨 artifact 去重用，确定性）。"""
    return f"{metric_code}|{as_of}|{source_value_text}"
