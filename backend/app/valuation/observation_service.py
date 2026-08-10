"""Valuation metric observation service (stage 4C.2A).

`create_observation(draft)` 把"经过结构化输入并绑定到 exact Evidence quote 的
确定性估值倍数数值事实"登记为 `ValuationMetricObservation`（pe_ttm / pb_mrq /
ps_ttm）。**不计算**相对估值比较 / 不调用 LLM / 不自动选 peer / 不做任何
买卖建议 / 目标价 / 绝对公允价值——只把真实文档 Evidence 中**原文出现的原始
估值倍数**确定性登记，供后续 comparison（4C.2A）与 Analyst（4C.2B）使用。

流程（两步提交结构，镜像 FinancialMetricService）：
1. 短 DB session 从真实 PG 加载 EvidenceCard 并校验（缺失 /
   origin_type 非 document_chunk / evidence_type 非 metric /
   evidence.company_id != draft.company_id → ValuationObservationEvidenceMismatch），
   随后立即关闭 connection（纯函数阶段不持有 DB 连接）。
2. 纯函数派生（无 DB）：
   - **Decimal parse（grammar 先判）**：metric_value 完全由 source_value_text
     解析；非纯十进制字面量（"100万" / "abc" / "15.3%"）→
     ValuationValueNotNumeric；
   - **exact source value resolution**：source_value_text.strip() 必须是
     quote_text 中**一个完整数字 token**（复用 Financial 同一 grammar 的
     `find_financial_number_tokens`；0 个 → ValuationValueNotFound，>1 个 →
     ValuationValueAmbiguous；禁止 substring partial match / fuzzy / normalize
     后匹配 / 自动纠错）；
   - NUMERIC(38,12) 无失真存储校验（ValuationStorageRangeError，禁止静默
     quantize / round / truncate）；
   - fingerprint：canonical JSON + SHA-256。
3. 短 DB transaction：create_or_get（ON CONFLICT(fingerprint)，无进程锁）→
   已有 fingerprint 时 **重新加载 EvidenceCard + 重新 exact-match / parse /
   fingerprint** 并逐项核实 persisted observation；任一损坏 →
   ValuationIntegrityError，**不自动 repair**。任何 SQLAlchemyError → 整条
   rollback + ValuationPersistenceFailed（0 partial write）。并发 → 最终 1
   observation。

`metric_as_of` = **市场观测日**（该倍数对应的估值时点），不是来源发布时间；
**不要求** source availability <= metric_as_of（数据源可能次日才发布更晚的
估值）。observation 允许 0 / 负倍数（来源事实快照；可比较性由 comparison
阶段校验）。

**不访问** Chroma / BGE / LLM / RawArtifact bytes；**不复制 locator_refs**
（PG EvidenceCard 是 provenance truth source）。同一完全相同 observation →
replay 同一行；value / metric / date / source evidence 任一变化 → 新
observation，旧数据保留（无 update API）。
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.valuation_metric_observation import ValuationMetricObservationModel
from app.financial.number_parser import find_financial_number_tokens
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.valuation_metric_observation_repository import (
    ValuationMetricObservationRepository,
)
from app.valuation.contracts import (
    VALUATION_OBSERVATION_SCHEMA_VERSION,
    ValuationMetricDraft,
    ValuationObservationResult,
    compute_valuation_observation_fingerprint,
)
from app.valuation.errors import (
    ValuationIntegrityError,
    ValuationObservationEvidenceMismatch,
    ValuationPersistenceFailed,
    ValuationValueAmbiguous,
    ValuationValueNotFound,
)
from app.valuation.number_parser import (
    parse_valuation_number,
    validate_valuation_decimal_storage,
)

_ORIGIN_DOCUMENT_CHUNK = "document_chunk"
_EVIDENCE_TYPE_METRIC = "metric"


@dataclass(frozen=True)
class _DerivedObservation:
    """纯函数阶段派生的全部确定性值。"""

    metric_value: Decimal
    valuation_observation_fingerprint: str


class ValuationObservationService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_observation(self, draft: ValuationMetricDraft) -> ValuationObservationResult:
        """登记一条确定性的估值倍数观察（无 partial write，并发最终 1 行）。"""
        # 1. 短 DB session：从真实 PG 加载 EvidenceCard 并校验（连接即刻关闭）。
        async with self._sessionmaker() as session:
            card = await self._load_validate_card(session, draft)

        # 2. 纯函数派生（不持有 DB 连接）。
        derived = self._derive(draft, card)

        # 3. 短 DB transaction：create_or_get + replay 完整性校验。
        async with self._sessionmaker() as session:
            try:
                repo = ValuationMetricObservationRepository(session)
                observation = ValuationMetricObservationModel(
                    valuation_observation_id=uuid.uuid4(),
                    company_id=draft.company_id,
                    source_evidence_card_id=draft.source_evidence_card_id,
                    metric_code=draft.metric_code.value,
                    metric_as_of=draft.metric_as_of,
                    source_value_text=draft.source_value_text,
                    metric_value=derived.metric_value,
                    valuation_observation_schema_version=VALUATION_OBSERVATION_SCHEMA_VERSION,
                    valuation_observation_fingerprint=derived.valuation_observation_fingerprint,
                )
                persisted, created = await repo.create_or_get(observation)
                if not created:
                    await self._verify_replay(session, persisted, draft)
                await session.commit()
            except ValuationIntegrityError:
                # replay 校验发现既有 observation 损坏 → 显式回滚本事务（不依赖
                # session close 隐式 rollback），然后向上抛出。
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ValuationPersistenceFailed() from exc

        return ValuationObservationResult(
            valuation_observation_id=persisted.valuation_observation_id,
            valuation_observation_fingerprint=persisted.valuation_observation_fingerprint,
            replayed=not created,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    async def _load_validate_card(
        session: AsyncSession, draft: ValuationMetricDraft
    ) -> EvidenceCardModel:
        """从真实 PG 加载 EvidenceCard 并校验（缺失 / origin / type / company）。

        - v1 只允许 origin_type=document_chunk 且 evidence_type=metric；
        - evidence.company_id 必须 == draft.company_id（跨公司拒绝）；
        - 任何不匹配 → ValuationObservationEvidenceMismatch（不自动修复）。
        """
        card = await EvidenceCardRepository(session).get_by_id(draft.source_evidence_card_id)
        if card is None:
            raise ValuationObservationEvidenceMismatch("evidence card not found")
        if card.origin_type != _ORIGIN_DOCUMENT_CHUNK:
            raise ValuationObservationEvidenceMismatch("origin_type 必须是 document_chunk")
        if card.evidence_type != _EVIDENCE_TYPE_METRIC:
            raise ValuationObservationEvidenceMismatch("evidence_type 必须是 metric")
        if card.company_id != draft.company_id:
            raise ValuationObservationEvidenceMismatch("evidence card company 与 draft 不一致")
        return card

    @staticmethod
    def _resolve_source_value(card: EvidenceCardModel, source_value_text: str) -> None:
        """source_value_text.strip() 必须是 quote_text 中**一个完整数字 token**。

        复用 Financial 同一 grammar 的 `find_financial_number_tokens` 扫描
        quote_text 的全部完整数字 token；source_value_text 必须原样等于其中
        一个 token：0 个 → ValuationValueNotFound，>1 个 →
        ValuationValueAmbiguous。禁止 substring partial match / fuzzy /
        normalize 后匹配 / 自动纠错：`"市盈率30倍"` 里 "30" 接受而 "3" / "0"
        拒绝；"-123.45" / "(123.45)" 的符号与括号属于 token。
        """
        quote = card.quote_text
        if not quote:
            raise ValuationValueNotFound("quote_text 缺失")
        tokens = find_financial_number_tokens(quote)
        matching = [token for token in tokens if token.text == source_value_text]
        if not matching:
            raise ValuationValueNotFound()
        if len(matching) > 1:
            raise ValuationValueAmbiguous()

    def _derive(self, draft: ValuationMetricDraft, card: EvidenceCardModel) -> _DerivedObservation:
        """纯函数派生：parse（grammar）→ exact-match → storage guard → fingerprint。"""
        # 先做 grammar 校验：source_value_text 必须是纯十进制数字字面量
        # （"100万" / "abc" / "15.3%" → ValuationValueNotNumeric）。必须先于
        # exact-match，否则 "100万" 会被 token-match 误报为 NotFound 而非
        # NotNumeric（token-match 只判"是不是 quote 里的完整 token"，判不了
        # "本身是不是合法数字"）。
        metric_value = parse_valuation_number(draft.source_value_text)
        self._resolve_source_value(card, draft.source_value_text)
        # NUMERIC(38,12) 存储契约：metric_value 必须能无失真落库（禁止静默
        # quantize / round / truncate）。
        validate_valuation_decimal_storage(metric_value)
        fingerprint = compute_valuation_observation_fingerprint(
            valuation_observation_schema_version=VALUATION_OBSERVATION_SCHEMA_VERSION,
            company_id=draft.company_id,
            source_evidence_card_id=draft.source_evidence_card_id,
            metric_code=draft.metric_code.value,
            metric_as_of=draft.metric_as_of,
            source_value_text=draft.source_value_text,
            metric_value=metric_value,
        )
        return _DerivedObservation(
            metric_value=metric_value,
            valuation_observation_fingerprint=fingerprint,
        )

    async def _verify_replay(
        self,
        session: AsyncSession,
        persisted: ValuationMetricObservationModel,
        draft: ValuationMetricDraft,
    ) -> None:
        """已有 fingerprint 的 observation replay 完整性校验。

        重新加载 EvidenceCard + 重新 exact-match / parse / fingerprint（检测
        上游 Evidence 是否变化导致数值不再有效），再逐项核实 persisted
        observation。发现损坏只抛 ValuationIntegrityError，**不自动 repair**
        （修改 = 新 observation，无 update API）。
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
            ("metric_as_of", persisted.metric_as_of, draft.metric_as_of),
            ("source_value_text", persisted.source_value_text, draft.source_value_text),
            ("metric_value", persisted.metric_value, derived.metric_value),
            (
                "valuation_observation_schema_version",
                persisted.valuation_observation_schema_version,
                VALUATION_OBSERVATION_SCHEMA_VERSION,
            ),
            (
                "valuation_observation_fingerprint",
                persisted.valuation_observation_fingerprint,
                derived.valuation_observation_fingerprint,
            ),
        )
        for name, stored, expected in pairs:
            if stored != expected:
                raise ValuationIntegrityError(
                    f"valuation observation replay integrity check failed on {name}"
                )
