"""ClaimSynthesisService integration tests (stage 4D.1A, spec V).

需要真实 PostgreSQL（127.0.0.1:5433）。公司 / Evidence / Calculation /
Comparison / Claim 全部用真实服务链 seed（复用各 domain 测试的 helpers）；
synthesis 经 SynthesisService.create_or_get_synthesis 登记。**零 Chroma / 零
LLM / 零 LangGraph / 零 Report / 零 Audit**。

覆盖（spec V）：
- Integrity gateway：generic（business / event / risk）、financial、macro、
  valuation dispatch 全部可验证；缺失 Claim → IntegrityError；不受支持的
  schema version → UnsupportedClaimSchema；fingerprint / evidence link 损坏 →
  IntegrityError（**不自动 repair**）；
- Isolation：company 不一致 → SynthesisCompanyMismatch；research_question 不
  一致 → SynthesisResearchQuestionMismatch；
- Temporal no-lookahead：document published_at / macro snapshot fetched_at /
  macro chain analysis_as_of / valuation profile analysis_as_of 晚于 cutoff →
  SynthesisFutureEvidence（resolve_availability 的 None → Insufficient 映射在
  tests/synthesis/test_contracts.py 单元层覆盖）；
- Persistence / replay：首次创建原子落库（1 run + 4 input links，字段核对）；
  同 fingerprint replay 同 run；input 顺序无关；claim set / cutoff 变化 → 新
  run（旧 run 保留）；并发同 draft → 1 run；link 损坏 / claim 损坏 →
  IntegrityError；Claim / Evidence 永不改写；
- Cross-domain E2E：同一 company + 同一 research_question 下 1 business + 1
  financial + 1 macro + 1 valuation Claim → 1 SynthesisRun → 4 条精确 input
  links → replay 同一 run；
- Summary：确定性 domain / kind / confidence / importance 计数；
- 边界：无 Stage 5 report 表；Service 只持有 sessionmaker。
"""

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.claims.contracts import (
    CLAIM_SCHEMA_VERSION,
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimDraft,
    ClaimImportance,
    ClaimKind,
    compute_research_question_sha256,
)
from app.claims.macro_contracts import MACRO_CLAIM_SCHEMA_VERSION
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.financial.calculations.service import FinancialCalculationService
from app.repositories.claim_synthesis_input_link_repository import (
    ClaimSynthesisInputLinkRepository,
)
from app.repositories.claim_synthesis_run_repository import ClaimSynthesisRunRepository
from app.services.claim_service import ClaimService
from app.services.macro_claim_service import MacroClaimService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.contracts import (
    CLAIM_SYNTHESIS_SCHEMA_VERSION,
    SynthesisInputDraft,
)
from app.synthesis.errors import (
    SynthesisClaimIntegrityError,
    SynthesisCompanyMismatch,
    SynthesisFutureEvidence,
    SynthesisIntegrityError,
    SynthesisResearchQuestionMismatch,
    SynthesisUnsupportedClaimSchema,
)
from app.synthesis.integrity import ClaimIntegrityGateway
from app.synthesis.service import SynthesisService
from app.valuation.claim_contracts import VALUATION_CLAIM_SCHEMA_VERSION
from app.valuation.comparison_service import RelativeValuationComparisonService
from tests.integration.test_financial_claim_service import (
    _annual_revenue_pair,
    _create_fin,
    _fin_draft,
)
from tests.integration.test_financial_claim_service import (
    _calc as _fin_calc,
)
from tests.integration.test_macro_claim_service import (
    _draft as _macro_draft,
)
from tests.integration.test_macro_claim_service import (
    _seed_document_card,
    _seed_macro_card,
)
from tests.integration.test_macro_claim_service import (
    _service as _macro_service,
)
from tests.integration.test_valuation_claim_service import (
    _claim_draft as _val_claim_draft,
)
from tests.integration.test_valuation_claim_service import (
    _seed_company,
    _seed_comparison,
)
from tests.integration.test_valuation_claim_service import (
    _service as _val_service,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_GENERIC_STATEMENT = "贵州茅台2026年营收同比增长15%。"
_FINANCIAL_STATEMENT = "2026年贵州茅台净利润同比增长15%。"
_CUTOFF = date(2026, 8, 10)
_FUTURE_CUTOFF = date(2026, 8, 11)


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
        await session.execute(text("DELETE FROM claim_synthesis_results"))
        await session.execute(text("DELETE FROM claim_synthesis_input_links"))
        await session.execute(text("DELETE FROM claim_synthesis_runs"))
        await session.execute(text("DELETE FROM claim_relative_valuation_comparison_links"))
        await session.execute(text("DELETE FROM relative_valuation_claim_profiles"))
        await session.execute(text("DELETE FROM claim_financial_calculation_links"))
        await session.execute(text("DELETE FROM financial_calculation_inputs"))
        await session.execute(text("DELETE FROM financial_calculations"))
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM macro_transmission_evidence_links"))
        await session.execute(text("DELETE FROM macro_transmission_chains"))
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.execute(text("DELETE FROM claims"))
        await session.execute(text("DELETE FROM relative_valuation_comparison_peers"))
        await session.execute(text("DELETE FROM relative_valuation_comparisons"))
        await session.execute(text("DELETE FROM valuation_metric_observations"))
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
    company_id = await _seed_company(sessionmaker, "600519")
    peer_ids = [await _seed_company(sessionmaker, f"6005{2 + i:02d}") for i in range(3)]
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "target_company_id": company_id,
        "peer_company_ids": peer_ids,
    }
    await _cleanup(sessionmaker)


def _draft(env: dict, claim_ids: list[UUID], *, cutoff: date = _CUTOFF) -> SynthesisInputDraft:
    return SynthesisInputDraft(
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=cutoff,
        claim_ids=claim_ids,
    )


async def _seed_doc_card(env: dict, **kwargs) -> UUID:
    """一张真实 document EvidenceCard（financial seed 复用 env[evidence_card_id]）。"""
    doc_card = await _seed_document_card(env, **kwargs)
    env["evidence_card_id"] = doc_card
    return doc_card


async def _seed_generic_claim(
    env: dict,
    doc_card: UUID,
    *,
    domain=ClaimAnalysisDomain.BUSINESS,
    statement: str | None = None,
):
    """创建一条 generic（business / event / risk，v1）Claim。

    statement 不同 → 不同 fingerprint → 新 Claim（供 claim-set 变化测试）。
    """
    kind = ClaimKind.FACT if domain != ClaimAnalysisDomain.RISK else ClaimKind.RISK
    return await ClaimService(env["sessionmaker"]).create_claim(
        ClaimDraft(
            company_id=env["company_id"],
            research_question=_QUESTION,
            statement=statement or f"{domain.value}陈述：{_GENERIC_STATEMENT}",
            analysis_domain=domain,
            claim_kind=kind,
            confidence=ClaimConfidence.HIGH,
            importance=ClaimImportance.NORMAL,
            support_evidence_ids=[doc_card],
            contradict_evidence_ids=[],
            context_evidence_ids=[],
            analyst_name="structured-analyst",
            analyst_version=1,
            analyst_model_id="deepseek:deepseek-v4-flash",
        )
    )


async def _seed_financial_claim(env: dict):
    obs = await _annual_revenue_pair(env)
    calc = await _fin_calc(env, obs)
    return await _create_fin(
        env, _fin_draft(env, supports=[calc.calculation_id], research_question=_QUESTION)
    )


async def _seed_macro_claim(
    env: dict, macro_card: UUID, doc_card: UUID, *, analysis_as_of: date = _CUTOFF
):
    return await _macro_service(env).create_claim(
        _macro_draft(
            env,
            macro_driver=[macro_card],
            company_exposure=[doc_card],
            research_question=_QUESTION,
            analysis_as_of=analysis_as_of,
        )
    )


async def _seed_valuation_claim(env: dict, comparison, *, analysis_as_of: date = _CUTOFF):
    return await _val_service(env).create_claim(
        _val_claim_draft(
            env,
            supports=[comparison.comparison_id],
            research_question=_QUESTION,
            analysis_as_of=analysis_as_of,
        )
    )


async def _verify(sessionmaker, claim_id: UUID):
    gateway = ClaimIntegrityGateway(
        claim_service=ClaimService(sessionmaker),
        macro_claim_service=MacroClaimService(sessionmaker),
        financial_calculation_service=FinancialCalculationService(sessionmaker),
        valuation_comparison_service=RelativeValuationComparisonService(sessionmaker),
    )
    async with sessionmaker() as session:
        return await gateway.verify_claim(session, claim_id)


@pytest_asyncio.fixture
async def four_claims(env, monkeypatch) -> dict:
    """同一 company + 同一 research_question 下 4 条跨 domain Claim。"""
    doc_card = await _seed_doc_card(env)
    generic = await _seed_generic_claim(env, doc_card)
    financial = await _seed_financial_claim(env)
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    macro = await _seed_macro_claim(env, macro_card, doc_card)
    comparison = await _seed_comparison(env)
    valuation = await _seed_valuation_claim(env, comparison)
    return {
        "env": env,
        "generic": generic,
        "financial": financial,
        "macro": macro,
        "valuation": valuation,
        "doc_card": doc_card,
        "macro_card": macro_card,
        "chain": chain,
        "comparison": comparison,
        "claim_ids": [generic.claim_id, financial.claim_id, macro.claim_id, valuation.claim_id],
    }


# ---------------------------------------------------------------- gateway


async def test_gateway_generic_business_claim_verified(env) -> None:
    doc_card = await _seed_doc_card(env)
    result = await _seed_generic_claim(env, doc_card)
    verified = await _verify(env["sessionmaker"], result.claim_id)
    assert verified.claim_id == result.claim_id
    assert verified.claim_fingerprint == result.claim_fingerprint
    assert verified.analysis_domain == ClaimAnalysisDomain.BUSINESS
    assert verified.claim_kind == ClaimKind.FACT
    assert verified.claim_schema_version == CLAIM_SCHEMA_VERSION
    assert verified.evidence_card_ids == [doc_card]
    assert verified.domain_analysis_as_of is None
    assert verified.research_question_sha256 == compute_research_question_sha256(_QUESTION)


async def test_gateway_generic_event_and_risk_domains_verified(env) -> None:
    doc_card = await _seed_doc_card(env)
    for domain in (ClaimAnalysisDomain.EVENT, ClaimAnalysisDomain.RISK):
        result = await _seed_generic_claim(env, doc_card, domain=domain)
        verified = await _verify(env["sessionmaker"], result.claim_id)
        assert verified.analysis_domain == domain
        assert verified.claim_schema_version == CLAIM_SCHEMA_VERSION
        assert verified.claim_fingerprint == result.claim_fingerprint


async def test_gateway_financial_claim_verified(env) -> None:
    doc_card = await _seed_doc_card(env)
    result = await _seed_financial_claim(env)
    verified = await _verify(env["sessionmaker"], result.claim_id)
    assert verified.analysis_domain == ClaimAnalysisDomain.FINANCIAL
    assert verified.claim_schema_version in (2, 3)
    assert verified.claim_fingerprint == result.claim_fingerprint
    # v3 自动展开 calc 的 source Evidence 一律 relation=context。
    assert verified.evidence_card_ids == [doc_card]
    assert verified.domain_analysis_as_of is None


async def test_gateway_macro_claim_verified(env, monkeypatch) -> None:
    doc_card = await _seed_doc_card(env)
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    result = await _seed_macro_claim(env, macro_card, doc_card)
    verified = await _verify(env["sessionmaker"], result.claim_id)
    assert verified.analysis_domain == ClaimAnalysisDomain.MACRO
    assert verified.claim_schema_version == MACRO_CLAIM_SCHEMA_VERSION
    assert verified.claim_fingerprint == result.claim_fingerprint
    assert verified.domain_analysis_as_of == _CUTOFF
    assert sorted(verified.evidence_card_ids, key=str) == sorted([macro_card, doc_card], key=str)


async def test_gateway_valuation_claim_verified(env) -> None:
    comparison = await _seed_comparison(env)
    result = await _seed_valuation_claim(env, comparison)
    verified = await _verify(env["sessionmaker"], result.claim_id)
    assert verified.analysis_domain == ClaimAnalysisDomain.VALUATION
    assert verified.claim_schema_version == VALUATION_CLAIM_SCHEMA_VERSION
    assert verified.claim_fingerprint == result.claim_fingerprint
    assert verified.domain_analysis_as_of == _CUTOFF
    # 每 comparison → target + 全部 peer Observations 的 source Evidence 自动 context。
    assert len(verified.evidence_card_ids) == 4


async def test_gateway_missing_claim_raises_integrity_error(env) -> None:
    with pytest.raises(SynthesisClaimIntegrityError):
        await _verify(env["sessionmaker"], uuid4())


async def test_gateway_unsupported_schema_version(env) -> None:
    doc_card = await _seed_doc_card(env)
    result = await _seed_generic_claim(env, doc_card)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE claims SET claim_schema_version = 99 WHERE claim_id = :cid").bindparams(
                cid=result.claim_id
            )
        )
        await session.commit()
    with pytest.raises(SynthesisUnsupportedClaimSchema):
        await _verify(env["sessionmaker"], result.claim_id)


async def test_gateway_corrupted_claim_fingerprint(env) -> None:
    doc_card = await _seed_doc_card(env)
    result = await _seed_generic_claim(env, doc_card)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE claims SET claim_fingerprint = :fp WHERE claim_id = :cid").bindparams(
                fp="f" * 64, cid=result.claim_id
            )
        )
        await session.commit()
    with pytest.raises(SynthesisClaimIntegrityError):
        await _verify(env["sessionmaker"], result.claim_id)


async def test_gateway_corrupted_evidence_link(env) -> None:
    doc_card = await _seed_doc_card(env)
    result = await _seed_generic_claim(env, doc_card)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM claim_evidence_links WHERE claim_id = :cid").bindparams(
                cid=result.claim_id
            )
        )
        await session.commit()
    with pytest.raises(SynthesisClaimIntegrityError):
        await _verify(env["sessionmaker"], result.claim_id)


async def test_gateway_financial_calculation_result_corrupted(env) -> None:
    """financial child artifact 损坏：SQL 篡改 result_value 但不更新 fingerprint。

    Gateway 委托 FinancialCalculationService.verify_calculation_integrity 重新
    派生 result_value，与 persisted 不一致 → 拒绝（不 repair）。
    """
    await _seed_doc_card(env)
    obs = await _annual_revenue_pair(env)
    calc = await _fin_calc(env, obs)
    result = await _create_fin(
        env, _fin_draft(env, supports=[calc.calculation_id], research_question=_QUESTION)
    )
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE financial_calculations SET result_value = 999999999999 "
                "WHERE calculation_id = :cid"
            ).bindparams(cid=calc.calculation_id)
        )
        await session.commit()
    with pytest.raises(SynthesisClaimIntegrityError):
        await _verify(env["sessionmaker"], result.claim_id)


async def test_gateway_financial_calculation_input_link_corrupted(env) -> None:
    """financial child artifact 损坏：删除 financial_calculation_inputs link。

    verify_calculation_integrity 重建 draft 时 role 集合与 calculation_code 不匹配
    → FinancialCalculationError → Gateway 映射为 SynthesisClaimIntegrityError。
    """
    await _seed_doc_card(env)
    obs = await _annual_revenue_pair(env)
    calc = await _fin_calc(env, obs)
    result = await _create_fin(
        env, _fin_draft(env, supports=[calc.calculation_id], research_question=_QUESTION)
    )
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM financial_calculation_inputs WHERE calculation_id = :cid").bindparams(
                cid=calc.calculation_id
            )
        )
        await session.commit()
    with pytest.raises(SynthesisClaimIntegrityError):
        await _verify(env["sessionmaker"], result.claim_id)


async def test_gateway_macro_transmission_channel_type_corrupted(env, monkeypatch) -> None:
    """macro child artifact 损坏：篡改 macro_transmission_chains.channel_type。

    从篡改后的 chain 重建 MacroClaimDraft → 重新 replay 时 transmission
    fingerprint / channel_type 与派生值不一致 → 拒绝（不 repair）。
    """
    doc_card = await _seed_doc_card(env)
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    result = await _seed_macro_claim(env, macro_card, doc_card)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE macro_transmission_chains SET channel_type = :ct "
                "WHERE transmission_id = :tid"
            ).bindparams(ct="revenue", tid=result.transmission_id)
        )
        await session.commit()
    with pytest.raises(SynthesisClaimIntegrityError):
        await _verify(env["sessionmaker"], result.claim_id)


async def test_gateway_macro_transmission_evidence_link_corrupted(env, monkeypatch) -> None:
    """macro child artifact 损坏：删除一条 transmission evidence link。

    verify_claim_integrity 重建 MacroClaimDraft 后 replay 对比 transmission links
    by_role 与派生值不一致 → 拒绝（不 repair）。
    """
    doc_card = await _seed_doc_card(env)
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    result = await _seed_macro_claim(env, macro_card, doc_card)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "DELETE FROM macro_transmission_evidence_links WHERE transmission_id = :tid"
            ).bindparams(tid=result.transmission_id)
        )
        await session.commit()
    with pytest.raises(SynthesisClaimIntegrityError):
        await _verify(env["sessionmaker"], result.claim_id)


async def test_gateway_valuation_peer_median_corrupted(env) -> None:
    """valuation child artifact 损坏：篡改 relative_valuation_comparisons.peer_median。

    委托 RelativeValuationComparisonService.verify_comparison_integrity 重新派生
    stats，与 persisted peer_median 不一致 → 拒绝（不 repair）。
    """
    comparison = await _seed_comparison(env)
    result = await _seed_valuation_claim(env, comparison)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE relative_valuation_comparisons SET peer_median = peer_median + 1 "
                "WHERE comparison_id = :cid"
            ).bindparams(cid=comparison.comparison_id)
        )
        await session.commit()
    with pytest.raises(SynthesisClaimIntegrityError):
        await _verify(env["sessionmaker"], result.claim_id)


async def test_gateway_valuation_peer_link_corrupted(env) -> None:
    """valuation child artifact 损坏：删除一条 peer link（不足 3 家 peer）。

    verify_comparison_integrity 重建 ComparisonDraft 时 peer_observation_ids 不足
    → ValuationError → Gateway 映射为 SynthesisClaimIntegrityError。
    """
    comparison = await _seed_comparison(env)
    result = await _seed_valuation_claim(env, comparison)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "DELETE FROM relative_valuation_comparison_peers WHERE comparison_id = :cid"
            ).bindparams(cid=comparison.comparison_id)
        )
        await session.commit()
    with pytest.raises(SynthesisClaimIntegrityError):
        await _verify(env["sessionmaker"], result.claim_id)


# ---------------------------------------------------------------- isolation


async def test_company_mismatch_rejected(env) -> None:
    doc_card = await _seed_doc_card(env)
    result = await _seed_generic_claim(env, doc_card)
    other = await _seed_generic_claim(env, doc_card, statement="第二条陈述。")
    draft = SynthesisInputDraft(
        company_id=env["peer_company_ids"][0],
        research_question=_QUESTION,
        analysis_as_of=_CUTOFF,
        claim_ids=[result.claim_id, other.claim_id],
    )
    with pytest.raises(SynthesisCompanyMismatch):
        await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(draft)


async def test_research_question_mismatch_rejected(env) -> None:
    doc_card = await _seed_doc_card(env)
    result = await _seed_generic_claim(env, doc_card)
    other = await _seed_generic_claim(env, doc_card, statement="第二条陈述。")
    draft = SynthesisInputDraft(
        company_id=env["company_id"],
        research_question="另一个研究问题？",
        analysis_as_of=_CUTOFF,
        claim_ids=[result.claim_id, other.claim_id],
    )
    with pytest.raises(SynthesisResearchQuestionMismatch):
        await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(draft)


# ---------------------------------------------------------------- temporal


async def test_future_document_evidence_rejected(env) -> None:
    # document 卡 published_at 晚于 cutoff → no-lookahead 拒绝。
    doc_card = await _seed_doc_card(env, published_at=datetime(2026, 8, 11, 9, 30, tzinfo=UTC))
    result = await _seed_generic_claim(env, doc_card)
    other = await _seed_generic_claim(env, doc_card, statement="第二条未来证据陈述。")
    with pytest.raises(SynthesisFutureEvidence):
        await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
            _draft(env, [result.claim_id, other.claim_id])
        )


async def test_future_macro_snapshot_rejected(env, monkeypatch) -> None:
    doc_card = await _seed_doc_card(env)
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    result = await _seed_macro_claim(env, macro_card, doc_card)
    other = await _seed_generic_claim(env, doc_card, statement="第二条陈述。")
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE macro_dataset_snapshots SET fetched_at = :at WHERE snapshot_id = :sid"
            ).bindparams(at=datetime(2026, 8, 11, tzinfo=UTC), sid=chain["snapshot_id"])
        )
        await session.commit()
    with pytest.raises(SynthesisFutureEvidence):
        await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
            _draft(env, [result.claim_id, other.claim_id])
        )


async def test_macro_analysis_as_of_future_rejected(env, monkeypatch) -> None:
    doc_card = await _seed_doc_card(env)
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    # 域分析截止晚于 synthesis cutoff → 综合会基于未来信息。
    result = await _seed_macro_claim(env, macro_card, doc_card, analysis_as_of=_FUTURE_CUTOFF)
    other = await _seed_generic_claim(env, doc_card, statement="第二条陈述。")
    with pytest.raises(SynthesisFutureEvidence):
        await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
            _draft(env, [result.claim_id, other.claim_id])
        )


async def test_valuation_analysis_as_of_future_rejected(env) -> None:
    doc_card = await _seed_doc_card(env)
    comparison = await _seed_comparison(env, analysis_as_of=_FUTURE_CUTOFF)
    result = await _seed_valuation_claim(env, comparison, analysis_as_of=_FUTURE_CUTOFF)
    other = await _seed_generic_claim(env, doc_card, statement="第二条陈述。")
    with pytest.raises(SynthesisFutureEvidence):
        await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
            _draft(env, [result.claim_id, other.claim_id])
        )


# ---------------------------------------------------------------- persistence / replay


async def test_first_create_persists_run_and_links(four_claims) -> None:
    env = four_claims["env"]
    draft = _draft(env, four_claims["claim_ids"])
    result = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(draft)
    assert result.replayed is False
    assert len(result.fingerprint) == 64
    assert result.summary.claim_count == 4
    assert result.summary.domain_counts == {
        "business": 1,
        "financial": 1,
        "macro": 1,
        "valuation": 1,
    }
    assert result.summary.claim_kind_counts == {"fact": 2, "risk": 1, "relative_valuation": 1}
    assert result.summary.confidence_counts == {"high": 3, "medium": 1}
    assert result.summary.importance_counts == {"normal": 4}
    async with env["sessionmaker"]() as session:
        run = await ClaimSynthesisRunRepository(session).get_by_id(result.synthesis_id)
        links = await ClaimSynthesisInputLinkRepository(session).list_by_synthesis(
            result.synthesis_id
        )
    assert run is not None
    assert run.company_id == env["company_id"]
    assert run.research_question == _QUESTION
    assert run.research_question_sha256 == compute_research_question_sha256(_QUESTION)
    assert run.analysis_as_of == _CUTOFF
    assert run.synthesis_schema_version == CLAIM_SYNTHESIS_SCHEMA_VERSION
    assert run.synthesis_fingerprint == result.fingerprint
    assert sorted((str(link.claim_id) for link in links), key=str) == sorted(
        (str(c) for c in draft.claim_ids), key=str
    )
    assert len(links) == 4


async def test_replay_returns_same_run(four_claims) -> None:
    env = four_claims["env"]
    draft = _draft(env, four_claims["claim_ids"])
    service = SynthesisService(env["sessionmaker"])
    first = await service.create_or_get_synthesis(draft)
    second = await service.create_or_get_synthesis(draft)
    assert second.replayed is True
    assert second.synthesis_id == first.synthesis_id
    assert second.fingerprint == first.fingerprint
    assert second.claim_ids == tuple(draft.claim_ids)


async def test_input_order_independent(four_claims) -> None:
    env = four_claims["env"]
    ids = four_claims["claim_ids"]
    service = SynthesisService(env["sessionmaker"])
    first = await service.create_or_get_synthesis(_draft(env, ids))
    second = await service.create_or_get_synthesis(_draft(env, list(reversed(ids))))
    assert second.replayed is True
    assert second.synthesis_id == first.synthesis_id
    assert second.fingerprint == first.fingerprint


async def test_claim_set_change_new_run_old_preserved(four_claims) -> None:
    env = four_claims["env"]
    service = SynthesisService(env["sessionmaker"])
    first = await service.create_or_get_synthesis(_draft(env, four_claims["claim_ids"]))
    # 新 statement → 不同 fingerprint → 新 Claim，保证 5-claim draft 与原 4-claim
    # draft 指纹不同（同 statement 会因 fingerprint 相同而 replay 原 claim）。
    extra = await _seed_generic_claim(env, four_claims["doc_card"], statement="附加的新陈述。")
    second = await service.create_or_get_synthesis(
        _draft(env, four_claims["claim_ids"] + [extra.claim_id])
    )
    assert second.replayed is False
    assert second.synthesis_id != first.synthesis_id
    assert second.fingerprint != first.fingerprint
    async with env["sessionmaker"]() as session:
        old = await ClaimSynthesisRunRepository(session).get_by_id(first.synthesis_id)
        old_links = await ClaimSynthesisInputLinkRepository(session).list_by_synthesis(
            first.synthesis_id
        )
    assert old is not None
    assert len(old_links) == 4


async def test_cutoff_change_new_run(four_claims) -> None:
    env = four_claims["env"]
    service = SynthesisService(env["sessionmaker"])
    first = await service.create_or_get_synthesis(_draft(env, four_claims["claim_ids"]))
    later = _draft(env, four_claims["claim_ids"], cutoff=_FUTURE_CUTOFF)
    second = await service.create_or_get_synthesis(later)
    assert second.replayed is False
    assert second.synthesis_id != first.synthesis_id
    assert second.fingerprint != first.fingerprint


async def test_concurrent_same_draft_single_run(four_claims) -> None:
    env = four_claims["env"]
    draft = _draft(env, four_claims["claim_ids"])
    service = SynthesisService(env["sessionmaker"])
    a, b = await asyncio.gather(
        service.create_or_get_synthesis(draft),
        service.create_or_get_synthesis(draft),
    )
    assert a.synthesis_id == b.synthesis_id
    assert (a.replayed, b.replayed) in {(False, True), (True, False)}
    async with env["sessionmaker"]() as session:
        runs = (
            await session.execute(text("SELECT count(*) FROM claim_synthesis_runs"))
        ).scalar_one()
        links = (
            await session.execute(text("SELECT count(*) FROM claim_synthesis_input_links"))
        ).scalar_one()
    assert int(runs) == 1
    assert int(links) == 4


async def test_replay_detects_corrupted_link(four_claims) -> None:
    env = four_claims["env"]
    draft = _draft(env, four_claims["claim_ids"])
    service = SynthesisService(env["sessionmaker"])
    first = await service.create_or_get_synthesis(draft)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM claim_synthesis_input_links WHERE synthesis_id = :sid").bindparams(
                sid=first.synthesis_id
            )
        )
        await session.commit()
    with pytest.raises(SynthesisIntegrityError):
        await service.create_or_get_synthesis(draft)


async def test_replay_detects_corrupted_claim(four_claims) -> None:
    env = four_claims["env"]
    draft = _draft(env, four_claims["claim_ids"])
    service = SynthesisService(env["sessionmaker"])
    await service.create_or_get_synthesis(draft)
    # 篡改 Claim statement → gateway 重算 fingerprint 与 persisted 不一致。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE claims SET statement = :s WHERE claim_id = :cid").bindparams(
                s="被篡改的陈述。", cid=four_claims["generic"].claim_id
            )
        )
        await session.commit()
    with pytest.raises(SynthesisClaimIntegrityError):
        await service.create_or_get_synthesis(draft)


async def test_no_claim_or_evidence_mutation(four_claims) -> None:
    env = four_claims["env"]
    async with env["sessionmaker"]() as session:
        claims = (
            await session.execute(text("SELECT claim_id, claim_fingerprint FROM claims"))
        ).all()
        cards = (
            await session.execute(
                text("SELECT evidence_card_id, evidence_fingerprint FROM evidence_cards")
            )
        ).all()
    before_claims = {str(r[0]): str(r[1]) for r in claims}
    before_cards = {str(r[0]): str(r[1]) for r in cards}

    service = SynthesisService(env["sessionmaker"])
    draft = _draft(env, four_claims["claim_ids"])
    await service.create_or_get_synthesis(draft)
    await service.create_or_get_synthesis(draft)

    async with env["sessionmaker"]() as session:
        claims = (
            await session.execute(text("SELECT claim_id, claim_fingerprint FROM claims"))
        ).all()
        cards = (
            await session.execute(
                text("SELECT evidence_card_id, evidence_fingerprint FROM evidence_cards")
            )
        ).all()
    assert {str(r[0]): str(r[1]) for r in claims} == before_claims
    assert {str(r[0]): str(r[1]) for r in cards} == before_cards


# ---------------------------------------------------------------- E2E / boundary


async def test_cross_domain_e2e_single_run_replay(four_claims) -> None:
    env = four_claims["env"]
    service = SynthesisService(env["sessionmaker"])
    draft = _draft(env, four_claims["claim_ids"])
    first = await service.create_or_get_synthesis(draft)
    assert first.replayed is False
    assert len(first.fingerprint) == 64
    assert first.summary.domain_counts == {
        "business": 1,
        "financial": 1,
        "macro": 1,
        "valuation": 1,
    }
    async with env["sessionmaker"]() as session:
        links = await ClaimSynthesisInputLinkRepository(session).list_by_synthesis(
            first.synthesis_id
        )
    assert sorted((str(link.claim_id) for link in links), key=str) == sorted(
        (str(c) for c in draft.claim_ids), key=str
    )
    assert len(links) == 4
    # replay 同一 run。
    second = await service.create_or_get_synthesis(draft)
    assert second.replayed is True
    assert second.synthesis_id == first.synthesis_id
    assert second.fingerprint == first.fingerprint


async def test_boundary_no_stage5_no_llm_no_langgraph(env) -> None:
    async with env["sessionmaker"]() as session:
        stage5 = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name IN "
                    "('draft_sections','reports')"
                )
            )
        ).scalar_one()
    assert int(stage5) == 0
    # Stage 5A 的 report_outlines 表已存在（migration 0032），但本阶段不写行。
    outline_rows = (
        await session.execute(text("SELECT count(*) FROM report_outlines"))
    ).scalar_one()
    assert int(outline_rows) == 0
    service = SynthesisService(env["sessionmaker"])
    # 只持有 sessionmaker + integrity gateway（无 LLM / 无 LangGraph / 无 storage 副作用）。
    assert list(service.__dict__.keys()) == ["_sessionmaker", "_gateway"]
