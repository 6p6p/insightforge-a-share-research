"""P1.3 financial evidence recovery tests（纯函数 + 注入 fake 的 service 测试）。"""

import asyncio
from datetime import date, datetime
from uuid import uuid4

from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.financial.contracts import MetricCode, RawUnit
from app.rag.retrieval.contracts import RetrievalHit
from app.research_backflow.financial_recovery import (
    FinancialRecoveryService,
    build_recovery_card_draft,
    locate_metric_quote,
    resolve_alias_terms,
)


def _hit(text: str) -> RetrievalHit:
    return RetrievalHit(
        rank=1,
        chunk_id=uuid4(),
        chunk_set_id=uuid4(),
        parsed_source_id=uuid4(),
        source_id=uuid4(),
        company_id=uuid4(),
        text=text,
        distance=0.1,
        provider_key="sse",
        document_type="annual_report",
        source_title="2024 年年度报告",
        source_url=None,
        published_at=datetime(2025, 4, 30),
        reporting_period_end=date(2024, 12, 31),
        authority_tier=1,
        critical_claim_eligible=True,
        chunk_ordinal=0,
        locator_refs=[],
    )


class _FakeAliasModel:
    model_id = "fake-alias-model"

    def __init__(self, extra):
        self.extra = extra

    def generate_aliases(self, metric_label, period_label):
        return self.extra


class _FakeCardService:
    def __init__(self):
        self.calls = []
        self.card_id = uuid4()

    async def create_card(self, draft):
        self.calls.append(draft)
        return type("R", (), {"evidence_card_id": self.card_id, "replayed": False})()


class _FakeMetricService:
    def __init__(self):
        self.calls = []
        self.obs_id = uuid4()

    async def create_observation(self, draft):
        self.calls.append(draft)
        return type("R", (), {"metric_observation_id": self.obs_id, "replayed": False})()


class _FakeSessionMaker:
    def __call__(self):
        raise AssertionError("service 不应触碰真实 DB（no candidate）")

    def session_factory(self):
        return self


class _FakeRetrieval:
    def __init__(self, hits):
        self.hits = hits

    async def retrieve(self, query):
        return self.hits


def test_locate_quote_alias_number_unit():
    q = locate_metric_quote("本期营业收入 1,234.56 亿元，同比上升。", ["营业收入"])
    assert q is not None
    assert q.number_token == "1,234.56"
    assert q.raw_unit == RawUnit.HUNDRED_MILLION_YUAN
    assert q.alias == "营业收入"
    assert q.quote_text.count("1,234.56") == 1


def test_locate_quote_no_number_returns_none():
    assert locate_metric_quote("本期营业收入 同比上升。", ["营业收入"]) is None


def test_locate_quote_multiple_numbers_skipped():
    # alias 术语本身含数字 → quote 内多个数字 token → 不猜值，跳过。
    assert locate_metric_quote("2024年营业收入 100 亿元", ["2024年营业收入"]) is None


def test_resolve_alias_terms_deterministic_plus_model():
    terms = resolve_alias_terms(MetricCode.REVENUE, _FakeAliasModel(["营业收入", "主营业务收入"]))
    assert "营业收入" in terms
    assert "主营业务收入" in terms
    assert terms.count("营业收入") == 1  # 去重


def test_build_recovery_card_draft():
    q = locate_metric_quote("营业收入 88 亿元", ["营业收入"])
    draft = build_recovery_card_draft(
        research_question="营收情况？",
        chunk_id=uuid4(),
        quote=q,  # type: ignore[arg-type]
        model_id="fake-alias-model",
    )
    assert draft.evidence_type == EvidenceType.METRIC
    assert draft.extractor_name == "evidence_recovery"
    assert draft.extractor_confidence == EvidenceConfidence.MEDIUM
    assert draft.extractor_model_id == "fake-alias-model"
    assert draft.quote_end > draft.quote_start


def test_service_model_invented_number_rejected():
    # LLM alias 模型返回"编造数字"作为术语 → 已有来源文本不含它 → 不命中、不落卡。
    svc = FinancialRecoveryService(
        _FakeSessionMaker(),
        _FakeRetrieval([_hit("营业收入 100 亿元")]),
        model=_FakeAliasModel(["999999999999"]),
        card_service=_FakeCardService(),
        metric_service=_FakeMetricService(),
    )
    outcome = asyncio.run(
        svc.recover_metric(
            company_id=uuid4(),
            research_question="营收？",
            metric_code=MetricCode.REVENUE,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            alias_terms=["999999999999"],
        )
    )
    assert outcome.recovered is False
    assert outcome.reason == "no_valid_candidate"


def test_service_recover_metric_success_wiring():
    card_svc = _FakeCardService()
    metric_svc = _FakeMetricService()
    svc = FinancialRecoveryService(
        _FakeSessionMaker(),
        _FakeRetrieval([_hit("本期营业收入 88 亿元。")]),
        model=_FakeAliasModel([]),
        card_service=card_svc,
        metric_service=metric_svc,
    )
    outcome = asyncio.run(
        svc.recover_metric(
            company_id=uuid4(),
            research_question="营收？",
            metric_code=MetricCode.REVENUE,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            alias_terms=["营业收入"],
        )
    )
    assert outcome.recovered is True
    assert outcome.number == "88"
    assert outcome.raw_unit == RawUnit.HUNDRED_MILLION_YUAN.value
    assert outcome.evidence_card_id == card_svc.card_id
    assert outcome.metric_observation_id == metric_svc.obs_id
    assert len(card_svc.calls) == 1
    draft = card_svc.calls[0]
    slice_text = "本期营业收入 88 亿元。"[draft.quote_start : draft.quote_end]
    assert slice_text.count("88") == 1
    assert metric_svc.calls[0].metric_code == MetricCode.REVENUE
    assert metric_svc.calls[0].source_value_text == "88"
