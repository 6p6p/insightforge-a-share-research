"""EvidenceExtractionService unit tests (stage 3C.2, no DB / no network).

用 FakeEvidenceExtractionModel + 记录型 card_service stub，monkeypatch
`_load_fresh_chunk`（PG 短读）与 `_assert_not_stale`（纯 stale 校验）。
覆盖 spec 12 的语义侧：relevant=false / one / three / malformed / quote
not found / ambiguous / duplicate / type-confidence 映射 / model id 写入 /
stale 拒绝 / 确定性 start-end / 重跑 replay 计数 / authority 不参与。
"""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
from app.evidence.extractor.contracts import (
    EVIDENCE_EXTRACTOR_NAME,
    EVIDENCE_EXTRACTOR_VERSION,
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
    EvidenceExtractionReason,
)
from app.evidence.extractor.errors import (
    EvidenceExtractionInputError,
    EvidenceExtractionInputStale,
    EvidenceExtractionMalformedOutput,
    EvidenceExtractionQuoteAmbiguous,
    EvidenceExtractionQuoteNotFound,
    EvidenceExtractorUnavailable,
)
from app.evidence.extractor.service import EvidenceExtractionService, _FreshChunk
from app.rag.retrieval.contracts import RetrievalHit
from app.services.evidence_card_service import EvidenceCardResult
from tests.evidence.fakes import FakeEvidenceExtractionModel

pytestmark = pytest.mark.asyncio

_QUESTION = "公司2025年营业收入？"
_CHUNK_TEXT = "公司2025年营业收入为100亿元，同比增长12%；归属净利润862亿元。"


def _hit(text=_CHUNK_TEXT, **overrides) -> RetrievalHit:
    base = dict(
        rank=1,
        chunk_id=uuid4(),
        chunk_set_id=uuid4(),
        parsed_source_id=uuid4(),
        source_id=uuid4(),
        company_id=uuid4(),
        text=text,
        distance=0.1,
        provider_key="xinhuanet",
        document_type="news_article",
        source_title="标题",
        source_url=None,
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        reporting_period_end=None,
        authority_tier=3,
        critical_claim_eligible=False,
        chunk_ordinal=1,
        locator_refs=[],
    )
    base.update(overrides)
    return RetrievalHit(**base)


def _item(
    statement="营业收入为100亿元",
    quote="营业收入为100亿元",
    confidence="high",
) -> EvidenceExtractionItem:
    return EvidenceExtractionItem(
        evidence_statement=statement,
        evidence_type=EvidenceType.METRIC,
        quote_text=quote,
        confidence=EvidenceConfidence(confidence),
    )


def _dummy_sessionmaker():
    class _Never:
        def __call__(self):
            raise AssertionError("sessionmaker 不应在 service 单元测试中被使用")

    return _Never()


class _RecordingCardService:
    """记录收到的 drafts，返回可配置的 EvidenceCardResult（unit test stub）。"""

    def __init__(self, replayed: bool = False) -> None:
        self.drafts: list[EvidenceCardDraft] = []
        self._replayed = replayed

    async def create_card(self, draft: EvidenceCardDraft) -> EvidenceCardResult:
        self.drafts.append(draft)
        return EvidenceCardResult(
            evidence_card_id=uuid4(),
            chunk_id=draft.chunk_id,
            evidence_fingerprint="0" * 64,
            replayed=self._replayed,
        )


def _service(fake, *, card_service=None) -> EvidenceExtractionService:
    return EvidenceExtractionService(
        sessionmaker=_dummy_sessionmaker(),
        model=fake,
        card_service=card_service or _RecordingCardService(),
    )


def _install_fresh(service, monkeypatch, *, text=None) -> None:
    """把 `_load_fresh_chunk` 替换为纯内存加载（PG 短读由集成测试覆盖）。"""

    async def _load(hit: RetrievalHit) -> _FreshChunk:
        t = hit.text if text is None else text
        return _FreshChunk(
            chunk_id=hit.chunk_id,
            chunk_set_id=hit.chunk_set_id,
            parsed_source_id=hit.parsed_source_id,
            source_id=hit.source_id,
            company_id=hit.company_id,
            text=t,
            text_sha256=hashlib.sha256(t.encode("utf-8")).hexdigest(),
        )

    monkeypatch.setattr(service, "_load_fresh_chunk", _load)


# ---------------------------------------------------------------- relevant=false


async def test_relevant_false_returns_no_evidence_zero_writes(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=False,
            items=[],
            reason_code=EvidenceExtractionReason.NOT_RELEVANT,
        )
    )
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)

    result = await service.extract_from_hit(_QUESTION, _hit())
    assert result.relevant is False
    assert result.evidence_card_ids == []
    assert result.created_count == 0
    assert result.replayed_count == 0
    assert result.reason_code == EvidenceExtractionReason.NOT_RELEVANT
    assert card.drafts == []  # DB 0 写


# ---------------------------------------------------------------- single / multi


async def test_single_evidence_builds_card_with_deterministic_span(monkeypatch) -> None:
    hit = _hit()
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(relevant=True, items=[_item()])
    )
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)

    result = await service.extract_from_hit(f"  {_QUESTION}  ", hit)
    assert result.relevant is True
    assert result.created_count == 1
    assert len(card.drafts) == 1

    draft = card.drafts[0]
    start = hit.text.index("营业收入为100亿元")
    assert draft.quote_start == start
    assert draft.quote_end == start + len("营业收入为100亿元")
    assert draft.evidence_statement == "营业收入为100亿元"
    assert draft.evidence_type is EvidenceType.METRIC
    assert draft.extractor_confidence is EvidenceConfidence.HIGH
    assert draft.extractor_name == EVIDENCE_EXTRACTOR_NAME
    assert draft.extractor_version == EVIDENCE_EXTRACTOR_VERSION
    assert draft.extractor_model_id == "fake/structured-llm@1"
    assert draft.chunk_id == hit.chunk_id
    assert draft.research_question == _QUESTION  # trim 后传给 model 与 draft


async def test_model_receives_trimmed_question(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(relevant=True, items=[_item()])
    )
    service = _service(fake)
    _install_fresh(service, monkeypatch)
    await service.extract_from_hit("  毛 台  ", _hit())
    assert fake.calls[0][0] == "毛 台"


async def test_three_evidence_builds_three_cards(monkeypatch) -> None:
    items = [
        _item(statement="收入", quote="营业收入为100亿元"),
        _item(statement="增长", quote="同比增长12%"),
        _item(statement="净利", quote="归属净利润862亿元"),
    ]
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(relevant=True, items=items)
    )
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)

    result = await service.extract_from_hit(_QUESTION, _hit())
    assert result.created_count == 3
    assert len(result.evidence_card_ids) == 3
    assert len(card.drafts) == 3
    assert card.drafts[0].quote_start < card.drafts[2].quote_start


async def test_rerun_replays_counts_replayed(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(relevant=True, items=[_item()])
    )
    card = _RecordingCardService(replayed=True)  # 模拟 3C.1 replay 返回
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)

    result = await service.extract_from_hit(_QUESTION, _hit())
    assert result.created_count == 0
    assert result.replayed_count == 1


# ---------------------------------------------------------------- malformed / errors


async def test_relevant_true_with_zero_items_yields_no_cards(monkeypatch) -> None:
    """v3（V1.1 closure）：relevant=true + items=[] 合法 → 无卡创建（相关但无原子证据）。"""
    fake = FakeEvidenceExtractionModel(decision={"relevant": True, "items": []})
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)
    result = await service.extract_from_hit(_QUESTION, _hit())
    assert result.relevant is True
    assert result.created_count == 0
    assert result.replayed_count == 0
    assert card.drafts == []


async def test_none_output_raises_malformed(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(decision=None)
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)
    with pytest.raises(EvidenceExtractionMalformedOutput):
        await service.extract_from_hit(_QUESTION, _hit())
    assert card.drafts == []


async def test_reason_code_on_relevant_result_rejected(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(
        decision={"relevant": True, "items": [_item()], "reason_code": "not_relevant"}
    )
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)
    with pytest.raises(EvidenceExtractionMalformedOutput):
        await service.extract_from_hit(_QUESTION, _hit())
    assert card.drafts == []


async def test_duplicate_item_rejected(monkeypatch) -> None:
    item = _item()
    fake = FakeEvidenceExtractionModel(decision={"relevant": True, "items": [item, item]})
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)
    with pytest.raises(EvidenceExtractionMalformedOutput):
        await service.extract_from_hit(_QUESTION, _hit())
    assert card.drafts == []


async def test_quote_not_found_raises_and_no_cards(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(relevant=True, items=[_item(quote="不存在的文本")])
    )
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)
    with pytest.raises(EvidenceExtractionQuoteNotFound):
        await service.extract_from_hit(_QUESTION, _hit())
    assert card.drafts == []


async def test_quote_ambiguous_raises_and_no_cards(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(relevant=True, items=[_item(quote="重复")])
    )
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    hit = _hit(text="重复。重复。重复。")
    _install_fresh(service, monkeypatch)
    with pytest.raises(EvidenceExtractionQuoteAmbiguous):
        await service.extract_from_hit(_QUESTION, hit)
    assert card.drafts == []


async def test_model_unavailable_propagates_and_no_cards(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(error=EvidenceExtractorUnavailable)
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch)
    with pytest.raises(EvidenceExtractorUnavailable):
        await service.extract_from_hit(_QUESTION, _hit())
    assert card.drafts == []


# ---------------------------------------------------------------- stale guard


async def test_stale_provenance_rejected_before_model_and_persistence(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(relevant=True, items=[_item()])
    )
    card = _RecordingCardService()
    service = _service(fake, card_service=card)

    async def _stale(_hit: RetrievalHit) -> _FreshChunk:
        raise EvidenceExtractionInputStale()

    monkeypatch.setattr(service, "_load_fresh_chunk", _stale)
    with pytest.raises(EvidenceExtractionInputStale):
        await service.extract_from_hit(_QUESTION, _hit())
    assert fake.calls == []  # LLM 未被调用（短 DB read 阶段即拒绝）
    assert card.drafts == []


async def test_assert_not_stale_hash_mismatch() -> None:
    hit = _hit(text="原文")
    fresh = _FreshChunk(
        chunk_id=hit.chunk_id,
        chunk_set_id=hit.chunk_set_id,
        parsed_source_id=hit.parsed_source_id,
        source_id=hit.source_id,
        company_id=hit.company_id,
        text="原文",
        text_sha256=hashlib.sha256("别的文本".encode()).hexdigest(),
    )
    with pytest.raises(EvidenceExtractionInputStale):
        EvidenceExtractionService._assert_not_stale(hit, fresh)


async def test_assert_not_stale_id_mismatch() -> None:
    hit = _hit(text="原文")
    fresh = _FreshChunk(
        chunk_id=hit.chunk_id,
        chunk_set_id=hit.chunk_set_id,
        parsed_source_id=hit.parsed_source_id,
        source_id=hit.source_id,
        company_id=uuid4(),  # 公司不匹配 → stale
        text="原文",
        text_sha256=hashlib.sha256("原文".encode()).hexdigest(),
    )
    with pytest.raises(EvidenceExtractionInputStale):
        EvidenceExtractionService._assert_not_stale(hit, fresh)


async def test_assert_not_stale_matching_passes() -> None:
    hit = _hit(text="原文")
    fresh = _FreshChunk(
        chunk_id=hit.chunk_id,
        chunk_set_id=hit.chunk_set_id,
        parsed_source_id=hit.parsed_source_id,
        source_id=hit.source_id,
        company_id=hit.company_id,
        text="原文",
        text_sha256=hashlib.sha256("原文".encode()).hexdigest(),
    )
    EvidenceExtractionService._assert_not_stale(hit, fresh)  # 不应抛错


async def test_fresh_pg_text_is_used_for_quote_resolution(monkeypatch) -> None:
    # quote 解析以 PG 短读返回的 fresh.text 为准，而不是 hit.text（防御 stale）。
    hit = _hit(text="stale-text")
    fresh_text = _CHUNK_TEXT
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(relevant=True, items=[_item(quote="营业收入为100亿元")])
    )
    card = _RecordingCardService()
    service = _service(fake, card_service=card)
    _install_fresh(service, monkeypatch, text=fresh_text)

    await service.extract_from_hit(_QUESTION, hit)
    start = fresh_text.index("营业收入为100亿元")
    assert card.drafts[0].quote_start == start
    assert card.drafts[0].quote_end == start + len("营业收入为100亿元")


# ---------------------------------------------------------------- input / config


async def test_blank_research_question_rejected_before_model_call(monkeypatch) -> None:
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(relevant=True, items=[_item()])
    )
    service = _service(fake)
    with pytest.raises(EvidenceExtractionInputError):
        await service.extract_from_hit("   ", _hit())
    assert fake.calls == []  # LLM 未被调用


async def test_model_without_model_id_rejected() -> None:
    class _NoId:
        async def extract(self, research_question, retrieval_hit):
            raise AssertionError("not used")

    with pytest.raises(EvidenceExtractorUnavailable):
        EvidenceExtractionService(sessionmaker=_dummy_sessionmaker(), model=_NoId())


async def test_model_without_extract_rejected() -> None:
    class _NoExtract:
        model_id = "m/1"

    with pytest.raises(EvidenceExtractorUnavailable):
        EvidenceExtractionService(sessionmaker=_dummy_sessionmaker(), model=_NoExtract())
