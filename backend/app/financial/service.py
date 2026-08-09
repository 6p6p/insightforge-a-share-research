"""Financial metric observation service (stage 4B.2A).

`create_observation(draft)` 把"经过结构化输入并绑定到 exact Evidence quote 的
确定性财务数值事实"登记为 `FinancialMetricObservation`。**不计算**同比 / 环比 /
margin / ratio；**不调用 LLM**；**不自动从 PDF 表格猜财务数字**——只把真实
财务 Evidence 中**原文出现的原始数值**确定性登记，供后续（4B.2B）计算。

流程（两步提交结构，镜像 ClaimService）：
1. 短 DB session 从真实 PG 加载 EvidenceCard 并校验（缺失 /
   origin_type 非 document_chunk / evidence_type 非 metric /
   evidence.company_id != draft.company_id → FinancialMetricEvidenceMismatch），
   随后立即关闭 connection（纯函数阶段不持有 DB 连接）。
2. 纯函数派生（无 DB）：
   - exact source value resolution：source_value_text.strip() 必须是 quote_text
     中**一个完整数字 token**（`find_financial_number_tokens`，与 parse 同一
     grammar；0 个 → FinancialMetricValueNotFound，>1 个 →
     FinancialMetricValueAmbiguous；禁止 substring partial match / fuzzy /
     normalize 后匹配 / 自动纠错）；
   - Decimal parse：raw_value 完全由 source_value_text 解析
     （FinancialMetricValueNotNumeric）；
   - period rule：根据 metric_code 的 expected period_kind（balance → instant
     + period_start 必须 None；income/cash-flow → duration + period_start 必须
     非空且 <= period_end），不匹配 → FinancialMetricPeriodError；
   - unit normalize：normalized_value_cny = raw_value × raw_unit 系数（Decimal）；
   - storage bounds：raw_value 与 normalized_value_cny 都必须能无失真存入
     NUMERIC(38,12)（小数位 <= 12 且 abs < 10^26），不满足 →
     FinancialMetricStorageRangeError（禁止静默 quantize / round / truncate）；
   - fingerprint：canonical JSON + SHA-256。
3. 短 DB transaction：create_or_get（ON CONFLICT(metric_fingerprint)，无进程
   锁）→ 已有 fingerprint 时 **重新加载 EvidenceCard + 重新 exact-match /
   parse / normalize / fingerprint** 并逐项核实 persisted observation；任一
   损坏 → FinancialMetricIntegrityError，**不自动 repair**。任何
   SQLAlchemyError → 整条 rollback + FinancialMetricPersistenceFailed（0
   partial write）。并发 → 最终 1 observation。

**不访问** Chroma / BGE / LLM / RawArtifact bytes；**不复制 locator_refs**
（PG EvidenceCard 是 provenance truth source）。同一完全相同 observation →
replay 同一行；value / unit / period / metric code / source evidence 任一变化
→ 新 observation，旧数据保留（无 update API）。
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.financial.contracts import (
    FINANCIAL_METRIC_SCHEMA_VERSION,
    FinancialMetricDraft,
    FinancialMetricResult,
    PeriodKind,
    compute_metric_fingerprint,
    expected_period_kind,
)
from app.financial.errors import (
    FinancialMetricEvidenceMismatch,
    FinancialMetricIntegrityError,
    FinancialMetricPeriodError,
    FinancialMetricPersistenceFailed,
    FinancialMetricValueAmbiguous,
    FinancialMetricValueNotFound,
)
from app.financial.number_parser import (
    find_financial_number_tokens,
    normalize_value_cny,
    parse_financial_number,
    validate_financial_decimal_storage,
)
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.financial_metric_observation_repository import (
    FinancialMetricObservationRepository,
)

_ORIGIN_DOCUMENT_CHUNK = "document_chunk"
_EVIDENCE_TYPE_METRIC = "metric"


@dataclass(frozen=True)
class _DerivedObservation:
    """纯函数阶段派生的全部确定性值（period_kind 由 metric_code 推导）。"""

    metric_schema_version: int
    period_kind: str
    source_value_text: str
    raw_value: Decimal
    raw_unit: str
    normalized_value_cny: Decimal
    metric_fingerprint: str


class FinancialMetricService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_observation(self, draft: FinancialMetricDraft) -> FinancialMetricResult:
        """登记一条确定性的财务数值观察（无 partial write，并发最终 1 行）。"""
        # 1. 短 DB session：从真实 PG 加载 EvidenceCard 并校验（连接即刻关闭）。
        async with self._sessionmaker() as session:
            card = await self._load_validate_card(session, draft)

        # 2. 纯函数派生（不持有 DB 连接）。
        derived = self._derive(draft, card)

        # 3. 短 DB transaction：create_or_get + replay 完整性校验。
        async with self._sessionmaker() as session:
            try:
                repo = FinancialMetricObservationRepository(session)
                observation = FinancialMetricObservationModel(
                    metric_observation_id=uuid.uuid4(),
                    **self._observation_kwargs(draft, derived),
                )
                persisted, created = await repo.create_or_get(observation)
                if not created:
                    await self._verify_replay(session, persisted, draft)
                await session.commit()
            except FinancialMetricIntegrityError:
                # replay 校验发现既有 observation 损坏 → 显式回滚本事务（不依赖
                # session close 隐式 rollback），然后向上抛出。
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise FinancialMetricPersistenceFailed() from exc

        return FinancialMetricResult(
            metric_observation_id=persisted.metric_observation_id,
            metric_fingerprint=persisted.metric_fingerprint,
            replayed=not created,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    async def _load_validate_card(
        session: AsyncSession, draft: FinancialMetricDraft
    ) -> EvidenceCardModel:
        """从真实 PG 加载 EvidenceCard 并校验（缺失 / origin / type / company）。

        - v1 只允许 origin_type=document_chunk 且 evidence_type=metric；
        - evidence.company_id 必须 == draft.company_id（跨公司拒绝）；
        - 任何不匹配 → FinancialMetricEvidenceMismatch（不自动修复）。
        """
        card = await EvidenceCardRepository(session).get_by_id(draft.source_evidence_card_id)
        if card is None:
            raise FinancialMetricEvidenceMismatch("evidence card not found")
        if card.origin_type != _ORIGIN_DOCUMENT_CHUNK:
            raise FinancialMetricEvidenceMismatch("origin_type 必须是 document_chunk")
        if card.evidence_type != _EVIDENCE_TYPE_METRIC:
            raise FinancialMetricEvidenceMismatch("evidence_type 必须是 metric")
        if card.company_id != draft.company_id:
            raise FinancialMetricEvidenceMismatch("evidence card company 与 draft 不一致")
        return card

    @staticmethod
    def _resolve_source_value(card: EvidenceCardModel, source_value_text: str) -> None:
        """source_value_text.strip() 必须是 quote_text 中**一个完整数字 token**。

        `find_financial_number_tokens` 与 `parse_financial_number` 同一 grammar
        扫描 quote_text 的全部完整数字 token；source_value_text 必须原样等于其中
        一个 token：0 个 → FinancialMetricValueNotFound，>1 个 →
        FinancialMetricValueAmbiguous。禁止 substring partial match / fuzzy /
        normalize 后匹配 / 自动纠错：`"收入1000万元"` 里 "1000" 接受而 "100" /
        "000" 拒绝；"-123.45" / "(123.45)" 的符号与括号属于 token。
        """
        quote = card.quote_text
        if not quote:
            raise FinancialMetricValueNotFound("quote_text 缺失")
        tokens = find_financial_number_tokens(quote)
        matching = [token for token in tokens if token.text == source_value_text]
        if not matching:
            raise FinancialMetricValueNotFound()
        if len(matching) > 1:
            raise FinancialMetricValueAmbiguous()

    def _derive(self, draft: FinancialMetricDraft, card: EvidenceCardModel) -> _DerivedObservation:
        """纯函数派生：exact-match → parse → period rule → normalize → fingerprint。"""
        self._resolve_source_value(card, draft.source_value_text)
        raw_value = parse_financial_number(draft.source_value_text)

        expected_kind = expected_period_kind(draft.metric_code)
        if expected_kind == PeriodKind.INSTANT:
            # 资产负债表时点：period_start 必须为 None。
            if draft.period_start is not None:
                raise FinancialMetricPeriodError(
                    "balance sheet 指标必须 period_start=None（instant 时点）"
                )
            period_kind = PeriodKind.INSTANT.value
        else:
            # 利润表 / 现金流量表区间：period_start 必须非空（<= period_end 已在
            # draft 构造时校验）。
            if draft.period_start is None:
                raise FinancialMetricPeriodError(
                    "income/cash-flow 指标必须提供 period_start（duration 区间）"
                )
            period_kind = PeriodKind.DURATION.value

        raw_unit = draft.raw_unit.value
        normalized_value_cny = normalize_value_cny(raw_value, raw_unit)
        # NUMERIC(38,12) 存储契约：raw_value 与 normalized_value_cny 都必须能
        # 无失真落库（禁止静默 quantize / round / truncate）。
        validate_financial_decimal_storage(raw_value)
        validate_financial_decimal_storage(normalized_value_cny)
        metric_fingerprint = compute_metric_fingerprint(
            metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
            company_id=draft.company_id,
            source_evidence_card_id=draft.source_evidence_card_id,
            metric_code=draft.metric_code.value,
            statement_scope=draft.statement_scope.value,
            period_start=draft.period_start,
            period_end=draft.period_end,
            period_kind=period_kind,
            source_value_text=draft.source_value_text,
            raw_value=raw_value,
            raw_unit=raw_unit,
            normalized_value_cny=normalized_value_cny,
        )
        return _DerivedObservation(
            metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
            period_kind=period_kind,
            source_value_text=draft.source_value_text,
            raw_value=raw_value,
            raw_unit=raw_unit,
            normalized_value_cny=normalized_value_cny,
            metric_fingerprint=metric_fingerprint,
        )

    @staticmethod
    def _observation_kwargs(draft: FinancialMetricDraft, derived: _DerivedObservation) -> dict:
        return {
            "company_id": draft.company_id,
            "source_evidence_card_id": draft.source_evidence_card_id,
            "metric_code": draft.metric_code.value,
            "statement_scope": draft.statement_scope.value,
            "period_start": draft.period_start,
            "period_end": draft.period_end,
            "period_kind": derived.period_kind,
            "source_value_text": derived.source_value_text,
            "raw_value": derived.raw_value,
            "raw_unit": derived.raw_unit,
            "normalized_value_cny": derived.normalized_value_cny,
            "metric_schema_version": derived.metric_schema_version,
            "metric_fingerprint": derived.metric_fingerprint,
        }

    async def _verify_replay(
        self,
        session: AsyncSession,
        persisted: FinancialMetricObservationModel,
        draft: FinancialMetricDraft,
    ) -> None:
        """已有 fingerprint 的 observation replay 完整性校验。

        重新加载 EvidenceCard + 重新 exact-match / parse / normalize /
        fingerprint（检测上游 Evidence 是否变化导致数值不再有效），再逐项核实
        persisted observation。发现损坏只抛 FinancialMetricIntegrityError，
        **不自动 repair**（修改 = 新 observation，无 update API）。
        """
        card = await self._load_validate_card(session, draft)
        derived = self._derive(draft, card)
        pairs = (
            ("company_id", persisted.company_id, draft.company_id),
            (
                "source_evidence_card_id",
                persisted.source_evidence_card_id,
                draft.source_evidence_card_id,
            ),
            ("metric_code", persisted.metric_code, draft.metric_code.value),
            ("statement_scope", persisted.statement_scope, draft.statement_scope.value),
            ("period_start", persisted.period_start, draft.period_start),
            ("period_end", persisted.period_end, draft.period_end),
            ("period_kind", persisted.period_kind, derived.period_kind),
            ("source_value_text", persisted.source_value_text, derived.source_value_text),
            ("raw_value", persisted.raw_value, derived.raw_value),
            ("raw_unit", persisted.raw_unit, derived.raw_unit),
            (
                "normalized_value_cny",
                persisted.normalized_value_cny,
                derived.normalized_value_cny,
            ),
            (
                "metric_schema_version",
                persisted.metric_schema_version,
                derived.metric_schema_version,
            ),
            ("metric_fingerprint", persisted.metric_fingerprint, derived.metric_fingerprint),
        )
        for name, stored, expected in pairs:
            if stored != expected:
                raise FinancialMetricIntegrityError(
                    f"financial metric replay integrity check failed on {name}"
                )
