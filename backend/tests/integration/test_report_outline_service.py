"""ReportOutlineService integration tests (stage 5A, spec F-N).

真实 PostgreSQL + Fake LLM models + 真实 LangGraph + PG Checkpointer，全程
**零真实 DeepSeek**。覆盖：

- E2E：ResearchTask → Stage4WorkflowRun → SynthesisResult → ReportOutline
  （reuse test_stage4_workflow 的完整 seed / graph）；提纲派生正确：theme →
  theme section（全部 claims），evidence_gap → risks_and_gaps section；
- 持久化字段：schema_version=1、fingerprint / sha256 均为 64 hex、payload
  sections 结构正确；
- replay：同 synthesis_result_id 再次 create_or_get_outline → 同
  outline_id（replayed=True），只有 1 行；
- missing：不存在的 synthesis_result_id → SynthesisAnalysisResultNotFound；
- tamper：result_fingerprint / result_schema_version 被篡改 →
  SynthesisResultIntegrityError（verify_result_integrity 拒，0 行 outline）。
"""

import json
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.synthesis.errors import (
    SynthesisAnalysisResultNotFound,
    SynthesisResultIntegrityError,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.report_outline.contracts import REPORT_OUTLINE_SCHEMA_VERSION
from app.report_outline.errors import ReportOutlineIntegrityError, ReportOutlineNotFound
from app.report_outline.service import ReportOutlineService
from app.services.source_registry_service import SourceRegistryService
from app.stage4.runner import Stage4WorkflowRunner
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.integration.test_stage4_workflow import (
    _AS_OF,
    _build_deps,
    _cleanup,
    _good_models,
    _request,
    _seed_worker_inputs,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


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
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    from app.storage.raw_store import LocalRawArtifactStore
    from tests.integration.test_valuation_claim_service import _seed_company

    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
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
    from datetime import date as _date

    from app.db.models.research_task import ResearchTaskModel
    from app.repositories.research_task_repository import ResearchTaskRepository

    task_id = uuid4()
    async with sessionmaker() as session:
        await ResearchTaskRepository(session).create(
            ResearchTaskModel(
                task_id=task_id,
                company_query="600519",
                research_start_date=_date(2023, 1, 1),
                research_end_date=_date(2026, 12, 31),
                modules=["company_profile"],
                questions=[],
                require_plan_approval=False,
            )
        )
        await session.commit()
    return task_id


async def _run_stage4_to_result(env, monkeypatch, connection_uri) -> UUID:
    """跑一次完整 Stage 4 graph，返回 synthesis_result_id（reuse stage4 seed）。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    deps = _build_deps(env["sessionmaker"], _good_models())
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


async def _fetch_outline_row(sessionmaker, synthesis_result_id: UUID) -> dict | None:
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT outline_id, synthesis_result_id, company_id, "
                        "research_question_sha256, analysis_as_of, outline_schema_version, "
                        "outline_payload, outline_fingerprint "
                        "FROM report_outlines WHERE synthesis_result_id = :rid"
                    ).bindparams(rid=synthesis_result_id)
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


# ---------------------------------------------------------------- E2E


async def test_create_outline_from_stage4_result(env, monkeypatch, connection_uri) -> None:
    synthesis_result_id = await _run_stage4_to_result(env, monkeypatch, connection_uri)
    service = ReportOutlineService(env["sessionmaker"])

    outline = await service.create_or_get_outline(synthesis_result_id)

    # 派生字段。
    assert outline.synthesis_result_id == synthesis_result_id
    assert outline.company_id == env["company_id"]
    assert outline.analysis_as_of == _AS_OF
    assert outline.outline_schema_version == REPORT_OUTLINE_SCHEMA_VERSION
    assert len(outline.outline_fingerprint) == 64
    assert len(outline.research_question_sha256) == 64
    assert outline.replayed is False
    # _synthesis_output(5)：1 theme（覆盖全部 5 claims）+ 1 evidence_gap →
    # theme section + risks_and_gaps section。
    assert outline.section_count == 2

    row = await _fetch_outline_row(env["sessionmaker"], synthesis_result_id)
    assert row is not None
    assert row["outline_id"] == outline.outline_id
    assert row["outline_fingerprint"] == outline.outline_fingerprint
    assert row["research_question_sha256"] == outline.research_question_sha256
    assert row["analysis_as_of"] == _AS_OF

    sections = row["outline_payload"]["sections"]
    assert [s["section_type"] for s in sections] == ["theme", "risks_and_gaps"]
    assert [s["section_id"] for s in sections] == ["S1", "S2"]
    assert [s["section_order"] for s in sections] == [1, 2]
    theme = sections[0]
    assert theme["title"] == "多维度证据支持"  # theme label，不重写
    assert len(theme["claim_ids"]) == 5  # 全部 5 claims（无 duplicate）
    assert len(set(theme["claim_ids"])) == 5
    # risks_and_gaps：只存 indexes，不生成解释正文。
    gaps = sections[1]
    assert gaps["title"] == "风险、冲突与证据缺口"
    assert gaps["claim_ids"] == []
    assert gaps["conflict_indexes"] == []
    assert gaps["evidence_gap_indexes"] == [0]


async def test_create_outline_replay_same_row(env, monkeypatch, connection_uri) -> None:
    synthesis_result_id = await _run_stage4_to_result(env, monkeypatch, connection_uri)
    service = ReportOutlineService(env["sessionmaker"])

    first = await service.create_or_get_outline(synthesis_result_id)
    second = await service.create_or_get_outline(synthesis_result_id)

    assert second.outline_id == first.outline_id
    assert second.outline_fingerprint == first.outline_fingerprint
    assert second.replayed is True
    # 并发/重复输入 → 只有 1 个 Outline。
    async with env["sessionmaker"]() as session:
        count = int(
            (await session.execute(text("SELECT count(*) FROM report_outlines"))).scalar_one()
        )
    assert count == 1


# ---------------------------------------------------------------- missing / tamper


async def test_create_outline_missing_result_rejected(env) -> None:
    service = ReportOutlineService(env["sessionmaker"])
    with pytest.raises(SynthesisAnalysisResultNotFound):
        await service.create_or_get_outline(uuid4())


async def test_create_outline_rejects_tampered_result_fingerprint(
    env, monkeypatch, connection_uri
) -> None:
    synthesis_result_id = await _run_stage4_to_result(env, monkeypatch, connection_uri)
    # 篡改 result_fingerprint → verify_result_integrity 重算不符 → 拒绝。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE claim_synthesis_results SET result_fingerprint = :fp "
                "WHERE synthesis_result_id = :rid"
            ).bindparams(fp="f" * 64, rid=synthesis_result_id)
        )
        await session.commit()

    service = ReportOutlineService(env["sessionmaker"])
    with pytest.raises(SynthesisResultIntegrityError) as excinfo:
        await service.create_or_get_outline(synthesis_result_id)
    assert excinfo.value.code == "synthesis_result_integrity_error"
    # 拒绝派生：不产生 outline 行。
    assert await _fetch_outline_row(env["sessionmaker"], synthesis_result_id) is None


async def test_create_outline_rejects_tampered_result_schema_version(
    env, monkeypatch, connection_uri
) -> None:
    synthesis_result_id = await _run_stage4_to_result(env, monkeypatch, connection_uri)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE claim_synthesis_results SET result_schema_version = 999 "
                "WHERE synthesis_result_id = :rid"
            ).bindparams(rid=synthesis_result_id)
        )
        await session.commit()

    service = ReportOutlineService(env["sessionmaker"])
    with pytest.raises(SynthesisResultIntegrityError):
        await service.create_or_get_outline(synthesis_result_id)


# ------------------------------------------------ verify_outline_integrity (Stage 5B)


async def _create_outline(env, monkeypatch, connection_uri) -> UUID:
    """完整 Stage4 → result → outline，返回 outline_id。"""
    synthesis_result_id = await _run_stage4_to_result(env, monkeypatch, connection_uri)
    service = ReportOutlineService(env["sessionmaker"])
    outline = await service.create_or_get_outline(synthesis_result_id)
    return outline.outline_id


async def _tamper_outline(env, outline_id: UUID, sql: str, **params) -> None:
    async with env["sessionmaker"]() as session:
        await session.execute(text(sql).bindparams(**params))
        await session.commit()


async def test_verify_outline_integrity_valid_passes(env, monkeypatch, connection_uri) -> None:
    """有效 outline → verify_outline_integrity 返回 VerifiedReportOutline。"""
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    service = ReportOutlineService(env["sessionmaker"])

    verified = await service.verify_outline_integrity(outline_id)

    assert verified.outline_id == outline_id
    assert verified.outline_schema_version == REPORT_OUTLINE_SCHEMA_VERSION
    assert len(verified.outline_fingerprint) == 64
    assert len(verified.research_question_sha256) == 64
    assert verified.company_id == env["company_id"]
    # sections 从重派生 payload 解析：theme + risks_and_gaps。
    assert [s.section_type for s in verified.sections] == ["theme", "risks_and_gaps"]
    assert [s.section_order for s in verified.sections] == [1, 2]
    theme = verified.sections[0]
    assert theme.claim_ids  # 5 claims（无 duplicate）
    assert all(isinstance(cid, UUID) for cid in theme.claim_ids)
    gaps = verified.sections[1]
    assert gaps.claim_ids == ()
    assert gaps.conflict_indexes == ()
    assert gaps.evidence_gap_indexes == (0,)
    # 携带已验证上游结果（Writer 恢复 risks_and_gaps 用）。
    assert verified.verified_synthesis_result.synthesis_result_id is not None
    assert verified.verified_synthesis_result.output.themes


async def test_verify_outline_integrity_missing_rejected(env) -> None:
    service = ReportOutlineService(env["sessionmaker"])
    with pytest.raises(ReportOutlineNotFound):
        await service.verify_outline_integrity(uuid4())


async def test_verify_outline_integrity_rejects_tampered_payload(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    # SQL 篡改 outline_payload（标题改掉）→ 重派生对比不一致 → 拒绝。
    await _tamper_outline(
        env,
        outline_id,
        "UPDATE report_outlines SET outline_payload = CAST(:payload AS jsonb) "
        "WHERE outline_id = :oid",
        payload=json.dumps({"sections": [{"section_id": "S1", "title": "被篡改"}]}),
        oid=outline_id,
    )
    service = ReportOutlineService(env["sessionmaker"])
    with pytest.raises(ReportOutlineIntegrityError) as excinfo:
        await service.verify_outline_integrity(outline_id)
    assert excinfo.value.code == "report_outline_integrity_error"


async def test_verify_outline_integrity_rejects_tampered_company(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    # company_id 改为真实存在的 peer company（满足 FK）→ 重派生不一致 → 拒绝。
    peer_id = env["peer_company_ids"][0]
    await _tamper_outline(
        env,
        outline_id,
        "UPDATE report_outlines SET company_id = CAST(:cid AS uuid) WHERE outline_id = :oid",
        cid=peer_id,
        oid=outline_id,
    )
    service = ReportOutlineService(env["sessionmaker"])
    with pytest.raises(ReportOutlineIntegrityError):
        await service.verify_outline_integrity(outline_id)


async def test_verify_outline_integrity_rejects_tampered_cutoff(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    # analysis_as_of 篡改（cutoff 不是派生自 result 的值）→ 拒绝。
    await _tamper_outline(
        env,
        outline_id,
        "UPDATE report_outlines SET analysis_as_of = CAST(:asof AS date) WHERE outline_id = :oid",
        asof="2026-08-01",
        oid=outline_id,
    )
    service = ReportOutlineService(env["sessionmaker"])
    with pytest.raises(ReportOutlineIntegrityError):
        await service.verify_outline_integrity(outline_id)


async def test_verify_outline_integrity_rejects_tampered_fingerprint(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    await _tamper_outline(
        env,
        outline_id,
        "UPDATE report_outlines SET outline_fingerprint = :fp WHERE outline_id = :oid",
        fp="f" * 64,
        oid=outline_id,
    )
    service = ReportOutlineService(env["sessionmaker"])
    with pytest.raises(ReportOutlineIntegrityError):
        await service.verify_outline_integrity(outline_id)


async def test_verify_outline_integrity_rejects_tampered_upstream_result(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri)
    # 上游 SynthesisResult 被篡改（result_fingerprint）→ verify_result_integrity
    # 拒，verify_outline_integrity 原样传播 integrity 错误。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE claim_synthesis_results SET result_fingerprint = :fp "
                "WHERE synthesis_result_id = "
                "(SELECT synthesis_result_id FROM report_outlines WHERE outline_id = :oid)"
            ).bindparams(fp="f" * 64, oid=outline_id)
        )
        await session.commit()
    service = ReportOutlineService(env["sessionmaker"])
    with pytest.raises(SynthesisResultIntegrityError):
        await service.verify_outline_integrity(outline_id)
