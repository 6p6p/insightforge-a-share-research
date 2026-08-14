"""Structured evidence remap integration tests (stage 7B.1.4C.3).

真实 PostgreSQL 全程。流程：

1. **materialize**（source PG）：真实 document + financial observation +
   valuation observation + comparison（3 peers）→ frozen bundle（v2 payload 含
   stable semantic provenance）；
2. **rehydrate + attempt evidence**（隔离 PG）：frozen documents → parse → chunk
   → index → retrieval → **attempt 重新生成** EvidenceCard（fake extractor，
   0 真实 DeepSeek）；
3. **remap**：`StructuredEvidenceRemapService.remap_case` 把 frozen structured
   artifacts 重新绑定到 attempt 新 EvidenceCard（旧 runtime UUID 不落库），
   deterministic 重算 fingerprint；
4. 验收：
   - financial / valuation observation 行存在且 `source_evidence_card_id` 指向
     attempt 新卡（按 content_sha256 匹配）；
   - comparison 行存在，`verify_comparison_integrity` 通过（peer replay 闭包）；
   - **幂等**：remap 两次 → 0 新增行（fingerprint replay）；
   - **tamper**：篡改 payload provenance 的 content_sha256 → `EvalRemapError`
     （稳定 fail-fast，不静默绕过）；
   - **fingerprint 语义**：同 inputs 两次独立 attempt → 同 semantic 结果。

0 真实 DeepSeek / 0 外部网络 / 0 live provider。需要真实 PostgreSQL
（127.0.0.1:5433，CREATEDB）+ 真实 Chroma（127.0.0.1:8002）。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import func, select, text

from alembic import command
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.db.models.relative_valuation_comparison import RelativeValuationComparisonModel
from app.db.models.relative_valuation_comparison_peer import (
    RelativeValuationComparisonPeerModel,
)
from app.db.models.valuation_metric_observation import ValuationMetricObservationModel
from app.db.session import DatabaseManager
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.bundle.writer import EvaluationBundleWriter
from app.eval.contracts import StructuredArtifactType
from app.eval.errors import EvalRemapError
from app.eval.materialization import (
    EvalCaseMaterializationSpec,
    EvaluationSnapshotMaterializer,
    StructuredArtifactSelection,
)
from app.eval.remap import StructuredEvidenceRemapService
from app.eval.replay.rehydrator import EvaluationReplayRehydrator
from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.evidence.extractor.contracts import (
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
)
from app.rag.index.service import VectorIndexService
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.contracts import ComparisonDraft
from app.vectorstore.client import ChromaManager
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.integration.research_fulfillment_helpers import _unique_quote
from tests.integration.test_eval_snapshot_materializer import (
    _ANALYSIS_AS_OF,
    _QUESTION,
    _seed_financial_observation,
)
from tests.integration.test_valuation_comparison_service import (
    _seed_company,
    _seed_observation,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


# ---------------------------------------------------------------- 临时 DB helpers


def _parse_db_url(url: str) -> dict:
    parsed = urlparse(url.replace("+psycopg", "", 1))
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": parsed.path.lstrip("/"),
    }


def _admin_conn(db_name: str) -> psycopg.Connection:
    parts = _parse_db_url(get_settings().database_url)
    return psycopg.connect(
        host=parts["host"],
        port=parts["port"],
        user=parts["user"],
        password=parts["password"],
        dbname=db_name,
        autocommit=True,
    )


def _create_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')


def _drop_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def _upgrade_head() -> None:
    cfg = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.upgrade, cfg, "head")


@asynccontextmanager
async def _isolated_target(monkeypatch, tmp_path, *, label: str):
    shared_url = get_settings().database_url
    temp_db = f"insightforge_eval_remap_{label}_{uuid4().hex[:10]}"
    temp_url = shared_url.rsplit("/", 1)[0] + f"/{temp_db}"
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()

    iso_manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    iso_store = LocalRawArtifactStore(root=tmp_path / f"raw_remap_{label}", max_bytes=1024 * 1024)
    try:
        await _upgrade_head()
        yield iso_manager.session_factory(), iso_store
    finally:
        await iso_manager.dispose()
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


async def _drop_collection(client, collection_name: str) -> None:
    try:
        await client.delete_collection(collection_name)
    except Exception:
        pass


# ---------------------------------------------------------------- source PG fixtures


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
        await session.execute(text("DELETE FROM evidence_cards"))
        await session.execute(text("DELETE FROM chunk_vector_indexes"))
        await session.execute(text("DELETE FROM document_chunks"))
        await session.execute(text("DELETE FROM chunk_sets"))
        await session.execute(text("DELETE FROM parsed_source_blocks"))
        await session.execute(text("DELETE FROM parsed_sources"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.commit()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw_src", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    company_id = await _seed_company(sessionmaker, "600519")
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


async def _build_frozen_bundle(env: dict, bundle_root: Path) -> dict:
    """真实 PG → frozen bundle（document + financial + valuation + comparison）。"""
    fin = await _seed_financial_observation(env, env["company_id"])
    target = await _seed_observation(env, env["company_id"], "15.3")
    peer_company_ids = []
    peer_obs_ids = []
    for i, value in enumerate(["14.2", "15.0", "16.0"]):
        peer_company = await _seed_company(env["sessionmaker"], f"6005{2 + i:02d}")
        peer_company_ids.append(peer_company)
        peer_obs_ids.append(
            (await _seed_observation(env, peer_company, value))["valuation_observation_id"]
        )
    comparison = await RelativeValuationComparisonService(env["sessionmaker"]).create_comparison(
        ComparisonDraft(
            target_company_id=env["company_id"],
            target_observation_id=target["valuation_observation_id"],
            peer_observation_ids=tuple(peer_obs_ids),
            analysis_as_of=date(2026, 8, 10),
        )
    )
    # 全部 target 公司 news_article source（financial/valuation seed 各自建文档）。
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT source_id FROM source_records "
                    "WHERE company_id = :cid AND document_type = 'news_article'"
                ).bindparams(cid=env["company_id"])
            )
        ).all()
    document_source_ids = tuple(row[0] for row in rows)
    assert document_source_ids, "target company must have news_article sources"
    spec = EvalCaseMaterializationSpec(
        case_id="remap-case",
        case_version=1,
        company_id=env["company_id"],
        security_code="600519",
        research_question=_QUESTION,
        analysis_as_of=_ANALYSIS_AS_OF,
        tags=("remap",),
        document_source_ids=document_source_ids,
        macro_snapshot_ids=(),
        structured_artifacts=(
            StructuredArtifactSelection(
                artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                artifact_id=fin["metric_observation_id"],
            ),
            StructuredArtifactSelection(
                artifact_type=StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION,
                artifact_id=target["valuation_observation_id"],
            ),
            StructuredArtifactSelection(
                artifact_type=StructuredArtifactType.RELATIVE_VALUATION_COMPARISON,
                artifact_id=comparison.comparison_id,
            ),
        ),
    )
    materializer = EvaluationSnapshotMaterializer(env["sessionmaker"], env["raw_store"])
    materialized = await materializer.materialize_case(spec)
    writer = EvaluationBundleWriter(bundle_root)
    EvaluationSnapshotMaterializer.write_materialized(materialized, writer)
    return {
        "materialized": materialized,
        "fin_fingerprint": fin["metric_fingerprint"],
        "target_obs_id": target["valuation_observation_id"],
        "comparison_id": comparison.comparison_id,
        "peer_company_ids": peer_company_ids,
    }


# ---------------------------------------------------------------- attempt evidence 重建


class _PerHitExtractor:
    """按真实 RetrievalHit 文本生成确定性 decision（0 真实 DeepSeek）。"""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "deepseek:deepseek-v4-flash"

    async def extract(self, research_question, retrieval_hit):
        self.calls += 1
        text_value = retrieval_hit.text
        if not any(text_value[i] != text_value[i - 1] for i in range(1, len(text_value))):
            return EvidenceExtractionDecision(relevant=False, items=[], reason_code="not_relevant")
        return EvidenceExtractionDecision(
            relevant=True,
            items=[
                EvidenceExtractionItem(
                    evidence_statement="attempt 重新生成的证据。",
                    evidence_type=EvidenceType.METRIC,
                    quote_text=_unique_quote(text_value, 20),
                    confidence=EvidenceConfidence.HIGH,
                )
            ],
        )


async def _rebuild_attempt_evidence(
    sessionmaker,
    raw_store,
    loader: EvaluationBundleLoader,
    execution_case,
    chroma: ChromaManager,
    collection_name: str,
) -> None:
    """rehydrate frozen documents → parse → chunk → index → retrieval → extract。

    只生成 attempt 自己的 EvidenceCard（0 旧卡 seed）；remap 之后按
    content_sha256 匹配这些卡。
    """
    rehydrated = await EvaluationReplayRehydrator(sessionmaker, raw_store, loader).rehydrate_case(
        execution_case.case_id, execution_case.case_version
    )
    embedding = FakeEmbeddingProvider()
    index_service = VectorIndexService(
        sessionmaker, embedding, chroma, collection_name=collection_name
    )
    parsing = SourceParsingService(sessionmaker, raw_store)
    chunking = ChunkingService(sessionmaker)
    for doc in rehydrated.documents:
        parsed = await parsing.parse_source(doc.source_record_id)
        chunked = await chunking.chunk_parsed_source(parsed.parsed_source_id)
        await index_service.index_chunk_set(chunked.chunk_set_id)

    from app.evidence.extractor.service import EvidenceExtractionService
    from app.rag.retrieval.contracts import RetrievalQuery
    from app.rag.retrieval.service import RetrievalService

    retrieval = RetrievalService(sessionmaker, embedding, chroma, collection_name=collection_name)
    extractor = _PerHitExtractor()
    service = EvidenceExtractionService(sessionmaker, extractor)
    for doc in rehydrated.documents:
        hits = await retrieval.retrieve(
            RetrievalQuery(
                company_id=execution_case.company_id,
                query_text=execution_case.research_question,
                top_k=5,
                source_ids=[doc.source_record_id],
            )
        )
        for hit in hits:
            await service.extract_from_hit(execution_case.research_question, hit)


# ---------------------------------------------------------------- helpers


async def _count_rows(sessionmaker, model) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar())


# ---------------------------------------------------------------- E2E


async def test_remap_binds_attempt_evidence_and_replays_comparison(
    monkeypatch, tmp_path, env
) -> None:
    """financial / valuation observation + comparison 全部 remap 成功。"""
    bundle_root = tmp_path / "bundle"
    await _build_frozen_bundle(env, bundle_root)
    loader = EvaluationBundleLoader(bundle_root)
    execution_case = loader.load_execution_case("remap-case", 1)

    collection_name = f"test_remap_{uuid4().hex[:12]}"
    settings = get_settings()
    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    client = await chroma.get_client()
    try:
        async with _isolated_target(monkeypatch, tmp_path, label="e2e") as (
            sessionmaker,
            raw_store,
        ):
            await _rebuild_attempt_evidence(
                sessionmaker, raw_store, loader, execution_case, chroma, collection_name
            )
            remap = StructuredEvidenceRemapService(sessionmaker, loader)
            result = await remap.remap_case(execution_case)

            # (1) 三类 artifact 全部 remap。valuation = target(1) + comparison peers(3)。
            assert len(result.financial_observations) == 1
            assert len(result.valuation_observations) == 4
            assert len(result.comparisons) == 1
            assert len(result.created_peer_companies) == 3

            # (2) financial observation：绑定 attempt 新 EvidenceCard（按
            #     content_sha256 匹配，非旧 runtime UUID）。
            fin_remap = result.financial_observations[0]
            assert fin_remap.artifact_type == StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION
            assert fin_remap.replayed is False
            async with sessionmaker() as session:
                row = await session.get(FinancialMetricObservationModel, fin_remap.observation_id)
                assert row is not None
                card = await session.get(EvidenceCardModel, row.source_evidence_card_id)
                assert card is not None
                assert card.evidence_statement == "attempt 重新生成的证据。"
                # 数值确定性：frozen source_value_text → 重算值一致。
                assert row.source_value_text == "123,456"
                assert row.raw_unit == "ten_thousand_yuan"

            # (3) target valuation observation：绑定 attempt 新卡（frozen target）。
            val_remap = next(
                item
                for item in result.valuation_observations
                if "15.3" in item.semantic_key
            )
            assert (
                val_remap.artifact_type == StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION
            )
            async with sessionmaker() as session:
                val_row = await session.get(
                    ValuationMetricObservationModel, val_remap.observation_id
                )
                assert val_row is not None
                assert val_row.metric_value > 0

            # (4) comparison：verify_comparison_integrity 通过（peer replay 闭包）。
            comparison_id = result.comparisons[0][0]
            async with sessionmaker() as session:
                comparison = await session.get(RelativeValuationComparisonModel, comparison_id)
                assert comparison is not None
                verified = await RelativeValuationComparisonService(
                    sessionmaker
                ).verify_comparison_integrity(session, comparison_id)
            assert verified is not None
            assert verified.target_company_id == execution_case.company_id
            assert verified.peer_count == 3
            # peer 链接完整。
            async with sessionmaker() as session:
                links = (
                    (
                        await session.execute(
                            select(RelativeValuationComparisonPeerModel).where(
                                RelativeValuationComparisonPeerModel.comparison_id == comparison_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert len(links) == 3
    finally:
        await _drop_collection(client, collection_name)


async def test_remap_is_idempotent_within_attempt(monkeypatch, tmp_path, env) -> None:
    """remap 两次 → 0 新增行（fingerprint create-or-get replay）。"""
    bundle_root = tmp_path / "bundle"
    await _build_frozen_bundle(env, bundle_root)
    loader = EvaluationBundleLoader(bundle_root)
    execution_case = loader.load_execution_case("remap-case", 1)

    collection_name = f"test_remap_{uuid4().hex[:12]}"
    settings = get_settings()
    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    client = await chroma.get_client()
    try:
        async with _isolated_target(monkeypatch, tmp_path, label="idem") as (
            sessionmaker,
            raw_store,
        ):
            await _rebuild_attempt_evidence(
                sessionmaker, raw_store, loader, execution_case, chroma, collection_name
            )
            remap = StructuredEvidenceRemapService(sessionmaker, loader)
            first = await remap.remap_case(execution_case)
            counts = {
                "fin": await _count_rows(sessionmaker, FinancialMetricObservationModel),
                "val": await _count_rows(sessionmaker, ValuationMetricObservationModel),
                "cmp": await _count_rows(sessionmaker, RelativeValuationComparisonModel),
                "peer": await _count_rows(sessionmaker, RelativeValuationComparisonPeerModel),
            }
            second = await remap.remap_case(execution_case)
            counts2 = {
                "fin": await _count_rows(sessionmaker, FinancialMetricObservationModel),
                "val": await _count_rows(sessionmaker, ValuationMetricObservationModel),
                "cmp": await _count_rows(sessionmaker, RelativeValuationComparisonModel),
                "peer": await _count_rows(sessionmaker, RelativeValuationComparisonPeerModel),
            }
            assert counts == counts2
            # 两次结果 identity 一致（全部 replay）；第二次 0 新 peer 公司。
            assert first.financial_observations[0].observation_id == (
                second.financial_observations[0].observation_id
            )
            assert first.comparisons == second.comparisons
            assert second.created_peer_companies == ()
    finally:
        await _drop_collection(client, collection_name)


async def test_remap_tampered_provenance_fails_fast(monkeypatch, tmp_path, env) -> None:
    """篡改 payload provenance 的 content_sha256 → EvalRemapError（稳定 fail-fast）。"""
    bundle_root = tmp_path / "bundle"
    await _build_frozen_bundle(env, bundle_root)
    loader = EvaluationBundleLoader(bundle_root)
    execution_case = loader.load_execution_case("remap-case", 1)

    # 篡改 financial observation payload 的 provenance（改写 bundle 文件字节）。
    from app.eval.bundle import layout as bundle_layout

    ref = next(
        ref
        for ref in execution_case.snapshot.structured_artifacts
        if ref.artifact_type == StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION
    )
    payload_path = bundle_layout.structured_payload_path(
        bundle_root, ref.artifact_type, ref.artifact_fingerprint
    )
    import json as _json

    payload = _json.loads(payload_path.read_text(encoding="utf-8"))
    payload["provenance"]["source_evidence"]["content_sha256"] = "f" * 64
    payload_path.write_text(
        _json.dumps(payload, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )

    collection_name = f"test_remap_{uuid4().hex[:12]}"
    settings = get_settings()
    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    client = await chroma.get_client()
    try:
        async with _isolated_target(monkeypatch, tmp_path, label="tamper") as (
            sessionmaker,
            raw_store,
        ):
            await _rebuild_attempt_evidence(
                sessionmaker, raw_store, loader, execution_case, chroma, collection_name
            )
            remap = StructuredEvidenceRemapService(sessionmaker, loader)
            with pytest.raises(EvalRemapError) as exc:
                await remap.remap_case(execution_case)
            assert exc.value.code == "eval_remap_error"
    finally:
        await _drop_collection(client, collection_name)
