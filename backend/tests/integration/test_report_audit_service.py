"""ReportAuditService integration tests (stage 5D, spec T audit 清单 + semantic cases).

真实 PostgreSQL + Fake Writer + Fake Auditor，全程**零真实 DeepSeek**（Fake
模型都是确定性返回）。

覆盖（spec T audit 清单）：
- deterministic aliases（S/P/C/E/X/G）+ prompt 无 UUID / fingerprint；
- no-cherry-picking：omitted / duplicate reviewed ref reject；
- unknown S/P/C/E refs、cross-paragraph Claim、unbound Evidence reject；
- issue type / severity（AuditIssueCandidate schema 层）；
- route pass / rewrite / research / human_review / priority（纯函数）；
- model failure / invalid decision → 0 writes；
- audit create + ReviewIssues 持久化（ordinal 1..N）；
- replay → second call 0 LLM；concurrency → 1 Audit；issues atomic rollback；
- verify_audit_integrity：happy path / not found / audit tamper /
  upstream Check tamper → IntegrityError（不自动 repair）；
- semantic fake cases：paragraph 一致 → pass；wording_overclaim +
  evidence_mismatch → route=rewrite；supports+contradicts → pack 仍含
  contradicts E2 + omitted_counterevidence → rewrite。
"""

import asyncio
import json
import re
from datetime import date
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.analysis.claims.contracts import (
    ClaimAnalysisDecision,
    ClaimAnalysisRequest,
    ClaimCandidate,
)
from app.analysis.claims.service import ClaimAnalysisService
from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisAnalysisRequest,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisTheme,
)
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.audit.contracts import AuditDecision, AuditIssueCandidate, ReportAuditRequest
from app.audit.errors import (
    ReportAuditIntegrityError,
    ReportAuditMalformedOutput,
    ReportAuditModelUnavailable,
    ReportAuditNotFound,
    ReportAuditParagraphOmitted,
    ReportAuditPersistenceFailed,
    ReportAuditUnknownRef,
)
from app.audit.prompt import build_audit_messages
from app.audit.service import ReportAuditService
from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.draft_section.contracts import DraftSectionRequest, ParagraphCandidate, WriterDecision
from app.draft_section.service import DraftSectionService
from app.report.check_service import ReportCheckService
from app.report.contracts import CHECK_STATUS_PASS, ReportAssemblyDraft
from app.report.errors import ReportCheckIntegrityError
from app.report.service import ReportService
from app.report_outline.service import ReportOutlineService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.contracts import SynthesisInputDraft
from app.synthesis.service import SynthesisService
from tests.analysis.claims.fakes import FakeClaimAnalysisModel
from tests.analysis.synthesis.fakes import FakeSynthesisAnalysisModel
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_claim_service import _seed_document_card as _seed_claim_doc_card
from tests.integration.test_report_service import _seed_research_task
from tests.integration.test_stage4_workflow import _cleanup
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_AS_OF = date(2026, 8, 10)
_URL_A = "https://www.xinhuanet.com/2026/0809/s4biz.htm"
_URL_B = "https://www.xinhuanet.com/2026/0809/s4ctr.htm"
_URL_C = "https://www.xinhuanet.com/2026/0809/s4pln.htm"


# ---------------------------------------------------------------- decision factories


def _all_refs(pack) -> list[str]:
    return [paragraph.paragraph_ref for paragraph in pack.paragraphs]


def _paragraph_issue(pack, *, issue_type, severity="normal", extra_issue=None):
    """针对 pack 第一段的一条合法 issue（claim/evidence 都真实绑定该段）。"""
    paragraph = pack.paragraphs[0]
    issue = AuditIssueCandidate(
        issue_type=issue_type,
        severity=severity,
        section_ref=paragraph.section_ref,
        paragraph_ref=paragraph.paragraph_ref,
        claim_refs=list(paragraph.claim_refs),
        evidence_refs=list(paragraph.evidence_refs),
        message="段落文字与证据支持范围不符",
    )
    if extra_issue is None:
        return issue
    return [issue, extra_issue]


def _second_issue(pack, *, issue_type, severity="normal"):
    paragraph = pack.paragraphs[0]
    return AuditIssueCandidate(
        issue_type=issue_type,
        severity=severity,
        section_ref=paragraph.section_ref,
        paragraph_ref=paragraph.paragraph_ref,
        claim_refs=list(paragraph.claim_refs),
        evidence_refs=list(paragraph.evidence_refs),
        message="证据陈述与段落文字表述不一致",
    )


def wording_overclaim_decision(pack):
    """semantic case 2：wording_overclaim + evidence_mismatch → route=rewrite。"""
    return AuditDecision(
        reviewed_paragraph_refs=_all_refs(pack),
        issues=[
            _paragraph_issue(pack, issue_type="wording_overclaim", severity="high"),
            _second_issue(pack, issue_type="evidence_mismatch"),
        ],
    )


def research_decision(pack):
    """unsupported_by_evidence → route=research。"""
    return AuditDecision(
        reviewed_paragraph_refs=_all_refs(pack),
        issues=[_paragraph_issue(pack, issue_type="unsupported_by_evidence")],
    )


def human_review_decision(pack):
    """unresolved_conflict critical → route=human_review。"""
    return AuditDecision(
        reviewed_paragraph_refs=_all_refs(pack),
        issues=[_paragraph_issue(pack, issue_type="unresolved_conflict", severity="critical")],
    )


def omitted_counterevidence_decision(pack):
    """semantic case 3：段落遗漏反驳证据 → route=rewrite。"""
    return AuditDecision(
        reviewed_paragraph_refs=_all_refs(pack),
        issues=[_paragraph_issue(pack, issue_type="omitted_counterevidence")],
    )


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


async def _cleanup_with_audits(sessionmaker) -> None:
    """先删 5D 审计层 + 5C 报告层（FK 引用 reports），再走公共 _cleanup。"""
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM review_issues"))
        await session.execute(text("DELETE FROM report_audits"))
        await session.execute(text("DELETE FROM report_check_results"))
        await session.execute(text("DELETE FROM reports"))
        await session.commit()
    await _cleanup(sessionmaker)


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker, monkeypatch) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup_with_audits(sessionmaker)
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
    await _cleanup_with_audits(sessionmaker)


# ---------------------------------------------------------------- helpers


def _audit_service(
    env, fake: FakeAuditModel, check_service: ReportCheckService
) -> ReportAuditService:
    return ReportAuditService(env["sessionmaker"], fake, check_service)


def _contradicts_claim_writer_factory(pack):
    """semantic case 3 专用 writer：段落**只引用 supports 证据**。

    找带 contradicts 关系的 Claim 及其 supports 证据（claim 创建时已通过正式
    ClaimService 绑定 contradicts）→ 段落 claim_refs/evidence_refs 只指向 supports。
    其它 section（无 contradicts claim）回退 `valid_decision_for`。
    """
    for claim in pack.claims:
        for item in pack.evidence:
            if claim.alias not in item.claim_aliases:
                continue
            relations = {rel for alias, rel in item.claim_relations if alias == claim.alias}
            if "contradicts" not in relations:
                continue
            support = next(
                (
                    ev
                    for ev in pack.evidence
                    if claim.alias in ev.claim_aliases
                    and "supports" in {r for a, r in ev.claim_relations if a == claim.alias}
                ),
                None,
            )
            assert support is not None, "claim 创建时必须已绑定 supports 证据"
            return WriterDecision(
                paragraphs=[
                    ParagraphCandidate(
                        text=f"{claim.statement} {support.evidence_statement}",
                        claim_refs=[claim.alias],
                        evidence_refs=[support.alias],
                    )
                ]
            )
    return valid_decision_for(pack)


async def _contradicts_claim_id(sessionmaker) -> UUID:
    """创建时绑定了 contradicts 关系的 Claim（正式 ClaimService 写入的 link）。

    contradicts 证据是 ClaimAnalysisService 按证据 pack alias 解析的（E2 = 排序
    第 2 的卡），不保证是某张具体卡 → 直接查任意 contradicts link。
    """
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT claim_id FROM claim_evidence_links "
                        "WHERE relation = 'contradicts' LIMIT 1"
                    )
                )
            )
            .mappings()
            .first()
        )
    assert row is not None, "claim 创建时必须已绑定 contradicts link"
    return UUID(str(row["claim_id"]))


async def _draft_outline_sections(env, outline_id: UUID, fake) -> dict[str, UUID]:
    """按 outline payload 的真实 section_ids 起草全部 sections（不硬编码 S1/S2）。"""
    async with env["sessionmaker"]() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT outline_payload FROM report_outlines WHERE outline_id = :oid"
                    ).bindparams(oid=outline_id)
                )
            )
            .mappings()
            .first()
        )
    assert row is not None, "outline 必须存在"
    section_ids = [section["section_id"] for section in row["outline_payload"]["sections"]]
    service = DraftSectionService(env["sessionmaker"], fake)
    ids: dict[str, UUID] = {}
    for section_id in section_ids:
        result = await service.create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id=section_id)
        )
        ids[section_id] = result.draft_section_id
    return ids


async def _build_minimal_chain(env, monkeypatch, connection_uri, *, writer_factory=None):
    """最小真实 PG 链：Evidence → ClaimService → Synthesis → Outline → Fake Writer
    → Report → Check(pass)。返回 (check_service, check_result_id, report_id)。

    Claim 通过**正式 ClaimAnalysisService**创建——C_contra 创建时同时绑定 supports
    E1 + contradicts E2（`claim_evidence_links` 由 ClaimService 原子写入，**禁止
    post-hoc SQL 改 link**）。段落只引用 supports 证据 → Audit Pack 仍包含
    contradicts E2（spec J：`_load_evidence` 加载 paragraph-referenced claims 的
    supports / contradicts / context links）。
    """
    sessionmaker = env["sessionmaker"]
    # 1. Evidence：3 张 document card（E1 supports / E2 contradicts / E3 supports）。
    card_a = await _seed_claim_doc_card(
        env, statement="公司披露2026年半年度营业收入同比增长。", source_url=_URL_A
    )
    card_b = await _seed_claim_doc_card(
        env, statement="公司披露2026年半年度部分业务收入同比下降。", source_url=_URL_B
    )
    card_c = await _seed_claim_doc_card(
        env, statement="公司披露2026年半年度毛利率保持稳定。", source_url=_URL_C
    )
    evidence_ids = [
        card_a["evidence_card_id"],
        card_b["evidence_card_id"],
        card_c["evidence_card_id"],
    ]

    # 2. ClaimService：Claim C_contra 创建时同时有 supports E1 + contradicts E2。
    decision = ClaimAnalysisDecision(
        relevant=True,
        claims=[
            ClaimCandidate(
                statement="公司营业收入保持增长态势。",
                claim_kind=ClaimKind.INFERENCE,
                confidence=ClaimConfidence.MEDIUM,
                importance=ClaimImportance.NORMAL,
                support_refs=["E1"],
                contradict_refs=["E2"],
                context_refs=[],
            ),
            ClaimCandidate(
                statement="公司毛利率保持稳定。",
                claim_kind=ClaimKind.INFERENCE,
                confidence=ClaimConfidence.MEDIUM,
                importance=ClaimImportance.NORMAL,
                support_refs=["E3"],
                contradict_refs=[],
                context_refs=[],
            ),
        ],
    )
    claim_service = ClaimAnalysisService(sessionmaker, FakeClaimAnalysisModel(decision=decision))
    claim_result = await claim_service.analyze(
        ClaimAnalysisRequest(
            company_id=env["company_id"],
            research_question=_QUESTION,
            analysis_domain=ClaimAnalysisDomain.BUSINESS,
            evidence_card_ids=evidence_ids,
        )
    )
    claim_ids = claim_result.claim_ids

    # 3. Synthesis（evidence availability <= _AS_OF，temporal no-lookahead 通过）。
    synthesis = await SynthesisService(sessionmaker).create_or_get_synthesis(
        SynthesisInputDraft(
            company_id=env["company_id"],
            research_question=_QUESTION,
            analysis_as_of=_AS_OF,
            claim_ids=claim_ids,
        )
    )

    # 4. SynthesisAnalysis：theme A 单独放 contradicts claim → S1 段落引用它。
    contra_claim_id = await _contradicts_claim_id(sessionmaker)
    sorted_ids = sorted(claim_ids, key=str)
    contra_alias = f"C{sorted_ids.index(contra_claim_id) + 1}"
    other_claim_id = next(cid for cid in sorted_ids if cid != contra_claim_id)
    other_alias = f"C{sorted_ids.index(other_claim_id) + 1}"
    synth_model = FakeSynthesisAnalysisModel(
        output=SynthesisAnalysisOutput(
            summary="综合判断：营收与盈利质量。",
            themes=[
                SynthesisTheme(title="主题A：营收", summary="A", claim_refs=[contra_alias]),
                SynthesisTheme(title="主题B：盈利质量", summary="B", claim_refs=[other_alias]),
            ],
            claim_roles=[
                SynthesisClaimRoleAssignment(
                    claim_ref=contra_alias,
                    role=SynthesisClaimRole.SUPPORT,
                    rationale=f"支持 {contra_alias}",
                ),
                SynthesisClaimRoleAssignment(
                    claim_ref=other_alias,
                    role=SynthesisClaimRole.SUPPORT,
                    rationale=f"支持 {other_alias}",
                ),
            ],
            duplicates=[],
            conflicts=[],
            evidence_gaps=[],
        )
    )
    synth_result = await SynthesisAnalysisService(sessionmaker, synth_model).analyze(
        SynthesisAnalysisRequest(synthesis_id=synthesis.synthesis_id)
    )

    # 5. Outline（0 LLM，verified synthesis result → S1/S2）。
    outline = await ReportOutlineService(sessionmaker).create_or_get_outline(
        synth_result.synthesis_result_id
    )

    # 6. Fake Writer：起草全部 sections（段落只引用 supports 证据）。
    fake_writer = FakeDraftSectionModel(decision_factory=writer_factory or valid_decision_for)
    draft_ids = await _draft_outline_sections(env, outline.outline_id, fake_writer)

    # 7. Report 装配（0 LLM）。
    report_service = ReportService(sessionmaker, DraftSectionService(sessionmaker, fake_writer))
    report = await report_service.create_or_get_report(
        ReportAssemblyDraft(
            outline_id=outline.outline_id, draft_section_ids=tuple(draft_ids.values())
        )
    )

    # 8. Check（10 项 v1 全 pass → 标准 5D 输入）。
    check_service = ReportCheckService(sessionmaker, report_service)
    check = await check_service.run_report_checks(report.report_id)
    assert check.status == CHECK_STATUS_PASS
    return check_service, check.check_result_id, report.report_id


async def _audit_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text("SELECT count(*) FROM report_audits"))).scalar_one())


async def _issue_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text("SELECT count(*) FROM review_issues"))).scalar_one())


# ---------------------------------------------------------------- deterministic aliases + prompt


async def test_audit_aliases_deterministic_and_no_uuid_in_prompt(
    env, monkeypatch, connection_uri
) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=pass_decision)
    service = _audit_service(env, fake, check_service)
    await service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )

    pack = fake.calls[0]
    # S/P alias 稳定、连续编号。
    assert [section.section_ref for section in pack.sections] == [
        f"S{i}" for i in range(1, len(pack.sections) + 1)
    ]
    assert [paragraph.paragraph_ref for paragraph in pack.paragraphs] == [
        f"P{i}" for i in range(1, len(pack.paragraphs) + 1)
    ]
    for claim in pack.claims:
        assert claim.claim_ref.startswith("C")
    for item in pack.evidence:
        assert item.evidence_ref.startswith("E")

    # prompt（user payload）不含 UUID / fingerprint / provenance id。
    messages = build_audit_messages(pack)
    user_content = "\n".join(message["content"] for message in messages)
    assert "fingerprint" not in user_content
    uuid_pattern = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    assert not uuid_pattern.search(user_content)


# ---------------------------------------------------------------- no-cherry-picking（spec M）


async def test_audit_omitted_paragraph_reject(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )

    def _factory(pack):
        return AuditDecision(
            reviewed_paragraph_refs=_all_refs(pack)[:-1],
            issues=[],
        )

    fake = FakeAuditModel(decision_factory=_factory)
    service = _audit_service(env, fake, check_service)
    with pytest.raises(ReportAuditParagraphOmitted):
        await service.create_or_get_audit(
            ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
        )
    assert await _audit_count(env["sessionmaker"]) == 0


async def test_audit_duplicate_reviewed_ref_reject(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )

    def _factory(pack):
        refs = _all_refs(pack)
        return {"reviewed_paragraph_refs": refs + [refs[0]], "issues": []}

    fake = FakeAuditModel(decision_factory=_factory)
    service = _audit_service(env, fake, check_service)
    # pydantic schema 层拒绝重复 reviewed ref → MalformedOutput（0 写）。
    with pytest.raises(ReportAuditMalformedOutput):
        await service.create_or_get_audit(
            ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
        )
    assert await _audit_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- issue ref validation（spec N）


async def test_audit_unknown_section_ref_reject(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )

    def _factory(pack):
        paragraph = pack.paragraphs[0]
        return AuditDecision(
            reviewed_paragraph_refs=_all_refs(pack),
            issues=[
                AuditIssueCandidate(
                    issue_type="wording_overclaim",
                    severity="normal",
                    section_ref="S99",
                    paragraph_ref=paragraph.paragraph_ref,
                    claim_refs=list(paragraph.claim_refs),
                    evidence_refs=list(paragraph.evidence_refs),
                    message="未知 section 引用",
                )
            ],
        )

    fake = FakeAuditModel(decision_factory=_factory)
    service = _audit_service(env, fake, check_service)
    with pytest.raises(ReportAuditUnknownRef):
        await service.create_or_get_audit(
            ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
        )
    assert await _audit_count(env["sessionmaker"]) == 0


async def test_audit_cross_paragraph_claim_reject(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )

    def _factory(pack):
        # 第一段引用属于**其它段落**（且第一段未引用）的 claim → cross-scope reject。
        paragraph = pack.paragraphs[0]
        other = next(
            item
            for item in pack.paragraphs
            if item.paragraph_ref != paragraph.paragraph_ref
            and item.claim_refs
            and not (set(item.claim_refs) & set(paragraph.claim_refs))
        )
        return AuditDecision(
            reviewed_paragraph_refs=_all_refs(pack),
            issues=[
                AuditIssueCandidate(
                    issue_type="wording_overclaim",
                    severity="normal",
                    section_ref=paragraph.section_ref,
                    paragraph_ref=paragraph.paragraph_ref,
                    claim_refs=[other.claim_refs[0]],
                    evidence_refs=[],
                    message="跨段落 Claim 引用",
                )
            ],
        )

    fake = FakeAuditModel(decision_factory=_factory)
    service = _audit_service(env, fake, check_service)
    with pytest.raises(ReportAuditUnknownRef):
        await service.create_or_get_audit(
            ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
        )
    assert await _audit_count(env["sessionmaker"]) == 0


async def test_audit_unbound_evidence_reject(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )

    def _factory(pack):
        paragraph = pack.paragraphs[0]
        claim_ids = {str(cid) for cid in paragraph.claim_ids}
        # 找一张**不绑定该段落任何 claim** 的 evidence → unbound reject。
        other = next(
            item
            for item in pack.evidence
            if not (set(str(cid) for cid, _ in item.claim_relations) & claim_ids)
        )
        return AuditDecision(
            reviewed_paragraph_refs=_all_refs(pack),
            issues=[
                AuditIssueCandidate(
                    issue_type="wording_overclaim",
                    severity="normal",
                    section_ref=paragraph.section_ref,
                    paragraph_ref=paragraph.paragraph_ref,
                    claim_refs=list(paragraph.claim_refs),
                    evidence_refs=[other.evidence_ref],
                    message="未绑定 claim 的 evidence 引用",
                )
            ],
        )

    fake = FakeAuditModel(decision_factory=_factory)
    service = _audit_service(env, fake, check_service)
    with pytest.raises(ReportAuditUnknownRef):
        await service.create_or_get_audit(
            ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
        )
    assert await _audit_count(env["sessionmaker"]) == 0


# ---------------------------------------------------- model failure / invalid decision → 0 writes


async def test_audit_model_failure_zero_writes(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(error=ReportAuditModelUnavailable)
    service = _audit_service(env, fake, check_service)
    with pytest.raises(ReportAuditModelUnavailable):
        await service.create_or_get_audit(
            ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
        )
    assert await _audit_count(env["sessionmaker"]) == 0


async def test_audit_invalid_decision_zero_writes(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision={"reviewed_paragraph_refs": [], "issues": []})
    service = _audit_service(env, fake, check_service)
    with pytest.raises(ReportAuditMalformedOutput):
        await service.create_or_get_audit(
            ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
        )
    assert await _audit_count(env["sessionmaker"]) == 0


# ------------------------------------------------------------ audit create / replay / concurrency


async def test_audit_create_persists_issues(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=wording_overclaim_decision)
    service = _audit_service(env, fake, check_service)
    result = await service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )

    assert result.report_id == report_id
    assert result.check_result_id == check_result_id
    assert not result.replayed
    assert result.audit_status == "fail"
    assert result.recommended_route == "rewrite"
    assert result.issue_count == 2
    assert len(result.audit_fingerprint) == 64
    assert await _audit_count(env["sessionmaker"]) == 1
    assert await _issue_count(env["sessionmaker"]) == 2


async def test_audit_replay_second_call_zero_llm(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=wording_overclaim_decision)
    service = _audit_service(env, fake, check_service)
    request = ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)

    first = await service.create_or_get_audit(request)
    second = await service.create_or_get_audit(request)

    assert second.audit_id == first.audit_id
    assert second.replayed
    assert len(fake.calls) == 1  # 第二次 0 LLM
    assert await _audit_count(env["sessionmaker"]) == 1
    assert await _issue_count(env["sessionmaker"]) == 2


async def test_audit_concurrency_single_row(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=wording_overclaim_decision)
    service = _audit_service(env, fake, check_service)
    request = ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)

    results = await asyncio.gather(
        service.create_or_get_audit(request),
        service.create_or_get_audit(request),
    )
    assert len({result.audit_id for result in results}) == 1
    assert await _audit_count(env["sessionmaker"]) == 1
    assert await _issue_count(env["sessionmaker"]) == 2


async def test_audit_issues_atomic_rollback(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=wording_overclaim_decision)
    service = _audit_service(env, fake, check_service)

    def _boom(*args, **kwargs):
        raise SQLAlchemyError("issue persistence failed")

    monkeypatch.setattr(service, "_issue_models", _boom)
    with pytest.raises(ReportAuditPersistenceFailed):
        await service.create_or_get_audit(
            ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
        )
    # issue 写入失败 → 整个 audit 事务 rollback（0 partial write）。
    assert await _audit_count(env["sessionmaker"]) == 0
    assert await _issue_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- semantic fake cases


async def test_semantic_case1_paragraph_consistent_pass(env, monkeypatch, connection_uri) -> None:
    """paragraph 与 Claim/Evidence 一致（fake pass）→ status=pass, route=pass。"""
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=pass_decision)
    service = _audit_service(env, fake, check_service)
    result = await service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )
    assert result.audit_status == "pass"
    assert result.recommended_route == "pass"
    assert result.issue_count == 0
    assert await _issue_count(env["sessionmaker"]) == 0


async def test_semantic_case2_overclaim_route_rewrite(env, monkeypatch, connection_uri) -> None:
    """Claim 营收增长 / Evidence 营收增长15% / paragraph 盈利能力增强（结构合法）→
    Fake Audit: wording_overclaim + evidence_mismatch → status=fail, route=rewrite。"""
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=wording_overclaim_decision)
    service = _audit_service(env, fake, check_service)
    result = await service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )
    assert result.audit_status == "fail"
    assert result.recommended_route == "rewrite"
    assert result.issue_count == 2
    # ordinal 1..N 按 spec R deterministic 排序（同段同 claim/evidence 下按 issue_type
    # 字典序：evidence_mismatch < wording_overclaim）+ resolved 信息持久化。
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text("SELECT ordinal, issue_type, severity FROM review_issues ORDER BY ordinal")
            )
        ).all()
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (1, "evidence_mismatch", "normal"),
        (2, "wording_overclaim", "high"),
    ]


async def test_semantic_case3_contradicts_evidence_in_pack(
    env, monkeypatch, connection_uri
) -> None:
    """Claim 创建时通过正式 ClaimService 同时绑定 supports E1 + contradicts E2
    （`claim_evidence_links` 由 ClaimService 原子写入，**禁止 post-hoc SQL 改
    link**；也禁止放宽 claim fingerprint / integrity）；paragraph 只引用 supports
    E1 → Audit Pack 必须仍包含 contradicts E2（spec J）。Fake Audit:
    omitted_counterevidence → route=rewrite。"""
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri, writer_factory=_contradicts_claim_writer_factory
    )
    request = ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)

    fake = FakeAuditModel(decision_factory=omitted_counterevidence_decision)
    service = _audit_service(env, fake, check_service)
    result = await service.create_or_get_audit(request)

    pack = fake.calls[0]
    referenced = {str(cid) for para in pack.paragraphs for cid in para.claim_ids}
    contradicts = [
        item
        for item in pack.evidence
        if any(relation == "contradicts" for _, relation in item.claim_relations)
    ]
    assert contradicts, "Audit Pack 必须包含 contradicts Evidence（spec J）"
    for item in contradicts:
        bound = {str(cid) for cid, _ in item.claim_relations}
        assert bound & referenced, "contradicts Evidence 必须绑定到段落引用的 Claim"
        # paragraph 只引用了 supports 证据，未直接引用 contradicts 卡。
        paragraph_evidence = {r for para in pack.paragraphs for r in para.evidence_refs}
        assert item.evidence_ref not in paragraph_evidence
    assert result.audit_status == "fail"
    assert result.recommended_route == "rewrite"


# ---------------------------------------------------------------- verify_audit_integrity（spec S）


async def test_audit_verify_integrity_happy_path(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=wording_overclaim_decision)
    service = _audit_service(env, fake, check_service)
    result = await service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )

    verified = await service.verify_audit_integrity(result.audit_id)
    assert verified.audit_id == result.audit_id
    assert verified.report_id == report_id
    assert verified.check_result_id == check_result_id
    assert verified.audit_status == "fail"
    assert verified.recommended_route == "rewrite"
    assert verified.issue_count == 2
    assert len(verified.issues) == 2
    assert [issue.ordinal for issue in verified.issues] == [1, 2]
    # 上游 verified 产物一并返回（供 5E 复用，一个调用多个 verified）。
    assert verified.verified_check.check_result_id == check_result_id
    assert verified.verified_report.report_id == report_id


async def test_audit_verify_integrity_not_found(env) -> None:
    fake = FakeAuditModel(decision_factory=pass_decision)
    service = _audit_service(env, fake, check_service=object())  # type: ignore[arg-type]
    with pytest.raises(ReportAuditNotFound) as excinfo:
        await service.verify_audit_integrity(uuid4())
    assert excinfo.value.code == "report_audit_not_found"


async def test_audit_verify_rejects_tampered_fingerprint(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=wording_overclaim_decision)
    service = _audit_service(env, fake, check_service)
    result = await service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE report_audits SET audit_fingerprint = :fp WHERE audit_id = :id"
            ).bindparams(fp="0" * 64, id=result.audit_id)
        )
        await session.commit()

    with pytest.raises(ReportAuditIntegrityError) as excinfo:
        await service.verify_audit_integrity(result.audit_id)
    assert excinfo.value.code == "report_audit_integrity_error"


async def test_audit_verify_rejects_tampered_route(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=research_decision)
    service = _audit_service(env, fake, check_service)
    result = await service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )
    assert result.recommended_route == "research"

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE report_audits SET audit_status = 'pass', recommended_route = 'pass' "
                "WHERE audit_id = :id"
            ).bindparams(id=result.audit_id)
        )
        await session.commit()

    with pytest.raises(ReportAuditIntegrityError):
        await service.verify_audit_integrity(result.audit_id)


async def test_audit_verify_rejects_tampered_issue(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=wording_overclaim_decision)
    service = _audit_service(env, fake, check_service)
    result = await service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )

    # 篡改 issue message → scope 校验通过但 audit_fingerprint 重算不一致。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE review_issues SET message = :msg WHERE ordinal = 1 AND audit_id = :id"
            ).bindparams(msg="被篡改的 issue 消息", id=result.audit_id)
        )
        await session.commit()

    with pytest.raises(ReportAuditIntegrityError):
        await service.verify_audit_integrity(result.audit_id)


async def test_audit_verify_rejects_upstream_check_tamper(env, monkeypatch, connection_uri) -> None:
    check_service, check_result_id, report_id = await _build_minimal_chain(
        env, monkeypatch, connection_uri
    )
    fake = FakeAuditModel(decision_factory=pass_decision)
    service = _audit_service(env, fake, check_service)
    result = await service.create_or_get_audit(
        ReportAuditRequest(report_id=report_id, check_result_id=check_result_id)
    )

    # 篡改上游 Check findings → verify_check_result_integrity 先行拒绝（不 repair）。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE report_check_results SET findings = CAST(:f AS jsonb) "
                "WHERE check_result_id = :id"
            ).bindparams(f=json.dumps([{"code": "tampered"}]), id=check_result_id)
        )
        await session.commit()

    with pytest.raises(ReportCheckIntegrityError):
        await service.verify_audit_integrity(result.audit_id)
