"""Financial calculation service (stage 4B.2B).

`create_calculation(draft)` 把**已登记 FinancialMetricObservation** 通过确定性
公式计算为派生财务事实（同比 / 环比 / margin / ratio），形成
Calculation → Observation → EvidenceCard → Source 证据链。**0 LLM / 0 Chroma /
0 Analyst / 0 Claim / 0 Report / 0 Audit**；只消费已登记 Observation。

流程（两步提交结构，镜像 FinancialMetricService）：
1. 短 DB session：按 input_role 加载每个 Observation（缺失 →
   FinancialCalculationObservationNotFound），随后立即关闭 connection（纯函数
   阶段不持有 DB 连接）。
2. 纯函数派生（无 DB）：
   - comparability：company 必须 == draft.company_id（CompanyMismatch）；
     statement_scope 必须完全相同（ScopeMismatch）；metric_code 必须精确匹配
     role 期望 / current==baseline（InputMismatch）；
   - period 规则：absolute_change 要求 period_kind 相同；YoY 要求月/日对应 +
     baseline 年份 = current 年份 - 1；QoQ 只允许标准单季度（duration）或
     03-31/06-30/09-30/12-31（instant）且连续季度（PeriodMismatch）；
   - 公式：absolute_change = current - baseline（精确减法）；增长率 baseline
     必须 > 0（GrowthBaseNotPositive）；ratio 分母必须 > 0
     （ZeroDenominator）；除法 quantize 到 CALCULATION_SCALE=12 位、
     ROUND_HALF_EVEN；
   - storage bounds：result_value 必须能无失真存入 NUMERIC(38,12）
     （StorageRangeError，禁止静默 quantize / round / truncate）；
   - fingerprint：canonical JSON + SHA-256（含按 input_role 排序的
     (role, obs_id, obs fingerprint)）。
3. 短 DB transaction：create_or_get（ON CONFLICT(calculation_fingerprint)，
   无进程锁）→ created=True 时插入 inputs；created=False 时**重新加载
   Observation + 重新派生 + 逐项核实** persisted calculation 与其 inputs
   （任一损坏 → FinancialCalculationIntegrityError，**不自动 repair**）。
   任何 SQLAlchemyError → 整条 rollback + FinancialCalculationPersistenceFailed
   （0 partial write）。并发 → 最终 1 calculation。

**不访问** Chroma / BGE / LLM / RawArtifact bytes；**不复制** locator_refs。
同一完全相同输入 → replay 同一行；输入任一变化 → 新 calculation，旧行保留
（无 update API）。
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.financial_calculation import (
    FinancialCalculationInputModel,
    FinancialCalculationModel,
)
from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.financial.calculations.contracts import (
    FINANCIAL_CALCULATION_SCHEMA_VERSION,
    FORMULA_VERSION,
    CalculationCode,
    FinancialCalculationDraft,
    FinancialCalculationResult,
    InputRole,
    calculation_input_roles,
    compute_calculation_fingerprint,
    expected_metric_code,
)
from app.financial.calculations.errors import (
    FinancialCalculationCompanyMismatch,
    FinancialCalculationInputMismatch,
    FinancialCalculationIntegrityError,
    FinancialCalculationObservationNotFound,
    FinancialCalculationPeriodMismatch,
    FinancialCalculationPersistenceFailed,
    FinancialCalculationScopeMismatch,
    FinancialCalculationStorageRangeError,
)
from app.financial.calculations.formulas import compute_calculation_result
from app.financial.contracts import PeriodKind
from app.financial.number_parser import fits_numeric_38_12
from app.repositories.financial_calculation_input_repository import (
    FinancialCalculationInputRepository,
)
from app.repositories.financial_calculation_repository import (
    FinancialCalculationRepository,
)
from app.repositories.financial_metric_observation_repository import (
    FinancialMetricObservationRepository,
)

# duration 单季度的季末日（按季末月）。
_QUARTER_END_DAY = {3: 31, 6: 30, 9: 30, 12: 31}
_QUARTER_START_MONTHS = frozenset((1, 4, 7, 10))
_QUARTER_ENDS = frozenset(((3, 31), (6, 30), (9, 30), (12, 31)))

_GROWTH_CODES = frozenset(
    (
        CalculationCode.ABSOLUTE_CHANGE_CNY,
        CalculationCode.YOY_GROWTH_RATE,
        CalculationCode.QOQ_GROWTH_RATE,
    )
)


@dataclass(frozen=True)
class _DerivedCalculation:
    """纯函数阶段派生的全部确定性值（result_value 已 quantize / 已过 storage）。"""

    result_value: Decimal
    result_unit: str
    calculation_schema_version: int
    formula_version: int
    calculation_fingerprint: str


def _same_month_day(a: date, b: date) -> bool:
    return a.month == b.month and a.day == b.day


def _yoy_comparable(
    current: FinancialMetricObservationModel,
    baseline: FinancialMetricObservationModel,
) -> bool:
    """YoY 可比：baseline 年份 = current 年份 - 1，且月/日对应（duration 同时
    要求 period_start 对应；instant 两者 period_start 均为 None）。"""
    if current.period_start is not None or baseline.period_start is not None:
        if current.period_start is None or baseline.period_start is None:
            return False
        if baseline.period_start.year != current.period_start.year - 1:
            return False
        if not _same_month_day(current.period_start, baseline.period_start):
            return False
    return baseline.period_end.year == current.period_end.year - 1 and _same_month_day(
        current.period_end, baseline.period_end
    )


def _is_quarter_end(day: date) -> bool:
    return (day.month, day.day) in _QUARTER_ENDS


def _is_single_quarter(obs: FinancialMetricObservationModel) -> bool:
    """duration 单季度：period_start 必须是季首日（01/04/07/10-01）、period_end
    必须是同一季末（03-31 / 06-30 / 09-30 / 12-31）。"""
    start, end = obs.period_start, obs.period_end
    if start is None or end is None:
        return False
    if start.day != 1 or start.month not in _QUARTER_START_MONTHS:
        return False
    if end.year != start.year:
        return False
    if end.month != start.month + 2:
        return False
    return end.day == _QUARTER_END_DAY[end.month]


def _quarter_index(day: date) -> int:
    quarter = (day.month - 1) // 3 + 1
    return day.year * 4 + quarter


def _consecutive(current_end: date, baseline_end: date) -> bool:
    """baseline 是 current 的紧邻前一个季度（跨年由 year*4+quarter 处理）。"""
    return _quarter_index(baseline_end) == _quarter_index(current_end) - 1


class FinancialCalculationService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_calculation(
        self, draft: FinancialCalculationDraft
    ) -> FinancialCalculationResult:
        """计算并登记一条确定性的派生财务事实（无 partial write，并发最终 1 行）。"""
        # 1. 短 DB session：加载输入 Observation（连接即刻关闭）。
        async with self._sessionmaker() as session:
            observations = await self._load_observations(session, draft)

        # 2. 纯函数派生（不持有 DB 连接）。
        derived = self._derive(draft, observations)

        # 3. 短 DB transaction：create_or_get + 插入 inputs / replay 校验。
        async with self._sessionmaker() as session:
            try:
                calc_repo = FinancialCalculationRepository(session)
                input_repo = FinancialCalculationInputRepository(session)
                calculation = FinancialCalculationModel(
                    calculation_id=uuid.uuid4(),
                    **self._calculation_kwargs(draft, derived),
                )
                persisted, created = await calc_repo.create_or_get(calculation)
                if created:
                    await input_repo.insert_inputs(self._input_bindings(persisted, draft))
                else:
                    await self._verify_replay(session, persisted, draft)
                await session.commit()
            except FinancialCalculationIntegrityError:
                # replay 校验发现既有 calculation 损坏 → 显式回滚本事务，然后抛出。
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise FinancialCalculationPersistenceFailed() from exc

        return FinancialCalculationResult(
            calculation_id=persisted.calculation_id,
            calculation_fingerprint=persisted.calculation_fingerprint,
            replayed=not created,
        )

    # ------------------------------------------------------------------ 内部

    @staticmethod
    async def _load_observations(
        session: AsyncSession, draft: FinancialCalculationDraft
    ) -> dict[InputRole, FinancialMetricObservationModel]:
        """按 role 加载输入 Observation；缺失 → FinancialCalculationObservationNotFound。"""
        obs_repo = FinancialMetricObservationRepository(session)
        observations: dict[InputRole, FinancialMetricObservationModel] = {}
        for role in calculation_input_roles(draft.calculation_code):
            obs_id = draft.input_observation_ids[role]
            obs = await obs_repo.get_by_id(obs_id)
            if obs is None:
                raise FinancialCalculationObservationNotFound(
                    f"observation not found for role {role.value}"
                )
            observations[role] = obs
        return observations

    @staticmethod
    def _validate_comparability(
        draft: FinancialCalculationDraft,
        observations: dict[InputRole, FinancialMetricObservationModel],
    ) -> None:
        """company / statement_scope / metric_code 可比性（不自动纠错）。"""
        for role, obs in observations.items():
            if obs.company_id != draft.company_id:
                raise FinancialCalculationCompanyMismatch(
                    f"role {role.value} 的 observation company 与 draft 不一致"
                )
        scopes = {obs.statement_scope for obs in observations.values()}
        if len(scopes) != 1:
            raise FinancialCalculationScopeMismatch(
                "输入 observation 的 statement_scope 必须完全相同"
            )
        if draft.calculation_code in _GROWTH_CODES:
            current = observations[InputRole.CURRENT]
            baseline = observations[InputRole.BASELINE]
            if current.metric_code != baseline.metric_code:
                raise FinancialCalculationInputMismatch(
                    "current 与 baseline 的 metric_code 必须相同"
                )
            return
        for role, obs in observations.items():
            expected = expected_metric_code(role)
            if expected is not None and obs.metric_code != expected.value:
                raise FinancialCalculationInputMismatch(
                    f"role {role.value} 的 metric_code 必须是 {expected.value}"
                )

    @staticmethod
    def _validate_periods(
        code: CalculationCode,
        observations: dict[InputRole, FinancialMetricObservationModel],
    ) -> None:
        """period 可比性规则（absolute / YoY / QoQ；margin / ratio 无 period 要求）。"""
        if code not in _GROWTH_CODES:
            return
        current = observations[InputRole.CURRENT]
        baseline = observations[InputRole.BASELINE]
        if current.period_kind != baseline.period_kind:
            raise FinancialCalculationPeriodMismatch("current 与 baseline 的 period_kind 必须相同")
        if code == CalculationCode.ABSOLUTE_CHANGE_CNY:
            return
        if code == CalculationCode.YOY_GROWTH_RATE:
            if not _yoy_comparable(current, baseline):
                raise FinancialCalculationPeriodMismatch(
                    "YoY 必须月/日对应且 baseline 年份 = current 年份 - 1"
                )
            return
        # QoQ：只允许标准单季度（duration）或 03-31/06-30/09-30/12-31（instant），
        # 且连续季度。
        if current.period_kind == PeriodKind.INSTANT.value:
            if current.period_start is not None or baseline.period_start is not None:
                raise FinancialCalculationPeriodMismatch("instant QoQ 的 period_start 必须为 None")
            if not _is_quarter_end(current.period_end) or not _is_quarter_end(baseline.period_end):
                raise FinancialCalculationPeriodMismatch(
                    "instant QoQ 的 period_end 必须是 03-31/06-30/09-30/12-31"
                )
        else:
            if not _is_single_quarter(current) or not _is_single_quarter(baseline):
                raise FinancialCalculationPeriodMismatch(
                    "duration QoQ 必须是标准单季度（period_start 为季首日、period_end 为季末日）"
                )
        if not _consecutive(current.period_end, baseline.period_end):
            raise FinancialCalculationPeriodMismatch("QoQ 必须是连续季度")

    def _derive(
        self,
        draft: FinancialCalculationDraft,
        observations: dict[InputRole, FinancialMetricObservationModel],
    ) -> _DerivedCalculation:
        """纯函数派生：comparability → period → 公式 → storage → fingerprint。"""
        self._validate_comparability(draft, observations)
        self._validate_periods(draft.calculation_code, observations)

        values = {role: obs.normalized_value_cny for role, obs in observations.items()}
        result_value, result_unit = compute_calculation_result(draft.calculation_code, values)
        if not fits_numeric_38_12(result_value):
            raise FinancialCalculationStorageRangeError(
                "result_value 超出 NUMERIC(38,12) 存储范围（小数位 > 12 或 abs >= 10^26）"
            )

        inputs = [
            (role.value, obs.metric_observation_id, obs.metric_fingerprint)
            for role, obs in sorted(observations.items(), key=lambda item: item[0].value)
        ]
        fingerprint = compute_calculation_fingerprint(
            calculation_schema_version=FINANCIAL_CALCULATION_SCHEMA_VERSION,
            formula_version=FORMULA_VERSION,
            company_id=draft.company_id,
            calculation_code=draft.calculation_code.value,
            inputs=inputs,
            result_value=result_value,
            result_unit=result_unit.value,
        )
        return _DerivedCalculation(
            result_value=result_value,
            result_unit=result_unit.value,
            calculation_schema_version=FINANCIAL_CALCULATION_SCHEMA_VERSION,
            formula_version=FORMULA_VERSION,
            calculation_fingerprint=fingerprint,
        )

    @staticmethod
    def _calculation_kwargs(draft: FinancialCalculationDraft, derived: _DerivedCalculation) -> dict:
        return {
            "company_id": draft.company_id,
            "calculation_code": draft.calculation_code.value,
            "result_value": derived.result_value,
            "result_unit": derived.result_unit,
            "calculation_schema_version": derived.calculation_schema_version,
            "formula_version": derived.formula_version,
            "calculation_fingerprint": derived.calculation_fingerprint,
        }

    @staticmethod
    def _input_bindings(
        persisted: FinancialCalculationModel, draft: FinancialCalculationDraft
    ) -> list[FinancialCalculationInputModel]:
        return [
            FinancialCalculationInputModel(
                calculation_id=persisted.calculation_id,
                input_role=role.value,
                metric_observation_id=obs_id,
            )
            for role, obs_id in draft.input_observation_ids.items()
        ]

    async def _verify_replay(
        self,
        session: AsyncSession,
        persisted: FinancialCalculationModel,
        draft: FinancialCalculationDraft,
    ) -> None:
        """已有 fingerprint 的 calculation replay 完整性校验。

        重新加载 Observation + 重新派生（检测上游 Observation 是否被篡改导致结果
        不再有效），再逐项核实 persisted calculation 与其 inputs。发现损坏只抛
        FinancialCalculationIntegrityError，**不自动 repair**（修改 = 新
        calculation，无 update API）。
        """
        observations = await self._load_observations(session, draft)
        derived = self._derive(draft, observations)

        input_repo = FinancialCalculationInputRepository(session)
        rows = await input_repo.get_by_calculation_id(persisted.calculation_id)
        bound = {row.input_role: row.metric_observation_id for row in rows}
        expected = {role.value: obs_id for role, obs_id in draft.input_observation_ids.items()}
        if bound != expected:
            raise FinancialCalculationIntegrityError("financial calculation replay inputs mismatch")

        pairs = (
            ("company_id", persisted.company_id, draft.company_id),
            (
                "calculation_code",
                persisted.calculation_code,
                draft.calculation_code.value,
            ),
            ("result_value", persisted.result_value, derived.result_value),
            ("result_unit", persisted.result_unit, derived.result_unit),
            (
                "calculation_schema_version",
                persisted.calculation_schema_version,
                derived.calculation_schema_version,
            ),
            ("formula_version", persisted.formula_version, derived.formula_version),
            (
                "calculation_fingerprint",
                persisted.calculation_fingerprint,
                derived.calculation_fingerprint,
            ),
        )
        for name, stored, expected in pairs:
            if stored != expected:
                raise FinancialCalculationIntegrityError(
                    f"financial calculation replay integrity check failed on {name}"
                )
