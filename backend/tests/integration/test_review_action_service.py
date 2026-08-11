"""ReviewActionService integration tests (stage 5E.1, spec Q review 清单)。

真实 PostgreSQL + Fake Writer + Fake Auditor，全程**零真实 DeepSeek**（Fake
模型都是确定性返回）。复用 `test_report_audit_service` 的最小真实链
（Evidence → ClaimService → Synthesis → Outline → Fake Writer → Report →
Check）与 decision factories 构造 VerifiedReportAudit 输入。

覆盖（spec Q）：
- ReviewAction：pass→finalize、rewrite→rewrite、research→research、
  human_review→human_review；target section dedupe/order；提升 route 保留全部
  issue ids；research_need_codes；create/replay；concurrency → 1 行；
  Audit tamper / Action tamper → verify 拒绝（不 repair）；
- HumanRequest：非 human_review action 拒绝；payload 只存 IDs + issue summaries
  （不复制 evidence / prompt）；create/replay；tamper 拒绝；
- HumanDecision：approve 后 Audit 保持 fail+human_review（spec L，不修改上游）；
  相同 decision/comment → replay；不同 → HumanReviewAlreadyResolved（不覆盖历史）；
  comment normalize（空白→None / 超长拒绝）；非法 decision 拒绝；tamper 拒绝。
"""

import asyncio
import json
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.audit.contracts import AuditDecision, AuditIssueCandidate, ReportAuditRequest
from app.audit.errors import ReportAuditIntegrityError
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.draft_section.service import DraftSectionService
from app.report.check_service import ReportCheckService
from app.report.contracts import CHECK_STATUS_FAIL, ReportAssemblyDraft
from app.report.service import ReportService
from app.review.errors import (
    HumanReviewAlreadyResolved,
    HumanReviewDecisionIntegrityError,
    HumanReviewRequestIntegrityError,
    ReviewActionCheckNotPass,
    ReviewActionIntegrityError,
    ReviewInputError,
    ReviewRequestNotHumanReview,
)
from app.review.service import ReviewActionService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.audit.fakes import FakeAuditModel
from tests.draft_section.fakes import FakeDraftSectionModel
from tests.integration.test_draft_section_service import _create_outline, _two_theme_models
from tests.integration.test_report_audit_service import (
    _audit_service,
    _build_minimal_chain,
    human_review_decision,
    pass_decision,
    research_decision,
    wording_overclaim_decision,
)
from tests.integration.test_report_check_integrity import (
    _draft_mixed_sections,
    _risks_gap_omitted_decision,
)
from tests.integration.test_report_service import _seed_research_task
from tests.integration.test_stage4_workflow import _cleanup
from tests.integration.test_valuation_claim_service import _seed_company

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


async def _cleanup_with_review(sessionmaker) -> None:
    """先删 5E.1 review 层（FK：decisions → requests → actions → audits）再走公共 _cleanup。"""
    async with sessionmaker() as session:
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
    await _cleanup_with_review(sessionmaker)
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
    await _cleanup_with_review(sessionmaker)


# ---------------------------------------------------------------- helpers


def _review_service(env: dict, audit_service) -> ReviewActionService:
    return ReviewActionService(env["sessionmaker"], audit_service)


async def _create_audit(env, monkeypatch, connection_uri, decision_factory):
    """最小真实链 → Fake Auditor audit → (audit_service, audit)。"""
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=decision_factory)
    audit_service = _audit_service(env, fake, check_service)
    audit = await audit_service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )
    return audit_service, audit


async def _create_human_review_flow(env, monkeypatch, connection_uri):
    """human_review audit → action → request，返回 (audit_service, audit, action, request)。"""
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, human_review_decision
    )
    service = _review_service(env, audit_service)
    action = await service.create_or_get_action(audit.audit_id)
    assert action.action_type == "human_review"
    request = await service.create_or_get_human_request(action.review_action_id)
    return audit_service, audit, action, request


async def _action_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM report_review_actions"))).scalar_one()
        )


async def _request_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM human_review_requests"))).scalar_one()
        )


async def _decision_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM human_review_decisions"))
            ).scalar_one()
        )


async def _issue_types(sessionmaker, audit_id) -> set[str]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text("SELECT issue_type FROM review_issues WHERE audit_id = :aid").bindparams(
                    aid=audit_id
                )
            )
        ).all()
    return {row[0] for row in rows}


def _mixed_research_decision(pack):
    """wording_overclaim（P1/S1，rewrite 类）+ insufficient_evidence（P2/S2，research 类）
    → route=research（priority 提升）。"""
    section_0 = pack.sections[0].section_ref
    p1 = next(paragraph for paragraph in pack.paragraphs if paragraph.section_ref == section_0)
    p2 = next(paragraph for paragraph in pack.paragraphs if paragraph.section_ref != section_0)
    return AuditDecision(
        reviewed_paragraph_refs=[paragraph.paragraph_ref for paragraph in pack.paragraphs],
        issues=[
            AuditIssueCandidate(
                issue_type="wording_overclaim",
                severity="normal",
                section_ref=p1.section_ref,
                paragraph_ref=p1.paragraph_ref,
                claim_refs=list(p1.claim_refs),
                evidence_refs=list(p1.evidence_refs),
                message="文字表述超出证据支持范围",
            ),
            AuditIssueCandidate(
                issue_type="insufficient_evidence",
                severity="normal",
                section_ref=p2.section_ref,
                paragraph_ref=p2.paragraph_ref,
                claim_refs=list(p2.claim_refs),
                evidence_refs=list(p2.evidence_refs),
                message="证据不足以支撑结论",
            ),
        ],
    )


# ---------------------------------------------------------------- route → action_type（spec F）


async def test_action_pass_routes_to_finalize(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(env, monkeypatch, connection_uri, pass_decision)
    assert audit.audit_status == "pass"
    service = _review_service(env, audit_service)

    result = await service.create_or_get_action(audit.audit_id)
    assert result.action_type == "finalize"
    assert result.action_payload == {
        "source_report_id": str(audit.report_id),
        "source_audit_id": str(audit.audit_id),
    }
    assert len(result.action_fingerprint) == 64
    assert not result.replayed
    assert await _action_count(env["sessionmaker"]) == 1


async def test_action_finalize_rejects_check_fail_audit_pass(
    env, monkeypatch, connection_uri
) -> None:
    """Gate 0：deterministic Check=fail + Audit 人为/fixture=pass → 拒绝 finalize。

    用 risks_and_gaps 遗漏 outline 要求的 evidence_gap_index 构造**真实** status=fail
    的 CheckResult（draft 期合法，非 SQL tamper）→ Fake Auditor 返回 0 issues →
    Audit=pass/route=pass。`create_or_get_action` 必须因 deterministic Check failure
    reject finalize（0 ReviewAction write）——Agent Audit 不得覆盖 Check 失败。
    """
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
    # check fail 可被 read-side verify 接受（合法 fail），Audit 也能在其上创建。
    await check_service.verify_check_result_integrity(check.check_result_id)

    fake = FakeAuditModel(decision_factory=pass_decision)
    audit_service = _audit_service(env, fake, check_service)
    audit = await audit_service.create_or_get_audit(
        ReportAuditRequest(report_id=report.report_id, check_result_id=check.check_result_id)
    )
    assert audit.audit_status == "pass"
    assert audit.recommended_route == "pass"

    service = _review_service(env, audit_service)
    with pytest.raises(ReviewActionCheckNotPass):
        await service.create_or_get_action(audit.audit_id)
    assert await _action_count(env["sessionmaker"]) == 0


async def test_action_rewrite_routes_to_rewrite(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, wording_overclaim_decision
    )
    assert audit.recommended_route == "rewrite"
    service = _review_service(env, audit_service)

    result = await service.create_or_get_action(audit.audit_id)
    assert result.action_type == "rewrite"
    assert set(result.action_payload) == {
        "source_report_id",
        "source_audit_id",
        "target_section_ids",
        "review_issue_ids",
    }
    assert result.action_payload["review_issue_ids"]
    assert result.action_payload["target_section_ids"]


async def test_action_research_routes_to_research(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(env, monkeypatch, connection_uri, research_decision)
    assert audit.recommended_route == "research"
    service = _review_service(env, audit_service)

    result = await service.create_or_get_action(audit.audit_id)
    assert result.action_type == "research"
    assert result.action_payload["research_need_codes"] == ["missing_support"]
    assert result.action_payload["related_claim_ids"]
    assert result.action_payload["related_evidence_card_ids"]


async def test_action_human_review_routes_to_human_review(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, human_review_decision
    )
    assert audit.recommended_route == "human_review"
    service = _review_service(env, audit_service)

    result = await service.create_or_get_action(audit.audit_id)
    assert result.action_type == "human_review"


async def test_action_elevated_research_preserves_all_issue_ids(
    env, monkeypatch, connection_uri
) -> None:
    """提升 route：research 类 + rewrite 类 issues → 全部 ids / sections 保留。"""
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, _mixed_research_decision
    )
    assert audit.recommended_route == "research"
    service = _review_service(env, audit_service)

    result = await service.create_or_get_action(audit.audit_id)
    assert result.action_type == "research"
    # 不丢 rewrite 类 issue（wording_overclaim）→ review_issue_ids 包含全部 2 条。
    assert await _issue_types(env["sessionmaker"], audit.audit_id) == {
        "wording_overclaim",
        "insufficient_evidence",
    }
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT review_issue_id, section_id FROM review_issues WHERE audit_id = :aid"
                ).bindparams(aid=audit.audit_id)
            )
        ).all()
    expected_ids = sorted(str(row[0]) for row in rows)
    expected_sections = sorted({row[1] for row in rows})
    assert result.action_payload["review_issue_ids"] == expected_ids
    # 两个不同 section 都在 target_section_ids（rewrite 类 issue 的 section 不丢失）。
    assert len(expected_sections) == 2
    assert result.action_payload["target_section_ids"] == expected_sections


# ---------------------------------------------------------------- create / replay / concurrency


async def test_action_replay_second_call_reuses_row(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, wording_overclaim_decision
    )
    service = _review_service(env, audit_service)

    first = await service.create_or_get_action(audit.audit_id)
    second = await service.create_or_get_action(audit.audit_id)
    assert second.review_action_id == first.review_action_id
    assert second.replayed
    assert await _action_count(env["sessionmaker"]) == 1


async def test_action_concurrency_single_row(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, wording_overclaim_decision
    )
    service = _review_service(env, audit_service)

    results = await asyncio.gather(
        service.create_or_get_action(audit.audit_id),
        service.create_or_get_action(audit.audit_id),
    )
    assert len({result.review_action_id for result in results}) == 1
    assert await _action_count(env["sessionmaker"]) == 1


# ---------------------------------------------------------------- verify integrity（spec N）


async def test_action_verify_integrity_happy_path(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, wording_overclaim_decision
    )
    service = _review_service(env, audit_service)
    result = await service.create_or_get_action(audit.audit_id)

    verified = await service.verify_review_action_integrity(result.review_action_id)
    assert verified.review_action_id == result.review_action_id
    assert verified.action_type == "rewrite"
    assert verified.audit_id == audit.audit_id
    assert verified.verified_audit.audit_id == audit.audit_id


async def test_action_verify_rejects_audit_tamper(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, wording_overclaim_decision
    )
    service = _review_service(env, audit_service)
    result = await service.create_or_get_action(audit.audit_id)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE report_audits SET audit_fingerprint = :fp WHERE audit_id = :id"
            ).bindparams(fp="0" * 64, id=audit.audit_id)
        )
        await session.commit()

    # 上游 Audit 被篡改 → verify_audit_integrity 先行拒绝（不 repair）。
    with pytest.raises(ReportAuditIntegrityError):
        await service.verify_review_action_integrity(result.review_action_id)


async def test_action_verify_rejects_action_type_tamper(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, wording_overclaim_decision
    )
    service = _review_service(env, audit_service)
    result = await service.create_or_get_action(audit.audit_id)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE report_review_actions SET action_type = 'research' "
                "WHERE review_action_id = :id"
            ).bindparams(id=result.review_action_id)
        )
        await session.commit()

    with pytest.raises(ReviewActionIntegrityError):
        await service.verify_review_action_integrity(result.review_action_id)


async def test_action_verify_rejects_action_fingerprint_tamper(
    env, monkeypatch, connection_uri
) -> None:
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, wording_overclaim_decision
    )
    service = _review_service(env, audit_service)
    result = await service.create_or_get_action(audit.audit_id)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE report_review_actions SET action_fingerprint = :fp "
                "WHERE review_action_id = :id"
            ).bindparams(fp="0" * 64, id=result.review_action_id)
        )
        await session.commit()

    with pytest.raises(ReviewActionIntegrityError):
        await service.verify_review_action_integrity(result.review_action_id)


# ---------------------------------------------------------------- human review request（spec J）


async def test_human_request_requires_human_review_action(env, monkeypatch, connection_uri) -> None:
    audit_service, audit = await _create_audit(
        env, monkeypatch, connection_uri, wording_overclaim_decision
    )
    service = _review_service(env, audit_service)
    action = await service.create_or_get_action(audit.audit_id)
    assert action.action_type == "rewrite"

    with pytest.raises(ReviewRequestNotHumanReview):
        await service.create_or_get_human_request(action.review_action_id)
    assert await _request_count(env["sessionmaker"]) == 0


async def test_human_request_create_payload(env, monkeypatch, connection_uri) -> None:
    audit_service, audit, action, _ = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    async with env["sessionmaker"]() as session:
        issue = (
            await session.execute(
                text(
                    "SELECT issue_type, severity, section_id, paragraph_index "
                    "FROM review_issues WHERE audit_id = :aid"
                ).bindparams(aid=audit.audit_id)
            )
        ).one()
    async with env["sessionmaker"]() as session:
        row = (
            await session.execute(
                text(
                    "SELECT request_payload FROM human_review_requests "
                    "WHERE review_action_id = :aid"
                ).bindparams(aid=action.review_action_id)
            )
        ).scalar_one()
    payload = dict(row)

    assert payload["report_id"] == str(audit.report_id)
    assert payload["audit_id"] == str(audit.audit_id)
    assert payload["review_issue_ids"]
    # section_ids 是真实 outline section UUID（非 alias S1）。
    assert payload["section_ids"] == sorted({issue[2]})
    assert payload["issue_summaries"] == [
        {
            "issue_type": issue[0],
            "severity": issue[1],
            "section_id": issue[2],
            "paragraph_index": issue[3],
        }
    ]
    assert await _request_count(env["sessionmaker"]) == 1


async def test_human_request_replay(env, monkeypatch, connection_uri) -> None:
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)

    second = await service.create_or_get_human_request(action.review_action_id)
    assert second.human_request_id == request.human_request_id
    assert second.replayed
    assert await _request_count(env["sessionmaker"]) == 1


async def test_human_request_verify_rejects_payload_tamper(
    env, monkeypatch, connection_uri
) -> None:
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE human_review_requests SET request_payload = CAST(:p AS jsonb) "
                "WHERE human_request_id = :id"
            ).bindparams(p=json.dumps({"report_id": str(uuid4())}), id=request.human_request_id)
        )
        await session.commit()

    with pytest.raises(HumanReviewRequestIntegrityError):
        await service.verify_human_request_integrity(request.human_request_id)


# ---------------------------------------------------------------- human decision（spec K/L）


async def test_decision_approve_keeps_audit_fail_human_review(
    env, monkeypatch, connection_uri
) -> None:
    """人工 approve 之后：decision 持久化；Audit 保持 fail+human_review（spec L）。"""
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)

    decision = await service.resolve_human_request(
        request.human_request_id, "approve", comment=" 人工审核通过 "
    )
    assert decision.decision == "approve"
    assert decision.comment == "人工审核通过"
    assert not decision.replayed
    assert len(decision.decision_fingerprint) == 64

    # 人工 decision 不修改 Audit route / issues / Report。
    verified = await audit_service.verify_audit_integrity(audit.audit_id)
    assert verified.audit_status == "fail"
    assert verified.recommended_route == "human_review"
    assert len(verified.issues) == 1
    # decision 自身 integrity 通过。
    verified_decision = await service.verify_human_decision_integrity(decision.human_decision_id)
    assert verified_decision.decision == "approve"
    assert await _decision_count(env["sessionmaker"]) == 1


async def test_decision_replay_same_decision_comment(env, monkeypatch, connection_uri) -> None:
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)

    first = await service.resolve_human_request(
        request.human_request_id, "rewrite", comment=" 修改"
    )
    second = await service.resolve_human_request(
        request.human_request_id, "rewrite", comment="修改"
    )
    assert second.human_decision_id == first.human_decision_id
    assert second.replayed
    assert await _decision_count(env["sessionmaker"]) == 1


async def test_decision_different_decision_reject_no_overwrite(
    env, monkeypatch, connection_uri
) -> None:
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)

    first = await service.resolve_human_request(request.human_request_id, "approve")
    with pytest.raises(HumanReviewAlreadyResolved):
        await service.resolve_human_request(request.human_request_id, "rewrite")
    # 历史不被覆盖：仍是 approve。
    verified = await service.verify_human_decision_integrity(first.human_decision_id)
    assert verified.decision == "approve"
    assert await _decision_count(env["sessionmaker"]) == 1


async def test_decision_comment_blank_normalizes_to_none(env, monkeypatch, connection_uri) -> None:
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)

    decision = await service.resolve_human_request(
        request.human_request_id, "cancel", comment="   "
    )
    assert decision.comment is None


async def test_decision_comment_too_long_reject(env, monkeypatch, connection_uri) -> None:
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)

    with pytest.raises(ReviewInputError):
        await service.resolve_human_request(request.human_request_id, "approve", comment="x" * 1001)
    assert await _decision_count(env["sessionmaker"]) == 0


async def test_decision_invalid_decision_reject(env, monkeypatch, connection_uri) -> None:
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)

    with pytest.raises(ReviewInputError):
        await service.resolve_human_request(request.human_request_id, "finalize")
    assert await _decision_count(env["sessionmaker"]) == 0


async def test_decision_verify_rejects_decision_tamper(env, monkeypatch, connection_uri) -> None:
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)
    decision = await service.resolve_human_request(request.human_request_id, "approve")

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE human_review_decisions SET decision = 'research' "
                "WHERE human_decision_id = :id"
            ).bindparams(id=decision.human_decision_id)
        )
        await session.commit()

    with pytest.raises(HumanReviewDecisionIntegrityError):
        await service.verify_human_decision_integrity(decision.human_decision_id)


async def test_decision_verify_rejects_comment_tamper(env, monkeypatch, connection_uri) -> None:
    audit_service, audit, action, request = await _create_human_review_flow(
        env, monkeypatch, connection_uri
    )
    service = _review_service(env, audit_service)
    decision = await service.resolve_human_request(request.human_request_id, "approve")

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE human_review_decisions SET comment = :c WHERE human_decision_id = :id"
            ).bindparams(c="被篡改的备注", id=decision.human_decision_id)
        )
        await session.commit()

    with pytest.raises(HumanReviewDecisionIntegrityError):
        await service.verify_human_decision_integrity(decision.human_decision_id)
