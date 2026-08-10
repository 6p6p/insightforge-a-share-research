"""DraftSectionService integration tests (stage 5B, spec C/I/J/K/L/M/N/O/P).

真实 PostgreSQL + Fake Writer + 真实 LangGraph + PG Checkpointer，全程
**零真实 DeepSeek**（Fake 模型都是确定性返回 / 抛错）。

覆盖（spec Q）：
- E2E：Stage4 → SynthesisResult → ReportOutline → DraftSectionService(Fake
  Writer) 起草 theme section；持久化字段 / payload 只存真实 ID / fake 只看到
  alias（LLM 永不看 UUID / fingerprint）；
- risks_and_gaps section（conflict + gap 恢复，X/G alias）；
- replay：同输入再次起草 → 同 draft_section_id（replayed=True），0 次模型调用；
- 并发：asyncio.gather 同输入 → 只有 1 行（无进程锁）；
- 拒绝路径（0 写）：unknown ref / cross-section / unbound evidence / numeric
  hallucination / forbidden language / model failure / invalid decision；
- missing：outline 不存在 / section 不存在 → DraftSectionNotFound；
- tamper：persisted payload / section_fingerprint / 正文篡改 → replay 拒绝
  DraftSectionIntegrityError（不自动 repair）。
"""

import asyncio
import json
import re
from datetime import date
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisConflict,
    SynthesisEvidenceGap,
    SynthesisPriority,
    SynthesisSeverity,
    SynthesisTheme,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.draft_section.contracts import (
    DRAFT_SECTION_SCHEMA_VERSION,
    WRITER_NAME,
    WRITER_VERSION,
    DraftSectionRequest,
    VerifiedDraftSection,
)
from app.draft_section.errors import (
    DraftSectionCrossSectionRef,
    DraftSectionForbiddenLanguage,
    DraftSectionIntegrityError,
    DraftSectionLegacyVersionUnsupported,
    DraftSectionMalformedOutput,
    DraftSectionModelUnavailable,
    DraftSectionNotFound,
    DraftSectionNumericGroundingError,
    DraftSectionParagraphContract,
    DraftSectionUnboundEvidence,
    DraftSectionUnknownRef,
)
from app.draft_section.prompt import SECTION_PACK_END, SECTION_PACK_START, build_writer_messages
from app.draft_section.service import DraftSectionService
from app.report_outline.service import ReportOutlineService
from app.services.source_registry_service import SourceRegistryService
from app.stage4.runner import Stage4WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.analysis.synthesis.fakes import FakeSynthesisAnalysisModel
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_stage4_workflow import (
    _cleanup,
    _good_models,
    _request,
    _seed_worker_inputs,
)
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX64_RE = re.compile(r"\b[0-9a-f]{64}\b")

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_AS_OF = date(2026, 8, 10)


# ---------------------------------------------------------------- fixtures


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
async def connection_uri() -> str:
    return to_postgres_connection_uri(get_settings().database_url)


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker, monkeypatch) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = await _seed_company(sessionmaker, "600519")
    peer_company_ids = [await _seed_company(sessionmaker, f"6005{2 + i:02d}") for i in range(3)]
    task_id = await _seed_research_task(sessionmaker)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "target_company_id": company_id,
        "peer_company_ids": peer_company_ids,
        "task_id": task_id,
    }
    await _cleanup(sessionmaker)


async def _seed_research_task(sessionmaker) -> UUID:
    from app.db.models.research_task import ResearchTaskModel
    from app.repositories.research_task_repository import ResearchTaskRepository

    task_id = uuid4()
    async with sessionmaker() as session:
        await ResearchTaskRepository(session).create(
            ResearchTaskModel(
                task_id=task_id,
                company_query="600519",
                research_start_date=date(2023, 1, 1),
                research_end_date=date(2026, 12, 31),
                modules=["company_profile"],
                questions=[],
                require_plan_approval=False,
            )
        )
        await session.commit()
    return task_id


# ---------------------------------------------------------------- stage4 helpers


async def _run_stage4_to_result(env, monkeypatch, connection_uri, models) -> UUID:
    """完整 Stage 4 graph → synthesis_result_id（允许注入 synthesis models 变体）。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    deps = _build_deps(env["sessionmaker"], models)
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage4_run(request)
        result = await runner.execute_stage4(run.run_id, request)
    finally:
        await manager.close()
    assert result["synthesis_result_id"] is not None
    return UUID(result["synthesis_result_id"])


def _build_deps(sessionmaker, models: dict) -> None:
    from app.analysis.claims.service import ClaimAnalysisService
    from app.analysis.financial.service import FinancialAnalysisService
    from app.analysis.macro.service import MacroAnalysisService
    from app.analysis.synthesis.service import SynthesisAnalysisService
    from app.analysis.valuation.service import ValuationAnalysisService
    from app.stage4.dependencies import Stage4AnalysisDependencies
    from app.synthesis.service import SynthesisService

    return Stage4AnalysisDependencies(
        sessionmaker=sessionmaker,
        claim_analysis_service=ClaimAnalysisService(sessionmaker, models["claim"]),
        financial_analysis_service=FinancialAnalysisService(sessionmaker, models["financial"]),
        macro_analysis_service=MacroAnalysisService(sessionmaker, models["macro"]),
        valuation_analysis_service=ValuationAnalysisService(sessionmaker, models["valuation"]),
        synthesis_service=SynthesisService(sessionmaker),
        synthesis_analysis_service=SynthesisAnalysisService(sessionmaker, models["synthesis"]),
    )


async def _create_outline(env, monkeypatch, connection_uri, models=None) -> UUID:
    """Stage4 → SynthesisResult → ReportOutline，返回 outline_id。"""
    models = models if models is not None else _good_models()
    synthesis_result_id = await _run_stage4_to_result(env, monkeypatch, connection_uri, models)
    outline = await ReportOutlineService(env["sessionmaker"]).create_or_get_outline(
        synthesis_result_id
    )
    return outline.outline_id


def _two_theme_models() -> dict:
    """两个 theme → theme A（C1/C2）+ theme B（C3/C4/C5）+ risks_and_gaps。"""
    models = _good_models()
    refs = [f"C{i + 1}" for i in range(5)]
    output = SynthesisAnalysisOutput(
        summary="综合判断：营收增长确定、财务稳健、宏观有传导、估值偏高。",
        themes=[
            SynthesisTheme(title="主题A：营收与财务", summary="A", claim_refs=["C1", "C2"]),
            SynthesisTheme(title="主题B：宏观与估值", summary="B", claim_refs=["C3", "C4", "C5"]),
        ],
        claim_roles=[
            SynthesisClaimRoleAssignment(
                claim_ref=ref, role=SynthesisClaimRole.SUPPORT, rationale=f"支持 {ref}"
            )
            for ref in refs
        ],
        duplicates=[],
        conflicts=[
            SynthesisConflict(
                claim_refs=["C1", "C2"],
                description="营收口径存在分歧",
                severity=SynthesisSeverity.MEDIUM,
                resolution_direction="以年报披露为准",
            )
        ],
        evidence_gaps=[
            SynthesisEvidenceGap(
                description="缺少经营现金流证据",
                claim_refs=refs[:1],
                suggested_evidence="经营现金流数据",
                priority=SynthesisPriority.MEDIUM,
            )
        ],
    )
    models["synthesis"] = FakeSynthesisAnalysisModel(output=output)
    return models


def _service(env, fake: FakeDraftSectionModel) -> DraftSectionService:
    return DraftSectionService(env["sessionmaker"], fake)


async def _draft_row(sessionmaker, draft_section_id: UUID) -> dict | None:
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT draft_section_id, outline_id, section_id, section_order, "
                        "section_type, title, section_schema_version, writer_name, "
                        "writer_version, writer_model_id, writer_input_fingerprint, "
                        "section_payload, section_fingerprint "
                        "FROM draft_sections WHERE draft_section_id = :id"
                    ).bindparams(id=draft_section_id)
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


async def _draft_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM draft_sections"))).scalar_one()
        )


# ---------------------------------------------------------------- E2E theme section


async def test_create_theme_section_e2e(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    service = _service(env, fake)

    result = await service.create_or_get_section(
        DraftSectionRequest(outline_id=outline_id, section_id="S1")
    )

    assert result.replayed is False
    assert result.paragraph_count >= 1
    assert len(result.writer_input_fingerprint) == 64
    assert len(result.section_fingerprint) == 64

    # fake 恰好调用一次；pack 只含 alias / 最小字段（LLM 永不看 UUID/fingerprint）。
    assert len(fake.calls) == 1
    pack = fake.calls[0]
    assert pack.claims
    assert pack.evidence
    messages = build_writer_messages(pack)
    assert [m["role"] for m in messages] == ["system", "user"]
    payload_str = messages[1]["content"]
    assert SECTION_PACK_START in payload_str and SECTION_PACK_END in payload_str
    assert _UUID_RE.search(payload_str) is None
    assert _HEX64_RE.search(payload_str) is None
    for forbidden in ("claim_id", "evidence_card_id", "claim_fingerprint", "evidence_fingerprint"):
        assert forbidden not in payload_str

    row = await _draft_row(env["sessionmaker"], result.draft_section_id)
    assert row is not None
    assert row["outline_id"] == outline_id
    assert row["section_id"] == "S1"
    assert row["section_order"] == 1
    assert row["section_type"] == "theme"
    assert row["section_schema_version"] == DRAFT_SECTION_SCHEMA_VERSION
    assert row["writer_name"] == WRITER_NAME
    assert row["writer_version"] == WRITER_VERSION
    assert row["writer_model_id"] == fake.model_id
    assert len(row["writer_input_fingerprint"]) == 64
    assert len(row["section_fingerprint"]) == 64
    paragraphs = row["section_payload"]["paragraphs"]
    assert paragraphs
    for paragraph in paragraphs:
        assert paragraph["text"]
        # 只存真实 UUID（不是 alias / 内部标识）。
        assert paragraph["claim_ids"] and all(
            _UUID_RE.fullmatch(cid) for cid in paragraph["claim_ids"]
        )
        assert paragraph["evidence_card_ids"] and all(
            _UUID_RE.fullmatch(eid) for eid in paragraph["evidence_card_ids"]
        )


async def test_create_risks_and_gaps_section_with_conflict_and_gap(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    service = _service(env, fake)

    result = await service.create_or_get_section(
        DraftSectionRequest(outline_id=outline_id, section_id="S3")
    )

    assert result.replayed is False
    assert result.paragraph_count >= 3  # 基础段 + X + G
    pack = fake.calls[0]
    # risks_and_gaps 允许整个合成输入集 + 恢复 X/G（spec I）。
    assert len(pack.claims) == 5
    assert len(pack.conflicts) == 1
    assert len(pack.gaps) == 1
    assert pack.conflicts[0].alias == "X1"
    assert pack.gaps[0].alias == "G1"
    row = await _draft_row(env["sessionmaker"], result.draft_section_id)
    assert row["section_type"] == "risks_and_gaps"
    assert _UUID_RE.fullmatch(row["section_payload"]["paragraphs"][0]["claim_ids"][0])


# ---------------------------------------------------------------- replay / concurrency


async def test_replay_same_row_zero_model_calls(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    fake1 = FakeDraftSectionModel(decision_factory=valid_decision_for)
    first = await _service(env, fake1).create_or_get_section(
        DraftSectionRequest(outline_id=outline_id, section_id="S1")
    )
    assert len(fake1.calls) == 1

    # 第二次调用：新 fake（模型 id 相同）→ replay，0 次模型调用。
    fake2 = FakeDraftSectionModel(decision_factory=valid_decision_for)
    second = await _service(env, fake2).create_or_get_section(
        DraftSectionRequest(outline_id=outline_id, section_id="S1")
    )
    assert second.draft_section_id == first.draft_section_id
    assert second.replayed is True
    assert fake2.calls == []
    assert await _draft_count(env["sessionmaker"]) == 1


async def test_concurrent_same_input_single_row(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    service = _service(env, fake)
    request = DraftSectionRequest(outline_id=outline_id, section_id="S1")

    first, second = await asyncio.gather(
        service.create_or_get_section(request),
        service.create_or_get_section(request),
    )

    assert first.draft_section_id == second.draft_section_id
    assert await _draft_count(env["sessionmaker"]) == 1


# ---------------------------------------------------------------- rejection paths (0 writes)


async def _assert_rejected(env, outline_id, fake_factory, error_cls) -> None:
    fake = fake_factory()
    service = _service(env, fake)
    with pytest.raises(error_cls):
        await service.create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id="S1")
        )
    assert await _draft_count(env["sessionmaker"]) == 0


def _unknown_ref_factory():
    def _factory(pack):
        from app.draft_section.contracts import ParagraphCandidate, WriterDecision

        claim = pack.claims[0]
        ev = next(e for e in pack.evidence if claim.alias in e.claim_aliases)
        return WriterDecision(
            paragraphs=[
                ParagraphCandidate(text="未知引用。", claim_refs=["C99"], evidence_refs=[ev.alias])
            ]
        )

    return lambda: FakeDraftSectionModel(decision_factory=_factory)


def _unbound_evidence_factory():
    def _factory(pack):
        from app.draft_section.contracts import ParagraphCandidate, WriterDecision

        claim = pack.claims[0]
        # 找一个不绑定该 claim 的 evidence → unbound。
        ev = next(e for e in pack.evidence if claim.alias not in e.claim_aliases)
        return WriterDecision(
            paragraphs=[
                ParagraphCandidate(
                    text="公司营收保持增长态势。",
                    claim_refs=[claim.alias],
                    evidence_refs=[ev.alias],
                )
            ]
        )

    return lambda: FakeDraftSectionModel(decision_factory=_factory)


def _numeric_hallucination_factory():
    def _factory(pack):
        from app.draft_section.contracts import ParagraphCandidate, WriterDecision

        claim = pack.claims[0]
        ev = next(e for e in pack.evidence if claim.alias in e.claim_aliases)
        return WriterDecision(
            paragraphs=[
                ParagraphCandidate(
                    text="营收增长99%。", claim_refs=[claim.alias], evidence_refs=[ev.alias]
                )
            ]
        )

    return lambda: FakeDraftSectionModel(decision_factory=_factory)


def _forbidden_language_factory():
    def _factory(pack):
        from app.draft_section.contracts import ParagraphCandidate, WriterDecision

        claim = pack.claims[0]
        ev = next(e for e in pack.evidence if claim.alias in e.claim_aliases)
        return WriterDecision(
            paragraphs=[
                ParagraphCandidate(
                    text="建议买入该股票。", claim_refs=[claim.alias], evidence_refs=[ev.alias]
                )
            ]
        )

    return lambda: FakeDraftSectionModel(decision_factory=_factory)


async def test_reject_unknown_claim_ref(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    await _assert_rejected(env, outline_id, _unknown_ref_factory(), DraftSectionUnknownRef)


async def test_reject_unbound_evidence(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    await _assert_rejected(
        env, outline_id, _unbound_evidence_factory(), DraftSectionUnboundEvidence
    )


async def test_reject_numeric_hallucination(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    await _assert_rejected(
        env, outline_id, _numeric_hallucination_factory(), DraftSectionNumericGroundingError
    )


async def test_reject_forbidden_language(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    await _assert_rejected(
        env, outline_id, _forbidden_language_factory(), DraftSectionForbiddenLanguage
    )


async def test_reject_model_failure_zero_writes(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    fake = FakeDraftSectionModel(error=DraftSectionModelUnavailable)
    service = _service(env, fake)
    with pytest.raises(DraftSectionModelUnavailable):
        await service.create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id="S1")
        )
    assert await _draft_count(env["sessionmaker"]) == 0


async def test_reject_invalid_decision_zero_writes(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    # 0 段落 → WriterDecision schema 校验失败 → MalformedOutput。
    fake = FakeDraftSectionModel(decision={"paragraphs": []})
    service = _service(env, fake)
    with pytest.raises(DraftSectionMalformedOutput):
        await service.create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id="S1")
        )
    assert await _draft_count(env["sessionmaker"]) == 0


async def test_reject_cross_section_claim(env, monkeypatch, connection_uri) -> None:
    # 两个 theme：S1 只允许 C1/C2；引用 S2 的 C3（在合成集内）→ CrossSection。
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())

    def _factory(pack):
        from app.draft_section.contracts import ParagraphCandidate, WriterDecision

        claim = pack.claims[0]
        ev = next(e for e in pack.evidence if claim.alias in e.claim_aliases)
        return WriterDecision(
            paragraphs=[
                ParagraphCandidate(
                    text="公司营收保持增长态势。", claim_refs=["C3"], evidence_refs=[ev.alias]
                )
            ]
        )

    fake = FakeDraftSectionModel(decision_factory=_factory)
    service = _service(env, fake)
    with pytest.raises(DraftSectionCrossSectionRef) as excinfo:
        await service.create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id="S1")
        )
    assert excinfo.value.code == "draft_section_cross_section_ref"
    assert await _draft_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- missing


async def test_missing_outline_rejected(env) -> None:
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    service = _service(env, fake)
    with pytest.raises(DraftSectionNotFound):
        await service.create_or_get_section(
            DraftSectionRequest(outline_id=uuid4(), section_id="S1")
        )
    assert fake.calls == []


async def test_missing_section_rejected(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    service = _service(env, fake)
    with pytest.raises(DraftSectionNotFound):
        await service.create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id="S99")
        )
    assert fake.calls == []


# ---------------------------------------------------------------- tamper → replay rejects


async def test_replay_rejects_tampered_payload(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    await _service(
        env, FakeDraftSectionModel(decision_factory=valid_decision_for)
    ).create_or_get_section(DraftSectionRequest(outline_id=outline_id, section_id="S1"))
    # 篡改 section_payload（合法结构但 ID 不属于 allowed 集）→ replay 拒。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_sections SET section_payload = CAST(:payload AS jsonb) "
                "WHERE outline_id = :oid"
            ).bindparams(
                payload=json.dumps(
                    {
                        "paragraphs": [
                            {
                                "text": "篡改正文",
                                "claim_ids": [str(uuid4())],
                                "evidence_card_ids": [str(uuid4())],
                                "conflict_indexes": [],
                                "evidence_gap_indexes": [],
                            }
                        ]
                    }
                ),
                oid=outline_id,
            )
        )
        await session.commit()

    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    with pytest.raises(DraftSectionIntegrityError) as excinfo:
        await _service(env, fake).create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id="S1")
        )
    assert excinfo.value.code == "draft_section_integrity_error"
    assert fake.calls == []  # replay 路径 0 次模型调用


async def test_replay_rejects_tampered_section_fingerprint(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    await _service(
        env, FakeDraftSectionModel(decision_factory=valid_decision_for)
    ).create_or_get_section(DraftSectionRequest(outline_id=outline_id, section_id="S1"))
    # 篡改 section_fingerprint → 重算不匹配 → 拒。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_sections SET section_fingerprint = :fp WHERE outline_id = :oid"
            ).bindparams(fp="f" * 64, oid=outline_id)
        )
        await session.commit()

    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    with pytest.raises(DraftSectionIntegrityError):
        await _service(env, fake).create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id="S1")
        )
    assert fake.calls == []


async def test_replay_rejects_tampered_text_in_payload(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    first = await _service(
        env, FakeDraftSectionModel(decision_factory=valid_decision_for)
    ).create_or_get_section(DraftSectionRequest(outline_id=outline_id, section_id="S1"))
    row = await _draft_row(env["sessionmaker"], first.draft_section_id)
    # 只改正文（structure 仍合法，ID 仍在 allowed 集）→ verify_resolved_payload
    # 通过，但 section_fingerprint 重算不匹配 → 拒。
    corrupted = dict(row["section_payload"])
    corrupted["paragraphs"] = [dict(p, text="被篡改的正文。") for p in corrupted["paragraphs"]]
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_sections SET section_payload = CAST(:payload AS jsonb) "
                "WHERE draft_section_id = :id"
            ).bindparams(
                payload=json.dumps(corrupted, ensure_ascii=False), id=first.draft_section_id
            )
        )
        await session.commit()

    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    with pytest.raises(DraftSectionIntegrityError):
        await _service(env, fake).create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id="S1")
        )
    assert fake.calls == []


# ---------------------------------------------------------------- verify draft section integrity


async def test_verify_integrity_happy_path(env, monkeypatch, connection_uri) -> None:
    """起草后完整重建验证通过：返回 VerifiedDraftSection，0 次模型调用。"""
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    service = _service(env, fake)
    created = await service.create_or_get_section(
        DraftSectionRequest(outline_id=outline_id, section_id="S1")
    )
    assert len(fake.calls) == 1

    verified: VerifiedDraftSection = await service.verify_draft_section_integrity(
        created.draft_section_id
    )
    assert verified.draft_section_id == created.draft_section_id
    assert verified.outline_id == outline_id
    assert verified.section_id == "S1"
    assert verified.section_order == 1
    assert verified.section_type == "theme"
    assert verified.section_schema_version == DRAFT_SECTION_SCHEMA_VERSION
    assert verified.writer_name == WRITER_NAME
    assert verified.writer_version == WRITER_VERSION
    assert verified.writer_model_id == fake.model_id
    assert verified.writer_input_fingerprint == created.writer_input_fingerprint
    assert verified.section_fingerprint == created.section_fingerprint
    assert verified.paragraph_count == created.paragraph_count
    assert len(fake.calls) == 1  # verify 不触发模型调用


async def test_verify_integrity_missing_draft_rejected(env) -> None:
    service = _service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    with pytest.raises(DraftSectionNotFound):
        await service.verify_draft_section_integrity(uuid4())


async def test_verify_integrity_legacy_version_rejected(env, monkeypatch, connection_uri) -> None:
    """v1 旧行无法被 v2 contract 稳定重建 → 明确 unsupported，不假验证。"""
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    created = await _service(
        env, FakeDraftSectionModel(decision_factory=valid_decision_for)
    ).create_or_get_section(DraftSectionRequest(outline_id=outline_id, section_id="S1"))
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_sections SET writer_version = :v WHERE draft_section_id = :id"
            ).bindparams(v=1, id=created.draft_section_id)
        )
        await session.commit()

    service = _service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    with pytest.raises(DraftSectionLegacyVersionUnsupported) as excinfo:
        await service.verify_draft_section_integrity(created.draft_section_id)
    assert excinfo.value.code == "draft_section_legacy_version_unsupported"


async def test_verify_integrity_rejects_tampered_identity(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    created = await _service(
        env, FakeDraftSectionModel(decision_factory=valid_decision_for)
    ).create_or_get_section(DraftSectionRequest(outline_id=outline_id, section_id="S1"))
    # 篡改身份字段（title）→ 身份对比失败。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE draft_sections SET title = :t WHERE draft_section_id = :id").bindparams(
                t="被篡改的标题", id=created.draft_section_id
            )
        )
        await session.commit()

    service = _service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    with pytest.raises(DraftSectionIntegrityError) as excinfo:
        await service.verify_draft_section_integrity(created.draft_section_id)
    assert excinfo.value.code == "draft_section_integrity_error"


async def test_verify_integrity_rejects_tampered_input_fingerprint(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    created = await _service(
        env, FakeDraftSectionModel(decision_factory=valid_decision_for)
    ).create_or_get_section(DraftSectionRequest(outline_id=outline_id, section_id="S1"))
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_sections SET writer_input_fingerprint = :fp "
                "WHERE draft_section_id = :id"
            ).bindparams(fp="0" * 64, id=created.draft_section_id)
        )
        await session.commit()

    service = _service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    with pytest.raises(DraftSectionIntegrityError):
        await service.verify_draft_section_integrity(created.draft_section_id)


async def test_verify_integrity_rejects_tampered_section_fingerprint(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    created = await _service(
        env, FakeDraftSectionModel(decision_factory=valid_decision_for)
    ).create_or_get_section(DraftSectionRequest(outline_id=outline_id, section_id="S1"))
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_sections SET section_fingerprint = :fp WHERE draft_section_id = :id"
            ).bindparams(fp="1" * 64, id=created.draft_section_id)
        )
        await session.commit()

    service = _service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    with pytest.raises(DraftSectionIntegrityError):
        await service.verify_draft_section_integrity(created.draft_section_id)


async def test_verify_integrity_rejects_contract_violating_payload(
    env, monkeypatch, connection_uri
) -> None:
    """structure 合法但违反 Section-aware contract（theme 段落缺 evidence）→
    verify_payload_contracts 拒绝（不自动 repair）。"""
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    created = await _service(
        env, FakeDraftSectionModel(decision_factory=valid_decision_for)
    ).create_or_get_section(DraftSectionRequest(outline_id=outline_id, section_id="S1"))
    row = await _draft_row(env["sessionmaker"], created.draft_section_id)
    corrupted = dict(row["section_payload"])
    corrupted["paragraphs"] = [dict(p, evidence_card_ids=[]) for p in corrupted["paragraphs"]]
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_sections SET section_payload = CAST(:payload AS jsonb) "
                "WHERE draft_section_id = :id"
            ).bindparams(
                payload=json.dumps(corrupted, ensure_ascii=False),
                id=created.draft_section_id,
            )
        )
        await session.commit()

    service = _service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    with pytest.raises(DraftSectionParagraphContract) as excinfo:
        await service.verify_draft_section_integrity(created.draft_section_id)
    assert excinfo.value.code == "draft_section_paragraph_contract"
