"""Relative valuation comparison service (stage 4C.2A).

`create_comparison(draft)` 对**显式 peer 集合**（caller 传 peer observation
ids，程序不自动选 peer / 不按市值过滤 / 不随机）做确定性相对估值比较，登记为
`RelativeValuationComparison`：

- **同一 metric_code 且同一 metric_as_of**（严格 same-date，不自动就近交易日
  对齐）——违反 → ValuationMetricMismatch / ValuationDateMismatch；
- **全部 metric_value > 0**（0 / 负倍数可作为来源事实快照，但不可参与比较）——
  违反 → ValuationMetricNotComparable；
- **no-lookahead**：`analysis_as_of >= metric_as_of`，且每个 observation 的来源
  文档 availability（SourceRecord.published_at 否则 acquired_at，复用
  macro_policy.resolve_availability 纯 helper，**绝不用 reporting_period_end**）
  `<= analysis_as_of`——违反 → ValuationFutureEvidence；
- **确定性公式 v1（comparison_method=peer_median）**：peer_median（奇数取中位、
  偶数取两中位算术平均，全 Decimal）、peer_min / peer_max、
  `premium_discount_to_median = (target_value - peer_median) / peer_median`；
  除法统一 `CALCULATION_SCALE=12` + `ROUND_HALF_EVEN`（显式 local quantize）；
  结果经 NUMERIC(38,12) storage guard 校验后才落库。**不做任何分类**
  （relative_high / reasonable / relative_low 属 4C.2B Analyst 判断）。

流程（两步提交结构）：
1. **短 DB session** 加载 target + 全部 peer Observation、Companies、
   EvidenceCard、SourceRecord 并逐条校验（缺失 → ValuationObservationNotFound /
   ValuationCompanyNotFound；target observation 公司 ≠ draft.target_company_id →
   ValuationCompanyMismatch；peer 公司重复 → ValuationPeerDuplicateError；peer
   含 target 公司 → ValuationPeerIncludesTargetError；metric / date / positive /
   no-lookahead），随后立即关闭 connection。
2. **纯函数派生**（无 DB）：stats + fingerprint（canonical JSON + SHA-256，
   peer list 按 peer_company_id 排序）。
3. **短 PG transaction**：create_or_get Comparison（ON CONFLICT(comparison_fingerprint)，
   无进程锁）→ created 时同事务 bulk insert peer links；否则（replay / 并发输家）
   **不写 links**，重新加载并完整校验（ValuationIntegrityError，不自动 repair）。
   任何 SQLAlchemyError → 整条 rollback + ValuationPersistenceFailed（0
   partial write）。并发 → 最终 1 Comparison + 1 套完整 peer links。

**Comparison 不是 EvidenceCard**；不复制全部 evidence id 到本表（provenance 经
observation 链接）。不做交易建议 / 目标价 / 盈利预测 / 绝对公允价值；不调用
LLM / Chroma / Retrieval。
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.claims.macro_policy import resolve_availability
from app.db.models.company import CompanyModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.relative_valuation_comparison import RelativeValuationComparisonModel
from app.db.models.relative_valuation_comparison_peer import (
    RelativeValuationComparisonPeerModel,
)
from app.db.models.source_record import SourceRecordModel
from app.db.models.valuation_metric_observation import ValuationMetricObservationModel
from app.repositories.relative_valuation_comparison_peer_repository import (
    RelativeValuationComparisonPeerRepository,
)
from app.repositories.relative_valuation_comparison_repository import (
    RelativeValuationComparisonRepository,
)
from app.repositories.valuation_metric_observation_repository import (
    ValuationMetricObservationRepository,
)
from app.valuation.contracts import (
    RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION,
    VALUATION_FORMULA_VERSION,
    ComparisonDraft,
    ComparisonMethod,
    ComparisonResult,
    DerivedComparisonStats,
    compute_comparison_fingerprint,
)
from app.valuation.errors import (
    ValuationCompanyMismatch,
    ValuationCompanyNotFound,
    ValuationDateMismatch,
    ValuationError,
    ValuationFutureEvidence,
    ValuationIntegrityError,
    ValuationMetricMismatch,
    ValuationMetricNotComparable,
    ValuationObservationNotFound,
    ValuationPeerDuplicateError,
    ValuationPeerIncludesTargetError,
    ValuationPersistenceFailed,
)
from app.valuation.number_parser import validate_valuation_decimal_storage

# 除法结果固定保留 12 位小数、银行家舍入（与 Financial calculations 一致）。
CALCULATION_SCALE = 12
_QUANTUM = Decimal("0.000000000001")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def compute_peer_median(values: list[Decimal]) -> Decimal:
    """peer 中位数：奇数取中位；偶数取两中位的算术平均（全 Decimal，无 float）。

    结果 quantize 到 CALCULATION_SCALE / ROUND_HALF_EVEN（偶数两中位平均可能
    超出 12 位小数，确定性舍入后才可无失真存入 NUMERIC(38,12)）。
    """
    sorted_values = sorted(values)
    n = len(sorted_values)
    mid = n // 2
    if n % 2 == 1:
        return _quantize(sorted_values[mid])
    return _quantize((sorted_values[mid - 1] + sorted_values[mid]) / Decimal(2))


def compute_comparison_stats(
    target_value: Decimal, peer_values: list[Decimal]
) -> DerivedComparisonStats:
    """comparison_method=peer_median 的确定性派生统计（全 Decimal，无 float）。

    调用方保证 peer_values 非空（>=3）且全部 > 0（否则 median / premium 失去
    解释力；可比较性由 service 校验阶段拒绝）。
    """
    peer_median = compute_peer_median(peer_values)
    peer_min = min(peer_values)
    peer_max = max(peer_values)
    # premium / (discount) to median：正值 = 相对溢价，负值 = 相对折价。
    premium_discount_to_median = _quantize((target_value - peer_median) / peer_median)
    return DerivedComparisonStats(
        comparison_method=ComparisonMethod.PEER_MEDIAN.value,
        peer_count=len(peer_values),
        peer_median=peer_median,
        peer_min=peer_min,
        peer_max=peer_max,
        premium_discount_to_median=premium_discount_to_median,
    )


@dataclass(frozen=True)
class _LoadedComparisonRefs:
    """加载并校验后的真实 PG 引用。"""

    target_observation: ValuationMetricObservationModel
    peer_observations: tuple[ValuationMetricObservationModel, ...]
    evidence: dict[UUID, EvidenceCardModel]  # card_id -> card
    sources: dict[UUID, SourceRecordModel]  # source_id -> source
    companies: dict[UUID, CompanyModel]  # company_id -> company


@dataclass(frozen=True)
class _DerivedComparison:
    """纯函数阶段派生的全部确定性值。"""

    metric_code: str
    metric_as_of: object
    stats: DerivedComparisonStats
    peer_entries: list[dict]  # 按 peer_company_id 排序，供 fingerprint
    comparison_fingerprint: str


@dataclass(frozen=True)
class VerifiedComparison:
    """`verify_comparison_integrity` 的返回：完整校验后的 comparison + 真实引用。

    供 4C.2B.1 ValuationClaimService / 未来 Analyst 复用（spec I）：一次调用
    同时获得 comparison integrity 验证、真实 peer company / observation 集合、
    target / peer Observation 与全部 source EvidenceCard（供 automatic Evidence
    expansion 与 critical policy），**无需复制 formula / replay logic**。
    """

    comparison_id: UUID
    comparison_fingerprint: str
    target_company_id: UUID
    target_observation_id: UUID
    analysis_as_of: date
    metric_code: str
    metric_as_of: date
    peer_companies: tuple[UUID, ...]  # 去重后的真实 peer company 集合
    peer_observation_ids: tuple[UUID, ...]  # 全部 peer observation id
    target_observation: ValuationMetricObservationModel
    peer_observations: tuple[ValuationMetricObservationModel, ...]
    evidence: dict[UUID, EvidenceCardModel]  # card_id -> card（target + peers 的 source Evidence）
    # 已通过 replay 完整性核实的派生统计（4C.2B.2 供 ValuationAnalysisService
    # 构造 comparison pack；值来自 persisted 行且与重新派生一致，不复制 formula）。
    target_value: Decimal
    peer_median: Decimal
    peer_min: Decimal
    peer_max: Decimal
    premium_discount_to_median: Decimal
    peer_count: int
    comparison_method: str
    formula_version: int


class RelativeValuationComparisonService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_comparison(self, draft: ComparisonDraft) -> ComparisonResult:
        """登记一次确定性相对估值比较（无 partial write，并发最终 1 行 + 完整 peers）。"""
        # 1. 短 DB session：加载并校验全部真实引用（连接即刻关闭）。
        async with self._sessionmaker() as session:
            loaded = await self._load_validate(session, draft)

        # 2. 纯函数派生（不持有 DB 连接）。
        derived = self._derive(draft, loaded)

        # 3. 短 PG transaction：create_or_get + peer links / replay 完整性校验。
        async with self._sessionmaker() as session:
            try:
                comp_repo = RelativeValuationComparisonRepository(session)
                comparison = RelativeValuationComparisonModel(
                    comparison_id=uuid.uuid4(),
                    **self._comparison_kwargs(draft, derived),
                )
                persisted, created = await comp_repo.create_or_get(comparison)
                if created:
                    # 本事务创建了 Comparison：同事务写完整 peer links（任一失败 →
                    # 整条 rollback，0 partial write）。
                    await RelativeValuationComparisonPeerRepository(session).bulk_insert(
                        self._peer_links(persisted.comparison_id, loaded.peer_observations)
                    )
                else:
                    # replay / 并发输家：不写 links（已存在），完整校验。
                    await self._verify_replay(session, persisted, draft)
                await session.commit()
            except ValuationIntegrityError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ValuationPersistenceFailed() from exc

        return ComparisonResult(
            comparison_id=persisted.comparison_id,
            comparison_fingerprint=persisted.comparison_fingerprint,
            replayed=not created,
        )

    # ------------------------------------------------------------------ 加载校验

    async def _load_validate(
        self, session: AsyncSession, draft: ComparisonDraft
    ) -> _LoadedComparisonRefs:
        """加载 target + 全部 peer Observation / Companies / Evidence / Source 并逐条校验。

        - 全部 observation 存在（缺失 → ValuationObservationNotFound）；
        - target observation 的 company == draft.target_company_id（否则
          ValuationCompanyMismatch）；
        - peer 公司互不相同（ValuationPeerDuplicateError）且不含 target 公司
          （ValuationPeerIncludesTargetError）——显式 peer 集合，不自动过滤；
        - 全部 observation 同一 metric_code（ValuationMetricMismatch）且同一
          metric_as_of（ValuationDateMismatch，严格 same-date）；
        - 全部 metric_value > 0（ValuationMetricNotComparable）；
        - 全部公司存在（ValuationCompanyNotFound）；
        - no-lookahead：每个 observation 的来源文档 availability
          （published_at 否则 acquired_at，复用 resolve_availability，绝不用
          reporting_period_end）<= analysis_as_of（ValuationFutureEvidence）；
          且 analysis_as_of >= metric_as_of。
        """
        obs_ids = [draft.target_observation_id, *draft.peer_observation_ids]
        obs_by_id = await ValuationMetricObservationRepository(session).list_by_ids(obs_ids)
        if len(obs_by_id) != len(obs_ids):
            raise ValuationObservationNotFound()
        target_obs = obs_by_id[draft.target_observation_id]
        if target_obs.company_id != draft.target_company_id:
            raise ValuationCompanyMismatch()
        peer_obs = tuple(obs_by_id[pid] for pid in draft.peer_observation_ids)
        peer_companies = [obs.company_id for obs in peer_obs]
        if len(set(peer_companies)) != len(peer_companies):
            raise ValuationPeerDuplicateError()
        if draft.target_company_id in set(peer_companies):
            raise ValuationPeerIncludesTargetError()
        codes = {obs.metric_code for obs in obs_by_id.values()}
        if len(codes) != 1:
            raise ValuationMetricMismatch()
        dates = {obs.metric_as_of for obs in obs_by_id.values()}
        if len(dates) != 1:
            raise ValuationDateMismatch()
        for obs in obs_by_id.values():
            if obs.metric_value <= 0:
                raise ValuationMetricNotComparable()

        companies = await self._load_companies(session, {draft.target_company_id, *peer_companies})
        evidence = await self._load_evidence(session, obs_by_id.values())
        sources = await self._load_sources(session, evidence.values())

        metric_as_of = target_obs.metric_as_of
        if draft.analysis_as_of < metric_as_of:
            raise ValuationFutureEvidence("analysis_as_of 早于 metric_as_of，违反 no-lookahead")
        for obs in obs_by_id.values():
            availability = self._availability(obs, evidence, sources)
            if availability is None:
                raise ValuationIntegrityError(
                    "valuation comparison evidence availability missing (corrupted provenance)"
                )
            if availability.date() > draft.analysis_as_of:
                raise ValuationFutureEvidence()

        return _LoadedComparisonRefs(
            target_observation=target_obs,
            peer_observations=peer_obs,
            evidence=evidence,
            sources=sources,
            companies=companies,
        )

    @staticmethod
    async def _load_companies(
        session: AsyncSession, company_ids: set[UUID]
    ) -> dict[UUID, CompanyModel]:
        result = await session.execute(
            select(CompanyModel).where(CompanyModel.company_id.in_(company_ids))
        )
        companies = {c.company_id: c for c in result.scalars().all()}
        if len(companies) != len(company_ids):
            raise ValuationCompanyNotFound()
        return companies

    @staticmethod
    async def _load_evidence(
        session: AsyncSession, observations: list[ValuationMetricObservationModel]
    ) -> dict[UUID, EvidenceCardModel]:
        card_ids = {obs.source_evidence_card_id for obs in observations}
        result = await session.execute(
            select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(card_ids))
        )
        evidence = {c.evidence_card_id: c for c in result.scalars().all()}
        if len(evidence) != len(card_ids):
            raise ValuationIntegrityError(
                "valuation comparison evidence card missing (corrupted provenance)"
            )
        return evidence

    @staticmethod
    async def _load_sources(
        session: AsyncSession, cards: list[EvidenceCardModel]
    ) -> dict[UUID, SourceRecordModel]:
        source_ids = {c.source_id for c in cards if c.source_id is not None}
        if not source_ids:
            raise ValuationIntegrityError(
                "valuation comparison evidence source missing (corrupted provenance)"
            )
        result = await session.execute(
            select(SourceRecordModel).where(SourceRecordModel.source_id.in_(source_ids))
        )
        sources = {s.source_id: s for s in result.scalars().all()}
        if len(sources) != len(source_ids):
            raise ValuationIntegrityError(
                "valuation comparison evidence source missing (corrupted provenance)"
            )
        return sources

    @staticmethod
    def _availability(
        obs: ValuationMetricObservationModel,
        evidence: dict[UUID, EvidenceCardModel],
        sources: dict[UUID, SourceRecordModel],
    ):
        """observation 来源文档的信息可得时间（复用 frozen availability 纯 helper）。

        observation 绑定的 Evidence 恒为 document_chunk（创建时锁定），故
        resolve_availability 只会走 document 分支（published_at 否则
        acquired_at），**绝不用 reporting_period_end**；绝不接受 macro origin。
        """
        card = evidence[obs.source_evidence_card_id]
        source = sources.get(card.source_id)
        if source is None:
            raise ValuationIntegrityError(
                "valuation comparison evidence source missing (corrupted provenance)"
            )
        return resolve_availability(
            origin_type=card.origin_type,
            snapshot_fetched_at=None,
            source_published_at=source.published_at,
            source_acquired_at=source.acquired_at,
        )

    # ------------------------------------------------------------------ 纯函数派生

    def _derive(self, draft: ComparisonDraft, loaded: _LoadedComparisonRefs) -> _DerivedComparison:
        """纯函数派生：stats + storage guard + fingerprint。"""
        target_value = loaded.target_observation.metric_value
        peer_values = [obs.metric_value for obs in loaded.peer_observations]
        stats = compute_comparison_stats(target_value, peer_values)
        # NUMERIC(38,12) 存储契约：全部派生数值必须无失真落库（禁止静默
        # quantize / round / truncate；quantize 只用于除法舍入语义，不用于掩盖溢出）。
        for value in (
            stats.peer_median,
            stats.peer_min,
            stats.peer_max,
            stats.premium_discount_to_median,
        ):
            validate_valuation_decimal_storage(value)

        peer_entries = sorted(
            (
                {
                    "peer_company_id": str(obs.company_id),
                    "peer_observation_id": str(obs.valuation_observation_id),
                    "observation_fingerprint": obs.valuation_observation_fingerprint,
                }
                for obs in loaded.peer_observations
            ),
            key=lambda entry: entry["peer_company_id"],
        )
        metric_code = loaded.target_observation.metric_code
        metric_as_of = loaded.target_observation.metric_as_of
        fingerprint = compute_comparison_fingerprint(
            comparison_schema_version=RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION,
            formula_version=VALUATION_FORMULA_VERSION,
            comparison_method=stats.comparison_method,
            target_company_id=draft.target_company_id,
            target_observation_id=draft.target_observation_id,
            target_observation_fingerprint=loaded.target_observation.valuation_observation_fingerprint,
            metric_code=metric_code,
            metric_as_of=metric_as_of,
            analysis_as_of=draft.analysis_as_of,
            peers=peer_entries,
            peer_median=stats.peer_median,
            peer_min=stats.peer_min,
            peer_max=stats.peer_max,
            premium_discount_to_median=stats.premium_discount_to_median,
        )
        return _DerivedComparison(
            metric_code=metric_code,
            metric_as_of=metric_as_of,
            stats=stats,
            peer_entries=peer_entries,
            comparison_fingerprint=fingerprint,
        )

    @staticmethod
    def _comparison_kwargs(draft: ComparisonDraft, derived: _DerivedComparison) -> dict:
        return {
            "target_company_id": draft.target_company_id,
            "target_observation_id": draft.target_observation_id,
            "metric_code": derived.metric_code,
            "metric_as_of": derived.metric_as_of,
            "analysis_as_of": draft.analysis_as_of,
            "comparison_method": derived.stats.comparison_method,
            "peer_count": derived.stats.peer_count,
            "peer_median": derived.stats.peer_median,
            "peer_min": derived.stats.peer_min,
            "peer_max": derived.stats.peer_max,
            "premium_discount_to_median": derived.stats.premium_discount_to_median,
            "comparison_schema_version": RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION,
            "formula_version": VALUATION_FORMULA_VERSION,
            "comparison_fingerprint": derived.comparison_fingerprint,
        }

    @staticmethod
    def _peer_links(
        comparison_id: UUID, peer_observations: tuple[ValuationMetricObservationModel, ...]
    ) -> list[RelativeValuationComparisonPeerModel]:
        return [
            RelativeValuationComparisonPeerModel(
                comparison_id=comparison_id,
                peer_company_id=obs.company_id,
                peer_observation_id=obs.valuation_observation_id,
            )
            for obs in peer_observations
        ]

    # ------------------------------------------------------------------ replay

    async def _verify_replay(
        self,
        session: AsyncSession,
        persisted: RelativeValuationComparisonModel,
        draft: ComparisonDraft,
    ) -> _LoadedComparisonRefs:
        """已有 fingerprint 的 comparison replay 完整性校验。

        重新加载 target observation / peer links / peer observations / evidence
        provenance，重新执行 metric / date / positive / peer distinctness /
        no-lookahead 校验与 stats / fingerprint 派生，逐项核实 persisted
        comparison 与 peer links。发现损坏只抛 ValuationIntegrityError，
        **不自动 repair**（修改 = 新 comparison，无 update API）。

        返回 `_LoadedComparisonRefs`（已重新加载校验的真实引用），供
        `verify_comparison_integrity` / 调用方复用，避免重复加载。
        """
        loaded = await self._load_validate(session, draft)
        derived = self._derive(draft, loaded)
        pairs = (
            ("target_company_id", persisted.target_company_id, draft.target_company_id),
            (
                "target_observation_id",
                persisted.target_observation_id,
                draft.target_observation_id,
            ),
            ("metric_code", persisted.metric_code, derived.metric_code),
            ("metric_as_of", persisted.metric_as_of, derived.metric_as_of),
            ("analysis_as_of", persisted.analysis_as_of, draft.analysis_as_of),
            (
                "comparison_method",
                persisted.comparison_method,
                derived.stats.comparison_method,
            ),
            ("peer_count", persisted.peer_count, derived.stats.peer_count),
            ("peer_median", persisted.peer_median, derived.stats.peer_median),
            ("peer_min", persisted.peer_min, derived.stats.peer_min),
            ("peer_max", persisted.peer_max, derived.stats.peer_max),
            (
                "premium_discount_to_median",
                persisted.premium_discount_to_median,
                derived.stats.premium_discount_to_median,
            ),
            (
                "comparison_schema_version",
                persisted.comparison_schema_version,
                RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION,
            ),
            (
                "formula_version",
                persisted.formula_version,
                VALUATION_FORMULA_VERSION,
            ),
            (
                "comparison_fingerprint",
                persisted.comparison_fingerprint,
                derived.comparison_fingerprint,
            ),
        )
        for name, stored, expected in pairs:
            if stored != expected:
                raise ValuationIntegrityError(
                    f"valuation comparison replay integrity check failed on {name}"
                )
        links = await RelativeValuationComparisonPeerRepository(session).list_by_comparison(
            persisted.comparison_id
        )
        actual = {(link.peer_company_id, link.peer_observation_id) for link in links}
        expected = {
            (obs.company_id, obs.valuation_observation_id) for obs in loaded.peer_observations
        }
        if actual != expected:
            raise ValuationIntegrityError(
                "valuation comparison replay integrity check failed on peer links"
            )
        return loaded

    # -------------------------------------------------- shared helper（4C.2B.1 spec I）

    async def _verify_persisted_replay(
        self,
        session: AsyncSession,
        persisted: RelativeValuationComparisonModel,
    ) -> _LoadedComparisonRefs:
        """从 persisted comparison + peer links 重建 draft 并完整重放校验。

        peer links 是持久化事实；若损坏（<3 / 重复 / 含 target / 缺 observation），
        `ComparisonDraft` 构造或 `_verify_replay` 会抛 `ValuationInputError` 等
        普通 `ValuationError`——这些在 replay/integrity API 语境下都是**已持久化
        数据损坏**的信号，由 `verify_comparison_integrity` 统一包装为
        `ValuationIntegrityError`（不复制 formula / replay logic）。
        """
        links = await RelativeValuationComparisonPeerRepository(session).list_by_comparison(
            persisted.comparison_id
        )
        draft = ComparisonDraft(
            target_company_id=persisted.target_company_id,
            target_observation_id=persisted.target_observation_id,
            peer_observation_ids=tuple(link.peer_observation_id for link in links),
            analysis_as_of=persisted.analysis_as_of,
        )
        return await self._verify_replay(session, persisted, draft)

    async def verify_comparison_integrity(
        self, session: AsyncSession, comparison_id: UUID
    ) -> VerifiedComparison | None:
        """加载并完整校验一个既有 comparison 的内部一致性（供 Claim service / Analyst 复用）。

        用 comparison 的**自身持久化字段** + peer links 重建 `ComparisonDraft`，
        重新执行 `_load_validate` + `_derive` + `_verify_replay`（metric / date /
        positive / peer distinctness / no-lookahead / stats / fingerprint /
        peer links 全部逐项核实）——**不复制 formula / replay logic**，直接复用
        本 service 的既有实现。

        - comparison_id 不存在 → 返回 None（由调用方决定错误语义，如
          `ValuationClaimComparisonNotFound`）；
        - comparison 存在但任一内部损坏（含 peer links 缺失 / 少于 3 / 重复 /
          含 target 导致 `ComparisonDraft` 构造失败，或 replay 校验发现字段 /
          stats / fingerprint / links 不符）→ `ValuationIntegrityError`（包装自
          `ValuationError`，保留 `raise ... from exc`），**不自动 repair**。

        Gate 0（4C.2B.2）：本 replay/integrity API 的稳定错误边界——除 comparison
        missing 返回 None 外，任何由 **persisted DB state** 导致的 validation
        failure 一律以 `ValuationIntegrityError` 呈现，**不泄漏** `ValuationInputError`
        等普通输入错误；`create_comparison(new draft)` 的用户输入 taxonomy 不受影响。
        """
        persisted = await RelativeValuationComparisonRepository(session).get_by_id(comparison_id)
        if persisted is None:
            return None
        try:
            loaded = await self._verify_persisted_replay(session, persisted)
        except ValuationIntegrityError:
            raise
        except ValuationError as exc:
            raise ValuationIntegrityError(
                "valuation comparison persisted state failed integrity validation"
            ) from exc
        return VerifiedComparison(
            comparison_id=persisted.comparison_id,
            comparison_fingerprint=persisted.comparison_fingerprint,
            target_company_id=persisted.target_company_id,
            target_observation_id=persisted.target_observation_id,
            analysis_as_of=persisted.analysis_as_of,
            metric_code=persisted.metric_code,
            metric_as_of=persisted.metric_as_of,
            peer_companies=tuple(
                sorted({obs.company_id for obs in loaded.peer_observations}, key=str)
            ),
            peer_observation_ids=tuple(
                obs.valuation_observation_id for obs in loaded.peer_observations
            ),
            target_observation=loaded.target_observation,
            peer_observations=loaded.peer_observations,
            evidence=loaded.evidence,
            target_value=loaded.target_observation.metric_value,
            peer_median=persisted.peer_median,
            peer_min=persisted.peer_min,
            peer_max=persisted.peer_max,
            premium_discount_to_median=persisted.premium_discount_to_median,
            peer_count=persisted.peer_count,
            comparison_method=persisted.comparison_method,
            formula_version=persisted.formula_version,
        )
