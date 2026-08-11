"""ReportCheckService.verify_check_result_integrity integration tests (Stage 5D Gate 0, spec A/B/C).

真实 PostgreSQL + Fake Writer，全程**零真实 DeepSeek**（Fake 模型都是确定性返回）。

背景（spec A 审计）：5C 结束时 `ReportCheckService` 只有 `run_report_checks`，
**没有** public read-only `verify_check_result_integrity`——本阶段 Gate 0 新增它，
并补齐腐败测试证明其能力。

覆盖（spec A/B/C）：
- 新增 public read-only `verify_check_result_integrity(check_result_id)`；
- 有效 CheckResult verify 通过（pass 与 fail 均验证）；
- 篡改（status / findings / check_fingerprint）→ ReportCheckIntegrityError
  （不自动 repair）；
- 上游 Report 篡改（report_fingerprint）→ ReportIntegrityError（上游先行拒绝）；
- check_result_id 不存在 → ReportCheckNotFound；
- spec C：Report integrity valid 但 risks_and_gaps 遗漏 Outline 要求的
  evidence_gap_index → run_report_checks 必须**真实持久化** status=fail +
  finding.code=conflict_gap_preservation（draft 期合法，非 SQL tamper）。
"""

import json
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.draft_section.contracts import DraftSectionRequest, ParagraphCandidate, WriterDecision
from app.draft_section.packs import SectionInputPack
from app.draft_section.service import DraftSectionService
from app.report.check_service import ReportCheckService
from app.report.contracts import (
    CHECK_STATUS_FAIL,
    CHECK_STATUS_PASS,
    REPORT_CHECK_SCHEMA_VERSION,
    ReportAssemblyDraft,
)
from app.report.errors import ReportCheckIntegrityError, ReportCheckNotFound, ReportIntegrityError
from app.report.service import ReportService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_draft_section_service import _create_outline, _two_theme_models
from tests.integration.test_report_service import (
    _cleanup_with_reports,
    _draft_all_sections,
    _seed_research_task,
)
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


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker, monkeypatch) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup_with_reports(sessionmaker)
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
    await _cleanup_with_reports(sessionmaker)


# ---------------------------------------------------------------- helpers


def _report_service(env, fake: FakeDraftSectionModel) -> ReportService:
    return ReportService(
        env["sessionmaker"],
        DraftSectionService(env["sessionmaker"], fake),
    )


def _risks_gap_omitted_decision(pack: SectionInputPack) -> WriterDecision:
    """risks_and_gaps：引用 conflict 但**故意遗漏** outline 要求的 evidence_gap。

    draft 期合法（Section-aware contract 只要求每段至少 claim / conflict / gap
    之一，不要求覆盖全部 outline gap index）→ 产生的 DraftSection 与 Report 均可
    通过完整性验证；但 Report check `conflict_gap_preservation` 必须捕获漏掉的
    evidence_gap_index → status=fail（spec C，非 SQL tamper）。
    """
    claim = pack.claims[0]
    evidence = next(item for item in pack.evidence if claim.alias in item.claim_aliases)
    paragraphs = [
        ParagraphCandidate(
            text=f"{claim.statement} {evidence.evidence_statement}",
            claim_refs=[claim.alias],
            evidence_refs=[evidence.alias],
        )
    ]
    for conflict in pack.conflicts:
        if conflict.claim_aliases:
            c_alias = conflict.claim_aliases[0]
            c = next(item for item in pack.claims if item.alias == c_alias)
            ev = next(item for item in pack.evidence if c_alias in item.claim_aliases)
            paragraphs.append(
                ParagraphCandidate(
                    text=f"{conflict.description} {c.statement} {ev.evidence_statement}",
                    claim_refs=[c_alias],
                    evidence_refs=[ev.alias],
                    conflict_refs=[conflict.alias],
                )
            )
    # gap 段落**故意不生成** → 没有任何 paragraph 引用 evidence_gap_index。
    return WriterDecision(paragraphs=paragraphs)


async def _draft_mixed_sections(
    env, outline_id: UUID, s3_fake: FakeDraftSectionModel
) -> dict[str, UUID]:
    """S1/S2 用标准 fake，S3（risks_and_gaps）用自定义 fake。"""
    ids: dict[str, UUID] = {}
    for section_id in ("S1", "S2"):
        service = DraftSectionService(
            env["sessionmaker"], FakeDraftSectionModel(decision_factory=valid_decision_for)
        )
        result = await service.create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id=section_id)
        )
        ids[section_id] = result.draft_section_id
    result = await DraftSectionService(env["sessionmaker"], s3_fake).create_or_get_section(
        DraftSectionRequest(outline_id=outline_id, section_id="S3")
    )
    ids["S3"] = result.draft_section_id
    return ids


async def _pass_check(env, monkeypatch, connection_uri) -> tuple[ReportCheckService, UUID, UUID]:
    """全标准 draft → Report → Check(pass)，返回 (check_service, check_result_id, report_id)。"""
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    draft_ids = await _draft_all_sections(env, outline_id, fake)
    service = _report_service(env, fake)
    report = await service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )
    check_service = ReportCheckService(env["sessionmaker"], service)
    check = await check_service.run_report_checks(report.report_id)
    assert check.status == CHECK_STATUS_PASS
    assert check.findings == ()
    return check_service, check.check_result_id, report.report_id


# ---------------------------------------------------------------- spec A/B：valid verify


async def test_verify_check_result_integrity_valid_pass(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _pass_check(env, monkeypatch, connection_uri)

    verified = await check_service.verify_check_result_integrity(check_result_id)
    assert verified.check_result_id == check_result_id
    assert verified.report_id == report_id
    assert verified.check_schema_version == REPORT_CHECK_SCHEMA_VERSION
    assert verified.status == CHECK_STATUS_PASS
    assert verified.findings == ()
    assert len(verified.check_fingerprint) == 64
    # verified_report 一并返回（供 5D Audit 复用，一个调用两个 verified 产物）。
    assert verified.verified_report.report_id == report_id


async def test_verify_check_result_integrity_not_found(env) -> None:
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    service = _report_service(env, fake)
    check_service = ReportCheckService(env["sessionmaker"], service)

    with pytest.raises(ReportCheckNotFound) as excinfo:
        await check_service.verify_check_result_integrity(uuid4())
    assert excinfo.value.code == "report_check_not_found"


# ---------------------------------------------------------------- spec B：corruption rejects


async def test_verify_check_result_integrity_rejects_status_tamper(
    env, monkeypatch, connection_uri
) -> None:
    check_service, check_result_id, _ = await _pass_check(env, monkeypatch, connection_uri)
    # pass → fail 篡改：status 不在 check_fingerprint 内，只有重跑 checks 才能发现。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE report_check_results SET status = 'fail' WHERE check_result_id = :id"
            ).bindparams(id=check_result_id)
        )
        await session.commit()

    with pytest.raises(ReportCheckIntegrityError) as excinfo:
        await check_service.verify_check_result_integrity(check_result_id)
    assert excinfo.value.code == "report_check_integrity_error"


async def test_verify_check_result_integrity_rejects_findings_tamper(
    env, monkeypatch, connection_uri
) -> None:
    check_service, check_result_id, _ = await _pass_check(env, monkeypatch, connection_uri)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE report_check_results SET findings = CAST(:findings AS jsonb) "
                "WHERE check_result_id = :id"
            ).bindparams(
                findings=json.dumps([{"code": "empty_section", "section_id": "S1"}]),
                id=check_result_id,
            )
        )
        await session.commit()

    with pytest.raises(ReportCheckIntegrityError) as excinfo:
        await check_service.verify_check_result_integrity(check_result_id)
    assert excinfo.value.code == "report_check_integrity_error"


async def test_verify_check_result_integrity_rejects_fingerprint_tamper(
    env, monkeypatch, connection_uri
) -> None:
    check_service, check_result_id, _ = await _pass_check(env, monkeypatch, connection_uri)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE report_check_results SET check_fingerprint = :fp "
                "WHERE check_result_id = :id"
            ).bindparams(fp="f" * 64, id=check_result_id)
        )
        await session.commit()

    with pytest.raises(ReportCheckIntegrityError) as excinfo:
        await check_service.verify_check_result_integrity(check_result_id)
    assert excinfo.value.code == "report_check_integrity_error"


async def test_verify_check_result_integrity_rejects_upstream_report_tamper(
    env, monkeypatch, connection_uri
) -> None:
    check_service, check_result_id, report_id = await _pass_check(env, monkeypatch, connection_uri)
    # 上游 Report 篡改：check_fingerprint 会自洽（含 tampered report_fingerprint），
    # 必须靠 verify_report_integrity 先行拒绝（不能只重算指纹）。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE reports SET report_fingerprint = :fp WHERE report_id = :id").bindparams(
                fp="f" * 64, id=report_id
            )
        )
        await session.commit()

    with pytest.raises(ReportIntegrityError) as excinfo:
        await check_service.verify_check_result_integrity(check_result_id)
    assert excinfo.value.code == "report_integrity_error"


# ---------------------------------------------------------------- spec C：gap omission → fail


async def test_check_result_fail_persisted_conflict_gap_preservation(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    s3_fake = FakeDraftSectionModel(decision_factory=_risks_gap_omitted_decision)
    draft_ids = await _draft_mixed_sections(env, outline_id, s3_fake)

    service = _report_service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    report = await service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )
    # Report integrity valid：draft 期合法（不要求覆盖全部 gap index），非 SQL tamper。
    await service.verify_report_integrity(report.report_id)

    check_service = ReportCheckService(env["sessionmaker"], service)
    check = await check_service.run_report_checks(report.report_id)

    # 必须真实持久化 status=fail + conflict_gap_preservation finding。
    assert check.status == CHECK_STATUS_FAIL
    assert check.replayed is False
    assert any(
        f.code == "conflict_gap_preservation" and f.section_id == "S3" for f in check.findings
    )
    async with env["sessionmaker"]() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT status, findings FROM report_check_results "
                        "WHERE check_result_id = :id"
                    ).bindparams(id=check.check_result_id)
                )
            )
            .mappings()
            .first()
        )
    assert row["status"] == CHECK_STATUS_FAIL
    assert any(item["code"] == "conflict_gap_preservation" for item in row["findings"])

    # fail 结果同样可被 verify（read-side 接受合法 fail）。
    verified = await check_service.verify_check_result_integrity(check.check_result_id)
    assert verified.status == CHECK_STATUS_FAIL
    assert any(
        f.code == "conflict_gap_preservation" and f.section_id == "S3" for f in verified.findings
    )
