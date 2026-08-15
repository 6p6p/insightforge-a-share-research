"""ClaimAnalysisService integration tests (stage 4B.1, spec 12/13/14)。

需要真实 PostgreSQL（127.0.0.1:5433）。Evidence 用真实 HTML 链 →
EvidenceCardService（document），**零真实 LLM / 零 Chroma / 零 LangGraph**：
LLM 一律用 FakeClaimAnalysisModel。

覆盖：
- 端到端：Evidence Pack → fake decision → create_claim_batch 原子持久化 claims
  + claim_evidence_links（analyst_name=具体 strategy / analyst_version /
  analyst_model_id=fake.model_id 落库）；
- relevant=false → 0-claims 结果（不写 Claim，reason_code 透传）；
- 拒绝：未知 E ref / 跨 relation 冲突 → 整次失败 0 写；company mismatch；
  domain not ready（defensive）；critical 缺 eligible support；
- replay：同请求 + 同 decision 第二次 → replayed；
- malformed output / model unavailable 映射；
- 最小投影：传给模型的是 E1..En 必要字段（无 UUID / locator / raw / fingerprint）；
- 边界：claims / claim_evidence_links 允许存在；未来阶段（5E+）表不得存在，
  Stage 5A-5D 表允许存在但不写行。

全程使用真实 PG + fake model（不手写 RetrievalHit / DocumentChunk）。
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.claims.contracts import (
    CLAIM_ANALYST_VERSION,
    ClaimAnalysisContext,
    ClaimAnalysisDecision,
    ClaimAnalysisReason,
    ClaimAnalysisRequest,
    ClaimCandidate,
    EvidencePack,
)
from app.analysis.claims.errors import (
    ClaimAnalysisDomainNotReady,
    ClaimAnalysisEvidenceCompanyMismatch,
    ClaimAnalysisMalformedOutput,
    ClaimAnalysisModelUnavailable,
    ClaimAnalysisRelationConflict,
    ClaimAnalysisUnknownEvidenceRef,
)
from app.analysis.claims.service import ClaimAnalysisService
from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.session import DatabaseManager
from app.repositories.company_repository import CompanyRepository
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.analysis.claims.fakes import FakeClaimAnalysisModel
from tests.integration.test_claim_service import (
    _cleanup,
    _seed_document_card,
    _seed_other_company,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "2024年公司海外业务增长情况？"
_STATEMENT = "海外业务是公司2024年收入增长的重要驱动因素"

_URL_1 = "https://www.xinhuanet.com/2026/0809/0101.htm"
_URL_2 = "https://www.xinhuanet.com/2026/0809/0102.htm"
_URL_3 = "https://www.xinhuanet.com/2026/0809/0103.htm"


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


# ---------------------------------------------------------------- helpers


def _candidate(**overrides) -> ClaimCandidate:
    values = dict(
        statement=_STATEMENT,
        claim_kind=ClaimKind.INFERENCE,
        confidence=ClaimConfidence.MEDIUM,
        importance=ClaimImportance.NORMAL,
        support_refs=["E1"],
        contradict_refs=[],
        context_refs=[],
    )
    values.update(overrides)
    return ClaimCandidate(**values)


def _decision(
    relevant: bool = True, claims: list | None = None, reason_code=None
) -> ClaimAnalysisDecision:
    return ClaimAnalysisDecision(relevant=relevant, claims=claims or [], reason_code=reason_code)


def _request(
    env: dict,
    *,
    evidence_card_ids,
    domain=ClaimAnalysisDomain.BUSINESS,
    company_id=None,
    question=_QUESTION,
) -> ClaimAnalysisRequest:
    return ClaimAnalysisRequest(
        company_id=company_id if company_id is not None else env["company_id"],
        research_question=question,
        analysis_domain=domain,
        evidence_card_ids=evidence_card_ids,
    )


def _service(env: dict, decision, model_id="deepseek:deepseek-v4-flash"):
    model = FakeClaimAnalysisModel(decision=decision, model_id=model_id)
    service = ClaimAnalysisService(env["sessionmaker"], model)
    return service, model


async def _claim_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text("SELECT count(*) FROM claims"))).scalar_one())


async def _claim_row(sessionmaker, claim_id):
    async with sessionmaker() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT analyst_name, analyst_version, analyst_model_id, "
                        "analysis_domain FROM claims WHERE claim_id = :cid"
                    ).bindparams(cid=claim_id)
                )
            )
            .mappings()
            .one()
        )


# ---------------------------------------------------------------- 端到端


async def test_analyze_creates_claim_with_links_and_analyst_identity(env) -> None:
    a = await _seed_document_card(env, statement="海外收入同比增长31.4%", source_url=_URL_1)
    b = await _seed_document_card(env, statement="公司海外销售占比持续提升", source_url=_URL_2)
    sorted_ids = sorted([a["evidence_card_id"], b["evidence_card_id"]], key=str)

    decision = _decision(
        claims=[
            _candidate(support_refs=["E1"], context_refs=["E2"]),
        ]
    )
    service, _model = _service(env, decision)
    result = await service.analyze(_request(env, evidence_card_ids=[sorted_ids[0], sorted_ids[1]]))

    assert result.relevant is True
    assert len(result.claim_ids) == 1
    assert result.created_count == 1
    assert result.replayed_count == 0
    row = await _claim_row(env["sessionmaker"], result.claim_ids[0])
    assert row["analyst_name"] == "business_event_v1"
    assert row["analyst_version"] == CLAIM_ANALYST_VERSION
    assert row["analyst_model_id"] == "deepseek:deepseek-v4-flash"
    assert row["analysis_domain"] == "business"

    # link 关系：E1 → supports、E2 → context。
    async with env["sessionmaker"]() as session:
        links = (
            (
                await session.execute(
                    text(
                        "SELECT evidence_card_id, relation FROM claim_evidence_links "
                        "WHERE claim_id = :cid"
                    ).bindparams(cid=result.claim_ids[0])
                )
            )
            .mappings()
            .all()
        )
    assert sorted((str(link["evidence_card_id"]), link["relation"]) for link in links) == sorted(
        [
            (str(sorted_ids[0]), "supports"),
            (str(sorted_ids[1]), "context"),
        ]
    )


async def test_domain_maps_to_strategy_for_analyst_name(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    for domain, expected_strategy in (
        (ClaimAnalysisDomain.BUSINESS, "business_event_v1"),
        (ClaimAnalysisDomain.EVENT, "business_event_v1"),
        (ClaimAnalysisDomain.RISK, "risk_skeptic_v1"),
    ):
        decision = _decision(claims=[_candidate(support_refs=["E1"])])
        service, _ = _service(env, decision)
        result = await service.analyze(
            _request(env, evidence_card_ids=[card["evidence_card_id"]], domain=domain)
        )
        row = await _claim_row(env["sessionmaker"], result.claim_ids[0])
        assert row["analyst_name"] == expected_strategy
        assert row["analysis_domain"] == domain.value


async def test_relevant_false_creates_no_claims(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    service, _ = _service(
        env,
        _decision(relevant=False, reason_code=ClaimAnalysisReason.INSUFFICIENT_EVIDENCE),
    )
    result = await service.analyze(_request(env, evidence_card_ids=[card["evidence_card_id"]]))
    assert result.relevant is False
    assert result.claim_ids == []
    assert result.created_count == 0
    assert result.replayed_count == 0
    assert result.reason_code == ClaimAnalysisReason.INSUFFICIENT_EVIDENCE
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- 拒绝（0 写）


async def test_unknown_evidence_ref_aborts_with_zero_writes(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    service, _ = _service(env, _decision(claims=[_candidate(support_refs=["E99"])]))
    with pytest.raises(ClaimAnalysisUnknownEvidenceRef):
        await service.analyze(_request(env, evidence_card_ids=[card["evidence_card_id"]]))
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_cross_relation_conflict_aborts_with_zero_writes(env) -> None:
    a = await _seed_document_card(env, source_url=_URL_1)
    b = await _seed_document_card(env, source_url=_URL_2)
    sorted_ids = sorted([a["evidence_card_id"], b["evidence_card_id"]], key=str)
    service, _ = _service(
        env,
        _decision(claims=[_candidate(support_refs=["E1"], context_refs=["E1"])]),
    )
    with pytest.raises(ClaimAnalysisRelationConflict):
        await service.analyze(_request(env, evidence_card_ids=sorted_ids))
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_company_mismatch_rejected(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    other = await _seed_other_company(env["sessionmaker"])
    service, _ = _service(env, _decision(claims=[_candidate(support_refs=["E1"])]))
    with pytest.raises(ClaimAnalysisEvidenceCompanyMismatch):
        await service.analyze(
            _request(
                env,
                evidence_card_ids=[card["evidence_card_id"]],
                company_id=other,
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_domain_not_ready_defensive(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    service, _ = _service(env, _decision(claims=[_candidate(support_refs=["E1"])]))
    for domain in (
        ClaimAnalysisDomain.FINANCIAL,
        ClaimAnalysisDomain.MACRO,
        ClaimAnalysisDomain.VALUATION,
    ):
        with pytest.raises(ClaimAnalysisDomainNotReady):
            await service.analyze(
                _request(env, evidence_card_ids=[card["evidence_card_id"]], domain=domain)
            )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_critical_policy_enforced(env) -> None:
    """V1.1 closure：supports 无 critical-eligible 证据时 critical 确定性降级 normal。

    修复前：ClaimAnalysisService 直接透传 CRITICAL → ClaimService 抛
    ClaimCriticalEvidenceInsufficient 炸掉整个 Stage4 分析；现在降级为 normal
    （模型不知道证据 eligibility，政策不泄漏给模型）。
    """
    card = await _seed_document_card(env, critical_claim_eligible=False, source_url=_URL_1)
    decision = _decision(
        claims=[_candidate(support_refs=["E1"], importance=ClaimImportance.CRITICAL)]
    )
    service, _ = _service(env, decision)
    result = await service.analyze(_request(env, evidence_card_ids=[card["evidence_card_id"]]))
    assert result.created_count == 1
    async with env["sessionmaker"]() as session:
        from sqlalchemy import select

        from app.db.models.claim import ClaimModel

        row = (
            await session.execute(
                select(ClaimModel).where(ClaimModel.company_id == env["company_id"])
            )
        ).scalar_one()
    assert row.importance == ClaimImportance.NORMAL.value


async def test_critical_with_eligible_support_accepted(env) -> None:
    card = await _seed_document_card(env, critical_claim_eligible=True, source_url=_URL_1)
    decision = _decision(
        claims=[_candidate(support_refs=["E1"], importance=ClaimImportance.CRITICAL)]
    )
    service, _ = _service(env, decision)
    result = await service.analyze(_request(env, evidence_card_ids=[card["evidence_card_id"]]))
    assert len(result.claim_ids) == 1


# ---------------------------------------------------------------- replay


async def test_replay_returns_replayed_claim(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    service, _ = _service(env, _decision(claims=[_candidate(support_refs=["E1"])]))
    request = _request(env, evidence_card_ids=[card["evidence_card_id"]])
    first = await service.analyze(request)
    second = await service.analyze(request)
    assert first.created_count == 1
    assert first.replayed_count == 0
    assert second.created_count == 0
    assert second.replayed_count == 1
    assert second.claim_ids == first.claim_ids
    assert await _claim_count(env["sessionmaker"]) == 1


# ---------------------------------------------------------------- 模型错误映射


async def test_malformed_output_rejected(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    # fake 返回 dict：statement 空白 → schema 校验失败 → ClaimAnalysisMalformedOutput。
    model = FakeClaimAnalysisModel(
        decision={
            "relevant": True,
            "claims": [
                {
                    "statement": "   ",
                    "claim_kind": "inference",
                    "confidence": "medium",
                    "importance": "normal",
                    "support_refs": ["E1"],
                    "contradict_refs": [],
                    "context_refs": [],
                }
            ],
        }
    )
    service = ClaimAnalysisService(env["sessionmaker"], model)
    with pytest.raises(ClaimAnalysisMalformedOutput):
        await service.analyze(_request(env, evidence_card_ids=[card["evidence_card_id"]]))
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_relative_valuation_kind_rejected(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    model = FakeClaimAnalysisModel(
        decision={
            "relevant": True,
            "claims": [
                {
                    "statement": "目标价50元",
                    "claim_kind": "relative_valuation",
                    "confidence": "high",
                    "importance": "normal",
                    "support_refs": ["E1"],
                    "contradict_refs": [],
                    "context_refs": [],
                }
            ],
        }
    )
    service = ClaimAnalysisService(env["sessionmaker"], model)
    with pytest.raises(ClaimAnalysisMalformedOutput):
        await service.analyze(_request(env, evidence_card_ids=[card["evidence_card_id"]]))
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_model_unavailable_propagates(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    # Protocol 契约：provider 失败由模型层翻译为 ClaimAnalysisModelUnavailable
    # （DeepSeek 适配器已做）；服务层保持薄，直接透传。
    model = FakeClaimAnalysisModel(decision=None, error=ClaimAnalysisModelUnavailable)
    service = ClaimAnalysisService(env["sessionmaker"], model)
    with pytest.raises(ClaimAnalysisModelUnavailable):
        await service.analyze(_request(env, evidence_card_ids=[card["evidence_card_id"]]))
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- 最小投影


async def test_evidence_pack_projection_to_model(env) -> None:
    a = await _seed_document_card(env, statement="海外收入同比增长31.4%", source_url=_URL_1)
    b = await _seed_document_card(env, statement="海外销售占比提升", source_url=_URL_2)
    c = await _seed_document_card(env, statement="汇率波动影响毛利", source_url=_URL_3)
    sorted_ids = sorted(
        [a["evidence_card_id"], b["evidence_card_id"], c["evidence_card_id"]], key=str
    )

    service, model = _service(env, _decision(claims=[_candidate(support_refs=["E1"])]))
    await service.analyze(_request(env, evidence_card_ids=sorted_ids))

    assert len(model.calls) == 1
    context: ClaimAnalysisContext = model.calls[0][0]
    pack: EvidencePack = model.calls[0][1]
    assert context.research_question == _QUESTION
    assert context.analysis_domain == ClaimAnalysisDomain.BUSINESS
    assert context.strategy == "business_event_v1"
    # E1..En 确定性映射到 seed 的 evidence ids。
    assert [item.evidence_ref for item in pack.items] == ["E1", "E2", "E3"]
    assert pack.ref_to_card_id["E1"] == sorted_ids[0]
    assert pack.ref_to_card_id["E3"] == sorted_ids[2]
    # 最小投影：包不暴露内部字段。
    for item in pack.items:
        assert not hasattr(item, "evidence_card_id")
        assert not hasattr(item, "locator_refs")
        assert not hasattr(item, "fingerprint")


# ---------------------------------------------------------------- 边界


async def test_no_stage5_report_tables_created(env) -> None:
    card = await _seed_document_card(env, source_url=_URL_1)
    service, _ = _service(env, _decision(claims=[_candidate(support_refs=["E1"])]))
    await service.analyze(_request(env, evidence_card_ids=[card["evidence_card_id"]]))
    async with env["sessionmaker"]() as session:
        stage5_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name IN "
                    "('report_sections')"
                )
            )
        ).scalar_one()
    assert stage5_tables == 0
    # Stage 5A/5B/5C 表已存在（migration 0032/0033/0034），但本阶段不写行。
    outline_rows = (
        await session.execute(text("SELECT count(*) FROM report_outlines"))
    ).scalar_one()
    assert int(outline_rows) == 0
    report_rows = (await session.execute(text("SELECT count(*) FROM reports"))).scalar_one()
    assert int(report_rows) == 0
    check_rows = (
        await session.execute(text("SELECT count(*) FROM report_check_results"))
    ).scalar_one()
    assert int(check_rows) == 0
    # Stage 5D 的 report_audits / review_issues（migration 0035）已存在，
    # 但本阶段不写行。
    audit_rows = (await session.execute(text("SELECT count(*) FROM report_audits"))).scalar_one()
    assert int(audit_rows) == 0
    issue_rows = (await session.execute(text("SELECT count(*) FROM review_issues"))).scalar_one()
    assert int(issue_rows) == 0
