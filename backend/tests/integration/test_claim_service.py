"""ClaimService integration tests (stage 4A, spec 13/14).

需要真实 PostgreSQL（127.0.0.1:5433）。Evidence 用真实 SourceRecord →
ParsedSource → ChunkingService → EvidenceCardService（document）与真实
MacroPersistenceService + MacroEvidenceService（macro），**零 Chroma / 零 LLM /
零 LangGraph / 零 Report 表**。

覆盖：
- 创建：document / macro Claim 持久化 claims + claim_evidence_links；
- relation：supports / contradicts / context 三种 link 各自落库；
- 拒绝：company mismatch / 证据缺失 / 无 supports / critical 缺 eligible /
  macro 缺 macro support / macro 缺 document 传导；
- replay：同 fingerprint 复用同一行 / 并发 → 1 / 语句变化 → 新 Claim /
  证据关系变化 → 新 Claim / analyst version 变化 → 新 Claim；
- integrity：篡改 link / 篡改 critical eligibility → ClaimIntegrityError，
  **不自动 repair**；EvidenceCard 行永远不被改写；
- E2E 回溯：document Claim → link → EvidenceCard → Chunk → ParsedSource →
  SourceRecord → RawArtifact；macro Claim → Evidence → Observation / Snapshot /
  Series / RawArtifact；
- 边界：claims / claim_evidence_links 允许存在；Stage 5 report 表不得存在。

全程使用真实 PG（不手写 RetrievalHit / DocumentChunk，不 seed 伪造 Evidence）。
"""

import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.claims.contracts import (
    CLAIM_SCHEMA_VERSION,
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimDraft,
    ClaimImportance,
    ClaimKind,
    compute_research_question_sha256,
)
from app.claims.errors import (
    ClaimCriticalEvidenceInsufficient,
    ClaimDraftError,
    ClaimEvidenceCompanyMismatch,
    ClaimEvidenceInsufficient,
    ClaimIntegrityError,
    MacroClaimTransmissionEvidenceInsufficient,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
    MacroEvidenceDraft,
)
from app.repositories.claim_evidence_link_repository import ClaimEvidenceLinkRepository
from app.repositories.claim_repository import ClaimRepository
from app.repositories.company_repository import CompanyRepository
from app.services.claim_service import ClaimService
from app.services.evidence_card_service import EvidenceCardService
from app.services.macro_evidence_service import MacroEvidenceService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_macro_evidence_service import _seed_macro_chain

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "2024年贵州茅台净利润增长情况？"
_STATEMENT = "2024年贵州茅台归属净利润同比增长15%。"
_MACRO_STATEMENT = "2024年中国GDP增速5.0%。"

_URL = "https://www.xinhuanet.com/2026/0809/0001.htm"
_URL_2 = "https://www.xinhuanet.com/2026/0809/0002.htm"
_URL_3 = "https://www.xinhuanet.com/2026/0809/0003.htm"


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
        # Claim 先于 Evidence（link→evidence_cards RESTRICT）。
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
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code="600519",
                identity_key="SSE:600519",
                board="sse_main",
                official_name="测试公司",
                short_name="测试",
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


async def _seed_other_company(sessionmaker) -> UUID:
    """另一家 A 股公司（claim 绑定错误公司的场景）。"""
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SZSE",
                security_code="000001",
                identity_key="SZSE:000001",
                board="szse_main",
                official_name="其他公司",
                short_name="其他",
                listing_status="listed",
                identity_source_provider_key="szse",
                identity_source_url="https://www.szse.cn",
            )
        )
        await session.commit()
    return company_id


async def _seed_document_card(
    env: dict,
    *,
    critical_claim_eligible: bool = False,
    statement: str = _STATEMENT,
    source_url: str | None = None,
) -> dict:
    """真实 HTML 链 → EvidenceCardService 创建 document EvidenceCard。

    返回 {evidence_card_id, source_id, parsed_source_id, chunk_set_id, chunk_id}。
    """
    src, parsed_id, cs_id, chunks = await _seed_html_source(
        env,
        critical_claim_eligible=critical_claim_eligible,
        source_url=source_url if source_url is not None else _URL,
    )
    chunk = chunks[0]
    draft = EvidenceCardDraft(
        research_question=_QUESTION,
        evidence_statement=statement,
        evidence_type=EvidenceType.METRIC,
        chunk_id=chunk.chunk_id,
        quote_start=0,
        quote_end=20,
        extractor_name="test-extractor",
        extractor_version=1,
        extractor_model_id="test-model",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    result = await EvidenceCardService(env["sessionmaker"]).create_card(draft)
    return {
        "evidence_card_id": result.evidence_card_id,
        "source_id": src,
        "parsed_source_id": parsed_id,
        "chunk_set_id": cs_id,
        "chunk_id": chunk.chunk_id,
    }


async def _seed_macro_card(env: dict, monkeypatch) -> dict:
    """真实 macro 链 → MacroEvidenceService 创建 macro_observation EvidenceCard。

    返回 {evidence_card_id, chain}（chain 含 series/snapshot/observation id）。
    """
    chain = await _seed_macro_chain(env, monkeypatch)
    draft = MacroEvidenceDraft(
        company_id=env["company_id"],
        research_question=_QUESTION,
        macro_observation_id=chain["observation_id"],
        evidence_statement=_MACRO_STATEMENT,
        extractor_name="macro-extractor",
        extractor_version=1,
        extractor_model_id="deepseek:deepseek-v4-flash",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    result = await MacroEvidenceService(env["sessionmaker"]).create_macro_card(draft)
    return {"evidence_card_id": result.evidence_card_id, "chain": chain}


def _claim_draft(
    env: dict,
    *,
    supports=(),
    contradicts=(),
    context=(),
    domain=ClaimAnalysisDomain.FINANCIAL,
    importance=ClaimImportance.NORMAL,
    statement=_STATEMENT,
    **overrides,
) -> ClaimDraft:
    values = dict(
        company_id=env["company_id"],
        research_question=_QUESTION,
        statement=statement,
        analysis_domain=domain,
        claim_kind=ClaimKind.FACT,
        confidence=ClaimConfidence.HIGH,
        importance=importance,
        support_evidence_ids=list(supports),
        contradict_evidence_ids=list(contradicts),
        context_evidence_ids=list(context),
        analyst_name="structured-analyst",
        analyst_version=1,
        analyst_model_id="deepseek:deepseek-v4-flash",
    )
    values.update(overrides)
    return ClaimDraft(**values)


async def _create_claim(env: dict, draft: ClaimDraft):
    return await ClaimService(env["sessionmaker"]).create_claim(draft)


async def _claim_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text("SELECT count(*) FROM claims"))).scalar_one())


async def _link_rows(sessionmaker, claim_id: UUID) -> list[tuple[str, str]]:
    async with sessionmaker() as session:
        links = await ClaimEvidenceLinkRepository(session).list_by_claim(claim_id)
        return sorted((str(link.evidence_card_id), link.relation) for link in links)


# ---------------------------------------------------------------- 创建


async def test_create_document_claim_persists_claim_and_links(env) -> None:
    doc = await _seed_document_card(env)
    result = await _create_claim(env, _claim_draft(env, supports=[doc["evidence_card_id"]]))
    assert result.replayed is False
    assert len(result.claim_fingerprint) == 64

    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.company_id == env["company_id"]
    assert claim.research_question == _QUESTION
    assert claim.research_question_sha256 == compute_research_question_sha256(_QUESTION)
    assert claim.statement == _STATEMENT
    assert claim.analysis_domain == "financial"
    assert claim.claim_kind == "fact"
    assert claim.confidence == "high"
    assert claim.importance == "normal"
    assert claim.analyst_name == "structured-analyst"
    assert claim.analyst_version == 1
    assert claim.analyst_model_id == "deepseek:deepseek-v4-flash"
    assert claim.claim_schema_version == CLAIM_SCHEMA_VERSION
    assert claim.claim_fingerprint == result.claim_fingerprint
    assert claim.created_at is not None
    assert await _link_rows(env["sessionmaker"], result.claim_id) == [
        (str(doc["evidence_card_id"]), "supports")
    ]


async def test_create_macro_claim_persists_macro_domain_and_links(env, monkeypatch) -> None:
    macro = await _seed_macro_card(env, monkeypatch)
    doc = await _seed_document_card(env)
    result = await _create_claim(
        env,
        _claim_draft(
            env,
            supports=[macro["evidence_card_id"]],
            context=[doc["evidence_card_id"]],
            domain=ClaimAnalysisDomain.MACRO,
        ),
    )
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.analysis_domain == "macro"
    assert await _link_rows(env["sessionmaker"], result.claim_id) == sorted(
        [
            (str(macro["evidence_card_id"]), "supports"),
            (str(doc["evidence_card_id"]), "context"),
        ]
    )


async def test_mixed_relations_persist_supports_contradicts_context(env) -> None:
    a = await _seed_document_card(env, statement="支持表述", source_url=_URL)
    b = await _seed_document_card(env, statement="反对表述", source_url=_URL_2)
    c = await _seed_document_card(env, statement="背景表述", source_url=_URL_3)
    result = await _create_claim(
        env,
        _claim_draft(
            env,
            supports=[a["evidence_card_id"]],
            contradicts=[b["evidence_card_id"]],
            context=[c["evidence_card_id"]],
        ),
    )
    assert await _link_rows(env["sessionmaker"], result.claim_id) == sorted(
        [
            (str(a["evidence_card_id"]), "supports"),
            (str(b["evidence_card_id"]), "contradicts"),
            (str(c["evidence_card_id"]), "context"),
        ]
    )


# ---------------------------------------------------------------- 拒绝


async def test_company_mismatch_rejected(env) -> None:
    doc = await _seed_document_card(env)
    other_company = await _seed_other_company(env["sessionmaker"])
    draft = _claim_draft(env, supports=[doc["evidence_card_id"]], company_id=other_company)
    with pytest.raises(ClaimEvidenceCompanyMismatch):
        await _create_claim(env, draft)
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_missing_evidence_rejected(env) -> None:
    doc = await _seed_document_card(env)
    ghost = uuid4()
    draft = _claim_draft(env, supports=[doc["evidence_card_id"], ghost])
    with pytest.raises(ClaimEvidenceCompanyMismatch):
        await _create_claim(env, draft)
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_no_supports_rejected(env) -> None:
    doc = await _seed_document_card(env)
    draft = _claim_draft(env, supports=[], context=[doc["evidence_card_id"]])
    with pytest.raises(ClaimEvidenceInsufficient):
        await _create_claim(env, draft)
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_critical_without_eligible_support_rejected(env) -> None:
    doc = await _seed_document_card(env, critical_claim_eligible=False)
    draft = _claim_draft(
        env, supports=[doc["evidence_card_id"]], importance=ClaimImportance.CRITICAL
    )
    with pytest.raises(ClaimCriticalEvidenceInsufficient):
        await _create_claim(env, draft)


async def test_critical_with_eligible_support_accepted(env) -> None:
    doc = await _seed_document_card(env, critical_claim_eligible=True)
    result = await _create_claim(
        env,
        _claim_draft(env, supports=[doc["evidence_card_id"]], importance=ClaimImportance.CRITICAL),
    )
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.importance == "critical"


async def test_macro_without_macro_support_rejected(env) -> None:
    doc = await _seed_document_card(env)
    draft = _claim_draft(env, supports=[doc["evidence_card_id"]], domain=ClaimAnalysisDomain.MACRO)
    with pytest.raises(MacroClaimTransmissionEvidenceInsufficient):
        await _create_claim(env, draft)


async def test_macro_without_document_transmission_rejected(env, monkeypatch) -> None:
    macro = await _seed_macro_card(env, monkeypatch)
    draft = _claim_draft(
        env, supports=[macro["evidence_card_id"]], domain=ClaimAnalysisDomain.MACRO
    )
    with pytest.raises(MacroClaimTransmissionEvidenceInsufficient):
        await _create_claim(env, draft)


async def test_macro_valid_transmission_structure_accepted(env, monkeypatch) -> None:
    macro = await _seed_macro_card(env, monkeypatch)
    doc = await _seed_document_card(env)
    result = await _create_claim(
        env,
        _claim_draft(
            env,
            supports=[macro["evidence_card_id"]],
            context=[doc["evidence_card_id"]],
            domain=ClaimAnalysisDomain.MACRO,
        ),
    )
    assert result.replayed is False
    assert await _claim_count(env["sessionmaker"]) == 1


async def test_cross_relation_duplicate_rejected_by_database(env) -> None:
    """DB 层 UNIQUE(claim_id, evidence_card_id)（migration 0019）强制：
    同 claim + 同 evidence 已存在 supports 后，直接 SQL 插入 contradicts 被拒。
    """
    doc = await _seed_document_card(env)
    result = await _create_claim(env, _claim_draft(env, supports=[doc["evidence_card_id"]]))
    with pytest.raises(IntegrityError):
        async with env["sessionmaker"]() as session:
            await session.execute(
                text(
                    "INSERT INTO claim_evidence_links (claim_id, evidence_card_id, relation) "
                    "VALUES (CAST(:c AS uuid), CAST(:e AS uuid), 'contradicts')"
                ).bindparams(c=result.claim_id, e=doc["evidence_card_id"])
            )
            await session.commit()
    # 拒绝后不残留 contradicts 行，原 supports 行保留。
    async with env["sessionmaker"]() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT relation FROM claim_evidence_links "
                        "WHERE claim_id = :c AND evidence_card_id = :e"
                    ).bindparams(c=result.claim_id, e=doc["evidence_card_id"])
                )
            )
            .scalars()
            .all()
        )
    assert rows == ["supports"]


# ---------------------------------------------------------------- replay / 并发


async def test_fingerprint_deterministic_for_identical_drafts(env) -> None:
    doc = await _seed_document_card(env)
    a = await _create_claim(env, _claim_draft(env, supports=[doc["evidence_card_id"]]))
    b = await _create_claim(env, _claim_draft(env, supports=[doc["evidence_card_id"]]))
    assert a.claim_fingerprint == b.claim_fingerprint
    assert a.claim_id == b.claim_id
    assert b.replayed is True
    assert await _claim_count(env["sessionmaker"]) == 1


async def test_replay_returns_same_claim(env) -> None:
    doc = await _seed_document_card(env)
    first = await _create_claim(env, _claim_draft(env, supports=[doc["evidence_card_id"]]))
    second = await _create_claim(env, _claim_draft(env, supports=[doc["evidence_card_id"]]))
    assert first.replayed is False
    assert second.replayed is True
    assert first.claim_id == second.claim_id
    assert await _claim_count(env["sessionmaker"]) == 1


async def test_concurrent_create_yields_single_claim(env) -> None:
    doc = await _seed_document_card(env)
    draft = _claim_draft(env, supports=[doc["evidence_card_id"]])
    service = ClaimService(env["sessionmaker"])
    results = await asyncio.gather(*(service.create_claim(draft) for _ in range(5)))
    ids = {r.claim_id for r in results}
    assert len(ids) == 1
    assert sum(1 for r in results if r.replayed) == 4
    assert await _claim_count(env["sessionmaker"]) == 1
    claim_id = next(iter(ids))
    assert await _link_rows(env["sessionmaker"], claim_id) == [
        (str(doc["evidence_card_id"]), "supports")
    ]


async def test_statement_change_creates_new_claim(env) -> None:
    doc = await _seed_document_card(env)
    a = await _create_claim(
        env, _claim_draft(env, supports=[doc["evidence_card_id"]], statement="观点A")
    )
    b = await _create_claim(
        env, _claim_draft(env, supports=[doc["evidence_card_id"]], statement="观点B")
    )
    assert a.claim_id != b.claim_id
    assert b.replayed is False
    assert await _claim_count(env["sessionmaker"]) == 2  # 旧 Claim 保留


async def test_evidence_relation_change_creates_new_claim(env) -> None:
    a = await _seed_document_card(env, statement="支持A", source_url=_URL)
    b = await _seed_document_card(env, statement="背景B", source_url=_URL_2)
    # A supports + B context
    first = await _create_claim(
        env, _claim_draft(env, supports=[a["evidence_card_id"]], context=[b["evidence_card_id"]])
    )
    # 同一对证据关系交换（A context + B supports）→ 新指纹 → 新 Claim。
    second = await _create_claim(
        env, _claim_draft(env, supports=[b["evidence_card_id"]], context=[a["evidence_card_id"]])
    )
    assert first.claim_id != second.claim_id
    assert second.replayed is False
    assert await _claim_count(env["sessionmaker"]) == 2


async def test_analyst_version_change_creates_new_claim(env) -> None:
    doc = await _seed_document_card(env)
    a = await _create_claim(
        env, _claim_draft(env, supports=[doc["evidence_card_id"]], analyst_version=1)
    )
    b = await _create_claim(
        env, _claim_draft(env, supports=[doc["evidence_card_id"]], analyst_version=2)
    )
    assert a.claim_id != b.claim_id
    assert await _claim_count(env["sessionmaker"]) == 2


# ---------------------------------------------------------------- create_claim_batch


async def test_create_claim_batch_multiple_claims(env) -> None:
    a = await _seed_document_card(env, statement="支持A", source_url=_URL)
    b = await _seed_document_card(env, statement="支持B", source_url=_URL_2)
    drafts = [
        _claim_draft(env, supports=[a["evidence_card_id"]], statement="观点A"),
        _claim_draft(env, supports=[b["evidence_card_id"]], statement="观点B"),
    ]
    batch = await ClaimService(env["sessionmaker"]).create_claim_batch(drafts)
    assert len(batch.claim_ids) == 2
    assert len(batch.created) == 2
    assert len(batch.replayed) == 0
    assert await _claim_count(env["sessionmaker"]) == 2
    for claim_id in batch.claim_ids:
        links = await _link_rows(env["sessionmaker"], claim_id)
        assert len(links) == 1
        assert links[0][1] == "supports"


async def test_create_claim_batch_rejects_out_of_range(env) -> None:
    doc = await _seed_document_card(env)
    draft = _claim_draft(env, supports=[doc["evidence_card_id"]])
    service = ClaimService(env["sessionmaker"])
    with pytest.raises(ClaimDraftError):
        await service.create_claim_batch([])
    with pytest.raises(ClaimDraftError):
        await service.create_claim_batch([draft] * 6)
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_create_claim_batch_all_or_nothing_on_policy_failure(env) -> None:
    a = await _seed_document_card(env, statement="支持A", source_url=_URL)
    good = _claim_draft(env, supports=[a["evidence_card_id"]], statement="观点A")
    bad = _claim_draft(env, supports=[], statement="无证据观点")  # no supports
    with pytest.raises(ClaimEvidenceInsufficient):
        await ClaimService(env["sessionmaker"]).create_claim_batch([good, bad])
    # all-drafts-validate-first：good 也未写入 → 0 写（无 partial writes）。
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_create_claim_batch_replays_existing_claim(env) -> None:
    doc = await _seed_document_card(env)
    draft = _claim_draft(env, supports=[doc["evidence_card_id"]])
    first = await ClaimService(env["sessionmaker"]).create_claim_batch([draft])
    second = await ClaimService(env["sessionmaker"]).create_claim_batch([draft])
    assert first.claim_ids == second.claim_ids
    assert len(second.replayed) == 1
    assert len(second.created) == 0
    assert await _claim_count(env["sessionmaker"]) == 1


# ---------------------------------------------------------------- integrity


async def test_replay_corrupted_link_raises_integrity_error(env) -> None:
    doc = await _seed_document_card(env)
    draft = _claim_draft(env, supports=[doc["evidence_card_id"]])
    await _create_claim(env, draft)
    # 篡改：删除 link（replay 时 link 数量 / Evidence ID 校验失败）。
    async with env["sessionmaker"]() as session:
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.commit()
    with pytest.raises(ClaimIntegrityError):
        await _create_claim(env, draft)


async def test_replay_corrupted_claim_row_raises_integrity_error(env) -> None:
    doc = await _seed_document_card(env)
    draft = _claim_draft(env, supports=[doc["evidence_card_id"]])
    await _create_claim(env, draft)
    # 篡改已落库 Claim 的 statement（fingerprint 列不变）：replay 时逐字段比对失败。
    async with env["sessionmaker"]() as session:
        await session.execute(text("UPDATE claims SET statement = '篡改'"))
        await session.commit()
    with pytest.raises(ClaimIntegrityError):
        await _create_claim(env, draft)


async def test_claim_repository_has_no_update_api(env) -> None:
    assert not hasattr(ClaimRepository, "update")
    assert not hasattr(ClaimRepository, "update_by_id")


async def test_evidence_card_rows_never_modified(env) -> None:
    doc = await _seed_document_card(env)
    async with env["sessionmaker"]() as session:
        before = (await session.execute(select(EvidenceCardModel))).scalars().all()
        before_state = [
            (c.evidence_statement, c.quote_text, c.quote_start, c.quote_end, c.evidence_fingerprint)
            for c in before
        ]
    await _create_claim(env, _claim_draft(env, supports=[doc["evidence_card_id"]]))
    await _create_claim(env, _claim_draft(env, supports=[doc["evidence_card_id"]]))  # replay
    async with env["sessionmaker"]() as session:
        after = (await session.execute(select(EvidenceCardModel))).scalars().all()
        after_state = [
            (c.evidence_statement, c.quote_text, c.quote_start, c.quote_end, c.evidence_fingerprint)
            for c in after
        ]
    assert len(after) == len(before)  # 不新增 Evidence 行
    assert after_state == before_state  # 不改写既有 Evidence 行


# ---------------------------------------------------------------- E2E 回溯


async def test_document_claim_e2e_provenance_trace(env) -> None:
    doc = await _seed_document_card(env)
    result = await _create_claim(env, _claim_draft(env, supports=[doc["evidence_card_id"]]))
    async with env["sessionmaker"]() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT c.company_id AS claim_company, c.research_question_sha256, "
                        "       l.relation, l.evidence_card_id, "
                        "       ec.company_id AS ec_company, ec.origin_type, "
                        "       ec.chunk_id, ec.chunk_set_id, ec.parsed_source_id, ec.source_id, "
                        "       dc.chunk_set_id AS dc_set, "
                        "       ps.source_id AS ps_source, "
                        "       sr.company_id AS src_company, sr.artifact_id AS src_artifact "
                        "FROM claims c "
                        "JOIN claim_evidence_links l ON l.claim_id = c.claim_id "
                        "JOIN evidence_cards ec ON ec.evidence_card_id = l.evidence_card_id "
                        "JOIN document_chunks dc ON dc.chunk_id = ec.chunk_id "
                        "JOIN parsed_sources ps ON ps.parsed_source_id = ec.parsed_source_id "
                        "JOIN source_records sr ON sr.source_id = ec.source_id "
                        "WHERE c.claim_id = :cid"
                    ).bindparams(cid=result.claim_id)
                )
            )
            .mappings()
            .one()
        )
    assert row["claim_company"] == env["company_id"]
    assert row["ec_company"] == env["company_id"]
    assert row["src_company"] == env["company_id"]
    assert row["relation"] == "supports"
    assert row["evidence_card_id"] == doc["evidence_card_id"]
    assert row["origin_type"] == "document_chunk"
    assert row["chunk_id"] == doc["chunk_id"]
    assert row["chunk_set_id"] == doc["chunk_set_id"]
    assert row["dc_set"] == doc["chunk_set_id"]
    assert row["parsed_source_id"] == doc["parsed_source_id"]
    assert row["source_id"] == doc["source_id"]
    assert row["ps_source"] == doc["source_id"]
    assert row["src_artifact"] is not None
    assert row["research_question_sha256"] == compute_research_question_sha256(_QUESTION)


async def test_macro_claim_e2e_provenance_trace(env, monkeypatch) -> None:
    macro = await _seed_macro_card(env, monkeypatch)
    doc = await _seed_document_card(env)
    result = await _create_claim(
        env,
        _claim_draft(
            env,
            supports=[macro["evidence_card_id"]],
            context=[doc["evidence_card_id"]],
            domain=ClaimAnalysisDomain.MACRO,
        ),
    )
    async with env["sessionmaker"]() as session:
        # Claim → link → Evidence → Observation / Snapshot / Series（1:1）
        row = (
            (
                await session.execute(
                    text(
                        "SELECT c.analysis_domain, l.relation, l.evidence_card_id, "
                        "       ec.origin_type, ec.macro_observation_id, "
                        "       ec.macro_snapshot_id, ec.macro_series_id, "
                        "       mo.snapshot_id AS mo_snapshot, mo.period, "
                        "       ms.series_id AS ms_series "
                        "FROM claims c "
                        "JOIN claim_evidence_links l ON l.claim_id = c.claim_id "
                        "JOIN evidence_cards ec ON ec.evidence_card_id = l.evidence_card_id "
                        "JOIN macro_observations mo ON mo.observation_id = ec.macro_observation_id "
                        "JOIN macro_dataset_snapshots ms ON ms.snapshot_id = ec.macro_snapshot_id "
                        "WHERE c.claim_id = :cid AND ec.origin_type = 'macro_observation'"
                    ).bindparams(cid=result.claim_id)
                )
            )
            .mappings()
            .one()
        )
        # Snapshot 的 RawArtifact 真实落盘且可回溯。
        art = (
            (
                await session.execute(
                    text(
                        "SELECT ra.storage_key FROM macro_snapshot_artifacts msa "
                        "JOIN raw_artifacts ra ON ra.artifact_id = msa.artifact_id "
                        "WHERE msa.snapshot_id = :sid LIMIT 1"
                    ).bindparams(sid=macro["chain"]["snapshot_id"])
                )
            )
            .mappings()
            .first()
        )
    assert row["analysis_domain"] == "macro"
    assert row["relation"] == "supports"
    assert row["evidence_card_id"] == macro["evidence_card_id"]
    assert row["origin_type"] == "macro_observation"
    assert row["macro_observation_id"] == macro["chain"]["observation_id"]
    assert row["mo_snapshot"] == macro["chain"]["snapshot_id"]
    assert row["macro_snapshot_id"] == macro["chain"]["snapshot_id"]
    assert row["ms_series"] == macro["chain"]["series_id"]
    assert row["macro_series_id"] == macro["chain"]["series_id"]
    assert row["period"] == "2024"
    assert art is not None
    with env["raw_store"].open(art["storage_key"]) as stored:
        assert len(stored.read()) > 0


# ---------------------------------------------------------------- 边界


async def test_claims_tables_exist_and_no_stage5_report_tables(env) -> None:
    async with env["sessionmaker"]() as session:
        claim_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name IN ('claims', 'claim_evidence_links')"
                )
            )
        ).scalar_one()
        assert claim_tables == 2
        stage5_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name IN "
                    "('report_outlines','report_sections','reports','review_issues')"
                )
            )
        ).scalar_one()
        assert stage5_tables == 0


async def test_claim_service_takes_only_sessionmaker(env) -> None:
    service = ClaimService(env["sessionmaker"])
    # Service 只持有 sessionmaker：无 LLM / LangGraph / Report provider。
    assert set(service.__dict__) == {"_sessionmaker"}
