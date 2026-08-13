"""Single RAG variant runner (stage 7B.1.4C.1).

Frozen Bundle → rehydrate → parse → chunk → index → retrieve → **一次** LLM 生成 →
normalize `EvalVariantOutput`。

**禁止**：EvidenceExtractionService / Claim / Financial / Macro Analyst /
Synthesis / Writer / Audit / Revision——single_rag 只有一次 RAG 生成。

隔离不变量：
- rehydration 只落在隔离 target PG + store（`EvaluationReplayRehydrator`）；
- derived index 用 per-attempt 命名空间 collection（绑定 `EvalVariantRuntimeContext`
  `.execution_id`），**不**写回 bundle、**不**复用生产
  `insightforge_chunks_v2_*` collection；不同 attempt / variant 的 collection
  互相不可见；
- 检索只在当前 frozen snapshot 的 document source 内（`source_ids` 白名单），
  `research_question` 是唯一 query。
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.eval.bundle.loader import LoadedEvalExecutionCase
from app.eval.contracts import (
    EvalCitation,
    EvalClaim,
    EvalExecutionConfig,
    EvalExecutionSpec,
    EvalVariantOutput,
)
from app.eval.errors import (
    EvalExecutionAssemblyError,
    EvalOutputStructureError,
    EvalSingleRagInputError,
)
from app.eval.execution.contracts import EvalVariantRuntimeContext
from app.eval.fingerprints import compute_execution_config_fingerprint
from app.eval.replay.rehydrator import EvaluationReplayRehydrator
from app.eval.variants import EvalVariantId
from app.eval.variants.single_rag.contracts import (
    CITATION_KEY_PREFIX,
    SINGLE_RAG_CLAIM_TYPE,
    SINGLE_RAG_PROMPT_VERSION,
    SingleRagAnswerModel,
    SingleRagContextEntry,
)
from app.llm.instrumentation import LlmUsageObserver
from app.rag.embedding.contracts import BGE_SMALL_ZH_V1_5, EmbeddingProvider
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.contracts import DEFAULT_TOP_K, RetrievalQuery
from app.rag.retrieval.service import RetrievalService
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.vectorstore.client import ChromaManager


class SingleRagVariantRunner:
    variant_id = EvalVariantId.SINGLE_RAG

    def __init__(
        self,
        *,
        config: EvalExecutionConfig,
        rehydrator: EvaluationReplayRehydrator,
        parsing_service: SourceParsingService,
        chunking_service: ChunkingService,
        sessionmaker: async_sessionmaker,
        embedding_provider: EmbeddingProvider,
        chroma: ChromaManager,
        answer_model: SingleRagAnswerModel,
    ) -> None:
        # 构造期绑定 config；variant / prompt version 不匹配 = 装配错误（0 model call）。
        if config.variant_id != self.variant_id:
            raise EvalExecutionAssemblyError("config.variant_id 不是 single_rag")
        if config.prompt_version != SINGLE_RAG_PROMPT_VERSION:
            raise EvalExecutionAssemblyError(
                f"config.prompt_version {config.prompt_version!r} != {SINGLE_RAG_PROMPT_VERSION!r}"
            )
        self._config = config
        self._rehydrator = rehydrator
        self._parsing = parsing_service
        self._chunking = chunking_service
        self._sessionmaker = sessionmaker
        self._embedding = embedding_provider
        self._chroma = chroma
        self._answer_model = answer_model

    async def run(
        self,
        execution_case: LoadedEvalExecutionCase,
        execution_spec: EvalExecutionSpec,
        *,
        runtime_context: EvalVariantRuntimeContext,
        usage_observer: LlmUsageObserver | None,
    ) -> EvalVariantOutput:
        # 1. config ↔ spec 绑定（assembly，0 model call）。
        self._validate_spec(execution_spec)
        # 2. 模型身份 preflight：注入 answer model + embedding provider 必须匹配
        #    frozen config / 冻结 BGE 模型（0 model call）。
        self._validate_model_identity()
        self._validate_embedding_identity()
        # 3. input closure（document-only，0 model call）。
        self._validate_input(execution_case)

        # 4. rehydrate：frozen bundle → 隔离 PG + store。
        rehydrated = await self._rehydrator.rehydrate_case(
            execution_case.case_id, execution_case.case_version
        )
        if not rehydrated.documents:
            raise EvalSingleRagInputError("rehydration 未产出任何 document source")

        # 5. per-attempt collection 命名空间（绑定 execution_id；派生索引，不写回
        #    bundle，不同 execution_id 的 attempt 互相不可见）。
        collection_name = self._collection_name(runtime_context.execution_id)
        index_service = VectorIndexService(
            self._sessionmaker, self._embedding, self._chroma, collection_name=collection_name
        )
        retrieval_service = RetrievalService(
            self._sessionmaker, self._embedding, self._chroma, collection_name=collection_name
        )

        # 6. parse → chunk → index（每条 frozen document 走真实 deterministic pipeline）。
        #    parse/chunk 是幂等 create-or-get（跨 attempt 复用同一 ChunkSet），因此
        #    index 必须 force_rebuild：把 manifest 重置并重建进本 attempt 自己的
        #    collection，避免 ready replay 校验到空 collection。
        for doc in rehydrated.documents:
            parsed = await self._parsing.parse_source(doc.source_record_id)
            chunked = await self._chunking.chunk_parsed_source(parsed.parsed_source_id)
            await index_service.index_chunk_set(chunked.chunk_set_id, force_rebuild=True)

        # 7. retrieve：question 唯一 query，top_k 来自 frozen config，限定当前快照。
        source_ids = [doc.source_record_id for doc in rehydrated.documents]
        top_k = (
            self._config.retrieval_top_k
            if self._config.retrieval_top_k is not None
            else DEFAULT_TOP_K
        )
        hits = await retrieval_service.retrieve(
            RetrievalQuery(
                company_id=execution_case.company_id,
                query_text=execution_case.research_question,
                top_k=top_k,
                source_ids=source_ids,
            )
        )

        # 8. 组装 context entries + key → (content_sha256, locator) 映射（不进 prompt）。
        sha_by_source = {doc.source_record_id: doc.content_sha256 for doc in rehydrated.documents}
        context_entries: list[SingleRagContextEntry] = []
        key_to_sha: dict[str, str] = {}
        key_to_locator: dict[str, str | None] = {}
        for rank, hit in enumerate(hits, start=1):
            if hit.source_id not in sha_by_source:
                raise EvalOutputStructureError("retrieval hit 的 source 不在 frozen snapshot")
            key = f"{CITATION_KEY_PREFIX}{rank}"
            locator = _locator_from_hit(hit)
            context_entries.append(
                SingleRagContextEntry(
                    key=key,
                    text=hit.text,
                    source_title=hit.source_title,
                    locator=locator,
                )
            )
            key_to_sha[key] = sha_by_source[hit.source_id]
            key_to_locator[key] = locator

        # 9. 恰好一次 LLM 生成（usage_observer 线程到 adapter）。
        model_output = await self._answer_model.answer(
            execution_case.research_question,
            tuple(context_entries),
            usage_observer=usage_observer,
        )

        # 10. 归一化为 EvalVariantOutput（citation source_fingerprint 由 application
        #     映射 content_sha256，不取自模型）。
        return self._normalize(model_output, execution_case, key_to_sha, key_to_locator)

    # ------------------------------------------------------------------ 内部

    def _validate_spec(self, execution_spec: EvalExecutionSpec) -> None:
        if execution_spec.variant_id != self.variant_id:
            raise EvalExecutionAssemblyError("execution_spec.variant_id 不是 single_rag")
        if (
            compute_execution_config_fingerprint(self._config)
            != execution_spec.execution_config_fingerprint
        ):
            raise EvalExecutionAssemblyError(
                "execution_config_fingerprint 与 runner 绑定 config 不一致"
            )

    def _validate_model_identity(self) -> None:
        """模型身份 preflight：注入 answer model 必须等于 frozen config（0 model call）。

        确保同一个 execution_config 下不会注入错误 provider / model_id 的模型；
        不一致 = benchmark assembly corruption，抛 `EvalExecutionAssemblyError`。
        """
        config_model = self._config.model
        if self._answer_model.provider != config_model.provider:
            raise EvalExecutionAssemblyError(
                f"answer_model.provider {self._answer_model.provider!r} != "
                f"config {config_model.provider!r}"
            )
        if self._answer_model.model_id != config_model.model_id:
            raise EvalExecutionAssemblyError(
                f"answer_model.model_id {self._answer_model.model_id!r} != "
                f"config {config_model.model_id!r}"
            )

    def _validate_embedding_identity(self) -> None:
        """Embedding 模型身份 preflight（0 model call）。

        single_rag 的 embedding 层冻结为 BGE：注入 provider 的 `model_info`
        （model_id + immutable revision）必须等于 `BGE_SMALL_ZH_V1_5`。这防止同一个
        `execution_config_fingerprint` 下悄悄更换 embedding 模型——embedding 变化会
        改变 retrieval 结果，却不改变 config 指纹，使同 fingerprint 的 trial 不可比。
        `revision is None`（模型未配置）在 assembly 期 fail fast（index 层虽同样
        拒绝，但这里在 rehydration 之前失败更早）。未来更换 / 升级 BGE 必须同步
        bump config（retrieval_version / component_versions）以改变指纹。
        """
        spec = self._embedding.model_info
        frozen = BGE_SMALL_ZH_V1_5
        if spec.model_id != frozen.model_id:
            raise EvalExecutionAssemblyError(
                f"embedding model_id {spec.model_id!r} != frozen {frozen.model_id!r}"
            )
        if spec.revision is None:
            raise EvalExecutionAssemblyError(
                "embedding model 未配置 immutable revision（revision is None）"
            )
        if spec.revision != frozen.revision:
            raise EvalExecutionAssemblyError(
                f"embedding revision {spec.revision!r} != frozen {frozen.revision!r}"
            )

    @staticmethod
    def _validate_input(execution_case: LoadedEvalExecutionCase) -> None:
        snapshot = execution_case.snapshot
        if not snapshot.document_sources:
            raise EvalSingleRagInputError("single_rag v1 需要 >=1 条 document source")
        if snapshot.macro_snapshots:
            raise EvalSingleRagInputError("single_rag v1 不支持 macro 输入")
        if snapshot.structured_artifacts:
            raise EvalSingleRagInputError("single_rag v1 不支持 structured 输入")

    @staticmethod
    def _collection_name(execution_id: UUID) -> str:
        # 派生索引命名空间绑定 execution_id：同一 case/config/spec 的不同 attempt
        # （不同 trial 或不同 retry）必须各有独立 collection。execution_id.hex 是
        # 稳定 deterministic encoding，长度 32 在 Chroma collection 名限制内。
        return f"eval_single_rag_{execution_id.hex}"

    def _normalize(
        self,
        model_output,
        execution_case: LoadedEvalExecutionCase,
        key_to_sha: dict[str, str],
        key_to_locator: dict[str, str | None],
    ) -> EvalVariantOutput:
        claim_ids = [c.claim_id for c in model_output.claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise EvalOutputStructureError("duplicate claim_id")

        claims: list[EvalClaim] = []
        citation_claim_ids: dict[str, list[str]] = {}
        for model_claim in model_output.claims:
            for key in model_claim.citation_keys:
                if key not in key_to_sha:
                    raise EvalOutputStructureError(f"unknown citation_key: {key}")
            claims.append(
                EvalClaim(
                    claim_id=model_claim.claim_id,
                    statement=model_claim.statement,
                    claim_type=SINGLE_RAG_CLAIM_TYPE,
                    citation_ids=model_claim.citation_keys,
                )
            )
            for key in model_claim.citation_keys:
                citation_claim_ids.setdefault(key, []).append(model_claim.claim_id)

        citations: list[EvalCitation] = []
        for key in sorted(citation_claim_ids, key=_key_rank):
            citations.append(
                EvalCitation(
                    citation_id=key,
                    source_fingerprint=key_to_sha[key],
                    locator=key_to_locator.get(key),
                    claim_ids=tuple(citation_claim_ids[key]),
                )
            )

        return EvalVariantOutput(
            variant_id=self.variant_id,
            case_id=execution_case.case_id,
            case_version=execution_case.case_version,
            final_text=model_output.final_text,
            claims=tuple(claims),
            citations=tuple(citations),
        )


def _key_rank(key: str) -> int:
    """`D{rank}` → 数值 rank（用于 citation 稳定排序；非 D-key 回退 0）。"""
    if key.startswith(CITATION_KEY_PREFIX) and key[len(CITATION_KEY_PREFIX) :].isdigit():
        return int(key[len(CITATION_KEY_PREFIX) :])
    return 0


def _locator_from_hit(hit) -> str | None:
    """从真实检索 chunk 的 locator_refs 派生稳定 locator（block/page 定位）。"""
    refs = hit.locator_refs or []
    if not refs:
        return None
    first = refs[0] if isinstance(refs[0], dict) else {}
    locator = first.get("locator")
    if isinstance(locator, dict) and locator:
        return json.dumps(locator, sort_keys=True, ensure_ascii=False)
    return f"block {first.get('block_ordinal')}"
