"""Financial extraction ingestion orchestration (F1)。

把通过 provenance 校验的提取结果落库为完整财务供给链：

    ExtractedFinancialObservation（accepted）
        → FinancialExtractionEvidenceCard（quote = block 逐字切片，tier 继承
          原始报告 SourceRecord）
        → FinancialMetricService.create_observation（fingerprint replay 幂等）

- 每张卡 / 每条 observation 幂等（fingerprint replay → 0 重复写）；
- 单条失败不阻塞其它（记录 rejected 摘要；绝不放过无 quote 来源的数字）；
- 失败不抛确定性错误（调用方据此保持原 missing 语义 / human fallback）。
"""

from dataclasses import dataclass, field, replace
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.evidence.contracts import (
    EvidenceType,
    FinancialExtractionEvidenceDraft,
)
from app.financial.contracts import FinancialMetricDraft
from app.financial.extraction.evidence import FinancialExtractionEvidenceService
from app.financial.extraction.service import FinancialExtractionResult
from app.financial.service import FinancialMetricService


@dataclass(frozen=True)
class FinancialIngestionSummary:
    """一次落库的结果摘要（不含数值细节 / 正文）。"""

    cards_created: int = 0
    cards_replayed: int = 0
    observations_created: int = 0
    observations_replayed: int = 0
    rejected: tuple[tuple[UUID, str], ...] = field(default_factory=tuple)

    @property
    def persisted_any(self) -> bool:
        return self.cards_created + self.observations_created > 0


class FinancialExtractionIngestionService:
    """提取结果 → 证据卡 + observation 落库编排（幂等、逐条容错）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        evidence_service: FinancialExtractionEvidenceService | None = None,
        metric_service: FinancialMetricService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._evidence = evidence_service or FinancialExtractionEvidenceService(sessionmaker)
        self._metrics = metric_service or FinancialMetricService(sessionmaker)

    async def ingest(
        self,
        *,
        research_question: str,
        source_id: UUID,
        extraction: FinancialExtractionResult,
    ) -> FinancialIngestionSummary:
        """为 accepted 观测创建证据卡 + observation（全部幂等）。"""
        summary = FinancialIngestionSummary()
        rejected: list[tuple[UUID, str]] = []
        for observation in extraction.accepted:
            statement = (
                f"{observation.metric_code.value}（{observation.statement_scope.value}）"
                f"报告期 {observation.period_end.isoformat()}"
                f"的数值为 {observation.value_text}"
            )
            try:
                card_result = await self._evidence.create_card(
                    FinancialExtractionEvidenceDraft(
                        company_id=observation.company_id,
                        research_question=research_question,
                        source_id=source_id,
                        parsed_source_id=observation.parsed_source_id,
                        quote_block_id=observation.quote_block_id,
                        quote_start=observation.quote_start,
                        quote_end=observation.quote_end,
                        quote_text=observation.quote_text,
                        evidence_statement=statement,
                        evidence_type=EvidenceType.METRIC,
                    )
                )
            except Exception:  # noqa: BLE001 - 单条失败不阻塞其它
                rejected.append((observation.quote_block_id, "evidence_card_failed"))
                continue
            summary = replace(
                summary,
                cards_created=summary.cards_created + (0 if card_result.replayed else 1),
                cards_replayed=summary.cards_replayed + (1 if card_result.replayed else 0),
            )
            try:
                metric_result = await self._metrics.create_observation(
                    FinancialMetricDraft(
                        company_id=observation.company_id,
                        source_evidence_card_id=card_result.evidence_card_id,
                        metric_code=observation.metric_code,
                        statement_scope=observation.statement_scope,
                        period_start=observation.period_start,
                        period_end=observation.period_end,
                        source_value_text=observation.value_text,
                        raw_unit=observation.raw_unit,
                    )
                )
            except Exception:  # noqa: BLE001 - observation 失败不阻塞其它
                rejected.append((card_result.evidence_card_id, "observation_failed"))
                continue
            summary = replace(
                summary,
                observations_created=summary.observations_created
                + (0 if metric_result.replayed else 1),
                observations_replayed=summary.observations_replayed
                + (1 if metric_result.replayed else 0),
            )
        return replace(summary, rejected=tuple(rejected))
