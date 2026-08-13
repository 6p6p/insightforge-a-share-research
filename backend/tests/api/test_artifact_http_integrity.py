"""Gate0-E/F web artifact final correctness HTTP acceptance tests.

真实 PostgreSQL + 真实 lifespan（`app.router.lifespan_context`）+ httpx
ASGITransport，全程**零真实 DeepSeek**（verify 是 read-only 重放，模型全程
lazy，构造不调 API）。完整 Stage4→Stage5→approve→completed 链之后验证：

**Gate0-E（完整性）**：
1. canonical SynthesisResult tamper → `GET /analysis` 409 `task_artifact_integrity`；
2. Report tamper → `GET /report` 409；
3. Audit tamper → `GET /reviews` 409；
4. 统一信封 `{error:{code,message,request_id}}`，**不泄漏** traceback / SQL /
   原始异常（response 全文不含 psycopg / sqlalchemy / Traceback 等关键字）；
5. artifact 缺失任务（无 run）→ 三端点仍 200 空 / null（完整性错误 ≠ 缺失）。

**Gate0-F（0-LLM HTTP 读证明）**：生产 DI（`create_stage5_dependencies`）装配的
Writer / Auditor / Revision 模型与 DeepSeek 客户端全部替换为哨兵——任何属性访问
（除只读 `model_id`）或客户端构造 → `AssertionError`。全部 5 个 GET 必须成功，
证明 artifact 读路径**绝不初始化 LLM 客户端**。

HTTP 断言落在服务层之后：`TaskArtifactIntegrityError`（DomainError, 409）→
`domain_error_handler` → `ErrorEnvelope`。tamper 用原始 SQL 直接改产物行
（结果不可变 → 篡改 = 完整性失败，**不 repair / 不降级为空**）。
"""

from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.main import create_app
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.services.source_registry_service import SourceRegistryService
from app.stage5.contracts import STAGE5_GRAPH_NAME
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import _seed_research_task
from tests.integration.test_task_artifact_workspace import _run_full_chain_to_completed
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

# envelope 全文不得出现的泄漏关键字（traceback / SQL 驱动 / 原始异常类型）。
_LEAK_KEYWORDS = (
    "Traceback",
    "psycopg",
    "sqlalchemy",
    "ProgrammingError",
    "IntegrityError",
    "SELECT ",
    "UPDATE ",
    "INSERT ",
)


class _NoLLMSentinel:
    """Gate0-F 哨兵：替换生产 Writer / Auditor / Revision 模型。

    只暴露只读 `model_id`（verify 身份校验需要）；**任何其他**属性访问
    （`write` / `audit` / `rewrite` / `analyze` / `with_structured_output` …
    或打开 DeepSeek 客户端）→ `AssertionError`。证明 artifact GET 读路径绝不
    触碰 LLM 模型，只消费 read-only 重放。
    """

    def __init__(self, settings, usage_observer=None) -> None:
        self._settings = settings
        self._model_id = f"{settings.llm_provider}:{settings.llm_model}"

    @property
    def model_id(self) -> str:
        return self._model_id

    def __getattr__(self, name: str):
        raise AssertionError(f"artifact GET must not use LLM model: {name}")


def _forbid_client(*args, **kwargs):
    """Gate0-F 哨兵：任何 DeepSeek 客户端（ChatDeepSeek）构造 → AssertionError。"""
    raise AssertionError("artifact GET must not initialize a DeepSeek client")


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
async def env(tmp_path, sessionmaker) -> dict:
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


async def _read_stage5_state(sessionmaker, manager, task_id: UUID) -> dict:
    """读最新 Stage5 checkpoint（synthesis_result_id / report_id / audit_id）。

    与 `TaskArtifactService._read_state` 同一裸 checkpointer 读取路径
    （thread_id==run_id，不 build graph → 0 LLM）。
    """
    async with sessionmaker() as session:
        runs = await WorkflowRunRepository(session).list_for_task_by_graph(
            task_id, STAGE5_GRAPH_NAME
        )
        assert runs, "expect a completed Stage5 run"
        thread_id = runs[0].thread_id
    checkpointer = await manager.get_checkpointer()
    snapshot = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    assert snapshot is not None, "Stage5 checkpoint missing"
    return dict((snapshot.checkpoint or {}).get("channel_values") or {})


async def _tamper_fingerprint(sessionmaker, sql: str, rid: UUID) -> None:
    """直接把产物 fingerprint 篡改为 `0*64`（不可变产物被改 → 完整性失败）。"""
    async with sessionmaker() as session:
        await session.execute(text(sql).bindparams(rid=rid, tampered="0" * 64))
        await session.commit()


async def _fingerprint(sessionmaker, table: str, fp_column: str, pk_column: str, rid: UUID) -> str:
    """读取当前 fingerprint（测试自恢复 tamper 用，恢复 = 改回原值）。"""
    async with sessionmaker() as session:
        return (
            await session.execute(
                text(f"SELECT {fp_column} FROM {table} WHERE {pk_column} = :rid").bindparams(
                    rid=rid
                )
            )
        ).scalar_one()


async def _restore_fingerprint(sessionmaker, sql: str, rid: UUID, original: str) -> None:
    """把 fingerprint 改回原值（撤销测试自身的 tamper，非应用修复）。"""
    async with sessionmaker() as session:
        await session.execute(text(sql).bindparams(rid=rid, original=original))
        await session.commit()


def _assert_integrity_409(response: httpx.Response) -> None:
    """409 + 统一信封 + 无 traceback/SQL/原始异常泄漏。"""
    assert response.status_code == 409, response.text
    body = response.json()
    error = body["error"]
    assert error["code"] == "task_artifact_integrity"
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["request_id"], str) and error["request_id"]
    # envelope 只含 code/message/request_id；任一泄漏关键字出现即失败。
    for keyword in _LEAK_KEYWORDS:
        assert keyword not in response.text, f"response leaked {keyword!r}: {response.text}"


# ---------------------------------------------------------------- Gate0-E acceptance


async def test_artifact_integrity_http_409(env, monkeypatch, connection_uri) -> None:
    """篡改 canonical SynthesisResult / Report / Audit → 对应 GET 409 + 全信封。"""
    app = create_app(get_settings())
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    base = f"/api/v1/tasks/{task_id}"

    async with app.router.lifespan_context(app):
        # 先跑完真实 Stage4→Stage5→approve→completed 链（HTTP 外的 service 路径）。
        manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
        try:
            state = await _read_stage5_state(sessionmaker, manager, task_id)
            synthesis_result_id = UUID(state["synthesis_result_id"])
            report_id = UUID(state["report_id"])
            audit_id = UUID(state["audit_id"])
        finally:
            await manager.close()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # ---- baseline：未篡改全部 200 ----
            assert (await client.get(f"{base}/analysis")).status_code == 200
            assert (await client.get(f"{base}/report")).status_code == 200
            assert (await client.get(f"{base}/reviews")).status_code == 200

            # 记录原值（每场景 tamper 后自恢复，测试各自隔离）。
            synthesis_original = await _fingerprint(
                sessionmaker,
                "claim_synthesis_results",
                "result_fingerprint",
                "synthesis_result_id",
                synthesis_result_id,
            )
            report_original = await _fingerprint(
                sessionmaker,
                "reports",
                "report_fingerprint",
                "report_id",
                report_id,
            )
            audit_original = await _fingerprint(
                sessionmaker,
                "report_audits",
                "audit_fingerprint",
                "audit_id",
                audit_id,
            )
            restore_synthesis = (
                "UPDATE claim_synthesis_results SET result_fingerprint = :original "
                "WHERE synthesis_result_id = :rid"
            )
            restore_report = (
                "UPDATE reports SET report_fingerprint = :original WHERE report_id = :rid"
            )
            restore_audit = (
                "UPDATE report_audits SET audit_fingerprint = :original WHERE audit_id = :rid"
            )

            # ---- 1. canonical SynthesisResult tamper → GET /analysis 409 ----
            await _tamper_fingerprint(
                sessionmaker,
                "UPDATE claim_synthesis_results SET result_fingerprint = :tampered "
                "WHERE synthesis_result_id = :rid",
                synthesis_result_id,
            )
            _assert_integrity_409(await client.get(f"{base}/analysis"))
            # 根 synthesis 损坏 → report / reviews 的 verify 链（outline →
            # verify_result_integrity）同样重建失败 → 一律 409，**不是 500**。
            _assert_integrity_409(await client.get(f"{base}/report"))
            _assert_integrity_409(await client.get(f"{base}/reviews"))
            # 撤销 tamper → 读路径恢复 200（verify 只读重放，不 repair）。
            await _restore_fingerprint(
                sessionmaker, restore_synthesis, synthesis_result_id, synthesis_original
            )
            assert (await client.get(f"{base}/analysis")).status_code == 200

            # ---- 2. Audit tamper → GET /reviews 409 ----
            await _tamper_fingerprint(
                sessionmaker,
                "UPDATE report_audits SET audit_fingerprint = :tampered WHERE audit_id = :rid",
                audit_id,
            )
            _assert_integrity_409(await client.get(f"{base}/reviews"))
            # audit 损坏不影响 report（report verify 不依赖 audit）。
            assert (await client.get(f"{base}/report")).status_code == 200
            await _restore_fingerprint(sessionmaker, restore_audit, audit_id, audit_original)
            assert (await client.get(f"{base}/reviews")).status_code == 200

            # ---- 3. Report tamper → GET /report 409 ----
            await _tamper_fingerprint(
                sessionmaker,
                "UPDATE reports SET report_fingerprint = :tampered WHERE report_id = :rid",
                report_id,
            )
            _assert_integrity_409(await client.get(f"{base}/report"))
            await _restore_fingerprint(sessionmaker, restore_report, report_id, report_original)
            assert (await client.get(f"{base}/report")).status_code == 200

            # ---- 4. artifact 缺失任务（无 run）→ 200 空 / null ----
            empty_task_id = await _seed_research_task(sessionmaker)
            empty_base = f"/api/v1/tasks/{empty_task_id}"
            analysis = await client.get(f"{empty_base}/analysis")
            assert analysis.status_code == 200
            assert analysis.json()["work_items"] == []
            assert analysis.json()["claims"] == []
            assert analysis.json()["synthesis_id"] is None
            report = await client.get(f"{empty_base}/report")
            assert report.status_code == 200
            assert report.json()["report_id"] is None
            reviews = await client.get(f"{empty_base}/reviews")
            assert reviews.status_code == 200
            assert reviews.json()["audit_id"] is None


# ---------------------------------------------------------------- Gate0-F: 0-LLM HTTP read


async def test_http_read_only_zero_llm(env, monkeypatch, connection_uri) -> None:
    """Gate0-F：0-LLM HTTP 读证明——生产 DI 模型全部换哨兵，5 个 GET 全部成功。

    `create_stage5_dependencies` 在函数体内 import 三个 model 入口
    （`create_draft_section_model` / `create_revision_writer_model` /
    `DeepSeekAuditModel`），patch 后生产 DI 仍走真实装配链，但 Writer /
    Auditor / Revision 模型与 `ChatDeepSeek` 客户端任何触碰都会抛
    `AssertionError` → GET 若初始化 LLM 客户端必然 500，测试即失败。
    """
    monkeypatch.setattr(
        "app.draft_section.factory.create_draft_section_model",
        lambda settings, usage_observer=None: _NoLLMSentinel(settings),
    )
    monkeypatch.setattr(
        "app.revision.factory.create_revision_writer_model",
        lambda settings, usage_observer=None: _NoLLMSentinel(settings),
    )
    monkeypatch.setattr("app.audit.adapters.DeepSeekAuditModel", _NoLLMSentinel)
    monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", _forbid_client)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    app = create_app(get_settings())
    task_id = env["task_id"]
    base = f"/api/v1/tasks/{task_id}"

    async with app.router.lifespan_context(app):
        manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
        await manager.close()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            sources = await client.get(f"{base}/sources", params={"limit": 100})
            assert sources.status_code == 200, sources.text
            assert sources.json()["total"] > 0

            evidence = await client.get(f"{base}/evidence", params={"limit": 100})
            assert evidence.status_code == 200, evidence.text
            assert evidence.json()["total"] > 0

            analysis = await client.get(f"{base}/analysis")
            assert analysis.status_code == 200, analysis.text
            assert analysis.json()["synthesis_result_id"] is not None

            report = await client.get(f"{base}/report")
            assert report.status_code == 200, report.text
            assert report.json()["report_id"] is not None

            reviews = await client.get(f"{base}/reviews")
            assert reviews.status_code == 200, reviews.text
            assert reviews.json()["audit_id"] is not None
