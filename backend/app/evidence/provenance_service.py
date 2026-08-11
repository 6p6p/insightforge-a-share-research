"""Evidence verified provenance path (Stage 6B.2 spec I/M): 共享 read-only service.

Document 与 Macro **共用这一条** verified provenance 路径（spec I：不建立第二套 /
Stage6 第三套手工 SQL provenance）：
- document_chunk：EvidenceCard → DocumentChunk → ChunkSet → ParsedSource →
  SourceRecord → RawArtifact（+ SourceProvider label），quote 级 locator /
  context 投影；
- macro_observation：EvidenceCard → MacroObservation → MacroDatasetSnapshot →
  MacroSeries → SourceProvider + MacroSnapshotArtifact links → RawArtifact。

两类方法（stateless，接受短生命周期 session）：
- `load_closure` / `document_closure` / `macro_closure`：bulk boolean
  （has_provenance），从 ReportCheckService 私有 helper 提取，供
  citation_provenance_closure check 复用（行为不变）；
- `resolve_document` / `resolve_macro`：per-card verified provenance payload，
  任何 hop 缺失 / 不一致 → `EvidenceProvenanceIntegrityError`（spec M，**不
  repair**），供 Citation API 消费。

只读：不创建 / 不修改任何 Evidence / Claim / Report / Audit。
"""

from uuid import UUID

from sqlalchemy import select

from app.db.models.chunk_set import ChunkSetModel
from app.db.models.document_chunk import DocumentChunkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.evidence.contracts import EvidenceOrigin, compute_quote_sha256
from app.evidence.errors import EvidenceProvenanceIntegrityError
from app.schemas.citation import (
    CitationLocator,
    DocumentProvenance,
    EvidenceProvenance,
    MacroArtifactLink,
    MacroProvenance,
)

# context_text 上限（spec K：只返回安全纯文本上下文，≤5000 chars，不返回整篇原文）。
MAX_CONTEXT_CHARS = 5000


def context_window(
    *,
    chunk_text: str,
    quote_start: int,
    quote_end: int,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """quote 周围的安全纯文本上下文窗口（≤ max_chars），不返回整个原文。

    整 chunk 不长于上限时原样返回；否则以 quote 中点为中心取窗口，首尾用
    "…" 标记截断。纯函数，确定性输出，便于测试。
    """
    if len(chunk_text) <= max_chars:
        return chunk_text
    mid = (quote_start + quote_end) // 2
    start = max(0, mid - max_chars // 2)
    end = min(len(chunk_text), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(chunk_text) else ""
    return f"{prefix}{chunk_text[start:end]}{suffix}"


def _build_locator(ref: dict) -> CitationLocator:
    """EvidenceCard.locator_refs 的一条 ref → CitationLocator（原字段原样保留）。"""
    locator = ref.get("locator") or {}
    return CitationLocator(
        locator_type=str(locator.get("type") or ""),
        block_ordinal=ref.get("block_ordinal"),
        char_start=ref.get("char_start"),
        char_end=ref.get("char_end"),
        ordinal=locator.get("ordinal"),
        tag=locator.get("tag"),
        xpath=locator.get("xpath"),
        element_id=locator.get("element_id"),
        page_number=locator.get("page_number"),
        line_index=locator.get("line_index"),
        bbox=locator.get("bbox"),
        page_width=locator.get("page_width"),
        page_height=locator.get("page_height"),
    )


class EvidenceProvenanceService:
    """Evidence 的 verified provenance path（read-only，stateless）。

    所有方法接受短生命周期 session，不持有连接；不创建 / 不修改任何产物。
    """

    # ------------------------------------------------------------------ closure（bulk boolean）

    @staticmethod
    async def load_closure(
        session,
        cards: dict[UUID, EvidenceCardModel],
    ) -> dict[UUID, bool]:
        """按 origin_type 批量验证 Evidence → source → RawArtifact 真实可追溯。

        spec D：FK 非空不够——必须沿完整链走到真实 `raw_artifacts` 行：
        - document_chunk：`EvidenceCard.source_id → SourceRecord.artifact_id →
          RawArtifact`；
        - macro_observation：`EvidenceCard.macro_observation_id →
          MacroObservation.snapshot_id → MacroDatasetSnapshot →（series /
          provider + macro_snapshot_artifacts 链接）→ RawArtifact`。

        任一环节断裂 → `has_provenance=False`（`citation_provenance_closure`
        check 捕获，**不 repair / 不重新 retrieval**）。同一短 session 内批量
        IN 查询，避免 N+1。
        """
        provenance: dict[UUID, bool] = {}
        doc_source_by_card: dict[UUID, UUID] = {
            cid: card.source_id
            for cid, card in cards.items()
            if card.origin_type == EvidenceOrigin.DOCUMENT_CHUNK.value
            and card.source_id is not None
        }
        macro_obs_by_card: dict[UUID, UUID] = {
            cid: card.macro_observation_id
            for cid, card in cards.items()
            if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value
            and card.macro_observation_id is not None
        }
        for cid in cards:
            if cid not in doc_source_by_card and cid not in macro_obs_by_card:
                provenance[cid] = False

        if doc_source_by_card:
            provenance.update(
                await EvidenceProvenanceService.document_closure(session, doc_source_by_card)
            )
        if macro_obs_by_card:
            provenance.update(
                await EvidenceProvenanceService.macro_closure(session, macro_obs_by_card)
            )
        return provenance

    @staticmethod
    async def document_closure(
        session,
        source_by_card: dict[UUID, UUID],
    ) -> dict[UUID, bool]:
        """document_chunk 闭包：SourceRecord → artifact → RawArtifact。"""
        source_ids = list(dict.fromkeys(source_by_card.values()))
        result = await session.execute(
            select(SourceRecordModel.source_id, SourceRecordModel.artifact_id).where(
                SourceRecordModel.source_id.in_(source_ids)
            )
        )
        artifact_by_source = {row.source_id: row.artifact_id for row in result.all()}
        artifact_ids = [aid for aid in artifact_by_source.values() if aid is not None]
        existing_artifacts: set[UUID] = set()
        if artifact_ids:
            result = await session.execute(
                select(RawArtifactModel.artifact_id).where(
                    RawArtifactModel.artifact_id.in_(artifact_ids)
                )
            )
            existing_artifacts = {row.artifact_id for row in result.all()}
        return {
            cid: (sid in artifact_by_source and artifact_by_source[sid] in existing_artifacts)
            for cid, sid in source_by_card.items()
        }

    @staticmethod
    async def macro_closure(
        session,
        obs_by_card: dict[UUID, UUID],
    ) -> dict[UUID, bool]:
        """macro_observation 闭包：Observation → Snapshot →（Series/Provider +
        artifact links）→ RawArtifact。

        Snapshot 的 artifact links 是可选的（不像 document 链那样被 FK 完整
        保证）——若链接行被删 / 从未归档，Observation / Snapshot / Series /
        Provider 仍在而 RawArtifact 不可达 → 判定无 provenance。
        """
        obs_ids = list(dict.fromkeys(obs_by_card.values()))
        result = await session.execute(
            select(MacroObservationModel.observation_id, MacroObservationModel.snapshot_id).where(
                MacroObservationModel.observation_id.in_(obs_ids)
            )
        )
        snapshot_by_obs = {row.observation_id: row.snapshot_id for row in result.all()}

        snapshot_ids = [sid for sid in snapshot_by_obs.values() if sid is not None]
        series_by_snapshot: dict[UUID, UUID] = {}
        if snapshot_ids:
            result = await session.execute(
                select(
                    MacroDatasetSnapshotModel.snapshot_id,
                    MacroDatasetSnapshotModel.series_id,
                ).where(MacroDatasetSnapshotModel.snapshot_id.in_(snapshot_ids))
            )
            series_by_snapshot = {row.snapshot_id: row.series_id for row in result.all()}

        series_ids = [sid for sid in series_by_snapshot.values() if sid is not None]
        provider_by_series: dict[UUID, str] = {}
        if series_ids:
            result = await session.execute(
                select(MacroSeriesModel.series_id, MacroSeriesModel.provider_key).where(
                    MacroSeriesModel.series_id.in_(series_ids)
                )
            )
            provider_by_series = {row.series_id: row.provider_key for row in result.all()}

        provider_keys = [key for key in provider_by_series.values() if key]
        existing_providers: set[str] = set()
        if provider_keys:
            result = await session.execute(
                select(SourceProviderModel.provider_key).where(
                    SourceProviderModel.provider_key.in_(provider_keys)
                )
            )
            existing_providers = {row.provider_key for row in result.all()}

        artifact_ids_by_snapshot: dict[UUID, list[UUID]] = {}
        if snapshot_ids:
            result = await session.execute(
                select(
                    MacroSnapshotArtifactModel.snapshot_id,
                    MacroSnapshotArtifactModel.artifact_id,
                ).where(MacroSnapshotArtifactModel.snapshot_id.in_(snapshot_ids))
            )
            for snapshot_id, artifact_id in result.all():
                artifact_ids_by_snapshot.setdefault(snapshot_id, []).append(artifact_id)
        all_artifact_ids = [aid for ids in artifact_ids_by_snapshot.values() for aid in ids]
        existing_artifacts: set[UUID] = set()
        if all_artifact_ids:
            result = await session.execute(
                select(RawArtifactModel.artifact_id).where(
                    RawArtifactModel.artifact_id.in_(all_artifact_ids)
                )
            )
            existing_artifacts = {row.artifact_id for row in result.all()}

        def snapshot_ok(snapshot_id: UUID) -> bool:
            series_id = series_by_snapshot.get(snapshot_id)
            if series_id is None or series_id not in provider_by_series:
                return False
            if provider_by_series[series_id] not in existing_providers:
                return False
            linked = artifact_ids_by_snapshot.get(snapshot_id, [])
            return any(aid in existing_artifacts for aid in linked)

        return {
            cid: (obs_id in snapshot_by_obs and snapshot_ok(snapshot_by_obs[obs_id]))
            for cid, obs_id in obs_by_card.items()
        }

    # ------------------------------------------------------------ resolve per-card

    @staticmethod
    async def resolve(session, card: EvidenceCardModel) -> EvidenceProvenance:
        """per-card verified provenance（按 origin_type 分派，spec M）。

        Citation API 的单一入口：Document / Macro 都走本类同一条 verified
        路径；未知 origin → `EvidenceProvenanceIntegrityError`（DB CHECK 之外
        的防御，**不 repair**）。
        """
        if card.origin_type == EvidenceOrigin.DOCUMENT_CHUNK.value:
            return await EvidenceProvenanceService.resolve_document(session, card)
        if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value:
            return await EvidenceProvenanceService.resolve_macro(session, card)
        raise EvidenceProvenanceIntegrityError()

    @staticmethod
    async def resolve_document(session, card: EvidenceCardModel) -> DocumentProvenance:
        """per-card document provenance：Chunk → ChunkSet → ParsedSource →
        SourceRecord → RawArtifact 全链校验。

        任何 hop 缺失 / 不一致（含 quote 切片与 quote_sha256 不符，即 Chunk
        正文被篡改）→ `EvidenceProvenanceIntegrityError`，**不 repair**。
        """
        if (
            card.origin_type != EvidenceOrigin.DOCUMENT_CHUNK.value
            or card.source_id is None
            or card.chunk_id is None
            or card.parsed_source_id is None
            or card.chunk_set_id is None
        ):
            raise EvidenceProvenanceIntegrityError()

        chunk = (
            await session.execute(
                select(DocumentChunkModel).where(DocumentChunkModel.chunk_id == card.chunk_id)
            )
        ).scalar_one_or_none()
        if chunk is None:
            raise EvidenceProvenanceIntegrityError()
        if chunk.chunk_set_id != card.chunk_set_id:
            raise EvidenceProvenanceIntegrityError()

        chunk_set = (
            await session.execute(
                select(ChunkSetModel).where(ChunkSetModel.chunk_set_id == card.chunk_set_id)
            )
        ).scalar_one_or_none()
        if chunk_set is None:
            raise EvidenceProvenanceIntegrityError()
        if chunk_set.parsed_source_id != card.parsed_source_id:
            raise EvidenceProvenanceIntegrityError()

        parsed = (
            await session.execute(
                select(ParsedSourceModel).where(
                    ParsedSourceModel.parsed_source_id == card.parsed_source_id
                )
            )
        ).scalar_one_or_none()
        if parsed is None:
            raise EvidenceProvenanceIntegrityError()
        if parsed.source_id != card.source_id:
            raise EvidenceProvenanceIntegrityError()

        source = (
            await session.execute(
                select(SourceRecordModel).where(SourceRecordModel.source_id == card.source_id)
            )
        ).scalar_one_or_none()
        if source is None:
            raise EvidenceProvenanceIntegrityError()

        raw = (
            await session.execute(
                select(RawArtifactModel).where(RawArtifactModel.artifact_id == source.artifact_id)
            )
        ).scalar_one_or_none()
        if raw is None:
            raise EvidenceProvenanceIntegrityError()

        provider = (
            await session.execute(
                select(SourceProviderModel).where(
                    SourceProviderModel.provider_key == source.provider_key
                )
            )
        ).scalar_one_or_none()
        if provider is None:
            raise EvidenceProvenanceIntegrityError()

        refs = list(card.locator_refs or [])
        if not refs:
            raise EvidenceProvenanceIntegrityError()

        # quote 切片契约：quote_text == chunk.text[quote_start:quote_end]；任一
        # 不符 = Chunk 正文被篡改（spec M tamper）。
        if (
            card.quote_text is not None
            and card.quote_start is not None
            and card.quote_end is not None
        ):
            if chunk.text[card.quote_start : card.quote_end] != card.quote_text:
                raise EvidenceProvenanceIntegrityError()
        if card.quote_text is not None and card.quote_sha256 is not None:
            if compute_quote_sha256(card.quote_text) != card.quote_sha256:
                raise EvidenceProvenanceIntegrityError()

        context = context_window(
            chunk_text=chunk.text,
            quote_start=card.quote_start or 0,
            quote_end=card.quote_end or 0,
        )
        return DocumentProvenance(
            origin_type=EvidenceOrigin.DOCUMENT_CHUNK.value,
            source_id=source.source_id,
            provider_key=source.provider_key,
            provider_label=provider.display_name,
            title=source.title,
            source_url=source.source_url,
            published_at=source.published_at,
            authority_tier=source.authority_tier_snapshot,
            document_type=source.document_type,
            raw_artifact_id=raw.artifact_id,
            media_type=raw.media_type,
            parsed_source_id=parsed.parsed_source_id,
            chunk_id=chunk.chunk_id,
            locator=_build_locator(refs[0]),
            locator_refs=[_build_locator(ref) for ref in refs],
            context_text=context,
            quote_text=card.quote_text,
        )

    @staticmethod
    async def resolve_macro(session, card: EvidenceCardModel) -> MacroProvenance:
        """per-card macro provenance：Observation → Snapshot →（Series / Provider
        + artifact links）→ RawArtifact 全链校验。

        任何 hop 缺失 / 不一致（observation→snapshot→series→provider 身份链，
        或 snapshot 无任何可加载的归档 RawArtifact）→
        `EvidenceProvenanceIntegrityError`，**不 repair**。
        """
        if (
            card.origin_type != EvidenceOrigin.MACRO_OBSERVATION.value
            or card.macro_observation_id is None
            or card.macro_snapshot_id is None
            or card.macro_series_id is None
        ):
            raise EvidenceProvenanceIntegrityError()

        obs = (
            await session.execute(
                select(MacroObservationModel).where(
                    MacroObservationModel.observation_id == card.macro_observation_id
                )
            )
        ).scalar_one_or_none()
        if obs is None:
            raise EvidenceProvenanceIntegrityError()
        if obs.snapshot_id != card.macro_snapshot_id:
            raise EvidenceProvenanceIntegrityError()

        snapshot = (
            await session.execute(
                select(MacroDatasetSnapshotModel).where(
                    MacroDatasetSnapshotModel.snapshot_id == card.macro_snapshot_id
                )
            )
        ).scalar_one_or_none()
        if snapshot is None:
            raise EvidenceProvenanceIntegrityError()
        if snapshot.series_id != card.macro_series_id:
            raise EvidenceProvenanceIntegrityError()

        series = (
            await session.execute(
                select(MacroSeriesModel).where(MacroSeriesModel.series_id == card.macro_series_id)
            )
        ).scalar_one_or_none()
        if series is None:
            raise EvidenceProvenanceIntegrityError()
        if series.provider_key != card.provider_key:
            raise EvidenceProvenanceIntegrityError()

        provider = (
            await session.execute(
                select(SourceProviderModel).where(
                    SourceProviderModel.provider_key == series.provider_key
                )
            )
        ).scalar_one_or_none()
        if provider is None:
            raise EvidenceProvenanceIntegrityError()

        link_rows = (
            (
                await session.execute(
                    select(MacroSnapshotArtifactModel).where(
                        MacroSnapshotArtifactModel.snapshot_id == card.macro_snapshot_id
                    )
                )
            )
            .scalars()
            .all()
        )
        links: list[tuple] = []
        for link in link_rows:
            raw = (
                await session.execute(
                    select(RawArtifactModel).where(RawArtifactModel.artifact_id == link.artifact_id)
                )
            ).scalar_one_or_none()
            if raw is None:
                raise EvidenceProvenanceIntegrityError()
            links.append((link, raw))
        if not links:
            raise EvidenceProvenanceIntegrityError()

        # 主要 raw artifact：优先 observations_page（最小 page），否则按
        # (role, page) 确定序取第一条。
        primary_link, primary_raw = min(
            links,
            key=lambda pair: (
                0 if pair[0].role == "observations_page" else 1,
                pair[0].page if pair[0].page is not None else 0,
                str(pair[0].artifact_id),
            ),
        )
        value = str(obs.value_numeric) if obs.value_numeric is not None else None
        return MacroProvenance(
            origin_type=EvidenceOrigin.MACRO_OBSERVATION.value,
            observation_id=obs.observation_id,
            period=obs.period,
            value=value,
            is_missing=obs.is_missing,
            snapshot_id=snapshot.snapshot_id,
            fetched_at=snapshot.fetched_at,
            series_id=series.series_id,
            indicator=snapshot.indicator_name,
            geography=snapshot.geography_name,
            provider_key=series.provider_key,
            provider_label=provider.display_name,
            authority_tier=snapshot.authority_tier_snapshot,
            source_name=snapshot.source_name,
            source_organization=snapshot.source_organization,
            raw_artifact_id=primary_raw.artifact_id,
            media_type=primary_raw.media_type,
            artifact_links=[
                MacroArtifactLink(
                    role=link.role,
                    page=link.page,
                    artifact_id=raw.artifact_id,
                    media_type=raw.media_type,
                    fetched_at=link.fetched_at,
                )
                for link, raw in sorted(
                    links,
                    key=lambda pair: (
                        pair[0].role,
                        pair[0].page if pair[0].page is not None else 0,
                        str(pair[0].artifact_id),
                    ),
                )
            ],
        )
