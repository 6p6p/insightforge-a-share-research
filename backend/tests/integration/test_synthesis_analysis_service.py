"""SynthesisAnalysisService integration tests (stage 4D.1B, spec V).

需要真实 PostgreSQL（127.0.0.1:5433）。公司 / Evidence / Calculation /
Comparison / Claim 全部用真实服务链 seed；synthesis 经 SynthesisService 登记为
SynthesisRun；综合分析经 SynthesisAnalysisService + FakeSynthesisAnalysisModel。
**零 Chroma / 零真实 LLM / 零 LangGraph / 零 Report / 零 Audit**。

覆盖（spec V）：
- E2E：4 条跨 domain Claim → 1 run → Fake 合法输出 → 1 行 result（result_schema_
  version / analyst identity / fingerprint / JSONB 字段全部核对）；
- Replay：同 run + 同输出 → 同 fingerprint → replayed=True → 仍 1 行；
- strict validation：unknown C ref → UnknownRef；claim_roles 缺漏 → NoCherry-
  Picking（两者均 0 写）；
- malformed：模型返回缺字段 dict → MalformedOutput；provider 抛错 → 稳定错误冒泡；
- run 缺失 → RunNotFound（**不调用 LLM**，calls 为空）；
- claim 损坏（gateway 完整性）→ SynthesisClaimIntegrityError；
- result 篡改（改 themes 不改 fingerprint）→ replay 校验 → SynthesisIntegrityError
  （**不自动 repair**）；
- 边界：Service 只持有 sessionmaker + model；无 Stage 5 report 表。
"""

import json
from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.synthesis.contracts import (
    SYNTHESIS_ANALYST_NAME,
    SYNTHESIS_ANALYST_VERSION,
    SYNTHESIS_RESULT_SCHEMA_VERSION,
    SynthesisAnalysisOutput,
    SynthesisAnalysisRequest,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisEvidenceGap,
    SynthesisPriority,
    SynthesisTheme,
)
from app.analysis.synthesis.errors import (
    SynthesisAnalysisMalformedOutput,
    SynthesisAnalysisModelUnavailable,
    SynthesisAnalysisNoCherryPicking,
    SynthesisAnalysisRunNotFound,
    SynthesisAnalysisUnknownRef,
)
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.repositories.claim_synthesis_result_repository import (
    ClaimSynthesisResultRepository,
)
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.errors import SynthesisClaimIntegrityError, SynthesisIntegrityError
from app.synthesis.service import SynthesisService
from tests.analysis.synthesis.fakes import FakeSynthesisAnalysisModel
from tests.integration.test_claim_synthesis_service import (
    _draft,
    _seed_doc_card,
    _seed_financial_claim,
    _seed_generic_claim,
    _seed_macro_claim,
    _seed_valuation_claim,
)
from tests.integration.test_macro_claim_service import _seed_macro_card
from tests.integration.test_valuation_claim_service import _seed_company, _seed_comparison

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


def _valid_output() -> SynthesisAnalysisOutput:
    """Fake 的合法输出（claim_roles 恰好覆盖 C1..C4）。"""
    refs = ["C1", "C2", "C3", "C4"]
    return SynthesisAnalysisOutput(
        summary="贵州茅台综合判断：营收增长确定性较高，但估值偏高存在压力。",
        themes=[
            SynthesisTheme(
                title="营收增长确定",
                summary="多角度证据支持营收增长。",
                claim_refs=refs,
            )
        ],
        claim_roles=[
            SynthesisClaimRoleAssignment(
                claim_ref=ref,
                role=SynthesisClaimRole.SUPPORT,
                rationale=f"支持 {ref}",
            )
            for ref in refs
        ],
        duplicates=[],
        conflicts=[],
        evidence_gaps=[
            SynthesisEvidenceGap(
                description="缺少现金流证据",
                claim_refs=refs[:1],
                suggested_evidence="经营现金流数据",
                priority=SynthesisPriority.MEDIUM,
            )
        ],
    )


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


async def _seed_cross_domain_claims(env: dict, monkeypatch) -> list:
    """同一 company + 同一 research_question 下 4 条跨 domain Claim。"""
    doc_card = await _seed_doc_card(env)
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    business = await _seed_generic_claim(env, doc_card)
    financial = await _seed_financial_claim(env)
    macro = await _seed_macro_claim(env, macro_card, doc_card)
    comparison = await _seed_comparison(env)
    valuation = await _seed_valuation_claim(env, comparison)
    return [business.claim_id, financial.claim_id, macro.claim_id, valuation.claim_id]


async def _list_results(sessionmaker, synthesis_id) -> list:
    async with sessionmaker() as session:
        return await ClaimSynthesisResultRepository(session).list_by_synthesis(synthesis_id)


# ---------------------------------------------------------------- E2E


async def test_analysis_end_to_end(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    run = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
        _draft(env, claim_ids)
    )
    output = _valid_output()
    model = FakeSynthesisAnalysisModel(output=output, model_id="deepseek:deepseek-v4-flash")
    service = SynthesisAnalysisService(env["sessionmaker"], model)

    result = await service.analyze(SynthesisAnalysisRequest(synthesis_id=run.synthesis_id))
    assert result.replayed is False
    assert result.claim_count == 4
    assert result.synthesis_id == run.synthesis_id
    assert len(result.result_fingerprint) == 64

    rows = await _list_results(env["sessionmaker"], run.synthesis_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.synthesis_result_id == result.synthesis_result_id
    assert row.result_schema_version == SYNTHESIS_RESULT_SCHEMA_VERSION
    assert row.result_fingerprint == result.result_fingerprint
    assert row.analyst_name == SYNTHESIS_ANALYST_NAME
    assert row.analyst_version == SYNTHESIS_ANALYST_VERSION
    assert row.analyst_model_id == "deepseek:deepseek-v4-flash"
    assert row.summary == output.summary
    assert len(row.themes) == 1
    assert len(row.claim_roles) == 4
    assert len(row.evidence_gaps) == 1
    # LLM 只看到 C alias，不看到 UUID。
    assert len(model.calls) == 1
    pack = model.calls[0][1]
    assert [item.alias for item in pack.items] == ["C1", "C2", "C3", "C4"]


async def test_replay_same_output_returns_same_result(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    run = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
        _draft(env, claim_ids)
    )
    model = FakeSynthesisAnalysisModel(output=_valid_output())
    service = SynthesisAnalysisService(env["sessionmaker"], model)
    request = SynthesisAnalysisRequest(synthesis_id=run.synthesis_id)

    first = await service.analyze(request)
    second = await service.analyze(request)
    assert second.replayed is True
    assert second.synthesis_result_id == first.synthesis_result_id
    assert second.result_fingerprint == first.result_fingerprint
    assert len(await _list_results(env["sessionmaker"], run.synthesis_id)) == 1


# ---------------------------------------------------------------- strict validation


async def test_unknown_ref_rejected_zero_writes(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    run = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
        _draft(env, claim_ids)
    )
    output = _valid_output().model_copy(
        update={"themes": [SynthesisTheme(title="t", summary="s", claim_refs=["C99"])]}
    )
    service = SynthesisAnalysisService(
        env["sessionmaker"], FakeSynthesisAnalysisModel(output=output)
    )
    with pytest.raises(SynthesisAnalysisUnknownRef):
        await service.analyze(SynthesisAnalysisRequest(synthesis_id=run.synthesis_id))
    assert await _list_results(env["sessionmaker"], run.synthesis_id) == []


async def test_no_cherry_picking_rejected_zero_writes(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    run = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
        _draft(env, claim_ids)
    )
    output = _valid_output().model_copy(update={"claim_roles": _valid_output().claim_roles[:3]})
    service = SynthesisAnalysisService(
        env["sessionmaker"], FakeSynthesisAnalysisModel(output=output)
    )
    with pytest.raises(SynthesisAnalysisNoCherryPicking):
        await service.analyze(SynthesisAnalysisRequest(synthesis_id=run.synthesis_id))
    assert await _list_results(env["sessionmaker"], run.synthesis_id) == []


async def test_malformed_output_rejected(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    run = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
        _draft(env, claim_ids)
    )
    # dict 缺 claim_roles 等必填字段 → 服务层防御性 double-check → MalformedOutput。
    service = SynthesisAnalysisService(
        env["sessionmaker"], FakeSynthesisAnalysisModel(output={"summary": "x"})
    )
    with pytest.raises(SynthesisAnalysisMalformedOutput):
        await service.analyze(SynthesisAnalysisRequest(synthesis_id=run.synthesis_id))
    assert await _list_results(env["sessionmaker"], run.synthesis_id) == []


async def test_model_unavailable_propagates(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    run = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
        _draft(env, claim_ids)
    )
    service = SynthesisAnalysisService(
        env["sessionmaker"],
        FakeSynthesisAnalysisModel(error=SynthesisAnalysisModelUnavailable),
    )
    with pytest.raises(SynthesisAnalysisModelUnavailable):
        await service.analyze(SynthesisAnalysisRequest(synthesis_id=run.synthesis_id))


# ---------------------------------------------------------------- inputs / integrity


async def test_run_not_found_does_not_call_llm(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(_draft(env, claim_ids))
    model = FakeSynthesisAnalysisModel(output=_valid_output())
    service = SynthesisAnalysisService(env["sessionmaker"], model)
    with pytest.raises(SynthesisAnalysisRunNotFound):
        await service.analyze(SynthesisAnalysisRequest(synthesis_id=uuid4()))
    assert model.calls == []


async def test_corrupted_input_claim_rejected(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    run = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
        _draft(env, claim_ids)
    )
    # 篡改 generic claim fingerprint（不更新相关子表）→ gateway 完整性失败。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE claims SET claim_fingerprint = :fp WHERE claim_id = :cid").bindparams(
                fp="f" * 64, cid=claim_ids[0]
            )
        )
        await session.commit()
    service = SynthesisAnalysisService(
        env["sessionmaker"], FakeSynthesisAnalysisModel(output=_valid_output())
    )
    with pytest.raises(SynthesisClaimIntegrityError):
        await service.analyze(SynthesisAnalysisRequest(synthesis_id=run.synthesis_id))


# ---------------------------------------------------------------- Gate 0 read-side integrity


async def _seed_run(env, monkeypatch):
    """seed 合法跨 domain run + 合法 Fake 输出，返回 (service, request, model)。"""
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    run = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
        _draft(env, claim_ids)
    )
    model = FakeSynthesisAnalysisModel(output=_valid_output())
    service = SynthesisAnalysisService(env["sessionmaker"], model)
    request = SynthesisAnalysisRequest(synthesis_id=run.synthesis_id)
    return service, request, model


async def test_tampered_cutoff_rejected_zero_model_calls(env, monkeypatch) -> None:
    """SQL 篡改 run.analysis_as_of（fingerprint 不变）→ read-side 重算指纹不匹配
    → 拒绝，LLM 一次都不调用。"""
    service, request, model = await _seed_run(env, monkeypatch)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE claim_synthesis_runs SET analysis_as_of = :c WHERE synthesis_id = :sid"
            ).bindparams(c=date(2026, 8, 20), sid=request.synthesis_id)
        )
        await session.commit()
    with pytest.raises(SynthesisIntegrityError):
        await service.analyze(request)
    assert model.calls == []


async def test_tampered_input_links_rejected_zero_model_calls(env, monkeypatch) -> None:
    """SQL 篡改 input link set（删一条）→ claim set 变化 → 重算指纹不匹配
    → 拒绝，LLM 一次都不调用。"""
    service, request, model = await _seed_run(env, monkeypatch)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "DELETE FROM claim_synthesis_input_links "
                "WHERE synthesis_id = :sid AND claim_id = ("
                "SELECT claim_id FROM claim_synthesis_input_links "
                "WHERE synthesis_id = :sid ORDER BY claim_id::text LIMIT 1)"
            ).bindparams(sid=request.synthesis_id)
        )
        await session.commit()
    with pytest.raises(SynthesisIntegrityError):
        await service.analyze(request)
    assert model.calls == []


async def test_tampered_fingerprint_rejected_zero_model_calls(env, monkeypatch) -> None:
    """SQL 篡改 run.synthesis_fingerprint → 重算指纹 != persisted → 拒绝，
    LLM 一次都不调用。"""
    service, request, model = await _seed_run(env, monkeypatch)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE claim_synthesis_runs SET synthesis_fingerprint = :fp "
                "WHERE synthesis_id = :sid"
            ).bindparams(fp="f" * 64, sid=request.synthesis_id)
        )
        await session.commit()
    with pytest.raises(SynthesisIntegrityError):
        await service.analyze(request)
    assert model.calls == []


async def test_tampered_result_rejected_on_replay(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    run = await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(
        _draft(env, claim_ids)
    )
    service = SynthesisAnalysisService(
        env["sessionmaker"], FakeSynthesisAnalysisModel(output=_valid_output())
    )
    request = SynthesisAnalysisRequest(synthesis_id=run.synthesis_id)
    first = await service.analyze(request)
    # 篡改 JSONB themes 但不改 fingerprint → 同 fingerprint 命中 → replay 校验失败。
    async with env["sessionmaker"]() as session:
        # 原生 SQL 的 psycopg 无法直接适配 Python dict → 先 json.dumps，再 CAST AS JSONB。
        await session.execute(
            text(
                "UPDATE claim_synthesis_results SET themes = CAST(:t AS JSONB) "
                "WHERE synthesis_result_id = :rid"
            ).bindparams(
                t=json.dumps([{"title": "x", "summary": "y", "claim_refs": ["C1"]}]),
                rid=first.synthesis_result_id,
            )
        )
        await session.commit()
    with pytest.raises(SynthesisIntegrityError):
        await service.analyze(request)


# ---------------------------------------------------------------- boundary


async def test_boundary_no_stage5_service_holds_only_deps(env, monkeypatch) -> None:
    claim_ids = await _seed_cross_domain_claims(env, monkeypatch)
    await SynthesisService(env["sessionmaker"]).create_or_get_synthesis(_draft(env, claim_ids))
    service = SynthesisAnalysisService(
        env["sessionmaker"], FakeSynthesisAnalysisModel(output=_valid_output())
    )
    # read-side integrity 委托 SynthesisService，不复制 replay 规则。
    assert list(vars(service).keys()) == ["_sessionmaker", "_model", "_synthesis"]
    # 无 Stage 5D+ 表（audits / report_sections / review_issues）；Stage 5A/5B/5C
    # 表（report_outlines / draft_sections / reports / report_check_results，
    # migration 0032/0033/0034）允许存在。
    async with env["sessionmaker"]() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('audits', 'report_sections', 'review_issues')"
            )
        )
        assert result.scalars().all() == []
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
