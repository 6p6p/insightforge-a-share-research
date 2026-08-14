"""Benchmark experiment reliability / fault-injection integration tests (stage 7B.1.4D/§23).

真实 PostgreSQL + 真实 Chroma（dataset 构建器 → 每 attempt 独立隔离 PG +
per-attempt collection），验证 `_run_single_attempt` 的可靠性不变量：
1. success 路径：attempt SUCCESS + 执行/评分持久化 + 环境恢复 + collection 清理；
2. 故障注入：answer model 抛错 → harness 稳定 fallback error code，且**环境恢复 +
   collection 清理**仍然执行（finally 语义）；
3. honest fail-fast：macro/structured 输入 → 稳定 error code，0 model call。

全程 0 真实 DeepSeek（fake bundles）。dataset 构建器在 module 级 fixture 中跑一次。
"""

import asyncio
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.eval.benchmark import experiment as exp_mod
from app.eval.benchmark.dataset import build_benchmark_dataset
from app.eval.benchmark.experiment import _run_single_attempt
from app.eval.benchmark.fakes import FakeSingleRagAnswerModel
from app.eval.variants import EvalVariantId
from app.vectorstore.client import ChromaManager

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


@pytest.fixture(scope="module")
def dataset_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("bench_dataset") / "dataset"
    asyncio.run(build_benchmark_dataset(root))
    return root


async def _collection_names() -> set[str]:
    settings = get_settings()
    manager = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    client = await manager.get_client()
    collections = await client.list_collections()
    return {collection.name for collection in collections}


async def _run_attempt(dataset_root: Path, case_id: str, variant_id: EvalVariantId, workdir: Path):
    return await _run_single_attempt(
        dataset_root=dataset_root,
        case_id=case_id,
        variant_id=variant_id,
        attempt_no=1,
        mode="fake",
        workdir=workdir,
    )


async def test_success_path_persists_and_cleans_up(dataset_root, tmp_path) -> None:
    before = await _collection_names()
    record = await _run_attempt(
        dataset_root, "moutai-business", EvalVariantId.SINGLE_RAG, tmp_path
    )
    assert record.status == "success", record.error_code
    assert record.persisted is True
    assert record.citation_validity is not None and record.citation_coverage is not None
    assert record.variant_output_fingerprint is not None
    # 环境恢复：settings 重新解析回共享 DB URL。
    assert "insightforge_eval_bench_" not in get_settings().database_url
    # collection 清理：attempt 结束后无新增 collection。
    after = await _collection_names()
    assert after == before


async def test_fault_injection_runner_raise_cleans_up(dataset_root, tmp_path, monkeypatch) -> None:
    """answer model 抛错 → harness 稳定 fallback error code；finally 清理仍执行。"""
    before = await _collection_names()

    def _broken_fake(config, observer):
        model = FakeSingleRagAnswerModel(
            provider=config.model.provider,
            model_id=config.model.model_id,
            observer=observer,
        )

        async def answer(self, research_question, context_entries, *, usage_observer=None):
            raise RuntimeError("injected provider failure")

        model.answer = answer.__get__(model)  # type: ignore[method-assign]
        return model

    monkeypatch.setattr(exp_mod, "create_single_rag_fake_answer", _broken_fake)
    record = await _run_attempt(
        dataset_root, "moutai-business", EvalVariantId.SINGLE_RAG, tmp_path
    )

    assert record.status == "failed"
    assert record.error_code == "eval_variant_execution_error"  # 稳定 fallback code
    assert record.persisted is False
    assert "insightforge_eval_bench_" not in get_settings().database_url
    after = await _collection_names()
    assert after == before


async def test_fail_fast_honest_contract(dataset_root, tmp_path) -> None:
    """macro/structured 输入 → 稳定 error code（0 model call，不误报成功）。"""
    record = await _run_attempt(dataset_root, "moutai-full", EvalVariantId.SINGLE_RAG, tmp_path)
    assert record.status == "failed"
    assert record.error_code == "single_rag_input_not_supported"
    assert record.expected_fail_fast is True
    assert record.usage_call_count == 0
