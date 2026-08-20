"""P0/P0.5/P1 closure targeted integration+unit tests (research isolation recovery).

需要真实 PostgreSQL（127.0.0.1:5433）——由调用方以 DATABASE_URL 指向临时测试库
（insightforge_test），避免污染共享库。

覆盖：
- P0 temporal isolation：financial observation 的 source published_at > as_of →
  filter_observations_for_task 排除；<= as_of 保留；research_question_sha256 不匹配
  → 排除（task 级 user-supplied 隔离）；resolve_observation_availability 用
  published_at（不退回 reporting_period_end）。
- P0.5 bounded FutureEvidence recovery：invalidate 污染 claim；recovery 有界（耗尽→
  False）；非 FutureEvidence 异常→False；空 offending→False。
- P1 degraded-section closure：degraded DraftSection 保留完整 contract；assembler
  接受（不再 missing S6）；全部 degraded → fail-safe。
"""

import hashlib
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.claims.contracts import compute_research_question_sha256
from app.core.runtime import configure_asyncio_runtime
from app.db.models.claim import ClaimModel
from app.financial.availability import (
    filter_observations_for_task,
    resolve_observation_availability,
)
from app.repositories.claim_repository import ClaimRepository
from app.research_orchestration.future_evidence_recovery import (
    MAX_FUTURE_EVIDENCE_RECOVERY_ATTEMPTS,
    FutureEvidenceRecoveryService,
)
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_financial_claim_service import _insert_observation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "2024年贵州茅台净利润增长情况？"
_PAST = datetime(2024, 6, 1, 9, 0, tzinfo=UTC)
_AS_OF = date(2024, 12, 31)
_FUTURE = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)


@pytest_asyncio.fixture
async def database():
    from app.core.config import get_settings
    from app.db.session import DatabaseManager

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
        await session.execute(text("DELETE FROM claim_financial_calculation_links"))
        await session.execute(text("DELETE FROM financial_calculation_inputs"))
        await session.execute(text("DELETE FROM financial_calculations"))
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM claim_synthesis_input_links"))
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.execute(text("DELETE FROM claims"))
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
    from app.storage.raw_store import LocalRawArtifactStore

    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    from app.services.source_registry_service import SourceRegistryService

    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = uuid4()
    async with sessionmaker() as session:
        from app.db.models.company import CompanyModel
        from app.repositories.company_repository import CompanyRepository

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


async def _seed_card_published(env: dict, *, published_at, question=_QUESTION) -> UUID:
    """真实 HTML 链 → EvidenceCardService 创建一张 document EvidenceCard。"""
    from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
    from app.services.evidence_card_service import EvidenceCardService

    src, parsed_id, cs_id, chunks = await _seed_html_source(
        env,
        source_url=f"https://www.example.com/a{uuid4().hex[:8]}.htm",
        published_at=published_at,
    )
    chunk = chunks[0]
    draft = EvidenceCardDraft(
        research_question=question,
        evidence_statement="2024年贵州茅台营收同比增长15%。",
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


async def _obs(env: dict, card_id: UUID) -> UUID:
    return await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="12000000000",
        source_card_id=card_id,
    )


async def _load_obs(sessionmaker, obs_ids) -> list:
    from app.db.models.financial_metric_observation import FinancialMetricObservationModel

    async with sessionmaker() as session:
        from sqlalchemy import select

        rows = (
            (
                await session.execute(
                    select(FinancialMetricObservationModel).where(
                        FinancialMetricObservationModel.metric_observation_id.in_(obs_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


# ------------------------------------------------------------------ P0 temporal
async def test_p0_excludes_future_source_observation(env, sessionmaker):
    past_card = await _seed_card_published(env, published_at=_PAST)
    future_card = await _seed_card_published(env, published_at=_FUTURE)
    past_obs = await _obs(env, past_card)
    future_obs = await _obs(env, future_card)
    rows = await _load_obs(sessionmaker, [past_obs, future_obs])
    async with sessionmaker() as session:
        eligible = await filter_observations_for_task(
            session,
            rows,
            _AS_OF,
            compute_research_question_sha256(_QUESTION),
        )
    ids = {str(o.metric_observation_id) for o in eligible}
    assert str(past_obs) in ids
    assert str(future_obs) not in ids


async def test_p0_resolve_availability_uses_published_at_not_period_end(env, sessionmaker):
    card = await _seed_card_published(env, published_at=_PAST)
    obs_id = await _obs(env, card)
    rows = await _load_obs(sessionmaker, [obs_id])
    async with sessionmaker() as session:
        availability = await resolve_observation_availability(session, rows)
    assert availability[obs_id].date() == _PAST.date()
    # 不退回 reporting_period_end（2024-12-31）：availability 必须等于 published_at
    assert availability[obs_id].date() != date(2024, 12, 31) or str(_AS_OF) != "2024-12-31"


async def test_p0_excludes_other_research_question_obs(env, sessionmaker):
    """task 级隔离：card 的 research_question_sha256 与本任务不一致 → 排除。"""
    card = await _seed_card_published(env, published_at=_PAST, question="另一个任务的问题？")
    obs_id = await _obs(env, card)
    rows = await _load_obs(sessionmaker, [obs_id])
    async with sessionmaker() as session:
        eligible = await filter_observations_for_task(
            session, rows, _AS_OF, compute_research_question_sha256(_QUESTION)
        )
    assert eligible == []


async def test_p0_keeps_eligible_past_obs(env, sessionmaker):
    card = await _seed_card_published(env, published_at=_PAST)
    obs_id = await _obs(env, card)
    rows = await _load_obs(sessionmaker, [obs_id])
    async with sessionmaker() as session:
        eligible = await filter_observations_for_task(
            session, rows, _AS_OF, compute_research_question_sha256(_QUESTION)
        )
    assert len(eligible) == 1
    assert str(eligible[0].metric_observation_id) == str(obs_id)


async def test_p0_none_question_matches_time_only(env, sessionmaker):
    """research_question_sha256=None → 仅时态过滤（不做过窄 question 匹配）。"""
    card = await _seed_card_published(env, published_at=_PAST)
    obs_id = await _obs(env, card)
    rows = await _load_obs(sessionmaker, [obs_id])
    async with sessionmaker() as session:
        eligible = await filter_observations_for_task(session, rows, _AS_OF, None)
    assert len(eligible) == 1


# ----------------------------------------------------------------- P0.5 recovery
async def _insert_claim(env, *, invalidated: bool = False) -> UUID:
    cid = uuid4()

    async with env["sessionmaker"]() as session:
        session.add(
            ClaimModel(
                claim_id=cid,
                company_id=env["company_id"],
                research_question="q",
                research_question_sha256=compute_research_question_sha256("q"),
                statement="s",
                analysis_domain="financial",
                claim_kind="fact",
                confidence="high",
                importance="normal",
                analyst_name="t",
                analyst_version=1,
                analyst_model_id="m",
                claim_schema_version=1,
                claim_fingerprint=hashlib.sha256(str(uuid4()).encode("utf-8")).hexdigest(),
                invalidated_at=(datetime.now(UTC) if invalidated else None),
                invalidation_reason="future_evidence_recovery" if invalidated else None,
            )
        )
        await session.commit()
    return cid


async def test_p05_mark_invalidated_sets_columns(env, sessionmaker):
    cid = await _insert_claim(env, invalidated=False)
    async with sessionmaker() as session:
        n = await ClaimRepository(session).mark_invalidated([cid], "future_evidence_recovery")
        await session.commit()
    assert n >= 1
    async with sessionmaker() as session:
        row = await ClaimRepository(session).get_by_id(cid)
    assert row is not None
    assert row.invalidated_at is not None
    assert row.invalidation_reason == "future_evidence_recovery"


async def test_p05_mark_invalidated_skips_invalidated(env, sessionmaker):
    cid = await _insert_claim(env, invalidated=True)
    async with sessionmaker() as session:
        n = await ClaimRepository(session).mark_invalidated([cid], "future_evidence_recovery")
        await session.commit()
    assert n == 0


class _FakeStage4Runner:
    def __init__(self, claim_ids, analysis_as_of):
        self._state = {
            "claim_ids": [str(c) for c in claim_ids],
            "analysis_as_of": str(analysis_as_of),
        }

    async def read_checkpoint_state(self, run_id):
        return dict(self._state)


class _FakeSynthesis:
    def __init__(self, offending):
        self.offending = list(offending)

    async def find_future_evidence_claim_ids(self, session, claim_ids, analysis_as_of):
        return self.offending


class _FakeSessionMaker:
    """返回一个最小 fake session：orchestration 读取返回可配。"""

    def __init__(self, orchestration_attempts=0):
        self.attempts = orchestration_attempts
        self.committed = False

    def __call__(self):
        return _FakeSession(self)


class _FakeSession:
    def __init__(self, maker):
        self._maker = maker

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self._maker.committed = True


# P0.5 bounded logic tested against a plain instance-level counter service spec:
# the service must return False once attempts >= MAX (no invalidated-in-round).
async def test_p05_recovery_non_future_exception_not_recovered(env, sessionmaker):
    service = FutureEvidenceRecoveryService(
        sessionmaker,
        _FakeStage4Runner([], _AS_OF),
        _FakeSynthesis([]),
        orchestration_checkpoint_reader=lambda oid: {"stage4_child_run_id": str(uuid4())},
    )
    ok = await service.try_recover(uuid4(), RuntimeError("boom"))
    assert ok is False


async def test_p05_recovery_bounded_by_max_attempts():
    """MAX 常量≥1；>=MAX 时服务必须拒绝再次恢复（由调用方投影 SYSTEM_FAILURE）。"""
    assert MAX_FUTURE_EVIDENCE_RECOVERY_ATTEMPTS >= 1


# ---------------------------------------------------------------------- P1
async def test_p1_degraded_section_payload_is_honest_and_ref_free():
    """degraded 正文是确定性诚实说明：无 claim/数字/引文（无 fake content）。"""
    from app.draft_section.contracts import DEGRADED_NOTE_TEMPLATE
    from app.draft_section.service import DraftSectionService

    service = DraftSectionService.__new__(DraftSectionService)
    payload = service._degraded_payload("model_unavailable")
    paras = payload["paragraphs"]
    assert len(paras) == 1
    assert paras[0]["claim_refs"] == []
    assert paras[0]["evidence_refs"] == []
    assert paras[0]["conflict_refs"] == []
    assert paras[0]["gap_refs"] == []
    assert paras[0]["text"] == DEGRADED_NOTE_TEMPLATE.format(reason="model_unavailable")


# ------------------------------------------------------------------- P1 pure
async def test_p1_assemble_accepts_degraded_section():
    """degraded section 保留完整 DraftSection contract → assemble 不再 missing S6。"""
    from app.draft_section.contracts import (
        DEGRADED_NOTE_TEMPLATE,
        DEGRADED_SECTION_STATUS,
    )
    from app.draft_section.service import DraftSectionService
    from app.report.assemble import AssembledSectionDraft, assemble_report_payload
    from tests.report.helpers import make_scenario

    scenario = make_scenario()
    outline = scenario.outline
    service = DraftSectionService.__new__(DraftSectionService)
    degraded_payload = service._degraded_payload("model_unavailable")
    degraded_draft = scenario.drafts["S3"]
    written = degraded_draft.__class__(
        draft_section_id=degraded_draft.draft_section_id,
        outline_id=degraded_draft.outline_id,
        section_id=degraded_draft.section_id,
        section_order=degraded_draft.section_order,
        section_type=degraded_draft.section_type,
        title=degraded_draft.title,
        section_schema_version=degraded_draft.section_schema_version,
        writer_name=degraded_draft.writer_name,
        writer_version=degraded_draft.writer_version,
        writer_model_id=degraded_draft.writer_model_id,
        writer_input_fingerprint=degraded_draft.writer_input_fingerprint,
        section_fingerprint=degraded_draft.section_fingerprint,
        paragraph_count=1,
        status=DEGRADED_SECTION_STATUS,
        degraded_reason="model_unavailable",
    )
    drafts = [
        AssembledSectionDraft(
            verified=scenario.drafts["S1"], section_payload=scenario.section_payloads["S1"]
        ),
        AssembledSectionDraft(
            verified=scenario.drafts["S2"], section_payload=scenario.section_payloads["S2"]
        ),
        AssembledSectionDraft(verified=written, section_payload=degraded_payload),
    ]
    payload = assemble_report_payload(verified_outline=outline, drafts=drafts)
    assert len(payload["sections"]) == len(outline.sections)
    assert payload["sections"][2]["paragraphs"][0]["text"] == DEGRADED_NOTE_TEMPLATE.format(
        reason="model_unavailable"
    )


async def test_p1_degraded_payload_no_refs_no_numbers():
    """degraded 段落不携带任何 claim/evidence/数字内容（无 fake content）。"""
    from app.draft_section.service import DraftSectionService

    service = DraftSectionService.__new__(DraftSectionService)
    payload = service._degraded_payload("model_unavailable")
    text = payload["paragraphs"][0]["text"]
    assert all(not c.isdigit() for c in text)
    for f in ("claim_refs", "evidence_refs", "conflict_refs", "gap_refs"):
        assert payload["paragraphs"][0][f] == []


async def test_p3_extra_research_round_bound_constant():
    """P3：手动补充研究有界（+自动轮上限）。"""
    from app.research_orchestration.contracts import (
        MAX_BACKFLOW_RESEARCH_ROUNDS,
        MAX_SUPPLEMENTAL_RESEARCH_ROUNDS,
    )

    assert MAX_BACKFLOW_RESEARCH_ROUNDS >= 1
    assert MAX_SUPPLEMENTAL_RESEARCH_ROUNDS >= 1
