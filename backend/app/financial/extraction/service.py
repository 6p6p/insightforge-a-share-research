"""Financial auto extraction service (P3 Foundation): 接口层编排。

`FinancialExtractionService.extract(request)`：

1. 调注入的 `FinancialExtractionProvider`（只读 parsed blocks → 观测候选）；
2. 加载候选引用的 ParsedSourceBlock 文本（缺失 → 拒绝该候选）；
3. `validate_extraction_batch` 强制 numeric provenance（quote 逐字 / 数字
   唯一 token / period 规则）；
4. 返回 `FinancialExtractionResult(accepted, rejected)`——**不落库**
   （observation 持久化与 FinancialMetricService 集成是后续 milestone）。

任何校验失败 → 稳定错误码（候选拒绝）；provider 意外异常 →
`FinancialExtractionError("provider_failed")`（不泄漏异常文本）。
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.parsed_source_block import ParsedSourceBlockModel
from app.financial.extraction.contracts import (
    ExtractedFinancialObservation,
    FinancialExtractionProvider,
    FinancialExtractionRequest,
)
from app.financial.extraction.errors import FinancialExtractionError
from app.financial.extraction.validation import validate_extraction_batch


@dataclass(frozen=True)
class FinancialExtractionResult:
    """一次提取的结果（只含观测摘要与拒绝原因，不含 block 正文）。"""

    accepted: tuple[ExtractedFinancialObservation, ...]
    rejected: tuple[tuple[ExtractedFinancialObservation, str], ...]

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)


class FinancialExtractionService:
    """自动财务提取接口层（provider → provenance 校验 → 结果；不落库）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        provider: FinancialExtractionProvider,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._provider = provider

    @property
    def provider(self) -> FinancialExtractionProvider:
        return self._provider

    async def extract(self, request: FinancialExtractionRequest) -> FinancialExtractionResult:
        try:
            observations = await self._provider.extract(request)
        except Exception as exc:  # noqa: BLE001 - 契约违反：翻译为稳定错误
            raise FinancialExtractionError(
                "provider_failed", "financial extraction provider 调用失败"
            ) from exc

        block_ids = {o.quote_block_id for o in observations}
        block_texts: dict[UUID, str] = {}
        if block_ids:
            async with self._sessionmaker() as session:
                rows = (
                    (
                        await session.execute(
                            select(ParsedSourceBlockModel).where(
                                ParsedSourceBlockModel.block_id.in_(block_ids)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            block_texts = {row.block_id: row.text for row in rows}

        accepted, rejected = validate_extraction_batch(observations, block_texts)
        return FinancialExtractionResult(
            accepted=tuple(accepted),
            rejected=tuple(rejected),
        )
