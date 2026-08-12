"""Research fulfillment 测试共享 helpers（stage 7A.2A spec R/S）。

executor 级 / service 级测试共用：FulfillmentContext / MissingResearchNeed /
SourceRouteEntry 构造、FakeRetrieval / FakeIndexBuilder、RetrievalHit 构造、
macro chain seed、financial observation 的 source evidence card seed。

全程 **0 真实 DeepSeek / 0 Retrieval / 0 Chroma / 0 Web**（document 路径注入
FakeRetrieval + FakeEvidenceExtractionModel；macro seed 用 MockTransport）。
"""

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from app.db.models.source_provider import SourceProviderModel
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
)
from app.evidence.extractor.contracts import (
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
)
from app.rag.retrieval.contracts import RetrievalHit, RetrievalQuery
from app.rag.retrieval.errors import RetrievalIndexNotReady
from app.repositories.source_provider_repository import SourceProviderRepository
from app.research_fulfillment.service import FulfillmentContext
from app.research_planning.preparation import MissingReasonCode, MissingResearchNeed
from app.research_planning.router import SourceRouteEntry, SourceRouteType
from app.services.evidence_card_service import EvidenceCardService

_QUESTION = "分析贵州茅台的经营质量、主要风险和估值水平。"
_AS_OF = date(2026, 8, 10)


# ---------------------------------------------------------------- context / need / entry


def _make_context(env: dict, *, payload=None) -> FulfillmentContext:
    """构造一个 executor 可直接消费的 FulfillmentContext（plan/task 身份随机）。"""
    return FulfillmentContext(
        research_plan_id=uuid4(),
        route_plan_id=uuid4(),
        company_id=env["company_id"],
        task_id=uuid4(),
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        payload=payload if payload is not None else _plan_payload_default(),
    )


def _make_need(
    need_code: str,
    need_kind: str = "document",
    *,
    reason: MissingReasonCode = MissingReasonCode.NOT_FOUND,
) -> MissingResearchNeed:
    return MissingResearchNeed(
        need_code=need_code,
        need_kind=need_kind,
        reason_code=reason,
        detail="测试用 missing need",
    )


def _make_entry(
    need_code: str,
    *,
    need_kind: str = "document",
    route_type: SourceRouteType = SourceRouteType.NEWS_ARTICLE,
    provider_keys: tuple[str, ...] = ("xinhuanet",),
) -> SourceRouteEntry:
    return SourceRouteEntry(
        need_code=need_code,
        need_kind=need_kind,
        route_type=route_type,
        expected_document_type=None,
        provider_keys=list(provider_keys),
    )


def _plan_payload_default():
    """合法 ResearchPlanPayload（复用 research_planning 的 _plan_payload）。"""
    from tests.integration.test_research_planning_service import _plan_payload

    return _plan_payload()


# ---------------------------------------------------------------- retrieval fakes


class _FakeRetrieval:
    """确定性检索替身：固定 hits / 抛 RetrievalIndexNotReady / 首调失败后成功。"""

    def __init__(self, hits=(), *, not_ready: bool = False, ready_after: bool = False) -> None:
        self._hits = list(hits)
        self._not_ready = not_ready
        self._ready_after = ready_after
        self.calls: list[RetrievalQuery] = []
        self._call_count = 0

    async def retrieve(self, query: RetrievalQuery):
        self.calls.append(query)
        self._call_count += 1
        if self._not_ready:
            raise RetrievalIndexNotReady()
        if self._ready_after and self._call_count == 1:
            raise RetrievalIndexNotReady()
        return list(self._hits)


class _FakeIndexBuilder:
    """替身 IndexBuilder：固定返回值 + 记录被请求补建索引的 source_id。"""

    def __init__(self, result: bool = True) -> None:
        self._result = result
        self.calls: list[UUID] = []

    async def ensure_indexed(self, source_id: UUID) -> bool:
        self.calls.append(source_id)
        return self._result


def _make_hit(
    env: dict,
    source_id: UUID,
    parsed_id: UUID,
    chunk,
    *,
    rank: int = 1,
    provider_key: str = "xinhuanet",
    document_type: str = "news_article",
    source_title: str = "新闻标题",
    source_url: str | None = None,
    published_at: datetime | None = None,
    reporting_period_end: date | None = None,
    authority_tier: int = 3,
    critical_claim_eligible: bool = False,
) -> RetrievalHit:
    """由 `_seed_html_source` 返回的真实 chunk 构造 RetrievalHit（provenance 与
    PG 一致 → EvidenceExtractionService 的 stale 校验通过）。"""
    return RetrievalHit(
        rank=rank,
        chunk_id=chunk.chunk_id,
        chunk_set_id=chunk.chunk_set_id,
        parsed_source_id=parsed_id,
        source_id=source_id,
        company_id=env["company_id"],
        text=chunk.text,
        distance=0.05,
        provider_key=provider_key,
        document_type=document_type,
        source_title=source_title,
        source_url=source_url,
        published_at=published_at if published_at is not None else datetime(2026, 8, 7, tzinfo=UTC),
        reporting_period_end=reporting_period_end,
        authority_tier=authority_tier,
        critical_claim_eligible=critical_claim_eligible,
        chunk_ordinal=1,
        locator_refs=[],
    )


def _unique_quote(text: str, quote_len: int) -> str:
    """取 chunk.text 的唯一精确子串（quote resolver 要求出现恰好 1 次）。

    测试 chunk 文本是固定重复字符（`_MULTI_HTML`），纯前缀/后缀切片会因多次出现
    抛 EvidenceExtractionQuoteAmbiguous；跨第一个字符变化边界取子串则唯一。
    """
    boundary = next((i for i in range(1, len(text)) if text[i] != text[i - 1]), 0)
    start = max(0, boundary - quote_len // 2)
    return text[start : start + quote_len]


def _decision_for_chunk(
    chunk,
    *,
    statement: str = "贵州茅台发布经营相关新闻。",
    quote_len: int = 20,
) -> EvidenceExtractionDecision:
    """确定性 extraction decision：quote 取 chunk.text 的唯一子串（可精确解析）。"""
    return EvidenceExtractionDecision(
        relevant=True,
        items=[
            EvidenceExtractionItem(
                evidence_statement=statement,
                evidence_type=EvidenceType.METRIC,
                quote_text=_unique_quote(chunk.text, quote_len),
                confidence=EvidenceConfidence.HIGH,
            )
        ],
    )


# ---------------------------------------------------------------- seed helpers


async def _seed_world_bank_provider(sessionmaker) -> None:
    """seed world_bank provider（macro_series FK 需要；seed_defaults 不含它）。"""
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(
            SourceProviderModel(
                provider_key="world_bank",
                display_name="World Bank Open Data",
                provider_type="international_organization",
                authority_tier=1,
                homepage_url="https://data.worldbank.org",
                allowed_domains=["worldbank.org"],
                capabilities=["macro_data", "document_download"],
                acquisition_methods=["official_api"],
                exchange_scope=[],
                requires_api_key=False,
                critical_claim_eligible=True,
                enabled=True,
            )
        )
        await session.commit()


async def _seed_evidence_card(
    env: dict,
    *,
    statement: str = "贵州茅台2024年营业收入相关披露。",
) -> UUID:
    """seed 一张 company_announcement EvidenceCard（financial observation 的
    source evidence card），并写入 env["evidence_card_id"]。"""
    from tests.integration.test_evidence_card_service import _seed_pdf_source

    _, _, _, chunks = await _seed_pdf_source(env)
    chunk = chunks[0]
    result = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement=statement,
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=0,
            quote_end=20,
            extractor_name="test-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    env["evidence_card_id"] = result.evidence_card_id
    return result.evidence_card_id


async def _seed_revenue_pair(env: dict) -> dict:
    """seed 2024 / 2023 全年营收 observation（依赖 env["evidence_card_id"]）。"""
    from tests.integration.test_financial_claim_service import _annual_revenue_pair

    return await _annual_revenue_pair(env)
