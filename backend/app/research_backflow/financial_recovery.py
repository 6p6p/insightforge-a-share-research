"""P1.3 financial evidence recovery: model-assisted locators -> real quote -> deterministic value.

价值链约束（硬）：所有数字必须来自**真实来源 quote**（chunk.text 程序切片），
经确定性解析后绑定到真实 document_chunk EvidenceCard -> FinancialMetricObservation；
LLM 只负责"去哪找"（alias 术语 / section 关键词），**绝不输出数字或事实**。

流程（复用既有 evidence/extraction + financial/service，不重写）:
1. metadata/section 关键词 -> deterministic alias 表 + 可选 LLM 扩展 alias；
2. 真实 RetrievalService 检索 company 已有 Source Library（Chroma filtered + PG hydrate）；
3. 在 hit.text 中定位 alias 邻近的单个数字 token（quote 程序切片 + 单位解析）;
4. EvidenceCardService.create_card（document_chunk / metric / 精确 quote）创建证据卡;
5. FinancialMetricService.create_observation（quote 内唯一数字 token -> observation）;
    任一 SQLAlchemy / 口径 / 值冲突错误 -> 本候选跳过（不编造、不静默误写）。

标记：extractor_name='evidence_recovery' + extractor_model_id（LLM alias 模型 id 时）
即 model_assisted_recovery 内部标记；不替代 provenance。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
from app.evidence.errors import EvidenceError
from app.financial.contracts import (
    FinancialMetricDraft,
    MetricCode,
    PeriodKind,
    RawUnit,
    StatementScope,
    expected_period_kind,
)
from app.financial.errors import FinancialMetricError
from app.rag.retrieval.contracts import RetrievalQuery
from app.rag.retrieval.errors import RetrievalIndexNotReady
from app.services.evidence_card_service import EvidenceCardService

_ALLOWED_DOCUMENT_TYPES = (
    "annual_report",
    "semiannual_report",
    "quarterly_report",
    "company_announcement",
    "issuer_ir_material",
    "prospectus",
)
RECOVERY_EXTRACTOR_NAME = "evidence_recovery"
RECOVERY_EXTRACTOR_VERSION = 1

_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


@dataclass(frozen=True)
class RecoveredMetricQuote:
    """chunk.text 中一次确定性定位到的 alias+数字（quote 为程序切片）。"""

    alias: str
    quote_start: int
    quote_end: int
    quote_text: str
    number_token: str
    raw_unit: RawUnit | None


# metric_code -> deterministic alias 术语（确定性主干，LLM 只能扩展不能覆盖）。
METRIC_CODE_ALIASES: dict[MetricCode, tuple[str, ...]] = {
    MetricCode.REVENUE: ("营业收入", "营业总收入", "营业收入(元)", "revenue"),
    MetricCode.OPERATING_COST: ("营业成本", "operating cost"),
    MetricCode.OPERATING_PROFIT: ("营业利润", "operating profit"),
    MetricCode.PROFIT_BEFORE_TAX: ("利润总额", "profit before tax"),
    MetricCode.NET_PROFIT: ("净利润", "net profit"),
    MetricCode.NET_PROFIT_PARENT: (
        "归属于母公司股东的净利润",
        "归母净利润",
        "归属于母公司所有者的净利润",
    ),
    MetricCode.NET_PROFIT_PARENT_EXCL_NONRECURRING: (
        "归属于上市公司股东的扣除非经常性损益的净利润",
        "扣非净利润",
    ),
    MetricCode.OPERATING_CASH_FLOW_NET: (
        "经营活动产生的现金流量净额",
        "经营活动现金流量净额",
        "operating cash flow",
    ),
    MetricCode.TOTAL_ASSETS: ("资产总计", "总资产", "total assets"),
    MetricCode.TOTAL_LIABILITIES: ("负债合计", "总负债", "total liabilities"),
    MetricCode.EQUITY_PARENT: ("归属于母公司所有者权益合计", "归母所有者权益", "股东权益"),
}

_UNIT_BY_TERM: dict[str, RawUnit] = {
    "亿元": RawUnit.HUNDRED_MILLION_YUAN,
    "万元": RawUnit.TEN_THOUSAND_YUAN,
    "千元": RawUnit.THOUSAND_YUAN,
    "元": RawUnit.YUAN,
}
_UNIT_TERM_ORDER = ("亿元", "万元", "千元", "元")

_QUOTE_WINDOW = 40


class FinancialRecoveryAliasModel(Protocol):
    """LLM-assist 只负责生成"去哪找"的 alias/关键词；绝不返回数字或事实。"""

    model_id: str

    def generate_aliases(self, metric_label: str, period_label: str | None) -> list[str]: ...


def _unit_after(text: str, number_end: int) -> tuple[RawUnit | None, int]:
    """数字后（允许空白）紧邻的单位术语；返回 (unit, unit_end_exclusive)。"""
    i = number_end
    while i < len(text) and text[i] == " ":
        i += 1
    for term in _UNIT_TERM_ORDER:
        if text.startswith(term, i):
            return _UNIT_BY_TERM[term], i + len(term)
    return None, number_end


def locate_metric_quote(chunk_text: str, alias_terms: list[str]) -> RecoveredMetricQuote | None:
    """在 chunk.text 中确定性定位 alias 邻接的单个数字 token。

    规则：alias 出现后向前（最多 _QUOTE_WINDOW 字）找第一个数字 token（可带单位）；
    若 [quote_start, quote_end] 切片内含**多个**数字 token -> 跳过（保证
    FinancialMetricService 的 source_value_text 唯一性；不猜哪个是值）。
    """
    for alias in alias_terms:
        if not alias:
            continue
        lower_text = chunk_text.lower()
        alias_lower = alias.lower()
        pos = lower_text.find(alias_lower)
        if pos < 0:
            continue
        # 在 alias 之后的有界窗口内找数字。
        scan_start = pos + len(alias)
        scan_end = min(len(chunk_text), pos + len(alias) + _QUOTE_WINDOW)
        if scan_start >= scan_end and pos > 0:
            scan_start = max(0, pos - _QUOTE_WINDOW)
        segment = chunk_text[scan_start:scan_end] if scan_end > scan_start else ""
        m = None
        if segment:
            m = _NUMBER_RE.search(segment)
        if m is None:
            continue
        number_abs_end = scan_start + m.end()
        raw_unit, unit_end = _unit_after(chunk_text, number_abs_end)
        # quote 窗口：包含 alias 起点与数字终点（含单位），保证 slice 非空。
        quote_start = pos
        quote_end = unit_end if raw_unit is not None else number_abs_end
        quote_end = min(len(chunk_text), quote_end)
        quote_text = chunk_text[quote_start:quote_end].strip()
        if not quote_text:
            continue
        # 确定性守卫：quote 内必须恰好一个数字 token，否则跳过（不猜值）。
        tokens = list(_NUMBER_RE.finditer(quote_text))
        if len(tokens) != 1:
            continue
        return RecoveredMetricQuote(
            alias=alias,
            quote_start=quote_start,
            quote_end=quote_end,
            quote_text=quote_text,
            number_token=tokens[0].group(0),
            raw_unit=raw_unit,
        )
    return None


def resolve_alias_terms(
    metric_code: MetricCode,
    model: FinancialRecoveryAliasModel | None,
    period_label: str | None = None,
) -> list[str]:
    """确定性 alias 主干 + 可选 LLM 扩展（LLM 术语只用于定位，不覆盖主干）。"""
    aliases = list(METRIC_CODE_ALIASES.get(metric_code, ()))
    if model is not None:
        try:
            extra = model.generate_aliases(metric_code.value, period_label)
            for term in extra or []:
                if isinstance(term, str) and term.strip() and term not in aliases:
                    aliases.append(term.strip()[:80])
        except Exception:  # noqa: BLE001 - alias 生成失败退回确定性主干
            pass
    return aliases or [metric_code.value]


def build_recovery_card_draft(
    *,
    research_question: str,
    chunk_id: UUID,
    quote: RecoveredMetricQuote,
    model_id: str | None = None,
) -> EvidenceCardDraft:
    return EvidenceCardDraft(
        research_question=research_question.strip(),
        evidence_statement=(
            f"{quote.alias} 为 {quote.number_token}"
            + (f" {quote.raw_unit.value}" if quote.raw_unit else "")
        ),
        evidence_type=EvidenceType.METRIC,
        chunk_id=chunk_id,
        quote_start=quote.quote_start,
        quote_end=quote.quote_end,
        extractor_name=RECOVERY_EXTRACTOR_NAME,
        extractor_version=RECOVERY_EXTRACTOR_VERSION,
        extractor_model_id=model_id,
        extractor_confidence=EvidenceConfidence.MEDIUM,
    )


@dataclass(frozen=True)
class FinancialRecoveryOutcome:
    """单指标恢复结果（仅 application output，不保存 model reasoning）。"""

    metric_code: str
    recovered: bool
    evidence_card_id: UUID | None = None
    metric_observation_id: UUID | None = None
    quote: str | None = None
    number: str | None = None
    raw_unit: str | None = None
    reason: str | None = None
    replayed: bool = False


def _number_to_decimal(token: str) -> str | None:
    cleaned = token.replace(",", "")
    try:
        return str(Decimal(cleaned)) if Decimal(cleaned).is_finite() else None
    except Exception:  # noqa: BLE001
        return None


class FinancialRecoveryService:
    """P1.3：对单个财务指标执行已存在来源恢复（真实 quote -> 证据卡 -> observation）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        retrieval_service,
        model: FinancialRecoveryAliasModel | None = None,
        index_builder=None,
        card_service=None,
        metric_service=None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._retrieval = retrieval_service
        self._model = model
        self._index_builder = index_builder
        self._card_service = card_service
        self._metric_service = metric_service

    async def recover_metric(
        self,
        *,
        company_id: UUID,
        research_question: str,
        metric_code: MetricCode,
        period_start: date | None,
        period_end: date | None,
        analysis_as_of: date | None = None,
        allowed_source_types: list[str] | None = None,
        alias_terms: list[str] | None = None,
    ) -> FinancialRecoveryOutcome:
        aliases = alias_terms or resolve_alias_terms(
            metric_code,
            self._model,
            period_end.isoformat() if period_end else None,
        )
        query_text = " ".join(aliases[:6])
        query = RetrievalQuery(
            company_id=company_id,
            query_text=query_text,
            top_k=8,
            document_types=allowed_source_types or list(_ALLOWED_DOCUMENT_TYPES),
        )
        hits = await self._retrieval_unchecked(query)
        if not hits:
            return FinancialRecoveryOutcome(
                metric_code=metric_code.value, recovered=False, reason="no_hits"
            )

        card_service = self._card_service or EvidenceCardService(self._sessionmaker)
        if self._metric_service is None:
            from app.financial.service import FinancialMetricService
        metric_service = self._metric_service or FinancialMetricService(self._sessionmaker)
        for hit in hits:
            if analysis_as_of is not None and hit.published_at is not None:
                if hit.published_at.date() > analysis_as_of:
                    continue  # no-lookahead：不研究基准日之后可得来源
            quote = locate_metric_quote(hit.text, aliases)
            if quote is None or quote.raw_unit is None:
                continue
            try:
                card = await card_service.create_card(
                    build_recovery_card_draft(
                        research_question=research_question,
                        chunk_id=hit.chunk_id,
                        quote=quote,
                        model_id=getattr(self._model, "model_id", None) if self._model else None,
                    )
                )
                kind = expected_period_kind(metric_code)
                obs_start = period_start if kind == PeriodKind.DURATION else None
                obs_end = (
                    period_end
                    if period_end is not None
                    else (
                        hit.reporting_period_end if hit.reporting_period_end is not None else None
                    )
                )
                if obs_end is None and hit.reporting_period_end is not None:
                    obs_end = hit.reporting_period_end
                obs = await metric_service.create_observation(
                    FinancialMetricDraft(
                        company_id=company_id,
                        source_evidence_card_id=card.evidence_card_id,
                        metric_code=metric_code,
                        statement_scope=StatementScope.CONSOLIDATED,
                        period_start=obs_start,
                        period_end=obs_end,
                        source_value_text=quote.number_token,
                        raw_unit=quote.raw_unit,
                    )
                )
                return FinancialRecoveryOutcome(
                    metric_code=metric_code.value,
                    recovered=True,
                    evidence_card_id=card.evidence_card_id,
                    metric_observation_id=obs.metric_observation_id,
                    quote=quote.quote_text,
                    number=quote.number_token,
                    raw_unit=quote.raw_unit.value,
                    replayed=obs.replayed,
                )
            except (EvidenceError, FinancialMetricError, SQLAlchemyError):  # noqa: BLE001
                # 单候选失败（口径/期/唯一性）→ 跳过下一候选；不编造、不崩溃 backflow。
                continue
        return FinancialRecoveryOutcome(
            metric_code=metric_code.value, recovered=False, reason="no_valid_candidate"
        )

    async def _retrieval_unchecked(self, query):
        """真实检索；索引未就绪则本轮恢复不可用（交给后续来源/补充通道），不崩溃。"""
        try:
            return await self._retrieval.retrieve(query)
        except RetrievalIndexNotReady:
            return []
