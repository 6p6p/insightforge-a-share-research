"""Single RAG variant runner integration E2E（stage 7B.1.4C.1 spec S）。

Frozen Bundle → EvaluationReplayRehydrator（隔离临时 PG + RawArtifactStore）→
SourceParsingService → ChunkingService → VectorIndexService（real Chroma）→
RetrievalService（PG hydrate）→ **一次** FakeSingleRagAnswerModel 生成 →
normalize `EvalVariantOutput`（经 `execute_variant_attempt` harness，含
usage collector + output fingerprint + wall latency）。

全程 FakeEmbeddingProvider + FakeSingleRagAnswerModel + 独立临时 PostgreSQL
（`insightforge_eval_single_rag_*`，alembic head → 0045）+ 独立 Chroma
collection（`eval_single_rag_<sha12>`，finally 删除）：**0 真实 DeepSeek / 0
network / 0 live provider**。需要真实 PostgreSQL（127.0.0.1:5433）且账号有
CREATEDB 权限。
"""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config

from alembic import command
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.contracts import EvalExecutionConfig, EvalExecutionSpec, FrozenModelConfig
from app.eval.execution.contracts import (
    EvalExecutionAttempt,
    EvalTrialSpec,
    ExecutionAttemptStatus,
    compute_trial_fingerprint,
)
from app.eval.execution.harness import execute_variant_attempt
from app.eval.fingerprints import (
    compute_execution_config_fingerprint,
    compute_execution_spec_fingerprint,
    compute_source_snapshot_fingerprint,
    compute_variant_output_fingerprint,
)
from app.eval.variants import EvalVariantId
from app.eval.variants.single_rag import (
    SINGLE_RAG_PROMPT_VERSION,
    SingleRagModelClaim,
    SingleRagModelOutput,
    create_single_rag_runner,
)
from app.llm.instrumentation import LlmCallOutcome, LlmCallUsageRecord, UsageStatus
from app.storage.raw_store import LocalRawArtifactStore
from app.vectorstore.client import ChromaManager
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.integration.replay_bundle import CASE_ID, CASE_VERSION, DOC_SHA256, build_replay_bundle

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


# ---------------------------------------------------------------- 临时 DB helpers


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


async def _upgrade_head() -> None:
    cfg = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.upgrade, cfg, "head")


@asynccontextmanager
async def _isolated_target(monkeypatch, tmp_path):
    """独立临时 PG（alembic head）+ 隔离 raw store；finally DROP + 恢复 settings。"""
    shared_url = get_settings().database_url
    temp_db = f"insightforge_eval_single_rag_{uuid4().hex[:12]}"
    temp_url = _temp_url(shared_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()

    iso_manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    iso_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    try:
        await _upgrade_head()
        yield iso_manager.session_factory(), iso_store
    finally:
        await iso_manager.dispose()
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------- fake answer model


class _SingleRagAnswerModel:
    """按真实检索上下文生成确定性输出（不手工构造 hit / citation key）。

    claim 引用第一个 context entry 的 key（真实检索返回），保证 citation 闭合；
    通过 usage_observer 记录一条 `eval_single_rag_answer` usage。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.seen_entries = None

    async def answer(self, research_question, context_entries, *, usage_observer=None):
        self.calls += 1
        self.seen_entries = context_entries
        if usage_observer is not None:
            await usage_observer.record(
                LlmCallUsageRecord(
                    component_name="eval_single_rag_answer",
                    provider="deepseek",
                    model_id="deepseek-chat",
                    outcome=LlmCallOutcome.SUCCESS,
                    duration_ms=1,
                    usage_status=UsageStatus.REPORTED,
                    input_tokens=10,
                    output_tokens=20,
                    total_tokens=30,
                )
            )
        claims = ()
        if context_entries:
            first_key = context_entries[0].key
            claims = (
                SingleRagModelClaim(
                    claim_id="C1",
                    statement="结论可追溯到给定检索上下文。",
                    citation_keys=(first_key,),
                ),
            )
        return SingleRagModelOutput(final_text="这是基于检索上下文的结论。", claims=claims)


def _make_config() -> EvalExecutionConfig:
    return EvalExecutionConfig(
        variant_id=EvalVariantId.SINGLE_RAG,
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-chat",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        ),
        variant_version="v1",
        prompt_version=SINGLE_RAG_PROMPT_VERSION,
        retrieval_version="v1",
        pipeline_version="v1",
        retrieval_top_k=3,
    )


def _expected_collection_name(config: EvalExecutionConfig, case_fingerprint: str) -> str:
    digest = hashlib.sha256(
        (compute_execution_config_fingerprint(config) + case_fingerprint).encode("utf-8")
    ).hexdigest()
    return f"eval_single_rag_{digest[:12]}"


async def _drop_collection(client, collection_name: str) -> None:
    try:
        await client.delete_collection(collection_name)
    except Exception:
        pass


# ---------------------------------------------------------------- E2E


async def test_single_rag_full_path_real_chain(monkeypatch, tmp_path) -> None:
    """frozen bundle → 隔离 rehydrate → parse → chunk → real Chroma index →
    retrieve → 一次 fake answer model → harness 收敛为 SUCCESS result。"""
    bundle_root = tmp_path / "bundle"
    build_replay_bundle(bundle_root)

    async with _isolated_target(monkeypatch, tmp_path) as (sessionmaker, raw_store):
        loader = EvaluationBundleLoader(bundle_root)
        execution_case = loader.load_execution_case(CASE_ID, CASE_VERSION)
        config = _make_config()
        execution_spec = EvalExecutionSpec(
            case_fingerprint=execution_case.case_fingerprint,
            source_snapshot_fingerprint=compute_source_snapshot_fingerprint(
                execution_case.snapshot
            ),
            execution_config_fingerprint=compute_execution_config_fingerprint(config),
            variant_id=EvalVariantId.SINGLE_RAG,
        )
        trial_spec = EvalTrialSpec(
            execution_spec_fingerprint=compute_execution_spec_fingerprint(execution_spec),
            trial_no=1,
        )
        attempt = EvalExecutionAttempt(
            trial_fingerprint=compute_trial_fingerprint(trial_spec),
            attempt_no=1,
            execution_id=uuid4(),
        )

        settings = get_settings()
        chroma = ChromaManager(
            host=settings.chroma_host,
            port=settings.chroma_port,
            ssl=settings.chroma_ssl,
            timeout_seconds=settings.chroma_timeout_seconds,
        )
        embedding = FakeEmbeddingProvider()
        answer_model = _SingleRagAnswerModel()
        runner = create_single_rag_runner(
            config=config,
            bundle_loader=loader,
            sessionmaker=sessionmaker,
            raw_store=raw_store,
            chroma=chroma,
            embedding_provider=embedding,
            answer_model=answer_model,
        )

        collection_name = _expected_collection_name(config, execution_case.case_fingerprint)
        client = await chroma.get_client()
        try:
            result = await execute_variant_attempt(
                attempt=attempt,
                trial_spec=trial_spec,
                execution_spec=execution_spec,
                execution_case=execution_case,
                runner=runner,
            )

            # (1) harness 收敛为 success。
            assert result.status == ExecutionAttemptStatus.SUCCESS
            assert result.error_code is None
            assert result.variant_id == EvalVariantId.SINGLE_RAG

            # (2) output 存在且 fingerprint 闭合。
            output = result.variant_output
            assert output is not None
            assert result.variant_output_fingerprint == compute_variant_output_fingerprint(output)
            assert output.variant_id == EvalVariantId.SINGLE_RAG
            assert output.case_id == CASE_ID
            assert output.case_version == CASE_VERSION

            # (3) citation 有效：source_fingerprint == frozen content_sha256（app 映射）。
            assert output.citations, "必须产出至少一条 citation"
            for citation in output.citations:
                assert citation.source_fingerprint == DOC_SHA256

            # (4) usage：fake answer model 经 observer 记录一条 usage。
            assert answer_model.calls == 1
            usage = [
                r for r in result.usage_records if r.component_name == "eval_single_rag_answer"
            ]
            assert len(usage) == 1

            # (5) wall latency 非负（harness perf_counter 度量）。
            assert isinstance(result.wall_latency_ms, int) and result.wall_latency_ms >= 0

            # (6) Chroma collection 只属于本次 attempt（per-(config, case) 派生名，
            #     不以 production 名复用），且已真实写入。
            collection = await client.get_collection(collection_name)
            assert collection is not None
            assert collection_name.startswith("eval_single_rag_")
        finally:
            await _drop_collection(client, collection_name)
