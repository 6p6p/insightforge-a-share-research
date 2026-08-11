"""Integration test: migration 0038 research backflow downgrade guard (stage 5E.2B).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0038：

- (B) **空表通过**：无数据 → `alembic downgrade 0038 -> 0037` 成功、版本回到
  0037、`research_backflow_requests` / `research_backflow_fulfillments` 两表均被删；
- (A) **非空拒绝**：`research_backflow_requests` / `research_backflow_fulfillments`
  存在行 → `alembic downgrade 0038 -> 0037` 必须拒绝（RuntimeError），
  `alembic_version` 仍为 0038；request / fulfillment 行数据完整保留（Backflow 是
  正式 immutable research artifact——记录裁决后的一次研究交接 / 交接兑现，即使可
  确定性重放，也不在 downgrade 时静默删除历史）。

company / evidence / claim / synthesis run / input link / result / outline /
report / check / audit / issue 复用既有 guard 测试 helpers 直接 SQL seed；
research task / workflow run / review action / backflow request / fulfillment 行
直接 SQL 插入（满足全部 CHECK：fingerprints / sha256 用 64 hex，payload / ids 为
合法 JSONB，status / route / action_type / request_schema_version 合法）。

全程不触碰主库 `insightforge`，finally 恢复 settings 缓存并 DROP 临时库。
需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB 权限。
"""

import asyncio
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.db.session import DatabaseManager
from tests.integration.test_migration_0026_downgrade_guard import _seed_chain
from tests.integration.test_migration_0027_downgrade_guard import _seed_claim
from tests.integration.test_migration_0028_downgrade_guard import (
    _hex64,
    _seed_link,
    _seed_run,
)
from tests.integration.test_migration_0032_downgrade_guard import (
    _seed_outline,
    _seed_result,
)
from tests.integration.test_migration_0034_downgrade_guard import (
    _seed_check_result,
    _seed_report,
)
from tests.integration.test_migration_0035_downgrade_guard import (
    _seed_audit,
    _seed_issue,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_REQUESTS = "research_backflow_requests"
_FULFILLMENTS = "research_backflow_fulfillments"


def _parse_db_url(url: str) -> dict:
    parsed = urlparse(url.replace("+psycopg", "", 1))
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": parsed.path.lstrip("/"),
    }


def _admin_conn(db_name: str) -> psycopg.Connection:
    parts = _parse_db_url(get_settings().database_url)
    return psycopg.connect(
        host=parts["host"],
        port=parts["port"],
        user=parts["user"],
        password=parts["password"],
        dbname=db_name,
        autocommit=True,
    )


def _create_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')


def _drop_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _temp_url(base: str, db_name: str) -> str:
    return base.rsplit("/", 1)[0] + f"/{db_name}"


async def _version(temp_url: str) -> str:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return str(
                (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
            )
    finally:
        await manager.dispose()


async def _table_count(temp_url: str, table: str) -> int:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
    finally:
        await manager.dispose()


async def _table_exists(temp_url: str, table: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            exists = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name = :name"
                    ).bindparams(name=table)
                )
            ).scalar_one()
            return int(exists) == 1
    finally:
        await manager.dispose()


async def _seed_research_task(temp_url: str) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 research_tasks 行。"""
    task_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO research_tasks "
                    "(task_id, company_query, research_start_date, research_end_date, "
                    " modules, questions, include_relative_valuation, "
                    " require_plan_approval, status, current_stage, progress) "
                    "VALUES (CAST(:tid AS uuid), '贵州茅台 600519', "
                    " CAST(:start AS date), CAST(:end AS date), "
                    " CAST(:modules AS jsonb), CAST(:questions AS jsonb), "
                    " false, false, 'completed', 'exporting', 100)"
                ).bindparams(
                    tid=task_id,
                    start=date(2026, 7, 1),
                    end=date(2026, 8, 1),
                    modules=json.dumps(["report"], ensure_ascii=False),
                    questions=json.dumps(["2024年贵州茅台净利润增长情况？"], ensure_ascii=False),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return task_id


async def _seed_workflow_run(temp_url: str, task_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 workflow_runs 行（stage5_report）。"""
    run_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(run_id, task_id, thread_id, graph_name, graph_version, status) "
                    "VALUES (CAST(:rid AS uuid), CAST(:tid AS uuid), "
                    " :thread_id, 'stage5_report', '1', 'completed')"
                ).bindparams(rid=run_id, tid=task_id, thread_id=str(run_id))
            )
            await session.commit()
    finally:
        await manager.dispose()
    return run_id


async def _seed_review_action(temp_url: str, audit_id: UUID, report_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 report_review_actions 行（research）。"""
    action_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO report_review_actions "
                    "(review_action_id, audit_id, report_id, action_schema_version, "
                    " action_type, action_payload, action_fingerprint) "
                    "VALUES (CAST(:aid AS uuid), CAST(:audit AS uuid), "
                    " CAST(:report AS uuid), 1, 'research', "
                    " CAST(:payload AS jsonb), :fp)"
                ).bindparams(
                    aid=action_id,
                    audit=audit_id,
                    report=report_id,
                    payload=json.dumps(
                        {
                            "review_issue_ids": [str(uuid4())],
                            "target_section_ids": ["S1"],
                            "related_claim_ids": [],
                            "related_evidence_card_ids": [],
                            "research_need_codes": ["missing_support"],
                        },
                        ensure_ascii=False,
                    ),
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return action_id


async def _seed_backflow_request(
    temp_url: str,
    *,
    run_id: UUID,
    action_id: UUID,
    report_id: UUID,
    company_id: UUID,
) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 research_backflow_requests 行。"""
    request_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO research_backflow_requests "
                    "(research_request_id, source_stage5_run_id, review_action_id, "
                    " human_decision_id, source_report_id, company_id, "
                    " research_question_sha256, analysis_as_of, request_schema_version, "
                    " request_payload, request_fingerprint) "
                    "VALUES (CAST(:req AS uuid), CAST(:run AS uuid), "
                    " CAST(:action AS uuid), NULL, CAST(:report AS uuid), "
                    " CAST(:company AS uuid), :sha, CAST(:asof AS date), 1, "
                    " CAST(:payload AS jsonb), :fp)"
                ).bindparams(
                    req=request_id,
                    run=run_id,
                    action=action_id,
                    report=report_id,
                    company=company_id,
                    sha=_hex64(),
                    asof=date(2026, 8, 10),
                    payload=json.dumps(
                        {
                            "review_issue_ids": [str(uuid4())],
                            "target_section_ids": ["S1"],
                            "related_claim_ids": [],
                            "related_evidence_card_ids": [],
                            "research_need_codes": ["missing_support"],
                        },
                        ensure_ascii=False,
                    ),
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return request_id


async def _seed_backflow_fulfillment(temp_url: str, request_id: UUID, result_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 research_backflow_fulfillments 行。"""
    fulfillment_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO research_backflow_fulfillments "
                    "(fulfillment_id, research_request_id, new_synthesis_result_id, "
                    " fulfillment_schema_version, fulfillment_fingerprint) "
                    "VALUES (CAST(:fid AS uuid), CAST(:req AS uuid), "
                    " CAST(:result AS uuid), 1, :fp)"
                ).bindparams(
                    fid=fulfillment_id,
                    req=request_id,
                    result=result_id,
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return fulfillment_id


async def _seed_full_backflow_chain(temp_url: str, raw_root: Path) -> dict:
    """seed 整条 Source→…→Audit→ReviewAction→WorkflowRun→Backflow→Fulfillment 链。

    返回 {company_id, run_id, action_id, report_id, request_id, fulfillment_id,
    result_id, audit_id}（全部 UUID 对象）。
    """
    chain = await _seed_chain(temp_url, raw_root / "raw", "600519")
    company_id = UUID(chain["company_id"])
    claim_id = await _seed_claim(temp_url, chain)
    synthesis_id = await _seed_run(temp_url, company_id)
    await _seed_link(temp_url, synthesis_id, claim_id)
    result_id = await _seed_result(temp_url, synthesis_id)
    outline_id = await _seed_outline(temp_url, result_id, company_id)
    report_id = await _seed_report(temp_url, outline_id, company_id)
    check_id = await _seed_check_result(temp_url, report_id)
    audit_id = await _seed_audit(temp_url, report_id, check_id)
    await _seed_issue(temp_url, audit_id)

    task_id = await _seed_research_task(temp_url)
    run_id = await _seed_workflow_run(temp_url, task_id)
    action_id = await _seed_review_action(temp_url, audit_id, report_id)
    request_id = await _seed_backflow_request(
        temp_url,
        run_id=run_id,
        action_id=action_id,
        report_id=report_id,
        company_id=company_id,
    )
    fulfillment_id = await _seed_backflow_fulfillment(temp_url, request_id, result_id)
    return {
        "company_id": company_id,
        "run_id": run_id,
        "action_id": action_id,
        "report_id": report_id,
        "request_id": request_id,
        "fulfillment_id": fulfillment_id,
        "result_id": result_id,
        "audit_id": audit_id,
    }


@pytest.mark.asyncio
async def test_migration_0038_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0038 upgrade 建两表；无数据 → downgrade 0038→0037 成功，两表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0038")
        assert await _version(temp_url) == "0038"
        assert await _table_exists(temp_url, _REQUESTS) is True
        assert await _table_exists(temp_url, _FULFILLMENTS) is True
        assert await _table_count(temp_url, _REQUESTS) == 0
        assert await _table_count(temp_url, _FULFILLMENTS) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0037")
        assert await _version(temp_url) == "0037"
        assert await _table_exists(temp_url, _REQUESTS) is False
        assert await _table_exists(temp_url, _FULFILLMENTS) is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0038_downgrade_blocked_with_backflow(monkeypatch, tmp_path) -> None:
    """(A) request + fulfillment 存在行 → 拒绝 downgrade；数据完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0038")
        seeded = await _seed_full_backflow_chain(temp_url, tmp_path)
        assert await _table_count(temp_url, _REQUESTS) == 1
        assert await _table_count(temp_url, _FULFILLMENTS) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0038"):
            await asyncio.to_thread(command.downgrade, cfg, "0037")

        assert await _version(temp_url) == "0038"
        assert await _table_count(temp_url, _REQUESTS) == 1
        assert await _table_count(temp_url, _FULFILLMENTS) == 1
        # Backflow 行未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                req_fp = (
                    await session.execute(
                        text(
                            "SELECT request_fingerprint FROM research_backflow_requests "
                            "WHERE research_request_id = :rid"
                        ).bindparams(rid=seeded["request_id"])
                    )
                ).scalar_one()
                assert len(req_fp) == 64
                fulfillment_result = (
                    await session.execute(
                        text(
                            "SELECT new_synthesis_result_id FROM research_backflow_fulfillments "
                            "WHERE fulfillment_id = :fid"
                        ).bindparams(fid=seeded["fulfillment_id"])
                    )
                ).scalar_one()
                assert fulfillment_result == seeded["result_id"]
                # 上游 artifact 数据保留。
                audits = int(
                    (await session.execute(text("SELECT count(*) FROM report_audits"))).scalar_one()
                )
                assert audits == 1
                runs = int(
                    (await session.execute(text("SELECT count(*) FROM workflow_runs"))).scalar_one()
                )
                assert runs == 1
                actions = int(
                    (
                        await session.execute(text("SELECT count(*) FROM report_review_actions"))
                    ).scalar_one()
                )
                assert actions == 1
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
