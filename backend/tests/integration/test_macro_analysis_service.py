"""MacroAnalysisService integration tests (stage 4C.1B, spec Y)。

需要真实 PostgreSQL（127.0.0.1:5433）。复用 4C.1A 的 seeding helpers：
`_seed_macro_card`（真实 MacroEvidenceService 链）、`_seed_document_card`
（真实 EvidenceCardService 链）、`_seed_other_company`、`_claim_count`、
`_macro_tables_count`、`_set_macro_snapshot_fetched_at`。模型一律用
`FakeMacroAnalysisModel`——**零真实 LLM / 零网络 / 零 Chroma / 零 LangGraph /
零 Report / 零 Audit**。

覆盖（4C.1B E2E）：
- 10 步流程 happy path：MacroDriver Pack（M1）+ Company Evidence Pack（E1）→
  fake 决策 → v6 Claim 落库（analyst 身份固定 / analysis_domain=macro /
  analyst_model_id=model.model_id）+ v3 transmission（analysis_as_of 查询列
  持久化）+ transmission links（macro_driver / company_exposure）+ ClaimEvidenceLinks；
- news_article + event 文档卡作为 macro_driver 的 happy path（v3 资格）；
- 模型收到的两池是最小投影（无 UUID / fingerprint / source UUID）；M/E alias
  确定性（M1 → macro_card、E1 → doc_card 按 str(uuid) 升序）；
- relevant=false → 0 claims；reason_code 透传；
- 上游加载失败（Evidence 缺失 / 跨公司 / macro_driver origin 违反 /
  company origin 违反 / future evidence）→ 稳定错误，**不调用 LLM**（fake.calls 为空）；
- numeric guard / 未知 M ref / 未知 E ref / 跨 relation 冲突 → 整次失败 0 写；
- observed_impact 需 ≥1 observed_effect（合法路径落库 observed_effect 角色）；
- critical Claim 缺 eligible company_exposure → MacroClaimCriticalEvidenceInsufficient；
- malformed output → MacroAnalysisMalformedOutput；provider 失败 →
  MacroAnalysisModelUnavailable 透传；
- replay：同决策再分析 → replayed_count=1，同 claim_id，无重复行；
- multiple claims（inference + risk）合法落库；
- _check_overclaim_policy / _check_kind_policy 防御性兜底。
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.macro.contracts import (
    MACRO_ANALYST_NAME,
    MACRO_ANALYST_VERSION,
    MacroAnalysisDecision,
    MacroAnalysisReason,
    MacroAnalysisRequest,
    MacroClaimCandidate,
)
from app.analysis.macro.errors import (
    MacroAnalysisClaimKindPolicy,
    MacroAnalysisEvidenceCompanyMismatch,
    MacroAnalysisEvidenceNotFound,
    MacroAnalysisFutureEvidence,
    MacroAnalysisMalformedOutput,
    MacroAnalysisModelUnavailable,
    MacroAnalysisNumericLiteralForbidden,
    MacroAnalysisOriginViolation,
    MacroAnalysisOverclaimPolicy,
    MacroAnalysisRelationConflict,
    MacroAnalysisUnknownRef,
)
from app.analysis.macro.packs import ResolvedMacroClaim
from app.analysis.macro.service import MacroAnalysisService
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
from app.claims.macro_errors import MacroClaimCriticalEvidenceInsufficient
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.evidence.contracts import EvidenceType
from app.repositories.claim_repository import ClaimRepository
from app.repositories.macro_transmission_evidence_link_repository import (
    MacroTransmissionEvidenceLinkRepository,
)
from app.repositories.macro_transmission_repository import MacroTransmissionRepository
from tests.analysis.macro.fakes import FakeMacroAnalysisModel
from tests.integration.test_macro_claim_service import (
    _claim_count,
    _cleanup,
    _macro_tables_count,
    _seed_document_card,
    _seed_macro_card,
    _seed_other_company,
    _set_macro_snapshot_fetched_at,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "利率上行对贵州茅台融资成本的影响？"
_STATEMENT = "若利率持续上行，公司融资成本存在上升压力。"
_ANALYSIS_AS_OF = date(2026, 8, 10)


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    from app.core.config import get_settings

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


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    from app.db.models.company import CompanyModel
    from app.repositories.company_repository import CompanyRepository
    from app.services.source_registry_service import SourceRegistryService
    from app.storage.raw_store import LocalRawArtifactStore

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


# ---------------------------------------------------------------- helpers


def _request(
    env: dict,
    *,
    macro_driver: list[UUID],
    company: list[UUID],
    **overrides,
) -> MacroAnalysisRequest:
    values = dict(
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_ANALYSIS_AS_OF,
        macro_driver_evidence_ids=macro_driver,
        company_evidence_ids=company,
    )
    values.update(overrides)
    return MacroAnalysisRequest(**values)


def _candidate(**overrides) -> MacroClaimCandidate:
    values = dict(
        statement=_STATEMENT,
        claim_kind=ClaimKind.RISK,
        confidence=MacroClaimConfidence.MEDIUM,
        importance=MacroClaimImportance.NORMAL,
        channel_type=MacroChannelType.FINANCING,
        effect_direction=MacroEffectDirection.HEADWIND,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT,
        time_alignment=MacroTimeAlignment.ALIGNED,
        macro_driver_refs=["M1"],
        company_exposure_refs=["E1"],
        observed_effect_refs=[],
        additional_support_evidence_refs=[],
        additional_contradict_evidence_refs=[],
        additional_context_evidence_refs=[],
    )
    values.update(overrides)
    return MacroClaimCandidate(**values)


def _decision(**overrides) -> MacroAnalysisDecision:
    values = dict(relevant=True, claims=[_candidate()], reason_code=None)
    values.update(overrides)
    return MacroAnalysisDecision(**values)


def _service(env: dict, model: FakeMacroAnalysisModel) -> MacroAnalysisService:
    return MacroAnalysisService(env["sessionmaker"], model)


# ---------------------------------------------------------------- happy path


async def test_analyze_creates_v6_claim_with_v3_transmission_and_cutoff(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(decision=_decision())
    result = await _service(env, model).analyze(
        _request(env, macro_driver=[macro_card], company=[doc_card])
    )

    assert result.relevant is True
    assert result.created_count == 1
    assert result.replayed_count == 0
    assert result.reason_code is None
    assert len(result.claim_ids) == 1

    claim_id = result.claim_ids[0]
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(claim_id)
    assert claim is not None
    assert claim.company_id == env["company_id"]
    assert claim.analysis_domain == "macro"
    assert claim.claim_kind == "risk"
    assert claim.claim_schema_version == MACRO_CLAIM_SCHEMA_VERSION
    assert claim.analyst_name == MACRO_ANALYST_NAME
    assert claim.analyst_version == MACRO_ANALYST_VERSION
    assert claim.analyst_model_id == model.model_id

    async with env["sessionmaker"]() as session:
        chain_row = await MacroTransmissionRepository(session).get_by_claim_id(claim_id)
    assert chain_row is not None
    assert chain_row.company_id == env["company_id"]
    assert chain_row.channel_type == "financing"
    assert chain_row.effect_direction == "headwind"
    assert chain_row.impact_status == "plausible_impact"
    assert chain_row.time_alignment == "aligned"
    assert chain_row.transmission_schema_version == MACRO_TRANSMISSION_SCHEMA_VERSION
    # Gate 0：analysis_as_of 作为查询列持久化（不再只能从 fingerprint 反推）。
    assert chain_row.analysis_as_of == _ANALYSIS_AS_OF

    async with env["sessionmaker"]() as session:
        trans_links = await MacroTransmissionEvidenceLinkRepository(session).list_by_transmission(
            chain_row.transmission_id
        )
    by_card = {link.evidence_card_id: link.role for link in trans_links}
    assert by_card == {macro_card: "macro_driver", doc_card: "company_exposure"}

    # ClaimEvidenceLinks：macro_driver / company_exposure 一律 relation=context。
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT evidence_card_id, relation FROM claim_evidence_links "
                    "WHERE claim_id = :cid"
                ).bindparams(cid=claim_id)
            )
        ).all()
    assert sorted(r[1] for r in rows) == ["context", "context"]

    assert await _claim_count(env["sessionmaker"]) == 1
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_chains") == 1


async def test_analyze_news_article_event_driver_happy_path(env, monkeypatch) -> None:
    # news_article + event 文档卡作为 macro_driver（v3 资格）。
    event_card = await _seed_document_card(
        env, evidence_type=EvidenceType.EVENT, statement="央行宣布上调政策利率。"
    )
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(decision=_decision())
    result = await _service(env, model).analyze(
        _request(env, macro_driver=[event_card], company=[doc_card])
    )

    assert result.created_count == 1
    assert len(model.calls) == 1
    _, driver_pack, company_pack = model.calls[0]
    assert driver_pack.items[0].origin_type == "document_chunk"
    assert driver_pack.items[0].document_type == "news_article"
    assert driver_pack.items[0].quote_text is not None
    assert company_pack.items[0].evidence_ref == "E1"


async def test_analyze_model_receives_minimal_pack_projection(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(decision=_decision())
    await _service(env, model).analyze(_request(env, macro_driver=[macro_card], company=[doc_card]))

    assert len(model.calls) == 1
    context, driver_pack, company_pack = model.calls[0]
    assert context.research_question == _QUESTION
    assert context.analysis_as_of == _ANALYSIS_AS_OF
    # M1 → macro_card、E1 → doc_card（按 str(uuid) 升序，确定性 alias）。
    assert [item.macro_ref for item in driver_pack.items] == ["M1"]
    assert driver_pack.ref_to_card_id["M1"] == macro_card
    assert [item.evidence_ref for item in company_pack.items] == ["E1"]
    assert company_pack.ref_to_card_id["E1"] == doc_card

    # MacroDriver Pack 最小投影：无 UUID / fingerprint / source UUID。
    text_repr = "\n".join(str(item) for item in driver_pack.items)
    for forbidden in (
        str(macro_card),
        "fingerprint",
        "evidence_card_id",
        "snapshot_id",
        "observation_id",
        "source_id",
        "locator",
        "chroma",
    ):
        assert forbidden not in text_repr
    item = driver_pack.items[0]
    assert item.observation_period == "2024"
    assert item.value_summary is not None
    assert "观测期 2024" in item.effective_period_summary

    # Company Evidence Pack 最小投影。
    ctext = "\n".join(str(item) for item in company_pack.items)
    for forbidden in (
        str(doc_card),
        "fingerprint",
        "evidence_card_id",
        "source_id",
        "locator",
        "chroma",
    ):
        assert forbidden not in ctext


# ---------------------------------------------------------------- relevant=false


async def test_analyze_relevant_false_creates_no_claims(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(
        decision=MacroAnalysisDecision(
            relevant=False,
            claims=[],
            reason_code=MacroAnalysisReason.INSUFFICIENT_COMPANY_EVIDENCE,
        )
    )
    result = await _service(env, model).analyze(
        _request(env, macro_driver=[macro_card], company=[doc_card])
    )

    assert result.relevant is False
    assert result.claim_ids == []
    assert result.created_count == 0
    assert result.replayed_count == 0
    assert result.reason_code == MacroAnalysisReason.INSUFFICIENT_COMPANY_EVIDENCE
    assert await _claim_count(env["sessionmaker"]) == 0
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_chains") == 0


# ---------------------------------------------------------------- 上游加载失败（不调用 LLM）


async def test_analyze_missing_evidence_aborts_before_llm(env) -> None:
    model = FakeMacroAnalysisModel(decision=_decision())
    with pytest.raises(MacroAnalysisEvidenceNotFound):
        await _service(env, model).analyze(_request(env, macro_driver=[uuid4()], company=[uuid4()]))
    assert model.calls == []


async def test_analyze_missing_company_evidence_aborts_before_llm(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    model = FakeMacroAnalysisModel(decision=_decision())
    with pytest.raises(MacroAnalysisEvidenceNotFound):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[uuid4()])
        )
    assert model.calls == []


async def test_analyze_company_mismatch_aborts_before_llm(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    other_company = await _seed_other_company(env)
    model = FakeMacroAnalysisModel(decision=_decision())
    with pytest.raises(MacroAnalysisEvidenceCompanyMismatch):
        await _service(env, model).analyze(
            _request(
                env,
                macro_driver=[macro_card],
                company=[doc_card],
                company_id=other_company,
            )
        )
    assert model.calls == []


async def test_analyze_driver_origin_violation_metric_document(env) -> None:
    # metric 类型 news_article 卡不是合格 macro_driver（要求 event/fact/statement）。
    metric_card = await _seed_document_card(env, evidence_type=EvidenceType.METRIC)
    company_card = await _seed_document_card(env, evidence_type=EvidenceType.EVENT)
    model = FakeMacroAnalysisModel(decision=_decision())
    with pytest.raises(MacroAnalysisOriginViolation):
        await _service(env, model).analyze(
            _request(env, macro_driver=[metric_card], company=[company_card])
        )
    assert model.calls == []
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_company_origin_violation_macro_observation(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    other_macro_card, _ = await _seed_macro_card(
        env, monkeypatch, statement="2024年中国人均GDP为8.9万元（世界银行）。"
    )
    model = FakeMacroAnalysisModel(decision=_decision())
    with pytest.raises(MacroAnalysisOriginViolation):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[other_macro_card])
        )
    assert model.calls == []


async def test_analyze_future_macro_evidence_aborts_before_llm(env, monkeypatch) -> None:
    macro_card, chain = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    await _set_macro_snapshot_fetched_at(
        env, chain["snapshot_id"], datetime(2027, 1, 1, tzinfo=UTC)
    )
    model = FakeMacroAnalysisModel(decision=_decision())
    with pytest.raises(MacroAnalysisFutureEvidence):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[doc_card])
        )
    assert model.calls == []


async def test_analyze_future_document_evidence_aborts_before_llm(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env, published_at=datetime(2027, 1, 1, tzinfo=UTC))
    model = FakeMacroAnalysisModel(decision=_decision())
    with pytest.raises(MacroAnalysisFutureEvidence):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[doc_card])
        )
    assert model.calls == []


# ---------------------------------------------------------------- 失败路径（0 写）


async def test_analyze_numeric_literal_guard_aborts_zero_writes(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(
        decision=_decision(
            claims=[_candidate(statement="若利率上调五十个基点，公司融资成本存在上升压力。")]
        )
    )
    with pytest.raises(MacroAnalysisNumericLiteralForbidden):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[doc_card])
        )
    # LLM 被调用（guard 在模型输出之后），有界重试 5 次后仍失败；0 写。
    assert len(model.calls) == 5
    assert await _claim_count(env["sessionmaker"]) == 0
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_chains") == 0


async def test_analyze_unknown_macro_ref_aborts_zero_writes(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(
        decision=_decision(claims=[_candidate(macro_driver_refs=["M99"])])
    )
    with pytest.raises(MacroAnalysisUnknownRef):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[doc_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_unknown_company_ref_aborts_zero_writes(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(
        decision=_decision(claims=[_candidate(company_exposure_refs=["E99"])])
    )
    with pytest.raises(MacroAnalysisUnknownRef):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[doc_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_cross_relation_conflict_aborts_zero_writes(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    # 同一 E 同时出现在 company_exposure 与 additional_context → 跨 relation 冲突。
    model = FakeMacroAnalysisModel(
        decision=_decision(
            claims=[
                _candidate(
                    company_exposure_refs=["E1"],
                    additional_context_evidence_refs=["E1"],
                )
            ]
        )
    )
    with pytest.raises(MacroAnalysisRelationConflict):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[doc_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_malformed_output_mapped(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    # dict 且 relevant=true 但 claims 为空 → ValidationError → MalformedOutput。
    model = FakeMacroAnalysisModel(decision={"relevant": True, "claims": []})
    with pytest.raises(MacroAnalysisMalformedOutput):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[doc_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_model_unavailable_propagates(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(error=MacroAnalysisModelUnavailable)
    with pytest.raises(MacroAnalysisModelUnavailable):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[doc_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_critical_claim_requires_eligible_legs(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)  # critical_claim_eligible=False
    model = FakeMacroAnalysisModel(
        decision=_decision(claims=[_candidate(importance=MacroClaimImportance.CRITICAL)])
    )
    with pytest.raises(MacroClaimCriticalEvidenceInsufficient):
        await _service(env, model).analyze(
            _request(env, macro_driver=[macro_card], company=[doc_card])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- observed_impact / multi claims


async def test_analyze_observed_impact_with_observed_effect(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    effect_card = await _seed_document_card(env, statement="2024年下半年公司融资成本明显上升。")
    # E1/E2 按 str(uuid) 升序分配（build_company_evidence_pack 确定性）。
    e1_card, e2_card = sorted([doc_card, effect_card], key=str)
    model = FakeMacroAnalysisModel(
        decision=_decision(
            claims=[
                _candidate(
                    impact_status=MacroImpactStatus.OBSERVED_IMPACT,
                    company_exposure_refs=["E1"],
                    observed_effect_refs=["E2"],
                )
            ]
        )
    )
    result = await _service(env, model).analyze(
        _request(env, macro_driver=[macro_card], company=[doc_card, effect_card])
    )

    assert result.created_count == 1
    async with env["sessionmaker"]() as session:
        chain_row = await MacroTransmissionRepository(session).get_by_claim_id(result.claim_ids[0])
        trans_links = await MacroTransmissionEvidenceLinkRepository(session).list_by_transmission(
            chain_row.transmission_id
        )
    by_card = {link.evidence_card_id: link.role for link in trans_links}
    assert by_card == {
        macro_card: "macro_driver",
        e1_card: "company_exposure",
        e2_card: "observed_effect",
    }


async def test_analyze_multiple_claims_inference_and_risk_persisted(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(
        decision=_decision(
            claims=[
                _candidate(
                    statement="利率上行可能推高公司融资成本。",
                    claim_kind=ClaimKind.INFERENCE,
                ),
                _candidate(
                    statement="汇率波动对海外收入存在不确定性影响。",
                    claim_kind=ClaimKind.RISK,
                ),
            ]
        )
    )
    result = await _service(env, model).analyze(
        _request(env, macro_driver=[macro_card], company=[doc_card])
    )

    assert result.created_count == 2
    assert result.replayed_count == 0
    assert len(result.claim_ids) == 2
    assert len(set(result.claim_ids)) == 2
    kinds = set()
    async with env["sessionmaker"]() as session:
        for claim_id in result.claim_ids:
            claim = await ClaimRepository(session).get_by_id(claim_id)
            assert claim is not None
            kinds.add(claim.claim_kind)
    assert kinds == {"inference", "risk"}
    assert await _claim_count(env["sessionmaker"]) == 2


# ---------------------------------------------------------------- replay


async def test_analyze_replays_same_claim_on_second_run(env, monkeypatch) -> None:
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    doc_card = await _seed_document_card(env)
    model = FakeMacroAnalysisModel(decision=_decision())
    service = _service(env, model)

    first = await service.analyze(_request(env, macro_driver=[macro_card], company=[doc_card]))
    second = await service.analyze(_request(env, macro_driver=[macro_card], company=[doc_card]))

    assert first.created_count == 1
    assert first.replayed_count == 0
    assert second.created_count == 0
    assert second.replayed_count == 1
    assert second.claim_ids == first.claim_ids
    assert len(model.calls) == 2
    assert await _claim_count(env["sessionmaker"]) == 1
    assert await _macro_tables_count(env["sessionmaker"], "macro_transmission_chains") == 1


# ---------------------------------------------------------------- 防御性兜底（不依赖 DB）


async def test_overclaim_policy_defensive_rejects_observed_impact_without_effect() -> None:
    claim = ResolvedMacroClaim(
        statement=_STATEMENT,
        claim_kind=ClaimKind.RISK,
        confidence=MacroClaimConfidence.MEDIUM,
        importance=MacroClaimImportance.NORMAL,
        channel_type=MacroChannelType.FINANCING,
        effect_direction=MacroEffectDirection.HEADWIND,
        impact_status=MacroImpactStatus.OBSERVED_IMPACT,
        time_alignment=MacroTimeAlignment.ALIGNED,
        macro_driver_ids=(uuid4(),),
        company_exposure_ids=(uuid4(),),
        observed_effect_ids=(),
        additional_supports=(),
        additional_contradicts=(),
        additional_context=(),
    )
    with pytest.raises(MacroAnalysisOverclaimPolicy):
        MacroAnalysisService._check_overclaim_policy([claim])


async def test_overclaim_policy_defensive_rejects_uncertain_non_risk() -> None:
    claim = ResolvedMacroClaim(
        statement=_STATEMENT,
        claim_kind=ClaimKind.INFERENCE,
        confidence=MacroClaimConfidence.MEDIUM,
        importance=MacroClaimImportance.NORMAL,
        channel_type=MacroChannelType.FINANCING,
        effect_direction=MacroEffectDirection.HEADWIND,
        impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT,
        time_alignment=MacroTimeAlignment.UNCERTAIN,
        macro_driver_ids=(uuid4(),),
        company_exposure_ids=(uuid4(),),
        observed_effect_ids=(),
        additional_supports=(),
        additional_contradicts=(),
        additional_context=(),
    )
    with pytest.raises(MacroAnalysisOverclaimPolicy):
        MacroAnalysisService._check_overclaim_policy([claim])


async def test_kind_policy_defensive_rejects_fact_draft() -> None:
    """defensive 兜底：即使绕过 Pydantic / MacroClaimDraft（object.__new__ 绕过
    __post_init__ 的 kind 校验），Macro Analysis 路径也拒绝 fact →
    MacroAnalysisClaimKindPolicy。

    MacroClaimDraft 本身在 __post_init__ 已拒绝 fact（更低层 domain contract）；
    本防线是第二层兜底（直接构造不合法 draft 时仍拦截）。
    """
    draft = object.__new__(MacroClaimDraft)
    object.__setattr__(draft, "claim_kind", ClaimKind.FACT)
    with pytest.raises(MacroAnalysisClaimKindPolicy):
        MacroAnalysisService._check_kind_policy([draft])
