"""Financial auto extraction numeric provenance validation (P3 Foundation)。

核心安全边界：provider / 未来 LLM 输出**不得**绕过 provenance——观测候选
必须满足：

1. quote 逐字性：`quote_text` == block_text[quote_start:quote_end]（程序
   切片，空白容差与 evidence quote 契约一致——非空白字符骨架必须精确）；
2. value 唯一性：`value_text` 是 quote 内**唯一完整数字 token**（复用
   `find_financial_number_tokens` grammar，exact match）；
3. period 规则：metric_code 的 statement family → duration / instant
   （复用 `expected_period_kind`）；
4. metric_code 支持列表（复用 `statement_family`）。

校验失败 → 稳定错误码（`FinancialExtractionError`），候选被拒绝且**不落库**
（绝不把无法追溯的数字登记为 observation）。
"""

from uuid import UUID

from app.financial.contracts import (
    StatementScope,
    expected_period_kind,
    statement_family,
)
from app.financial.extraction.contracts import ExtractedFinancialObservation
from app.financial.extraction.errors import FinancialExtractionError
from app.financial.number_parser import find_financial_number_tokens


def _quote_matches(block_text: str, quote_start: int, quote_end: int, quote_text: str) -> bool:
    """程序切片 + 非空白骨架逐字校验（与 evidence quote 契约一致）。

    允许的差异只有空白（骨架 = 去除空白字符后的序列必须完全一致）。
    """
    if quote_start < 0 or quote_end > len(block_text) or quote_start > quote_end:
        return False
    sliced = block_text[quote_start:quote_end]
    return "".join(sliced.split()) == "".join(quote_text.split())


def validate_extracted_observation(
    observation: ExtractedFinancialObservation,
    block_text: str,
) -> None:
    """校验一条观测候选的 numeric provenance（失败 → FinancialExtractionError）。

    `block_text` 必须来自 observation.quote_block_id 对应的 ParsedSourceBlock
    （service 负责加载；加载失败 / 无该 block → 由 service 拒绝）。
    """
    # 1) metric_code 支持列表 + period 规则（确定性映射）。
    try:
        statement_family(observation.metric_code)
    except Exception as exc:
        raise FinancialExtractionError(
            "unsupported_metric_code", f"不支持的 metric_code: {observation.metric_code}"
        ) from exc
    expected = expected_period_kind(observation.metric_code)
    if expected.value == "instant":
        if observation.period_start is not None:
            raise FinancialExtractionError(
                "instant_period_requires_null_start",
                "balance sheet 观测 period_start 必须为 None",
            )
    elif observation.period_start is None or observation.period_start > observation.period_end:
        raise FinancialExtractionError(
            "duration_period_requires_start",
            "duration 观测必须提供 period_start 且 <= period_end",
        )
    if not isinstance(observation.statement_scope, StatementScope):
        raise FinancialExtractionError("invalid_statement_scope", "statement_scope 非法")

    # 2) quote 逐字性（程序切片）。
    if not _quote_matches(
        block_text, observation.quote_start, observation.quote_end, observation.quote_text
    ):
        raise FinancialExtractionError(
            "quote_not_verbatim", "quote_text 与 block 切片不一致（非逐字）"
        )

    # 3) value 数字 token 精确定位（exact match + span）。
    #    双列年报（本期/上期）同行多数字：value_text 必须等于 quote 内
    #    [value_start, value_end) 处的一个完整 token（不允许 substring /
    #    fuzzy / 自动纠错；span 定位保证无歧义）。
    tokens = find_financial_number_tokens(observation.quote_text)
    value_text = observation.value_text.strip()
    matched = [
        token
        for token in tokens
        if token.text == value_text
        and token.start == observation.value_start
        and token.end == observation.value_end
    ]
    if len(matched) != 1:
        raise FinancialExtractionError(
            "value_not_exact_numeric_token",
            "value_text 必须是 quote 内 [value_start, value_end) 处的完整数字 token",
        )


def validate_extraction_batch(
    observations: list[ExtractedFinancialObservation],
    block_texts: dict[UUID, str],
) -> tuple[list[ExtractedFinancialObservation], list[tuple[ExtractedFinancialObservation, str]]]:
    """批量校验：返回 (accepted, rejected_with_reason)。

    单条校验失败不阻塞其它候选（provider 输出部分非法 → 只拒绝非法者，
    合法者保留——绝不整批丢弃，也绝不放过非法数字）。
    """
    accepted: list[ExtractedFinancialObservation] = []
    rejected: list[tuple[ExtractedFinancialObservation, str]] = []
    for observation in observations:
        block_text = block_texts.get(observation.quote_block_id)
        if block_text is None:
            rejected.append((observation, "quote_block_missing"))
            continue
        try:
            validate_extracted_observation(observation, block_text)
        except FinancialExtractionError as exc:
            rejected.append((observation, exc.code))
            continue
        accepted.append(observation)
    return accepted, rejected
