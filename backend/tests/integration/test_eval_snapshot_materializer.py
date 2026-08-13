"""EvaluationSnapshotMaterializer integration tests (stage 7B.1.1B, spec Q/T).

需要真实 PostgreSQL（127.0.0.1:5433）。materializer 从真实 PG rows +
content-addressed raw bytes 加载三路 frozen input，逐条校验后投影为 frozen
contracts + source payloads，写入 Evaluation Bundle，再 `verify_bundle_integrity`
闭合引用完整性。

零 Chroma / 零 LLM / 零 network（macro 走 httpx.MockTransport，不访问真实
World Bank）/ 零 Alembic / 零 token capture / 零 variant runner。覆盖：
- happy path：document + macro + financial + valuation observation +
  relative valuation comparison 全五路 materialize → write → verify；
- 7 个 negative cases（spec Q）：
  1. company mismatch（document source 公司不一致）；
  2. future document（published_at > analysis_as_of）；
  3. future macro（fetched_at > analysis_as_of）；
  4. raw byte tamper（篡改归档字节 → content_sha256 mismatch）；
  5. duplicate content hash（两条 document 同 content_sha256）；
  6. unknown structured artifact（financial observation id 不存在）；
  7. tampered domain verifier fail（financial metric_fingerprint 被篡改）。
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.eval.bundle import EvaluationBundleWriter, verify_bundle_integrity
from app.eval.contracts import StructuredArtifactType
from app.eval.errors import EvalMaterializationError
from app.eval.fingerprints import compute_source_snapshot_fingerprint
from app.eval.materialization import (
    EvalCaseMaterializationSpec,
    EvaluationSnapshotMaterializer,
    StructuredArtifactSelection,
)
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
)
from app.financial.contracts import (
    FinancialMetricDraft,
    MetricCode,
    RawUnit,
    StatementScope,
)
from app.financial.service import FinancialMetricService
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.contracts import ComparisonDraft
from tests.integration.test_macro_evidence_service import _seed_macro_chain
from tests.integration.test_valuation_comparison_service import (
    _seed_company,
    _seed_observation,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_ANALYSIS_AS_OF = datetime(2026, 8, 10, tzinfo=UTC)
_DOC_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_QUESTION = "贵州茅台2024年营收与估值水平？"
_URL = "https://www.xinhuanet.com/2026/0809/0001.htm"
_SOURCE_TITLE = "研究新闻"


def _doc_html(body_text: str) -> bytes:
    return (
        "<html><head><title>研究新闻</title></head><body><article>"
        f"<p>{body_text}</p></article></body></html>"
    ).encode()


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM relative_valuation_comparison_peers"))
        await session.execute(text("DELETE FROM relative_valuation_comparisons"))
        await session.execute(text("DELETE FROM valuation_metric_observations"))
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.execute(text("DELETE FROM claims"))
        await session.execute(text("DELETE FROM evidence_cards"))
        await session.execute(text("DELETE FROM macro_observations"))
        await session.execute(text("DELETE FROM macro_snapshot_artifacts"))
        await session.execute(text("DELETE FROM macro_dataset_snapshots"))
        await session.execute(text("DELETE FROM macro_series"))
        await session.execute(text("DELETE FROM chunk_vector_indexes"))
        await session.execute(text("DELETE FROM document_chunks"))
        await session.execute(text("DELETE FROM chunk_sets"))
        await session.execute(text("DELETE FROM parsed_source_blocks"))
        await session.execute(text("DELETE FROM parsed_sources"))
        await session.execute(text("DELETE FROM news_source_verifications"))
        await session.execute(text("DELETE FROM news_discovery_candidates"))
        await session.execute(text("DELETE FROM news_discovery_runs"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.commit()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_root = tmp_path / "raw"
    raw_store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = await _seed_company(sessionmaker, "600519")
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "raw_root": raw_root,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


async def _seed_source_record(
    env: dict,
    *,
    company_id: UUID,
    artifact_id: UUID,
    published_at: datetime = _DOC_PUBLISHED_AT,
) -> UUID:
    """为既有 RawArtifact 建一条 SourceRecord（document 路由的最小 provenance）。"""
    async with env["sessionmaker"]() as session:
        record = SourceRecordModel(
            company_id=company_id,
            provider_key="xinhuanet",
            artifact_id=artifact_id,
            document_type="news_article",
            title=_SOURCE_TITLE,
            published_at=published_at,
            reporting_period_end=None,
            source_url=_URL + f"?uid={uuid4().hex[:8]}",
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        return record.source_id


async def _seed_document(
    env: dict,
    *,
    company_id: UUID,
    published_at: datetime = _DOC_PUBLISHED_AT,
    html: bytes | None = None,
) -> dict:
    """归档一条 HTML 原文 + SourceRecord，返回 materializer document 路由所需的引用。"""
    html = html if html is not None else _doc_html("2024年贵州茅台营业收入123,456万元")
    stored = env["raw_store"].put_html_bytes(html)
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
            assert artifact is not None
        await session.commit()
        artifact_id = artifact.artifact_id
    source_id = await _seed_source_record(
        env, company_id=company_id, artifact_id=artifact_id, published_at=published_at
    )
    return {
        "source_id": source_id,
        "artifact_id": artifact_id,
        "content_sha256": stored.content_sha256,
        "storage_key": stored.storage_key,
    }


async def _seed_financial_observation(env: dict, company_id: UUID) -> dict:
    """真实 document → evidence → FinancialMetricObservation 链，返回 financial 引用。"""
    html = _doc_html("2024年贵州茅台营业收入123,456万元")
    stored = env["raw_store"].put_html_bytes(html)
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
            assert artifact is not None
        record = SourceRecordModel(
            company_id=company_id,
            provider_key="xinhuanet",
            artifact_id=artifact.artifact_id,
            document_type="news_article",
            title=_SOURCE_TITLE,
            published_at=_DOC_PUBLISHED_AT,
            reporting_period_end=None,
            source_url=_URL + f"?uid={uuid4().hex[:8]}",
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsing = SourceParsingService(env["sessionmaker"], env["raw_store"])
    parsed = await parsing.parse_source(source_id)
    result = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(parsed.parsed_source_id)
    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(result.chunk_set_id)
    assert chunks, "financial seed must produce chunks"
    chunk = next(c for c in chunks if "123,456" in c.text)
    idx = chunk.text.index("123,456")
    card = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement="营业收入为" + chunk.text[idx : idx + 7] + "万元",
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=idx,
            quote_end=idx + 7,
            extractor_name="test-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    obs = await FinancialMetricService(env["sessionmaker"]).create_observation(
        FinancialMetricDraft(
            company_id=company_id,
            source_evidence_card_id=card.evidence_card_id,
            metric_code=MetricCode.REVENUE,
            statement_scope=StatementScope.CONSOLIDATED,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            source_value_text="123,456",
            raw_unit=RawUnit.TEN_THOUSAND_YUAN,
        )
    )
    return {
        "metric_observation_id": obs.metric_observation_id,
        "metric_fingerprint": obs.metric_fingerprint,
    }


def _materializer(env: dict) -> EvaluationSnapshotMaterializer:
    return EvaluationSnapshotMaterializer(env["sessionmaker"], env["raw_store"])


def _spec(env: dict, **overrides) -> EvalCaseMaterializationSpec:
    values = dict(
        case_id="moutai-revenue-valuation",
        case_version=1,
        company_id=env["company_id"],
        security_code="600519",
        research_question=_QUESTION,
        analysis_as_of=_ANALYSIS_AS_OF,
        tags=("revenue", "valuation"),
        document_source_ids=(),
        macro_snapshot_ids=(),
        structured_artifacts=(),
    )
    values.update(overrides)
    return EvalCaseMaterializationSpec(**values)


def _sel(artifact_type: StructuredArtifactType, artifact_id: UUID) -> StructuredArtifactSelection:
    return StructuredArtifactSelection(artifact_type=artifact_type, artifact_id=artifact_id)


# ---------------------------------------------------------------- happy path


async def test_materialize_full_chain_writes_verified_bundle(env, monkeypatch, tmp_path) -> None:
    """document + macro + financial + valuation observation + comparison →
    materialize → write → verify_bundle_integrity 闭合。"""
    doc = await _seed_document(env, company_id=env["company_id"])
    macro = await _seed_macro_chain(env, monkeypatch)
    fin = await _seed_financial_observation(env, env["company_id"])
    target = await _seed_observation(env, env["company_id"], "15.3")
    peers = []
    for i, value in enumerate(["14.2", "15.0", "16.0"]):
        peer_company = await _seed_company(env["sessionmaker"], f"6005{2 + i:02d}")
        peers.append(await _seed_observation(env, peer_company, value))
    comparison = await RelativeValuationComparisonService(env["sessionmaker"]).create_comparison(
        ComparisonDraft(
            target_company_id=env["company_id"],
            target_observation_id=target["valuation_observation_id"],
            peer_observation_ids=tuple(p["valuation_observation_id"] for p in peers),
            analysis_as_of=date(2026, 8, 10),
        )
    )

    spec = _spec(
        env,
        document_source_ids=(doc["source_id"],),
        macro_snapshot_ids=(macro["snapshot_id"],),
        structured_artifacts=(
            _sel(StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION, fin["metric_observation_id"]),
            _sel(
                StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION,
                target["valuation_observation_id"],
            ),
            _sel(StructuredArtifactType.RELATIVE_VALUATION_COMPARISON, comparison.comparison_id),
        ),
    )

    materialized = await _materializer(env).materialize_case(spec)

    snapshot = materialized.snapshot
    assert len(snapshot.document_sources) == 1
    assert len(snapshot.macro_snapshots) == 1
    assert len(snapshot.structured_artifacts) == 3
    assert {ref.artifact_type for ref in snapshot.structured_artifacts} == {
        StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
        StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION,
        StructuredArtifactType.RELATIVE_VALUATION_COMPARISON,
    }
    assert materialized.case.source_snapshot_fingerprint == compute_source_snapshot_fingerprint(
        snapshot
    )

    bundle_root = tmp_path / "bundle"
    writer = EvaluationBundleWriter(bundle_root)
    EvaluationSnapshotMaterializer.write_materialized(materialized, writer)
    manifest = EvaluationSnapshotMaterializer.assemble_dataset_manifest(
        "moutai-2024", 1, [materialized], description="smoke"
    )
    writer.write_manifest(manifest)

    verified = verify_bundle_integrity(bundle_root)
    assert verified.dataset_id == "moutai-2024"
    assert verified.dataset_version == 1
    assert len(verified.cases) == 1


# ---------------------------------------------------------------- negative（spec Q）


async def test_document_company_mismatch_rejected(env) -> None:
    other = await _seed_company(env["sessionmaker"], "000001")
    doc = await _seed_document(env, company_id=other)
    spec = _spec(env, document_source_ids=(doc["source_id"],))
    with pytest.raises(EvalMaterializationError, match="company mismatch"):
        await _materializer(env).materialize_case(spec)


async def test_future_document_rejected(env) -> None:
    doc = await _seed_document(
        env,
        company_id=env["company_id"],
        published_at=datetime(2026, 8, 11, 9, 30, tzinfo=UTC),
    )
    spec = _spec(env, document_source_ids=(doc["source_id"],))
    with pytest.raises(EvalMaterializationError, match="future evidence"):
        await _materializer(env).materialize_case(spec)


async def test_future_macro_rejected(env, monkeypatch) -> None:
    macro = await _seed_macro_chain(env, monkeypatch)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE macro_dataset_snapshots SET fetched_at = :at WHERE snapshot_id = :sid")
            .bindparams(at=datetime(2026, 8, 11, 9, 30, tzinfo=UTC), sid=macro["snapshot_id"])
        )
        await session.commit()
    spec = _spec(env, macro_snapshot_ids=(macro["snapshot_id"],))
    with pytest.raises(EvalMaterializationError, match="future evidence"):
        await _materializer(env).materialize_case(spec)


async def test_raw_byte_tamper_rejected(env) -> None:
    doc = await _seed_document(env, company_id=env["company_id"])
    (env["raw_root"] / doc["storage_key"]).write_bytes(b"<html>tampered bytes</html>")
    spec = _spec(env, document_source_ids=(doc["source_id"],))
    with pytest.raises(EvalMaterializationError, match="tampered"):
        await _materializer(env).materialize_case(spec)


async def test_duplicate_content_hash_rejected(env) -> None:
    doc = await _seed_document(env, company_id=env["company_id"])
    # 第二条 SourceRecord 指向同一 RawArtifact → 同 content_sha256。
    second = await _seed_source_record(
        env, company_id=env["company_id"], artifact_id=doc["artifact_id"]
    )
    spec = _spec(env, document_source_ids=(doc["source_id"], second))
    with pytest.raises(EvalMaterializationError, match="duplicate"):
        await _materializer(env).materialize_case(spec)


async def test_unknown_structured_artifact_rejected(env) -> None:
    spec = _spec(
        env,
        structured_artifacts=(
            _sel(StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION, uuid4()),
        ),
    )
    with pytest.raises(EvalMaterializationError, match="not found"):
        await _materializer(env).materialize_case(spec)


async def test_tampered_financial_fingerprint_rejected(env) -> None:
    fin = await _seed_financial_observation(env, env["company_id"])
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE financial_metric_observations SET metric_fingerprint = :fp "
                "WHERE metric_observation_id = :oid"
            ).bindparams(fp="0" * 64, oid=fin["metric_observation_id"])
        )
        await session.commit()
    spec = _spec(
        env,
        structured_artifacts=(
            _sel(StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION, fin["metric_observation_id"]),
        ),
    )
    with pytest.raises(EvalMaterializationError, match="fingerprint mismatch"):
        await _materializer(env).materialize_case(spec)
