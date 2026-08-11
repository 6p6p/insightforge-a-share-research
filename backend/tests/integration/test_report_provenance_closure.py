"""Report check `citation_provenance_closure` spec D tests: real provenance closure.

背景（spec D 审计）：5C 初期 `_has_source_provenance` 只检查 FK 非空——document 卡
有 source_id、macro 卡有 macro_observation_id 就算"有 provenance"。这不充分：
- document 链（EvidenceCard.source_id → SourceRecord.artifact_id → RawArtifact）
  被两级 FK RESTRICT 完整保证，**SQL 无法构造断裂**（删 source_record / raw_artifact
  都会被 RESTRICT 挡住）；
- macro 链（Observation → Snapshot → Series / Provider + macro_snapshot_artifacts
  → RawArtifact）的 artifact links **是可选的**：删除链接后 Observation /
  Snapshot / Series 仍在，FK 字段非空，但 RawArtifact 已不可达。

spec D 把 provenance 改为**真实闭包**（沿链走到真实 `raw_artifacts` 行）。本文件
用真实持久化的 macro 链证明：删除 snapshot 的 artifact links → closure 返回 False
（FK 字段完好，但真实闭包检测到断裂）。

覆盖：
- macro 卡真实链 → True；删除 snapshot artifact links → False（**核心 proof**）；
- document 卡真实链 → True；
- document / macro 的 closure 确实查询 DB（悬空 FK → False，防止退化为恒真）。

全程真实 PostgreSQL + 真实 SourceRecord / RawArtifact / macro 链，**零 LLM / 零
Chroma / 零 LangGraph**（复用 test_claim_service 的 seed 助手）。
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.evidence_card import EvidenceCardModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.draft_section.service import DraftSectionService
from app.evidence.provenance_service import EvidenceProvenanceService
from app.report.check_service import ReportCheckService
from app.report.service import ReportService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_claim_service import _seed_document_card, _seed_macro_card
from tests.integration.test_report_service import (
    _cleanup_with_reports,
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


async def _check_service(env) -> ReportCheckService:
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    return ReportCheckService(
        env["sessionmaker"],
        ReportService(env["sessionmaker"], DraftSectionService(env["sessionmaker"], fake)),
    )


# ---------------------------------------------------------------- tests


async def test_macro_provenance_closure_detects_deleted_artifact_links(env, monkeypatch) -> None:
    """spec D 核心 proof：macro FK 字段非空不够——删除 artifact links → closure=False。"""
    seeded = await _seed_macro_card(env, monkeypatch)
    card_id = seeded["evidence_card_id"]
    service = await _check_service(env)
    async with env["sessionmaker"]() as session:
        card = await session.get(EvidenceCardModel, card_id)
        assert card is not None
        assert card.origin_type == "macro_observation"
        assert card.macro_observation_id is not None  # FK 字段完好，未被篡改
        cards = {card_id: card}
        ok = await service._load_provenance_closure(session, cards)
        assert ok[card_id] is True
        # 删除该 snapshot 的 artifact links：Observation / Snapshot / Series 仍在，
        # FK 字段不变，但 RawArtifact 已不可达 → 真实闭包必须返回 False。
        await session.execute(
            text("DELETE FROM macro_snapshot_artifacts WHERE snapshot_id = :sid").bindparams(
                sid=seeded["chain"]["snapshot_id"]
            )
        )
        await session.commit()
        broken = await service._load_provenance_closure(session, cards)
    assert broken[card_id] is False


async def test_document_provenance_closure_valid_chain_true(env) -> None:
    """spec D：真实 document 链（SourceRecord → RawArtifact）→ closure=True。"""
    seeded = await _seed_document_card(env)
    card_id = seeded["evidence_card_id"]
    service = await _check_service(env)
    async with env["sessionmaker"]() as session:
        card = await session.get(EvidenceCardModel, card_id)
        assert card is not None
        assert card.origin_type == "document_chunk"
        ok = await service._load_provenance_closure(session, {card_id: card})
    assert ok[card_id] is True


async def test_document_provenance_closure_dangling_source_false(sessionmaker) -> None:
    """spec D：closure 真实查询 DB——FK 字段非空但源记录不存在 → False（防恒真退化）。"""
    async with sessionmaker() as session:
        ok = await EvidenceProvenanceService.document_closure(session, {uuid4(): uuid4()})
    assert list(ok.values()) == [False]


async def test_macro_provenance_closure_orphan_observation_false(sessionmaker) -> None:
    """spec D：closure 真实查询 DB——FK 字段非空但 Observation 不存在 → False。"""
    async with sessionmaker() as session:
        ok = await EvidenceProvenanceService.macro_closure(session, {uuid4(): uuid4()})
    assert list(ok.values()) == [False]
