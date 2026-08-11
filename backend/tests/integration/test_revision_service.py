"""RevisionService integration tests (stage 5E.2A, spec G-M/N/U revision 清单).

真实 PostgreSQL + Fake Writer + Fake Auditor + Fake Revision Writer，全程
**零真实 DeepSeek**（Fake 模型都是确定性返回）。复用 `test_report_audit_service`
的最小真实链（Evidence → ClaimService → Synthesis → Outline → Fake Writer →
Report → Check → Audit）与 decision factories 构造 trigger artifact。

覆盖（spec U）：
- trigger 三选一：audit_rewrite / human_rewrite / deterministic_check；
- target section 校验（spec H）：source section ∉ trigger targets → 0 write；
- feedback 派生（spec I）：audit（issue_type/severity/paragraph_index/message）、
  check（code + paragraph_index）、human（underlying issues + 末尾 human_comment）；
- 修订正文复用 5B 校验（spec J）：不添加新 Claim/Evidence、text 与 source 不同；
- replay（同输入 → 0 model calls → 同一行）；concurrency → 1 row；
- round-2 recursion：source 为 v1 修订输出，沿 revision link 递归；
- `verify_revision_integrity`（spec M）：happy path / 修订正文 tamper /
  revision_fingerprint tamper → RevisionIntegrityError（不自动 repair）；
- `verify_revised_draft_section`（spec N 装配修订输出的新 Report 前置）。
"""

import asyncio
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.audit.contracts import ReportAuditRequest
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.draft_section.service import DraftSectionService
from app.report.check_service import ReportCheckService
from app.report.contracts import CHECK_STATUS_FAIL, ReportAssemblyDraft
from app.report.service import ReportService
from app.review.contracts import ACTION_TYPE_FINALIZE
from app.review.service import ReviewActionService
from app.revision.contracts import (
    DRAFT_SECTION_REVISION_SCHEMA_VERSION,
    FEEDBACK_CODE_HUMAN_COMMENT,
    REVISION_WRITER_NAME,
    REVISION_WRITER_VERSION,
    TRIGGER_TYPE_AUDIT_REWRITE,
    TRIGGER_TYPE_DETERMINISTIC_CHECK,
    TRIGGER_TYPE_HUMAN_REWRITE,
    RevisionRequest,
    RevisionTrigger,
)
from app.revision.errors import (
    RevisionIntegrityError,
    RevisionNotFound,
    RevisionTargetSectionInvalid,
    RevisionTriggerInvalid,
)
from app.revision.service import RevisionService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel
from tests.integration.test_draft_section_service import _create_outline, _two_theme_models
from tests.integration.test_report_audit_service import (
    _audit_service,
    _build_minimal_chain,
    human_review_decision,
    wording_overclaim_decision,
)
from tests.integration.test_report_check_integrity import (
    _draft_mixed_sections,
    _risks_gap_omitted_decision,
)
from tests.integration.test_report_service import _seed_research_task
from tests.integration.test_stage4_workflow import _cleanup
from tests.integration.test_valuation_claim_service import _seed_company
from tests.revision.fakes import FakeRevisionWriterModel, revision_decision_for

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


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


async def _cleanup_with_revisions(sessionmaker) -> None:
    """先删 revision 层（FK 引用 draft_sections + check/action/decision），再走公共 _cleanup。"""
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM draft_section_revisions"))
        await session.execute(text("DELETE FROM human_review_decisions"))
        await session.execute(text("DELETE FROM human_review_requests"))
        await session.execute(text("DELETE FROM report_review_actions"))
        await session.execute(text("DELETE FROM review_issues"))
        await session.execute(text("DELETE FROM report_audits"))
        await session.execute(text("DELETE FROM report_check_results"))
        await session.execute(text("DELETE FROM reports"))
        await session.commit()
    await _cleanup(sessionmaker)


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker, monkeypatch) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup_with_revisions(sessionmaker)
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
    await _cleanup_with_revisions(sessionmaker)


# ---------------------------------------------------------------- helpers


async def _draft_id_for_section(sessionmaker, report_id: UUID, section_id: str) -> UUID:
    """报告所属 outline 下、指定 section_id 的 draft_section_id。"""
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT d.draft_section_id FROM draft_sections d "
                        "JOIN reports r ON r.outline_id = d.outline_id "
                        "WHERE r.report_id = :rid AND d.section_id = :sid"
                    ).bindparams(rid=report_id, sid=section_id)
                )
            )
            .mappings()
            .first()
        )
    assert row is not None, f"draft section {section_id!r} not found for report"
    return UUID(str(row["draft_section_id"]))


def _revision_service(env, fake_writer, check_service, review_service) -> RevisionService:
    return RevisionService(
        env["sessionmaker"],
        model=fake_writer,
        draft_section_service=DraftSectionService(env["sessionmaker"], FakeDraftSectionModel()),
        check_service=check_service,
        review_action_service=review_service,
    )


def _fake_writer() -> FakeRevisionWriterModel:
    return FakeRevisionWriterModel(decision_factory=revision_decision_for)


async def _revision_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM draft_section_revisions"))
            ).scalar_one()
        )


async def _audit_rewrite_setup(env, monkeypatch, connection_uri):
    """最小真实链 → rewrite audit → rewrite action → (check, review, action, target, source)。"""
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake_audit = FakeAuditModel(decision_factory=wording_overclaim_decision)
    audit_service = _audit_service(env, fake_audit, check_service)
    audit = await audit_service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )
    review_service = ReviewActionService(env["sessionmaker"], audit_service)
    action = await review_service.create_or_get_action(audit.audit_id)
    assert action.action_type == "rewrite"
    target_section_id = action.action_payload["target_section_ids"][0]
    source_draft_id = await _draft_id_for_section(env["sessionmaker"], report_id, target_section_id)
    return check_service, review_service, action, target_section_id, source_draft_id


async def _human_rewrite_setup(env, monkeypatch, connection_uri):
    """human_review audit → action → request → 人工 rewrite decision。"""
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake_audit = FakeAuditModel(decision_factory=human_review_decision)
    audit_service = _audit_service(env, fake_audit, check_service)
    audit = await audit_service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )
    review_service = ReviewActionService(env["sessionmaker"], audit_service)
    action = await review_service.create_or_get_action(audit.audit_id)
    assert action.action_type == "human_review"
    request = await review_service.create_or_get_human_request(action.review_action_id)
    decision = await review_service.resolve_human_request(
        request.human_request_id, "rewrite", comment=" 请重新表述营收增长依据 "
    )
    target_section_id = action.action_payload["target_section_ids"][0]
    source_draft_id = await _draft_id_for_section(env["sessionmaker"], report_id, target_section_id)
    return check_service, review_service, decision, source_draft_id


# ---------------------------------------------------------------- audit_rewrite（spec G/H/I/J）


async def test_audit_rewrite_creates_revision(env, monkeypatch, connection_uri) -> None:
    check_service, review_service, action, _, source_draft_id = await _audit_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    fake_writer = _fake_writer()
    service = _revision_service(env, fake_writer, check_service, review_service)

    result = await service.revise_section(
        RevisionRequest(
            source_draft_section_id=source_draft_id,
            trigger=RevisionTrigger(review_action_id=action.review_action_id),
            revision_round=1,
        )
    )
    assert result.trigger_type == TRIGGER_TYPE_AUDIT_REWRITE
    assert result.revision_round == 1
    assert result.revision_schema_version == DRAFT_SECTION_REVISION_SCHEMA_VERSION
    assert len(result.revision_fingerprint) == 64
    assert result.revised_draft_section_id != source_draft_id
    assert not result.replayed
    assert await _revision_count(env["sessionmaker"]) == 1
    assert len(fake_writer.calls) == 1

    # 修订正文进入 draft_sections：writer 身份 = evidence_bound_section_rewriter v1。
    async with env["sessionmaker"]() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT writer_name, writer_version, section_fingerprint "
                        "FROM draft_sections WHERE draft_section_id = :did"
                    ).bindparams(did=result.revised_draft_section_id)
                )
            )
            .mappings()
            .one()
        )
    assert row["writer_name"] == REVISION_WRITER_NAME
    assert row["writer_version"] == REVISION_WRITER_VERSION

    # 修订正文与 source 不同（revised text 带修订标记）→ section_fingerprint 不同。
    async with env["sessionmaker"]() as session:
        source_row = (
            await session.execute(
                text(
                    "SELECT section_fingerprint FROM draft_sections WHERE draft_section_id = :did"
                ).bindparams(did=source_draft_id)
            )
        ).scalar_one()
    assert row["section_fingerprint"] != source_row

    # read-side 完整重建验证（spec M）。
    verified = await service.verify_revision_integrity(result.revision_id)
    assert verified.revision_id == result.revision_id
    assert verified.trigger_type == TRIGGER_TYPE_AUDIT_REWRITE
    assert verified.verified_revised.draft_section_id == result.revised_draft_section_id
    assert verified.verified_revised.writer_name == REVISION_WRITER_NAME

    # ReportService 装配修订输出的新 Report 前置（spec N）。
    revised_verified = await service.verify_revised_draft_section(result.revised_draft_section_id)
    assert revised_verified.draft_section_id == result.revised_draft_section_id
    assert revised_verified.writer_version == REVISION_WRITER_VERSION


async def test_audit_rewrite_feedback_carries_issue_data(env, monkeypatch, connection_uri) -> None:
    check_service, review_service, action, _, source_draft_id = await _audit_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    fake_writer = _fake_writer()
    service = _revision_service(env, fake_writer, check_service, review_service)
    await service.revise_section(
        RevisionRequest(
            source_draft_section_id=source_draft_id,
            trigger=RevisionTrigger(review_action_id=action.review_action_id),
            revision_round=1,
        )
    )

    pack = fake_writer.calls[0]
    assert pack.input_pack.section_id == (await _section_id(source_draft_id, env))
    assert pack.original_paragraphs  # 原正文段落完整传入（修订的"原文"）
    feedback = pack.revision_feedback
    assert feedback and all(item.trigger_type == TRIGGER_TYPE_AUDIT_REWRITE for item in feedback)
    # spec I：只投影 issue_type/severity/paragraph_index/message，不含 issue id。
    # 顺序遵循 spec R 的 deterministic ordinal（issue_type 字母序 → evidence_mismatch 在前）。
    codes = {item.code for item in feedback}
    assert codes == {"wording_overclaim", "evidence_mismatch"}
    overclaim = next(item for item in feedback if item.code == "wording_overclaim")
    assert overclaim.severity == "high"
    assert overclaim.paragraph_index == 0
    assert overclaim.message  # 非空、可读，不含 issue id


async def test_audit_rewrite_target_section_validation_rejects(
    env, monkeypatch, connection_uri
) -> None:
    """spec H：source section ∉ action.target_section_ids → 0 write。"""
    check_service, review_service, action, target_section_id, _ = await _audit_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    # 取报告中**不在 targets 内**的另一个 section 作为 source。
    async with env["sessionmaker"]() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT d.section_id FROM draft_sections d "
                        "JOIN reports r ON r.outline_id = d.outline_id "
                        "WHERE r.report_id = :rid AND d.section_id <> :sid"
                    ).bindparams(
                        rid=UUID(action.action_payload["source_report_id"]), sid=target_section_id
                    )
                )
            )
            .mappings()
            .all()
        )
    assert rows
    other_section_id = str(rows[0]["section_id"])
    other_draft_id = await _draft_id_for_section(
        env["sessionmaker"], UUID(action.action_payload["source_report_id"]), other_section_id
    )

    fake_writer = _fake_writer()
    service = _revision_service(env, fake_writer, check_service, review_service)
    with pytest.raises(RevisionTargetSectionInvalid):
        await service.revise_section(
            RevisionRequest(
                source_draft_section_id=other_draft_id,
                trigger=RevisionTrigger(review_action_id=action.review_action_id),
                revision_round=1,
            )
        )
    assert await _revision_count(env["sessionmaker"]) == 0
    assert len(fake_writer.calls) == 0


# ---------------------------------------------------------------- human_rewrite（spec H/I）


async def test_human_rewrite_trigger_and_comment_feedback(env, monkeypatch, connection_uri) -> None:
    check_service, review_service, decision, source_draft_id = await _human_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    fake_writer = _fake_writer()
    service = _revision_service(env, fake_writer, check_service, review_service)

    result = await service.revise_section(
        RevisionRequest(
            source_draft_section_id=source_draft_id,
            trigger=RevisionTrigger(human_decision_id=decision.human_decision_id),
            revision_round=1,
        )
    )
    assert result.trigger_type == TRIGGER_TYPE_HUMAN_REWRITE
    assert not result.replayed
    assert await _revision_count(env["sessionmaker"]) == 1

    # 触发 FK：revision link 指向 human_decision_id。
    async with env["sessionmaker"]() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT human_decision_id, review_action_id, check_result_id "
                        "FROM draft_section_revisions WHERE revision_id = :rid"
                    ).bindparams(rid=result.revision_id)
                )
            )
            .mappings()
            .one()
        )
    assert row["human_decision_id"] == decision.human_decision_id
    assert row["review_action_id"] is None
    assert row["check_result_id"] is None

    # 反馈：underlying issues + 末尾一条 human_comment（spec I）。
    feedback = fake_writer.calls[0].revision_feedback
    assert feedback
    assert feedback[-1].code == FEEDBACK_CODE_HUMAN_COMMENT
    assert feedback[-1].message == "请重新表述营收增长依据"  # comment 已 normalize（trim）
    assert all(
        item.code != FEEDBACK_CODE_HUMAN_COMMENT for item in feedback[:-1]
    )  # 其余是 underlying issues


# ---------------------------------------------------------------- deterministic_check（spec G/H/I）


async def test_deterministic_check_trigger(env, monkeypatch, connection_uri) -> None:
    """真实 status=fail 的 CheckResult（risks_and_gaps 遗漏 gap）→ check 触发修订。"""
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    s3_fake = FakeDraftSectionModel(decision_factory=_risks_gap_omitted_decision)
    draft_ids = await _draft_mixed_sections(env, outline_id, s3_fake)
    report_service = ReportService(
        env["sessionmaker"], DraftSectionService(env["sessionmaker"], s3_fake)
    )
    report = await report_service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )
    check_service = ReportCheckService(env["sessionmaker"], report_service)
    check = await check_service.run_report_checks(report.report_id)
    assert check.status == CHECK_STATUS_FAIL

    source_draft_id = draft_ids["S3"]
    fake_writer = _fake_writer()
    review_service = ReviewActionService(
        env["sessionmaker"],
        _audit_service(env, FakeAuditModel(decision_factory=pass_decision), check_service),
    )
    service = _revision_service(env, fake_writer, check_service, review_service)

    result = await service.revise_section(
        RevisionRequest(
            source_draft_section_id=source_draft_id,
            trigger=RevisionTrigger(check_result_id=check.check_result_id),
            revision_round=1,
        )
    )
    assert result.trigger_type == TRIGGER_TYPE_DETERMINISTIC_CHECK
    assert await _revision_count(env["sessionmaker"]) == 1

    # feedback：只给 finding code + paragraph_index（无 message，spec I）。
    feedback = fake_writer.calls[0].revision_feedback
    assert feedback
    item = feedback[0]
    assert item.trigger_type == TRIGGER_TYPE_DETERMINISTIC_CHECK
    assert item.code == "conflict_gap_preservation"
    assert item.paragraph_index is None
    assert item.message is None


# ---------------------------------------------------------------- replay / concurrency / round-2


async def test_replay_same_input_zero_model_calls(env, monkeypatch, connection_uri) -> None:
    check_service, review_service, action, _, source_draft_id = await _audit_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    fake_writer = _fake_writer()
    service = _revision_service(env, fake_writer, check_service, review_service)
    request = RevisionRequest(
        source_draft_section_id=source_draft_id,
        trigger=RevisionTrigger(review_action_id=action.review_action_id),
        revision_round=1,
    )

    first = await service.revise_section(request)
    second = await service.revise_section(request)
    assert second.revision_id == first.revision_id
    assert second.revised_draft_section_id == first.revised_draft_section_id
    assert second.replayed
    assert await _revision_count(env["sessionmaker"]) == 1
    assert len(fake_writer.calls) == 1  # 0 额外 model calls


async def test_concurrency_single_row(env, monkeypatch, connection_uri) -> None:
    check_service, review_service, action, _, source_draft_id = await _audit_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    fake_writer = _fake_writer()
    service = _revision_service(env, fake_writer, check_service, review_service)
    request = RevisionRequest(
        source_draft_section_id=source_draft_id,
        trigger=RevisionTrigger(review_action_id=action.review_action_id),
        revision_round=1,
    )

    results = await asyncio.gather(service.revise_section(request), service.revise_section(request))
    assert len({result.revision_id for result in results}) == 1
    assert await _revision_count(env["sessionmaker"]) == 1


async def test_round2_recursion_on_v1_revised_source(env, monkeypatch, connection_uri) -> None:
    """spec G：source 为 v1 修订输出 → 沿 revision link 递归回源；round 2 新行。"""
    check_service, review_service, action, _, source_draft_id = await _audit_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    fake_writer = _fake_writer()
    service = _revision_service(env, fake_writer, check_service, review_service)
    round1 = await service.revise_section(
        RevisionRequest(
            source_draft_section_id=source_draft_id,
            trigger=RevisionTrigger(review_action_id=action.review_action_id),
            revision_round=1,
        )
    )
    round2 = await service.revise_section(
        RevisionRequest(
            source_draft_section_id=round1.revised_draft_section_id,
            trigger=RevisionTrigger(review_action_id=action.review_action_id),
            revision_round=2,
        )
    )
    assert round2.revision_round == 2
    assert round2.revised_draft_section_id != round1.revised_draft_section_id
    assert not round2.replayed
    assert await _revision_count(env["sessionmaker"]) == 2
    assert len(fake_writer.calls) == 2

    # round-2 修订可被完整重建验证（递归 source 一致 → 同一 section scope）。
    verified = await service.verify_revision_integrity(round2.revision_id)
    assert verified.source_draft_section_id == round1.revised_draft_section_id
    assert verified.source.section_id == (await _section_id(source_draft_id, env))


# ---------------------------------------------------------------- verify integrity（spec M）


async def test_verify_integrity_rejects_revised_draft_tamper(
    env, monkeypatch, connection_uri
) -> None:
    check_service, review_service, action, _, source_draft_id = await _audit_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    service = _revision_service(env, _fake_writer(), check_service, review_service)
    result = await service.revise_section(
        RevisionRequest(
            source_draft_section_id=source_draft_id,
            trigger=RevisionTrigger(review_action_id=action.review_action_id),
            revision_round=1,
        )
    )
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_sections SET section_fingerprint = :fp WHERE draft_section_id = :did"
            ).bindparams(fp="0" * 64, did=result.revised_draft_section_id)
        )
        await session.commit()

    with pytest.raises(RevisionIntegrityError):
        await service.verify_revision_integrity(result.revision_id)


async def test_verify_integrity_rejects_revision_fingerprint_tamper(
    env, monkeypatch, connection_uri
) -> None:
    check_service, review_service, action, _, source_draft_id = await _audit_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    service = _revision_service(env, _fake_writer(), check_service, review_service)
    result = await service.revise_section(
        RevisionRequest(
            source_draft_section_id=source_draft_id,
            trigger=RevisionTrigger(review_action_id=action.review_action_id),
            revision_round=1,
        )
    )
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_section_revisions SET revision_fingerprint = :fp "
                "WHERE revision_id = :rid"
            ).bindparams(fp="0" * 64, rid=result.revision_id)
        )
        await session.commit()

    with pytest.raises(RevisionIntegrityError):
        await service.verify_revision_integrity(result.revision_id)


async def test_verify_integrity_not_found(env, monkeypatch, connection_uri) -> None:
    check_service, review_service, _, _, _ = await _audit_rewrite_setup(
        env, monkeypatch, connection_uri
    )
    service = _revision_service(env, _fake_writer(), check_service, review_service)
    with pytest.raises(RevisionNotFound):
        await service.verify_revision_integrity(uuid4())


# ---------------------------------------------------------------- trigger artifact 校验（spec G）


async def test_trigger_rejects_non_rewrite_action(env, monkeypatch, connection_uri) -> None:
    """finalize action 不能作为 audit_rewrite trigger（action_type != rewrite）。"""
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake_audit = FakeAuditModel(decision_factory=pass_decision)
    audit_service = _audit_service(env, fake_audit, check_service)
    audit = await audit_service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )
    review_service = ReviewActionService(env["sessionmaker"], audit_service)
    action = await review_service.create_or_get_action(audit.audit_id)
    assert action.action_type == ACTION_TYPE_FINALIZE

    async with env["sessionmaker"]() as session:
        first = (
            (
                await session.execute(
                    text(
                        "SELECT d.draft_section_id FROM draft_sections d "
                        "JOIN reports r ON r.outline_id = d.outline_id "
                        "WHERE r.report_id = :rid LIMIT 1"
                    ).bindparams(rid=report_id)
                )
            )
            .mappings()
            .first()
        )
    service = _revision_service(env, _fake_writer(), check_service, review_service)
    with pytest.raises(RevisionTriggerInvalid):
        await service.revise_section(
            RevisionRequest(
                source_draft_section_id=UUID(str(first["draft_section_id"])),
                trigger=RevisionTrigger(review_action_id=action.review_action_id),
                revision_round=1,
            )
        )
    assert await _revision_count(env["sessionmaker"]) == 0


async def test_trigger_union_requires_exactly_one(env, monkeypatch, connection_uri) -> None:
    from app.revision.errors import RevisionInputError

    with pytest.raises(RevisionInputError):
        RevisionTrigger()
    with pytest.raises(RevisionInputError):
        RevisionTrigger(check_result_id=uuid4(), review_action_id=uuid4())


# ---------------------------------------------------------------- 小工具


async def _section_id(draft_section_id: UUID, env) -> str:
    async with env["sessionmaker"]() as session:
        return str(
            (
                await session.execute(
                    text(
                        "SELECT section_id FROM draft_sections WHERE draft_section_id = :did"
                    ).bindparams(did=draft_section_id)
                )
            ).scalar_one()
        )
