"""MacroClaimService integration tests (stage 4C.1A).

需要真实 PostgreSQL（127.0.0.1:5433）。macro Evidence 走真实
WorldBankProvider(MockTransport) + MacroPersistenceService + MacroEvidenceService；
company exposure Evidence 走真实 HTML 服务链 + EvidenceCardService。**零 Chroma /
零 LLM / 零 LangGraph / 零 Report / 零 Audit**。

覆盖：
- 创建：Claim(schema=4, domain=macro) + MacroTransmissionChain + transmission
  links（macro_driver / company_exposure role）+ ClaimEvidenceLinks（macro_driver /
  company_exposure 一律 relation=context）原子落库；
- origin：macro_driver 必须 macro_observation；company_exposure / observed_effect
  必须 document_chunk（违反 → MacroClaimOriginViolation）；
- temporal：已知时间晚于 analysis_as_of → MacroClaimFutureEvidence；无可用时间
  → MacroClaimTemporalEvidenceInsufficient（不伪造缺失日期）；
- critical：需 eligible macro_driver 且 eligible company_exposure；observed_impact
  额外需 eligible observed_effect；additional support 不能替代两条传导腿；
- impact-status：observed_impact 无 observed_effect → MacroClaimImpactStatusInsufficient；
- replay：同 fingerprint 复用同一 Claim + 同一 Transmission，replayed=True；
  并发 → 1 Claim + 1 Chain；篡改 → MacroClaimIntegrityError，**不自动 repair**；
- additional 证据 relation（supports/context）原样保留；time_alignment 不自动猜测；
- E2E provenance：Claim → Chain → {macro 卡, doc 卡} → Observation/Source → Artifact；
- 边界：macro_transmission_* 存在 / Stage 5 report 表不得存在；Service 只持有
  sessionmaker。
"""

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.claims.contracts import ClaimKind
from app.claims.macro_contracts import (
    MACRO_CLAIM_SCHEMA_VERSION,
    MACRO_TRANSMISSION_SCHEMA_VERSION,
    MacroChannelType,
    MacroClaimConfidence,
    MacroClaimDraft,
    MacroClaimImportance,
    MacroEffectDirection,
    MacroImpactStatus,
    MacroTimeAlignment,
)
from app.claims.macro_errors import (
    MacroClaimCriticalEvidenceInsufficient,
    MacroClaimEvidenceCompanyMismatch,
    MacroClaimEvidenceNotFound,
    MacroClaimFutureEvidence,
    MacroClaimImpactStatusInsufficient,
    MacroClaimIntegrityError,
    MacroClaimOriginViolation,
    MacroClaimTemporalEvidenceInsufficient,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
    MacroEvidenceDraft,
)
from app.repositories.claim_repository import ClaimRepository
from app.repositories.company_repository import CompanyRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.macro_transmission_evidence_link_repository import (
    MacroTransmissionEvidenceLinkRepository,
)
from app.repositories.macro_transmission_repository import MacroTransmissionRepository
from app.services.evidence_card_service import EvidenceCardService
from app.services.macro_claim_service import MacroClaimService
from app.services.macro_evidence_service import MacroEvidenceService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_macro_evidence_service import _seed_macro_chain

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "利率上行对贵州茅台融资成本的影响？"
_STATEMENT = "若利率持续上行，公司融资成本存在上升压力。"
_ANALYSIS_AS_OF = date(2026, 8, 10)


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
        await session.execute(text("DELETE FROM macro_transmission_evidence_links"))
        await session.execute(text("DELETE FROM macro_transmission_chains"))
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
    # world_bank（macro 链）+ xinhuanet（document 卡）等全部默认 provider。
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


async def _seed_macro_card(
    env: dict,
    monkeypatch,
    *,
    critical_claim_eligible: bool = True,
    statement: str = "2024年中国人口为14.1亿人（世界银行 SP.POP.TOTL）。",
) -> tuple[UUID, dict]:
    """真实 macro 链 → MacroEvidenceService 登记一张 macro_observation EvidenceCard。

    critical_claim_eligible=False 时先 UPDATE snapshot 的 eligibility 快照
    （证明卡片复制 provenance、不硬编码 World Bank tier）。statement 不同 →
    不同 evidence fingerprint → 新卡（同链可登记多张 macro 卡）。
    """
    chain = await _seed_macro_chain(env, monkeypatch)
    if not critical_claim_eligible:
        async with env["sessionmaker"]() as session:
            await session.execute(
                text(
                    "UPDATE macro_dataset_snapshots SET critical_claim_eligible_snapshot = false "
                    "WHERE snapshot_id = :sid"
                ),
                {"sid": chain["snapshot_id"]},
            )
            await session.commit()
    draft = MacroEvidenceDraft(
        company_id=env["company_id"],
        research_question="利率上行对中国企业融资成本的影响？",
        macro_observation_id=chain["observation_id"],
        evidence_statement=statement,
        extractor_name="macro-extractor",
        extractor_version=1,
        extractor_model_id="deepseek:deepseek-v4-flash",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    result = await MacroEvidenceService(env["sessionmaker"]).create_macro_card(draft)
    return result.evidence_card_id, chain


async def _seed_document_card(
    env: dict,
    *,
    critical_claim_eligible: bool = False,
    published_at=datetime(2026, 8, 7, 9, 30, tzinfo=UTC),
    reporting_period_end: date | None = None,
    statement: str = "2024年贵州茅台营业收入同比增长15%。",
) -> UUID:
    """真实 HTML 链 → EvidenceCardService 创建一张 document_chunk EvidenceCard。"""
    src, parsed_id, cs_id, chunks = await _seed_html_source(
        env,
        critical_claim_eligible=critical_claim_eligible,
        published_at=published_at,
        reporting_period_end=reporting_period_end,
        source_url=f"https://www.xinhuanet.com/2026/0809/{uuid4().hex[:8]}.htm",
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
    return result.evidence_card_id


async def _seed_other_company(env: dict) -> UUID:
    other = uuid4()
    async with env["sessionmaker"]() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=other,
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
    return other


def _draft(
    env: dict,
    *,
    macro_driver: list[UUID],
    company_exposure: list[UUID],
    observed_effect: list[UUID] | None = None,
    **overrides,
) -> MacroClaimDraft:
    values = dict(
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_ANALYSIS_AS_OF,
        statement=_STATEMENT,
        claim_kind=ClaimKind.RISK,
        confidence=MacroClaimConfidence.MEDIUM,
        importance=MacroClaimImportance.NORMAL,
        channel_type=MacroChannelType.FINANCING,
        effect_direction=MacroEffectDirection.HEADWIND,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT,
        time_alignment=MacroTimeAlignment.ALIGNED,
        macro_driver_evidence_ids=macro_driver,
        company_exposure_evidence_ids=company_exposure,
        observed_effect_evidence_ids=observed_effect or [],
        additional_support_evidence_ids=[],
        additional_contradict_evidence_ids=[],
        additional_context_evidence_ids=[],
        analyst_name="macro-analyst",
        analyst_version=1,
        analyst_model_id="deepseek:deepseek-v4-flash",
    )
    values.update(overrides)
    return MacroClaimDraft(**values)


def _service(env: dict) -> MacroClaimService:
    return MacroClaimService(env["sessionmaker"])


async def _claim_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM claims WHERE claim_schema_version = 4")
                )
            ).scalar_one()
        )


async def _macro_tables_count(sessionmaker, table: str) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


# ---------------------------------------------------------------- 创建


async def test_create_macro_claim_persists_transmission_provenance(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    result = await _service(env).create_claim(
        _draft(env, macro_driver=[macro_card], company_exposure=[doc_card])
    )

    assert result.replayed is False
    assert len(result.claim_fingerprint) == 64
    assert len(result.transmission_fingerprint) == 64
    assert await _claim_count(env["sessionmaker"]) == 1
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_chains") == 1
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_evidence_links") == 2

    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
        assert claim is not None
        assert claim.company_id == env["company_id"]
        assert claim.analysis_domain == "macro"
        assert claim.claim_kind == "risk"
        assert claim.claim_schema_version == MACRO_CLAIM_SCHEMA_VERSION
        assert claim.claim_fingerprint == result.claim_fingerprint

        chain_row = await MacroTransmissionRepository(session).get_by_claim_id(result.claim_id)
        assert chain_row is not None
        assert chain_row.transmission_id == result.transmission_id
        assert chain_row.company_id == env["company_id"]
        assert chain_row.channel_type == "financing"
        assert chain_row.effect_direction == "headwind"
        assert chain_row.impact_status == "plausible_impact"
        assert chain_row.time_alignment == "aligned"
        assert chain_row.transmission_schema_version == MACRO_TRANSMISSION_SCHEMA_VERSION
        assert chain_row.transmission_fingerprint == result.transmission_fingerprint

        trans_links = await MacroTransmissionEvidenceLinkRepository(session).list_by_transmission(
            result.transmission_id
        )
        by_card = {link.evidence_card_id: link.role for link in trans_links}
        assert by_card == {macro_card: "macro_driver", doc_card: "company_exposure"}

    # ClaimEvidenceLinks：macro_driver / company_exposure 一律 relation=context
    # （单条 macro 或单条 company 事实都不能独立证明"宏观→公司影响"）。
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT evidence_card_id, relation FROM claim_evidence_links "
                    "WHERE claim_id = :cid"
                ).bindparams(cid=result.claim_id)
            )
        ).all()
        relations = {(str(r[0]), r[1]) for r in rows}
        assert relations == {(str(macro_card), "context"), (str(doc_card), "context")}


async def test_macro_claim_locator_traces_observation_and_source(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    result = await _service(env).create_claim(
        _draft(env, macro_driver=[macro_card], company_exposure=[doc_card])
    )
    assert result.replayed is False

    async with env["sessionmaker"]() as session:
        macro_card_row = await EvidenceCardRepository(session).get_by_id(macro_card)
        doc_card_row = await EvidenceCardRepository(session).get_by_id(doc_card)
    assert macro_card_row is not None
    assert doc_card_row is not None
    # macro 卡 → MacroObservation 可用时间（真实 provenance，不伪造）。
    assert macro_card_row.origin_type == "macro_observation"
    # doc 卡 → SourceRecord 的 published_at 可用时间。
    assert doc_card_row.origin_type == "document_chunk"
    assert doc_card_row.source_published_at == datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
    assert doc_card_row.source_id is not None


# ---------------------------------------------------------------- origin 校验


async def test_macro_driver_must_be_macro_observation_origin(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    wrong_doc_card = await _seed_document_card(env, statement="错误当作宏观驱动的公司数据。")
    # 把 macro_driver 换成 document_chunk 卡 → origin 违反（company_exposure 仍是 doc 卡）。
    with pytest.raises(MacroClaimOriginViolation):
        await _service(env).create_claim(
            _draft(env, macro_driver=[wrong_doc_card], company_exposure=[doc_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_company_exposure_must_be_document_chunk_origin(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    # 第二张 macro 卡（不同 statement → 不同 fingerprint → 新卡）。
    other_macro, _ = await _seed_macro_card(
        env, monkeypatch, statement="2023年中国人口为14.1亿人（世界银行 SP.POP.TOTL）。"
    )
    assert other_macro != macro_card
    # company_exposure 用 macro_observation 卡 → origin 违反。
    with pytest.raises(MacroClaimOriginViolation):
        await _service(env).create_claim(
            _draft(env, macro_driver=[macro_card], company_exposure=[other_macro])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_observed_effect_must_be_document_chunk_origin(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    # 第二张 macro 卡（作 observed_effect → 违反 document_chunk 要求）。
    wrong_effect, _ = await _seed_macro_card(
        env, monkeypatch, statement="2022年中国人口为14.1亿人（世界银行 SP.POP.TOTL）。"
    )
    assert wrong_effect != macro_card
    doc_card = await _seed_document_card(env)
    with pytest.raises(MacroClaimOriginViolation):
        await _service(env).create_claim(
            _draft(
                env,
                macro_driver=[macro_card],
                company_exposure=[doc_card],
                observed_effect=[wrong_effect],
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- temporal


async def test_future_evidence_rejected(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    future_card = await _seed_document_card(env, published_at=datetime(2026, 9, 1, tzinfo=UTC))
    with pytest.raises(MacroClaimFutureEvidence):
        await _service(env).create_claim(
            _draft(env, macro_driver=[macro_card], company_exposure=[future_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_temporal_insufficient_when_no_usable_time(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    # document 卡无 published_at 且无 reporting_period_end → 无可用时间。
    timeless_card = await _seed_document_card(env, published_at=None, reporting_period_end=None)
    with pytest.raises(MacroClaimTemporalEvidenceInsufficient):
        await _service(env).create_claim(
            _draft(env, macro_driver=[macro_card], company_exposure=[timeless_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- critical policy


async def test_critical_requires_eligible_macro_driver_and_exposure(env, monkeypatch) -> None:
    ineligible_macro, _ = await _seed_macro_card(env, monkeypatch, critical_claim_eligible=False)
    eligible_doc = await _seed_document_card(env, critical_claim_eligible=True)
    # macro_driver 不 eligible → 拒绝（即使 company_exposure eligible）。
    with pytest.raises(MacroClaimCriticalEvidenceInsufficient):
        await _service(env).create_claim(
            _draft(
                env,
                macro_driver=[ineligible_macro],
                company_exposure=[eligible_doc],
                importance=MacroClaimImportance.CRITICAL,
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_critical_additional_support_cannot_substitute(env, monkeypatch) -> None:
    ineligible_macro, _ = await _seed_macro_card(env, monkeypatch, critical_claim_eligible=False)
    ineligible_doc = await _seed_document_card(env, critical_claim_eligible=False)
    eligible_doc = await _seed_document_card(env, critical_claim_eligible=True)
    # additional support（即使 eligible）不能替代 company_exposure 传导腿。
    with pytest.raises(MacroClaimCriticalEvidenceInsufficient):
        await _service(env).create_claim(
            _draft(
                env,
                macro_driver=[ineligible_macro],
                company_exposure=[ineligible_doc],
                importance=MacroClaimImportance.CRITICAL,
                additional_support_evidence_ids=[eligible_doc],
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_critical_with_eligible_both_accepted(env, monkeypatch) -> None:
    eligible_macro, chain = await _seed_macro_card(env, monkeypatch, critical_claim_eligible=True)
    eligible_doc = await _seed_document_card(env, critical_claim_eligible=True)
    result = await _service(env).create_claim(
        _draft(
            env,
            macro_driver=[eligible_macro],
            company_exposure=[eligible_doc],
            importance=MacroClaimImportance.CRITICAL,
        )
    )
    assert result.replayed is False
    assert await _claim_count(env["sessionmaker"]) == 1


async def test_critical_observed_impact_requires_eligible_observed_effect(env, monkeypatch) -> None:
    eligible_macro, chain = await _seed_macro_card(env, monkeypatch, critical_claim_eligible=True)
    eligible_doc = await _seed_document_card(env, critical_claim_eligible=True)
    ineligible_effect = await _seed_document_card(env, critical_claim_eligible=False)
    with pytest.raises(MacroClaimCriticalEvidenceInsufficient):
        await _service(env).create_claim(
            _draft(
                env,
                macro_driver=[eligible_macro],
                company_exposure=[eligible_doc],
                observed_effect=[ineligible_effect],
                importance=MacroClaimImportance.CRITICAL,
                impact_status=MacroImpactStatus.OBSERVED_IMPACT,
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- impact-status


async def test_observed_impact_without_observed_effect_rejected(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    with pytest.raises(MacroClaimImpactStatusInsufficient):
        await _service(env).create_claim(
            _draft(
                env,
                macro_driver=[macro_card],
                company_exposure=[doc_card],
                impact_status=MacroImpactStatus.OBSERVED_IMPACT,
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_observed_impact_with_observed_effect_accepted(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    effect_card = await _seed_document_card(env, statement="2024年下半年公司融资成本明显上升。")
    result = await _service(env).create_claim(
        _draft(
            env,
            macro_driver=[macro_card],
            company_exposure=[doc_card],
            observed_effect=[effect_card],
            impact_status=MacroImpactStatus.OBSERVED_IMPACT,
        )
    )
    assert result.replayed is False
    async with env["sessionmaker"]() as session:
        trans_links = await MacroTransmissionEvidenceLinkRepository(session).list_by_transmission(
            result.transmission_id
        )
        by_card = {link.evidence_card_id: link.role for link in trans_links}
        assert by_card[effect_card] == "observed_effect"
    # observed_effect 同样展开为 context。
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT evidence_card_id, relation FROM claim_evidence_links "
                    "WHERE claim_id = :cid"
                ).bindparams(cid=result.claim_id)
            )
        ).all()
        assert len(rows) == 3
        assert all(r[1] == "context" for r in rows)


# ---------------------------------------------------------------- replay / 并发


async def test_replay_returns_same_claim_and_transmission(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    service = _service(env)
    draft = _draft(env, macro_driver=[macro_card], company_exposure=[doc_card])
    first = await service.create_claim(draft)
    second = await service.create_claim(draft)

    assert first.replayed is False
    assert second.replayed is True
    assert first.claim_id == second.claim_id
    assert first.transmission_id == second.transmission_id
    assert await _claim_count(env["sessionmaker"]) == 1
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_chains") == 1
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_evidence_links") == 2


async def test_concurrent_create_yields_single_claim_and_chain(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    service = _service(env)
    draft = _draft(env, macro_driver=[macro_card], company_exposure=[doc_card])
    results = await asyncio.gather(*(service.create_claim(draft) for _ in range(5)))
    assert len({r.claim_id for r in results}) == 1
    assert len({r.transmission_id for r in results}) == 1
    assert sum(1 for r in results if r.replayed) == 4
    assert await _claim_count(env["sessionmaker"]) == 1
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_chains") == 1


async def test_corrupted_replay_raises_integrity_error_no_repair(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    service = _service(env)
    draft = _draft(env, macro_driver=[macro_card], company_exposure=[doc_card])
    await service.create_claim(draft)

    # 篡改已落库 Claim 的 statement（fingerprint 列不变）。
    async with env["sessionmaker"]() as session:
        await session.execute(text("UPDATE claims SET statement = '篡改'"))
        await session.commit()

    with pytest.raises(MacroClaimIntegrityError):
        await service.create_claim(draft)

    # 不自动 repair：篡改值仍在。
    async with env["sessionmaker"]() as session:
        value = (await session.execute(text("SELECT statement FROM claims"))).scalar_one()
        assert value == "篡改"


async def test_corrupted_transmission_raises_integrity_error(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    service = _service(env)
    draft = _draft(env, macro_driver=[macro_card], company_exposure=[doc_card])
    await service.create_claim(draft)

    # 篡改传导链的 channel_type（fingerprint 列不变）。
    async with env["sessionmaker"]() as session:
        await session.execute(text("UPDATE macro_transmission_chains SET channel_type = 'revenue'"))
        await session.commit()

    with pytest.raises(MacroClaimIntegrityError):
        await service.create_claim(draft)


# ---------------------------------------------------------------- 公司一致性


async def test_evidence_from_other_company_rejected(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    other_company = await _seed_other_company(env)
    # 把 document 卡改绑到其他公司（provenance 快照仍在，company 被篡改）。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE evidence_cards SET company_id = :other WHERE evidence_card_id = :cid"),
            {"other": other_company, "cid": doc_card},
        )
        await session.commit()

    with pytest.raises(MacroClaimEvidenceCompanyMismatch):
        await _service(env).create_claim(
            _draft(env, macro_driver=[macro_card], company_exposure=[doc_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_missing_evidence_rejected_no_partial_write(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    ghost = uuid4()
    with pytest.raises(MacroClaimEvidenceNotFound):
        await _service(env).create_claim(
            _draft(
                env,
                macro_driver=[macro_card],
                company_exposure=[doc_card],
                additional_context_evidence_ids=[ghost],
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_chains") == 0


# ---------------------------------------------------------------- 语义


async def test_additional_evidence_relations_preserved(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    support_card = await _seed_document_card(env, statement="行业利率上行证据（补充）。")
    context_card = await _seed_document_card(env, statement="公司负债结构背景。")
    result = await _service(env).create_claim(
        _draft(
            env,
            macro_driver=[macro_card],
            company_exposure=[doc_card],
            additional_support_evidence_ids=[support_card],
            additional_context_evidence_ids=[context_card],
        )
    )

    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT evidence_card_id, relation FROM claim_evidence_links "
                    "WHERE claim_id = :cid"
                ).bindparams(cid=result.claim_id)
            )
        ).all()
        by_card = {str(r[0]): r[1] for r in rows}
        # transmission 证据保持 context；additional 保持 supports/context 原样。
        assert by_card[str(macro_card)] == "context"
        assert by_card[str(doc_card)] == "context"
        assert by_card[str(support_card)] == "supports"
        assert by_card[str(context_card)] == "context"


async def test_time_alignment_not_auto_guessed(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    result = await _service(env).create_claim(
        _draft(
            env,
            macro_driver=[macro_card],
            company_exposure=[doc_card],
            time_alignment=MacroTimeAlignment.UNCERTAIN,
        )
    )
    async with env["sessionmaker"]() as session:
        chain_row = await MacroTransmissionRepository(session).get_by_claim_id(result.claim_id)
    assert chain_row is not None
    # 时间对应（对齐/不确定）由分析师判定，程序不自动猜测。
    assert chain_row.time_alignment == "uncertain"


# ---------------------------------------------------------------- E2E provenance


async def test_e2e_provenance_claim_to_observation_and_source(env, monkeypatch) -> None:
    from app.db.models.macro_observation import MacroObservationModel
    from app.db.models.raw_artifact import RawArtifactModel
    from app.db.models.source_record import SourceRecordModel

    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    result = await _service(env).create_claim(
        _draft(env, macro_driver=[macro_card], company_exposure=[doc_card])
    )
    assert result.replayed is False

    async with env["sessionmaker"]() as session:
        # 传导链 → macro 卡 → Observation（可用时间 2024-01-01）。
        macro_card_row = await EvidenceCardRepository(session).get_by_id(macro_card)
        obs = await session.get(MacroObservationModel, macro_card_row.macro_observation_id)
        assert obs is not None
        assert obs.normalized_period_start == date(2024, 1, 1)

        # 传导链 → doc 卡 → SourceRecord → RawArtifact。
        doc_card_row = await EvidenceCardRepository(session).get_by_id(doc_card)
        source = await session.get(SourceRecordModel, doc_card_row.source_id)
        assert source is not None
        artifact = await session.get(RawArtifactModel, source.artifact_id)
        assert artifact is not None
        assert source.provider_key == "xinhuanet"
        assert artifact.storage_key


# ---------------------------------------------------------------- 边界


async def test_macro_claim_boundary_no_stage5_tables(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    await _service(env).create_claim(
        _draft(env, macro_driver=[macro_card], company_exposure=[doc_card])
    )

    async with env["sessionmaker"]() as session:
        macro_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name IN "
                    "('macro_transmission_chains','macro_transmission_evidence_links')"
                )
            )
        ).scalar_one()
        assert macro_tables == 2
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


async def test_macro_claim_service_takes_only_sessionmaker(env, monkeypatch) -> None:
    service = _service(env)
    assert set(service.__dict__) == {"_sessionmaker"}
