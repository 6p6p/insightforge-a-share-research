"""Financial auto extraction contracts (P3 Foundation).

目标链路：

    PDF parsed blocks → FinancialExtractionProvider → 校验（numeric
    provenance）→ FinancialExtractionService →（未来）FinancialObservation

P3 只建立**接口层**：provider 契约 + 观测候选 + numeric provenance 校验
（不实现 PDF 表格解析器，不接 FinancialMetricService 落库——后续 milestone）。

**numeric provenance 不变量**（对齐 evidence quote 契约与
`find_financial_number_tokens` grammar）：
- `quote_text` 必须是 ParsedSourceBlock 文本的逐字切片（程序切片，不信任
  provider / LLM）；
- `value_text` 必须是 quote 内**唯一完整数字 token**（exact match，禁止
  fuzzy / normalize / 自动纠错）；
- period / metric_code 规则复用 `financial.contracts` 的确定性映射。
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

from app.financial.contracts import MetricCode, RawUnit, StatementScope


@dataclass(frozen=True)
class FinancialExtractionRequest:
    """一次自动提取请求（只含语义身份 + parsed 定位）。"""

    company_id: UUID
    parsed_source_id: UUID
    # 报告期结束日（来源报告期间；用于 period 推导与 no-lookahead 边界）。
    reporting_period_end: date


@dataclass(frozen=True)
class ExtractedFinancialObservation:
    """provider 输出的一个财务观测候选（含完整 numeric provenance）。

    字段语义：
    - metric_code / statement_scope / period_start / period_end / raw_unit：
      与 FinancialMetricDraft 对齐（期间规则由校验层强制）；
    - value_text：**逐字数字 token**（如 "45,678,901.23"，不含单位）；
    - quote_block_id / quote_start / quote_end：quote 在 ParsedSourceBlock
      文本中的精确切片区间（[start, end)）；
    - quote_text：与 block_text[quote_start:quote_end] 逐字一致。
    """

    company_id: UUID
    parsed_source_id: UUID
    metric_code: MetricCode
    statement_scope: StatementScope
    period_start: date | None
    period_end: date
    value_text: str
    # value 在 quote_text 内的精确 span（[start, end)）——双列年报（本期/上期）
    # 同行多数字时精确定位目标 token。
    value_start: int
    value_end: int
    raw_unit: RawUnit
    quote_block_id: UUID
    quote_start: int
    quote_end: int
    quote_text: str


class FinancialExtractionProvider(Protocol):
    """自动财务提取 Provider 契约。

    - 只读 ParsedSource（已 parse 的 blocks）；不访问网络 / LLM 可生成数字；
    - 输出必须是**逐字可验证**的观测候选（service 校验 numeric provenance，
      非法候选被拒绝，绝不落库）；
    - 实现不得生成 / 推断 / 修正数字（禁止 LLM 数字）。
    """

    provider_key: str

    async def extract(
        self, request: FinancialExtractionRequest
    ) -> list[ExtractedFinancialObservation]: ...
