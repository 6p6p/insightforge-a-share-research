"""Single RAG variant runner 单元测试（stage 7B.1.4C.1 spec R，11 cases）。

全部离线：fake rehydrator / parsing / chunking / answer model + monkeypatch
`VectorIndexService` / `RetrievalService`，0 DB / 0 LLM / 0 network / 0 Chroma。

覆盖 single_rag runner 的：
- input closure（macro / structured 不支持 → `EvalSingleRagInputError`，0 model call）；
- config ↔ spec binding（fingerprint mismatch → assembly error，0 model call）；
- prompt version 绑定（构造期校验）；
- normalize 的 hard 失败（unknown citation key / duplicate claim_id）；
- 归一化结构（citation source_fingerprint 由 application 映射 content_sha256，
  不取自模型；claim↔citation 双向闭合；usage observer 线程；恰好一次 model call）。
"""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.eval.bundle.loader import LoadedEvalExecutionCase
from app.eval.contracts import (
    EvalExecutionConfig,
    EvalExecutionSpec,
    FrozenCompanyIdentity,
    FrozenDocumentSourceRef,
    FrozenModelConfig,
    FrozenSourceSnapshot,
    FrozenStructuredArtifactRef,
    StructuredArtifactType,
)
from app.eval.errors import (
    EvalExecutionAssemblyError,
    EvalOutputStructureError,
    EvalSingleRagInputError,
)
from app.eval.fingerprints import compute_execution_config_fingerprint
from app.eval.replay.contracts import RehydratedCase, RehydratedDocument
from app.eval.usage.collector import EvalLlmUsageCollector
from app.eval.variants import EvalVariantId
from app.eval.variants.single_rag import (
    SINGLE_RAG_PROMPT_VERSION,
    SingleRagModelClaim,
    SingleRagModelOutput,
)
from app.eval.variants.single_rag.runner import SingleRagVariantRunner
from app.llm.instrumentation import LlmCallOutcome, LlmCallUsageRecord, UsageStatus
from app.rag.retrieval.contracts import RetrievalHit
from tests.eval.macro_factory import make_macro_ref

CASE_ID = "single-rag-case"
CASE_FP = "c" * 64
SNAP_FP = "d" * 64
UID = UUID("00000000-0000-0000-0000-000000000001")
SOURCE_RECORD_ID = UUID("00000000-0000-0000-0000-000000000002")
RAW_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000003")
CHUNK_ID = UUID("00000000-0000-0000-0000-000000000004")
CHUNK_SET_ID = UUID("00000000-0000-0000-0000-000000000005")
PARSED_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000006")
DOC_SHA = "a" * 64


# ---------------------------------------------------------------- 构建 helpers


def _make_config(**overrides) -> EvalExecutionConfig:
    kwargs = dict(
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
        retrieval_top_k=2,
    )
    kwargs.update(overrides)
    return EvalExecutionConfig(**kwargs)


def _doc_ref(content_sha256: str = DOC_SHA) -> FrozenDocumentSourceRef:
    return FrozenDocumentSourceRef(
        source_record_id=SOURCE_RECORD_ID,
        raw_artifact_id=RAW_ARTIFACT_ID,
        content_sha256=content_sha256,
        provider_key="cninfo",
        document_type="annual_report",
        media_type="application/pdf",
        title="测试文档",
        source_url="https://example.com/doc",
        acquired_at=datetime(2026, 8, 1, 12, 0, 0),
        authority_tier_snapshot=3,
        critical_claim_eligible_snapshot=True,
    )


def _company() -> FrozenCompanyIdentity:
    return FrozenCompanyIdentity(
        security_code="600519",
        official_name="测试公司",
        exchange="SSE",
        board="sse_main",
    )


def _snapshot(**overrides) -> FrozenSourceSnapshot:
    kwargs = dict(
        document_sources=(_doc_ref(),),
        macro_snapshots=(),
        structured_artifacts=(),
    )
    kwargs.update(overrides)
    return FrozenSourceSnapshot(**kwargs)


def _case(snapshot: FrozenSourceSnapshot | None = None) -> LoadedEvalExecutionCase:
    return LoadedEvalExecutionCase(
        case_fingerprint=CASE_FP,
        case_id=CASE_ID,
        case_version=1,
        company_id=UID,
        company=_company(),
        research_question="贵州茅台 2024 年营收增长如何？",
        analysis_as_of=datetime(2026, 8, 1, 12, 0, 0),
        tags=(),
        snapshot=snapshot if snapshot is not None else _snapshot(),
    )


def _spec(
    config: EvalExecutionConfig, *, config_fingerprint: str | None = None
) -> EvalExecutionSpec:
    return EvalExecutionSpec(
        case_fingerprint=CASE_FP,
        source_snapshot_fingerprint=SNAP_FP,
        execution_config_fingerprint=(
            config_fingerprint
            if config_fingerprint is not None
            else compute_execution_config_fingerprint(config)
        ),
        variant_id=EvalVariantId.SINGLE_RAG,
    )


def _rehydrated_doc() -> RehydratedDocument:
    return RehydratedDocument(
        source_record_id=SOURCE_RECORD_ID,
        raw_artifact_id=RAW_ARTIFACT_ID,
        content_sha256=DOC_SHA,
        storage_key="blobs/sha256/aa/" + DOC_SHA,
        byte_size=128,
        media_type="application/pdf",
    )


def _hit(*, text: str, chunk_ordinal: int, locator_dom: str) -> RetrievalHit:
    return RetrievalHit(
        rank=chunk_ordinal,
        chunk_id=CHUNK_ID,
        chunk_set_id=CHUNK_SET_ID,
        parsed_source_id=PARSED_SOURCE_ID,
        source_id=SOURCE_RECORD_ID,
        company_id=UID,
        text=text,
        distance=0.1,
        provider_key="cninfo",
        document_type="annual_report",
        source_title="测试文档",
        source_url="https://example.com/doc",
        published_at=None,
        reporting_period_end=None,
        authority_tier=3,
        critical_claim_eligible=True,
        chunk_ordinal=chunk_ordinal,
        locator_refs=[{"block_ordinal": chunk_ordinal, "locator": {"dom": locator_dom}}],
    )


def _two_hits() -> list[RetrievalHit]:
    return [
        _hit(text="营收同比增长 18%", chunk_ordinal=1, locator_dom="xpath1"),
        _hit(text="毛利率 55%", chunk_ordinal=2, locator_dom="xpath2"),
    ]


def _model_output() -> SingleRagModelOutput:
    return SingleRagModelOutput(
        final_text="2024 年营收增长，毛利率稳定。",
        claims=(
            SingleRagModelClaim(claim_id="C1", statement="营收同比增长 18%", citation_keys=("D1",)),
            SingleRagModelClaim(claim_id="C2", statement="毛利率 55%", citation_keys=("D1", "D2")),
        ),
    )


# ---------------------------------------------------------------- fakes


class _FakeAnswerModel:
    def __init__(self, output: SingleRagModelOutput, *, record_usage: bool = False) -> None:
        self._output = output
        self._record_usage = record_usage
        self.calls = 0
        self.seen_question = None
        self.seen_entries = None
        self.seen_observer = None

    async def answer(self, research_question, context_entries, *, usage_observer=None):
        self.calls += 1
        self.seen_question = research_question
        self.seen_entries = context_entries
        self.seen_observer = usage_observer
        if self._record_usage and usage_observer is not None:
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
        return self._output


class _FakeRehydrator:
    def __init__(self, documents=()) -> None:
        self._documents = tuple(documents)
        self.calls = 0

    async def rehydrate_case(self, case_id, case_version):
        self.calls += 1
        return RehydratedCase(company_id=UID, provider_keys=("cninfo",), documents=self._documents)


class _FakeParsing:
    def __init__(self) -> None:
        self.calls = 0

    async def parse_source(self, source_record_id):
        self.calls += 1
        return SimpleNamespace(parsed_source_id=PARSED_SOURCE_ID)


class _FakeChunking:
    def __init__(self) -> None:
        self.calls = 0

    async def chunk_parsed_source(self, parsed_source_id):
        self.calls += 1
        return SimpleNamespace(chunk_set_id=CHUNK_SET_ID)


class _FakeVectorIndexService:
    def __init__(self, sessionmaker, embedding, chroma, collection_name=None) -> None:
        self.collection_name = collection_name

    async def index_chunk_set(self, chunk_set_id) -> None:
        return None


class _FakeRetrievalService:
    def __init__(self, hits, collection_name=None) -> None:
        self._hits = list(hits)
        self.collection_name = collection_name
        self.seen_queries = []

    async def retrieve(self, query):
        self.seen_queries.append(query)
        return list(self._hits)


def _patch_rag(monkeypatch, hits):
    """monkeypatch runner 里的 VectorIndexService / RetrievalService，返回实例 holder。"""
    created: dict = {}

    def vec_factory(sessionmaker, embedding, chroma, collection_name=None):
        inst = _FakeVectorIndexService(sessionmaker, embedding, chroma, collection_name)
        created["vector"] = inst
        return inst

    def ret_factory(sessionmaker, embedding, chroma, collection_name=None):
        inst = _FakeRetrievalService(hits, collection_name)
        created["retrieval"] = inst
        return inst

    monkeypatch.setattr("app.eval.variants.single_rag.runner.VectorIndexService", vec_factory)
    monkeypatch.setattr("app.eval.variants.single_rag.runner.RetrievalService", ret_factory)
    return created


def _make_runner(monkeypatch, *, config, documents=(), hits=(), answer_model=None):
    rehydrator = _FakeRehydrator(documents=documents)
    parsing = _FakeParsing()
    chunking = _FakeChunking()
    if answer_model is None:
        answer_model = _FakeAnswerModel(output=_model_output())
    created = _patch_rag(monkeypatch, hits)
    runner = SingleRagVariantRunner(
        config=config,
        rehydrator=rehydrator,
        parsing_service=parsing,
        chunking_service=chunking,
        sessionmaker=None,
        embedding_provider=None,
        chroma=None,
        answer_model=answer_model,
    )
    return runner, rehydrator, parsing, chunking, answer_model, created


# ---------------------------------------------------------------- input closure


@pytest.mark.asyncio
async def test_macro_input_unsupported(monkeypatch) -> None:
    config = _make_config()
    snapshot = _snapshot(
        document_sources=(),
        macro_snapshots=(make_macro_ref(snapshot_fingerprint="e" * 64, payload_sha256="f" * 64),),
    )
    answer_model = _FakeAnswerModel(output=_model_output())
    runner, rehydrator, _, _, answer_model, _ = _make_runner(
        monkeypatch, config=config, answer_model=answer_model
    )
    with pytest.raises(EvalSingleRagInputError) as exc:
        await runner.run(_case(snapshot), _spec(config), usage_observer=None)
    assert exc.value.code == "single_rag_input_not_supported"
    assert answer_model.calls == 0
    assert rehydrator.calls == 0


@pytest.mark.asyncio
async def test_structured_input_unsupported(monkeypatch) -> None:
    config = _make_config()
    structured = FrozenStructuredArtifactRef(
        artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
        artifact_id=UID,
        artifact_fingerprint="e" * 64,
        payload_sha256="f" * 64,
    )
    snapshot = _snapshot(document_sources=(), structured_artifacts=(structured,))
    answer_model = _FakeAnswerModel(output=_model_output())
    runner, rehydrator, _, _, answer_model, _ = _make_runner(
        monkeypatch, config=config, answer_model=answer_model
    )
    with pytest.raises(EvalSingleRagInputError) as exc:
        await runner.run(_case(snapshot), _spec(config), usage_observer=None)
    assert exc.value.code == "single_rag_input_not_supported"
    assert answer_model.calls == 0
    assert rehydrator.calls == 0


# ---------------------------------------------------------------- config ↔ spec binding


@pytest.mark.asyncio
async def test_config_fingerprint_mismatch_zero_calls(monkeypatch) -> None:
    config = _make_config()
    answer_model = _FakeAnswerModel(output=_model_output())
    runner, rehydrator, _, _, answer_model, _ = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        hits=_two_hits(),
        answer_model=answer_model,
    )
    bad_spec = _spec(config, config_fingerprint="e" * 64)
    with pytest.raises(EvalExecutionAssemblyError):
        await runner.run(_case(), bad_spec, usage_observer=None)
    assert answer_model.calls == 0
    assert rehydrator.calls == 0


def test_wrong_prompt_version_rejected_at_construction(monkeypatch) -> None:
    config = _make_config(prompt_version="v2")
    with pytest.raises(EvalExecutionAssemblyError):
        _make_runner(monkeypatch, config=config)


# ---------------------------------------------------------------- normalize hard failure


@pytest.mark.asyncio
async def test_unknown_citation_key_fails(monkeypatch) -> None:
    config = _make_config()
    bad_output = SingleRagModelOutput(
        final_text="结论",
        claims=(SingleRagModelClaim(claim_id="C1", statement="x", citation_keys=("D99",)),),
    )
    answer_model = _FakeAnswerModel(output=bad_output)
    runner, _, _, _, answer_model, _ = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        hits=_two_hits(),
        answer_model=answer_model,
    )
    with pytest.raises(EvalOutputStructureError):
        await runner.run(_case(), _spec(config), usage_observer=None)
    assert answer_model.calls == 1


@pytest.mark.asyncio
async def test_duplicate_claim_id_fails(monkeypatch) -> None:
    config = _make_config()
    bad_output = SingleRagModelOutput(
        final_text="结论",
        claims=(
            SingleRagModelClaim(claim_id="C1", statement="x", citation_keys=("D1",)),
            SingleRagModelClaim(claim_id="C1", statement="y", citation_keys=("D1",)),
        ),
    )
    answer_model = _FakeAnswerModel(output=bad_output)
    runner, _, _, _, answer_model, _ = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        hits=_two_hits(),
        answer_model=answer_model,
    )
    with pytest.raises(EvalOutputStructureError):
        await runner.run(_case(), _spec(config), usage_observer=None)
    assert answer_model.calls == 1


# ---------------------------------------------------------------- normalization


@pytest.mark.asyncio
async def test_valid_output_normalized(monkeypatch) -> None:
    config = _make_config()
    answer_model = _FakeAnswerModel(output=_model_output())
    runner, _, _, _, answer_model, created = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        hits=_two_hits(),
        answer_model=answer_model,
    )
    output = await runner.run(_case(), _spec(config), usage_observer=None)

    assert output.variant_id == EvalVariantId.SINGLE_RAG
    assert output.case_id == CASE_ID
    assert output.case_version == 1
    assert output.final_text == "2024 年营收增长，毛利率稳定。"
    assert len(output.claims) == 2
    assert output.claims[0].claim_id == "C1"
    assert output.claims[0].claim_type == "fact"
    assert len(output.citations) == 2
    # 排序按 D1 < D2（_key_rank 稳定）。
    assert [c.citation_id for c in output.citations] == ["D1", "D2"]
    # 无 duplicate identity（verify_variant_output_identity 前提）。
    assert len({c.claim_id for c in output.claims}) == len(output.claims)
    assert len({c.citation_id for c in output.citations}) == len(output.citations)
    # retrieval 用 research_question 为唯一 query，source 白名单 + top_k 来自 config。
    query = created["retrieval"].seen_queries[0]
    assert query.query_text == "贵州茅台 2024 年营收增长如何？"
    assert query.top_k == 2
    assert query.source_ids == [SOURCE_RECORD_ID]
    # per-(config, case) collection 命名空间派生。
    assert created["vector"].collection_name.startswith("eval_single_rag_")


@pytest.mark.asyncio
async def test_citation_sha_from_application_not_model(monkeypatch) -> None:
    config = _make_config()
    answer_model = _FakeAnswerModel(output=_model_output())
    runner, _, _, _, answer_model, _ = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        hits=_two_hits(),
        answer_model=answer_model,
    )
    output = await runner.run(_case(), _spec(config), usage_observer=None)

    # citation source_fingerprint 必须等于 frozen content_sha256（application 映射），
    # 而不是模型给出的任何值（模型只能产出 D1/D2 短 key）。
    for citation in output.citations:
        assert citation.source_fingerprint == DOC_SHA
    # 模型收到的 context entries 不含 content_sha256（公平性边界）。
    for entry in answer_model.seen_entries:
        assert DOC_SHA not in entry.text
        assert DOC_SHA not in (entry.source_title or "")
        assert DOC_SHA not in (entry.locator or "")


@pytest.mark.asyncio
async def test_bidirectional_closure(monkeypatch) -> None:
    config = _make_config()
    runner, _, _, _, _, _ = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        hits=_two_hits(),
        answer_model=_FakeAnswerModel(output=_model_output()),
    )
    output = await runner.run(_case(), _spec(config), usage_observer=None)

    citation_ids = {c.citation_id for c in output.citations}
    claim_ids = {c.claim_id for c in output.claims}
    citation_by_id = {c.citation_id: c for c in output.citations}
    # claim → citation：每个 claim 的 citation_ids 都存在。
    for claim in output.claims:
        assert set(claim.citation_ids) <= citation_ids
    # citation → claim：每个 citation 的 claim_ids 都存在，且与 claim 侧互指。
    for citation in output.citations:
        assert set(citation.claim_ids) <= claim_ids
        for cid in citation.claim_ids:
            claim = next(c for c in output.claims if c.claim_id == cid)
            assert citation.citation_id in claim.citation_ids
    # 同一 key 被多个 claim 引用 → 合并进单条 citation（D1 被 C1、C2 引用）。
    assert citation_by_id["D1"].claim_ids == ("C1", "C2")


@pytest.mark.asyncio
async def test_usage_observer_passthrough(monkeypatch) -> None:
    config = _make_config()
    answer_model = _FakeAnswerModel(output=_model_output(), record_usage=True)
    collector = EvalLlmUsageCollector(
        execution_spec_fingerprint=SNAP_FP, variant_id=EvalVariantId.SINGLE_RAG, case_id=CASE_ID
    )
    runner, _, _, _, answer_model, _ = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        hits=_two_hits(),
        answer_model=answer_model,
    )
    await runner.run(_case(), _spec(config), usage_observer=collector)

    assert answer_model.seen_observer is collector
    records = collector.records()
    assert len(records) == 1
    assert records[0].component_name == "eval_single_rag_answer"


@pytest.mark.asyncio
async def test_exactly_one_model_call(monkeypatch) -> None:
    config = _make_config()
    answer_model = _FakeAnswerModel(output=_model_output())
    runner, _, _, _, answer_model, _ = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        hits=_two_hits(),
        answer_model=answer_model,
    )
    await runner.run(_case(), _spec(config), usage_observer=None)
    assert answer_model.calls == 1
