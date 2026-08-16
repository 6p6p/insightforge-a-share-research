"""Financial auto extraction foundation unit tests (P3).

- numeric provenance 校验：quote 逐字性 / 数字唯一 token / period 规则 /
  metric_code 支持列表；
- validate_extraction_batch 部分拒绝语义；
- FinancialExtractionService：provider 异常翻译 / block 缺失拒绝 /
  合法候选通过（FakeProvider）。
"""

from datetime import date
from uuid import UUID, uuid4

import pytest

from app.financial.contracts import MetricCode, RawUnit, StatementScope
from app.financial.extraction.contracts import (
    ExtractedFinancialObservation,
    FinancialExtractionRequest,
)
from app.financial.extraction.errors import FinancialExtractionError
from app.financial.extraction.service import FinancialExtractionService
from app.financial.extraction.validation import (
    validate_extracted_observation,
    validate_extraction_batch,
)

_COMPANY_ID = uuid4()
_PARSED_ID = uuid4()
_BLOCK_ID = uuid4()
_PERIOD_END = date(2024, 12, 31)
_BLOCK_TEXT = "营业收入 45,678,901.23 元，同比增长较快。"
# 双列年报行（本期 / 上期两个数字）。
_TWO_COLUMN_BLOCK = "营业收入 45,678,901.23 43,210,987.65"


def _observation(**overrides) -> ExtractedFinancialObservation:
    base = dict(
        company_id=_COMPANY_ID,
        parsed_source_id=_PARSED_ID,
        metric_code=MetricCode.REVENUE,
        statement_scope=StatementScope.CONSOLIDATED,
        period_start=date(2024, 1, 1),
        period_end=_PERIOD_END,
        value_text="45,678,901.23",
        value_start=_BLOCK_TEXT.index("45,678,901.23"),
        value_end=_BLOCK_TEXT.index("45,678,901.23") + len("45,678,901.23"),
        raw_unit=RawUnit.YUAN,
        quote_block_id=_BLOCK_ID,
        quote_start=0,
        quote_end=len(_BLOCK_TEXT),
        quote_text=_BLOCK_TEXT,
    )
    base.update(overrides)
    return ExtractedFinancialObservation(**base)


# ---------------------------------------------------------------- validation 纯函数


def test_valid_observation_passes() -> None:
    validate_extracted_observation(_observation(), _BLOCK_TEXT)  # 不抛


def test_quote_not_verbatim_rejected() -> None:
    obs = _observation(quote_text="45,678,902.23")  # 数字被改
    with pytest.raises(FinancialExtractionError) as exc_info:
        validate_extracted_observation(obs, _BLOCK_TEXT)
    assert exc_info.value.code == "quote_not_verbatim"


def test_quote_slice_mismatch_rejected() -> None:
    obs = _observation(quote_start=0, quote_end=5)  # 切片与 quote_text 不一致
    with pytest.raises(FinancialExtractionError):
        validate_extracted_observation(obs, _BLOCK_TEXT)


def test_two_column_value_resolved_by_span() -> None:
    """双列年报：quote 含两个数字，value span 精确定位目标 token。"""
    start = _TWO_COLUMN_BLOCK.index("45,678,901.23")
    end = start + len("45,678,901.23")
    obs = _observation(
        value_text="45,678,901.23",
        value_start=start,
        value_end=end,
        quote_start=0,
        quote_end=len(_TWO_COLUMN_BLOCK),
        quote_text=_TWO_COLUMN_BLOCK,
    )
    validate_extracted_observation(obs, _TWO_COLUMN_BLOCK)  # 不抛


def test_value_wrong_span_rejected() -> None:
    """value span 指向另一个 token → 拒绝（span 定位防歧义）。"""
    second_start = _TWO_COLUMN_BLOCK.index("43,210,987.65")
    obs = _observation(
        value_text="45,678,901.23",
        value_start=second_start,
        value_end=second_start + len("43,210,987.65"),
        quote_start=0,
        quote_end=len(_TWO_COLUMN_BLOCK),
        quote_text=_TWO_COLUMN_BLOCK,
    )
    with pytest.raises(FinancialExtractionError) as exc_info:
        validate_extracted_observation(obs, _TWO_COLUMN_BLOCK)
    assert exc_info.value.code == "value_not_exact_numeric_token"


def test_value_partial_token_rejected() -> None:
    # value_text 是完整 token 的子串（partial match 禁止）
    obs = _observation(value_text="45,678")
    with pytest.raises(FinancialExtractionError) as exc_info:
        validate_extracted_observation(obs, _BLOCK_TEXT)
    assert exc_info.value.code == "value_not_exact_numeric_token"


def test_instant_period_requires_null_start() -> None:
    obs = _observation(
        metric_code=MetricCode.TOTAL_ASSETS,
        period_start=date(2024, 1, 1),  # balance sheet 必须 None
    )
    with pytest.raises(FinancialExtractionError) as exc_info:
        validate_extracted_observation(obs, _BLOCK_TEXT)
    assert exc_info.value.code == "instant_period_requires_null_start"


def test_duration_period_requires_start() -> None:
    obs = _observation(period_start=None)  # income statement 必须 start
    with pytest.raises(FinancialExtractionError) as exc_info:
        validate_extracted_observation(obs, _BLOCK_TEXT)
    assert exc_info.value.code == "duration_period_requires_start"


def test_batch_partial_rejection() -> None:
    good = _observation()
    bad = _observation(quote_text="改过的文本")
    accepted, rejected = validate_extraction_batch(
        [good, bad],
        {_BLOCK_ID: _BLOCK_TEXT},
    )
    assert accepted == [good]
    assert len(rejected) == 1
    assert rejected[0][1] == "quote_not_verbatim"


def test_batch_missing_block_rejected() -> None:
    obs = _observation()
    accepted, rejected = validate_extraction_batch([obs], {})
    assert accepted == []
    assert rejected[0][1] == "quote_block_missing"


# ---------------------------------------------------------------- service（FakeProvider）


class FakeBlockStore:
    """内存 block 文本（替代 DB 查询）。"""

    def __init__(self, texts: dict[UUID, str]) -> None:
        self._texts = texts

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, stmt):
        class Rows:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return self._rows

        class Row:
            def __init__(self, block_id, text):
                self.block_id = block_id
                self.text = text

        # 内存 fake：返回全部行（service 按 block_id 精确匹配候选）。
        return Rows([Row(block_id, text) for block_id, text in self._texts.items()])


class FakeProvider:
    """确定性 provider：固定观测 / 可注入失败。"""

    def __init__(self, observations=None, fail_with=None) -> None:
        self._observations = observations or []
        self._fail_with = fail_with
        self.calls = []

    @property
    def provider_key(self) -> str:
        return "test:fake-extraction"

    async def extract(self, request: FinancialExtractionRequest):
        self.calls.append(request)
        if self._fail_with is not None:
            raise self._fail_with
        return list(self._observations)


class FakeSessionMaker:
    """返回 FakeBlockStore 的 sessionmaker。"""

    def __init__(self, texts: dict[UUID, str]) -> None:
        self._texts = texts

    def __call__(self):
        return FakeBlockStore(self._texts)


@pytest.mark.asyncio
async def test_service_accepts_valid_and_rejects_invalid() -> None:
    good = _observation()
    bad = _observation(quote_text="被篡改的数字 99.99")
    service = FinancialExtractionService(
        FakeSessionMaker({_BLOCK_ID: _BLOCK_TEXT}),
        FakeProvider([good, bad]),
    )

    result = await service.extract(
        FinancialExtractionRequest(
            company_id=_COMPANY_ID,
            parsed_source_id=_PARSED_ID,
            reporting_period_end=_PERIOD_END,
        )
    )

    assert result.accepted_count == 1
    assert len(result.rejected) == 1
    assert result.rejected[0][1] == "quote_not_verbatim"


@pytest.mark.asyncio
async def test_service_provider_failure_translated() -> None:
    service = FinancialExtractionService(
        FakeSessionMaker({}),
        FakeProvider(fail_with=RuntimeError("boom")),
    )
    with pytest.raises(FinancialExtractionError) as exc_info:
        await service.extract(
            FinancialExtractionRequest(
                company_id=_COMPANY_ID,
                parsed_source_id=_PARSED_ID,
                reporting_period_end=_PERIOD_END,
            )
        )
    assert exc_info.value.code == "provider_failed"
