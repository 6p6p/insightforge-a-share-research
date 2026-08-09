"""Financial calculation contracts (stage 4B.2B).

把**已登记 FinancialMetricObservation** 通过确定性公式计算为派生财务事实
（同比 / 环比 / margin / ratio），形成
Calculation → Observation → EvidenceCard → Source 证据链。本阶段 0 LLM /
0 Chroma / 0 Analyst / 0 Claim / 0 Report / 0 Audit。

冻结：
- `FINANCIAL_CALCULATION_SCHEMA_VERSION = 1`、`FORMULA_VERSION = 1`；
  v1 calculation_code 只 7 个（见 `CalculationCode`），result_unit 只有
  cny / ratio（ratio 存 0.1234 而非 12.34）。
- 每个 calculation_code → 明确 input roles（deterministic mapping，见
  `calculation_input_roles`）；fixed-role 的 metric_code 期望见
  `expected_metric_code`；current / baseline 的 metric_code 由输入一致决定。
- `FinancialCalculationDraft` 只允许提供语义输入（company_id /
  calculation_code / input_observation_ids 按 role）；**不得**提供
  result_value / result_unit / formula / Evidence ID / source ID / period
  metadata / fingerprint（全部由 FinancialCalculationService 从已登记
  Observation 确定性派生）。
- `calculation_fingerprint` = canonical JSON + SHA-256（含 schema_version /
  formula_version / company / code / 按 input_role 排序的 (role, obs_id,
  obs fingerprint) / result_value canonical string / result_unit；**不含**
  calculation_id / created_at）。同一完全相同输入 → replay 同一行；输入任一
  变化 → 新 fingerprint → 新行，旧行保留（无 update API）。
"""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from app.financial.calculations.errors import (
    FinancialCalculationInputError,
    FinancialCalculationInputMismatch,
)
from app.financial.contracts import MetricCode

# financial_calculations.calculation_schema_version 的当前值（改结构时递增）。
FINANCIAL_CALCULATION_SCHEMA_VERSION = 1
# v1 公式版本（公式语义 / 舍入规则变化时递增）。
FORMULA_VERSION = 1


class CalculationCode(StrEnum):
    """v1 冻结 calculation_code（先少而精，7 个）。"""

    ABSOLUTE_CHANGE_CNY = "absolute_change_cny"
    YOY_GROWTH_RATE = "yoy_growth_rate"
    QOQ_GROWTH_RATE = "qoq_growth_rate"
    GROSS_MARGIN = "gross_margin"
    OPERATING_MARGIN = "operating_margin"
    NET_MARGIN_PARENT = "net_margin_parent"
    DEBT_TO_ASSETS_RATIO = "debt_to_assets_ratio"


class CalculationResultUnit(StrEnum):
    """结果单位：cny（金额差值）/ ratio（比率，存 0.1234 而非 12.34）。"""

    CNY = "cny"
    RATIO = "ratio"


class InputRole(StrEnum):
    """calculation 输入的角色（每个 role 恰好绑定一个 Observation）。"""

    CURRENT = "current"
    BASELINE = "baseline"
    REVENUE = "revenue"
    OPERATING_COST = "operating_cost"
    OPERATING_PROFIT = "operating_profit"
    NET_PROFIT_PARENT = "net_profit_parent"
    TOTAL_ASSETS = "total_assets"
    TOTAL_LIABILITIES = "total_liabilities"


# calculation_code → 输入 roles（冻结顺序，deterministic mapping）。
_ROLE_BY_CODE: dict[CalculationCode, tuple[InputRole, ...]] = {
    CalculationCode.ABSOLUTE_CHANGE_CNY: (InputRole.CURRENT, InputRole.BASELINE),
    CalculationCode.YOY_GROWTH_RATE: (InputRole.CURRENT, InputRole.BASELINE),
    CalculationCode.QOQ_GROWTH_RATE: (InputRole.CURRENT, InputRole.BASELINE),
    CalculationCode.GROSS_MARGIN: (InputRole.REVENUE, InputRole.OPERATING_COST),
    CalculationCode.OPERATING_MARGIN: (InputRole.REVENUE, InputRole.OPERATING_PROFIT),
    CalculationCode.NET_MARGIN_PARENT: (InputRole.REVENUE, InputRole.NET_PROFIT_PARENT),
    CalculationCode.DEBT_TO_ASSETS_RATIO: (
        InputRole.TOTAL_ASSETS,
        InputRole.TOTAL_LIABILITIES,
    ),
}

# 固定 role → 期望 metric_code（current / baseline 没有固定 code：由输入一致决定）。
_FIXED_ROLE_METRIC_CODE: dict[InputRole, MetricCode] = {
    InputRole.REVENUE: MetricCode.REVENUE,
    InputRole.OPERATING_COST: MetricCode.OPERATING_COST,
    InputRole.OPERATING_PROFIT: MetricCode.OPERATING_PROFIT,
    InputRole.NET_PROFIT_PARENT: MetricCode.NET_PROFIT_PARENT,
    InputRole.TOTAL_ASSETS: MetricCode.TOTAL_ASSETS,
    InputRole.TOTAL_LIABILITIES: MetricCode.TOTAL_LIABILITIES,
}


def supported_calculation_codes() -> tuple[CalculationCode, ...]:
    """v1 支持的全部 calculation_code（冻结顺序）。"""
    return tuple(_ROLE_BY_CODE)


def calculation_input_roles(code: CalculationCode) -> tuple[InputRole, ...]:
    """calculation_code → 输入 roles（冻结顺序；非法 code → InputError）。"""
    if not isinstance(code, CalculationCode):
        raise FinancialCalculationInputError("calculation_code 必须是 CalculationCode")
    roles = _ROLE_BY_CODE.get(code)
    if roles is None:
        raise FinancialCalculationInputError(f"不支持 calculation_code: {code}")
    return roles


def expected_metric_code(role: InputRole) -> MetricCode | None:
    """fixed role → 期望 metric_code；current / baseline → None（由输入一致决定）。"""
    if not isinstance(role, InputRole):
        raise FinancialCalculationInputError("input_role 必须是 InputRole")
    return _FIXED_ROLE_METRIC_CODE.get(role)


def calculation_result_unit(code: CalculationCode) -> CalculationResultUnit:
    """calculation_code → result_unit（cny / ratio；非法 code → InputError）。"""
    if not isinstance(code, CalculationCode):
        raise FinancialCalculationInputError("calculation_code 必须是 CalculationCode")
    if code == CalculationCode.ABSOLUTE_CHANGE_CNY:
        return CalculationResultUnit.CNY
    return CalculationResultUnit.RATIO


@dataclass(frozen=True)
class FinancialCalculationDraft:
    """调用方提交的确定性计算语义输入（构造时校验，不可变）。

    只允许提供：company_id / calculation_code / input_observation_ids
    （每个 input_role 恰好一个已登记 Observation 的 ID）。**不得**提供
    result_value / result_unit / formula / Evidence ID / source ID / period
    metadata / fingerprint（由 FinancialCalculationService 确定性派生）。

    - input_observation_ids 的 role 集合必须与 calculation_code 的
      `calculation_input_roles` 完全一致（不多不少）；
    - 所有 metric_observation_id 必须互不相同（同一 Observation 不能充当多个
      role → FinancialCalculationInputMismatch）；
    - Observation 的 company / statement_scope / metric_code / period
      可比性由 Service 校验并抛对应 FinancialCalculationError。
    """

    company_id: UUID
    calculation_code: CalculationCode
    input_observation_ids: dict[InputRole, UUID]

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise FinancialCalculationInputError("company_id 必须是 UUID")
        if not isinstance(self.calculation_code, CalculationCode):
            raise FinancialCalculationInputError("calculation_code 必须是 CalculationCode")
        roles = calculation_input_roles(self.calculation_code)
        if not isinstance(self.input_observation_ids, dict):
            raise FinancialCalculationInputError("input_observation_ids 必须是 dict")
        provided = set(self.input_observation_ids)
        if provided != set(roles):
            raise FinancialCalculationInputError(
                f"input_observation_ids 的 role 必须恰好是 {[r.value for r in roles]}"
            )
        for role, obs_id in self.input_observation_ids.items():
            if isinstance(obs_id, bool) or not isinstance(obs_id, UUID):
                raise FinancialCalculationInputError(
                    f"input_observation_ids[{role.value}] 必须是 UUID"
                )
        # 同一 Observation 不能充当多个 role（输入去重语义）：draft 内所有
        # metric_observation_id 必须互不相同，避免同源数值被重复当作两个输入。
        obs_ids = list(self.input_observation_ids.values())
        if len(set(obs_ids)) != len(obs_ids):
            raise FinancialCalculationInputMismatch(
                "input_observation_ids 的 observation 必须互不相同"
                "（同一 observation 不能充当多个 role）"
            )


@dataclass(frozen=True)
class FinancialCalculationResult:
    """一次 create_calculation 的结果摘要（不含任何数值 / 输入细节）。"""

    calculation_id: UUID
    calculation_fingerprint: str
    replayed: bool


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def compute_calculation_fingerprint(
    *,
    calculation_schema_version: int,
    formula_version: int,
    company_id: UUID,
    calculation_code: str,
    inputs: list[tuple[str, UUID, str]],
    result_value: Decimal,
    result_unit: str,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：calculation_schema_version / formula_version / company_id /
    calculation_code / 按 input_role 排序的 (role, observation_id,
    observation fingerprint) / result_value（canonical decimal string）/
    result_unit。

    **不得包含** calculation_id / created_at。同一完全相同输入 → 同一指纹 →
    replay 同一行；输入任一变化 → 新指纹 → 新行，旧行保留（无 update API）。
    Decimal 用 str() 序列化（规范形式，无 float 精度歧义）。
    """
    # 按 input_role 排序保证 canonical 顺序（与 inputs 传入顺序无关）。
    sorted_inputs = [
        {"input_role": role, "metric_observation_id": str(obs_id), "fingerprint": fp}
        for role, obs_id, fp in sorted(inputs, key=lambda item: item[0])
    ]
    payload = {
        "calculation_schema_version": calculation_schema_version,
        "formula_version": formula_version,
        "company_id": str(company_id),
        "calculation_code": calculation_code,
        "inputs": sorted_inputs,
        "result_value": str(result_value),
        "result_unit": result_unit,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
