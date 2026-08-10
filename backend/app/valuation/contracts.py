"""Valuation data & comparison contracts (stage 4C.2A).

目标：**Source → EvidenceCard(metric) → ValuationMetricObservation →
RelativeValuationComparison →（4C.2B）Relative Valuation Claim** 的相对估值
证据链。**Observation = source-backed raw valuation multiple fact**（登记原始
倍数数值）；**Comparison = program-deterministic derived comparison fact**
（对显式 peer 集合计算 median / min / max / premium-discount，**不是
EvidenceCard**）。透明 / 可解释：source / method / peers / valuation date /
research cutoff / assumptions 全部可见。**不做**交易建议 / 目标价 / 盈利预测 /
绝对公允价值 / LLM / 自动选 peer / 分类。

冻结：
- `VALUATION_OBSERVATION_SCHEMA_VERSION = 1`、
  `RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION = 1`、
  `VALUATION_FORMULA_VERSION = 1`；comparison_method v1 = `peer_median`。
- v1 metric_code 只有 `pe_ttm` / `pb_mrq` / `ps_ttm`（**不做** DCF / PEG /
  EV / EBITDA / FCFF / FCFE / target price / dividend model）。
- `ValuationMetricDraft` 只允许提供语义输入（company_id /
  source_evidence_card_id / metric_code / metric_as_of / source_value_text）；
  **不得**提供 metric_value / fingerprint / authority / provider / source_id /
  comparison result。
- `ComparisonDraft` 只允许提供（target_company_id / target_observation_id /
  peer_observation_ids / analysis_as_of）；**不得**提供 metric / metric_as_of /
  peer company id / median / min / max / premium_discount / 分类 / fingerprint
  ——全部由 service 从真实 Observation 确定性派生。
- observation fingerprint = canonical JSON + SHA-256（不含 id / created_at）；
  comparison fingerprint 同法（不含 comparison_id / created_at）。同一完全相同
  → replay 同一行；任一输入变化 → 新指纹 → 新行，旧行保留（无 update API）。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from app.valuation.errors import ValuationInputError

# 各表 schema 版本与公式版本的当前值（改结构 / 公式语义时递增；历史行原样保留）。
VALUATION_OBSERVATION_SCHEMA_VERSION = 1
RELATIVE_VALUATION_COMPARISON_SCHEMA_VERSION = 1
VALUATION_FORMULA_VERSION = 1

# v1 comparison_method 白名单（只允许 peer_median，确定性公式）。
_VALIDATION_COMPARISON_METHODS = ("peer_median",)

# peer 集合约束（spec O）：min 3 / max 20。
MIN_PEER_COUNT = 3
MAX_PEER_COUNT = 20


class ValuationMetricCode(StrEnum):
    """v1 冻结 metric_code（只做同指标 / 同日期 / 显式 peer 的确定性比较）。"""

    PE_TTM = "pe_ttm"
    PB_MRQ = "pb_mrq"
    PS_TTM = "ps_ttm"


_VALIDATION_METRIC_CODES = frozenset(
    (ValuationMetricCode.PE_TTM, ValuationMetricCode.PB_MRQ, ValuationMetricCode.PS_TTM)
)


class ComparisonMethod(StrEnum):
    """v1 冻结 comparison_method（确定性公式，无 LLM / 无分类）。"""

    PEER_MEDIAN = "peer_median"


_VALIDATION_METHODS = frozenset((ComparisonMethod.PEER_MEDIAN,))


def supported_valuation_metric_codes() -> tuple[ValuationMetricCode, ...]:
    """v1 支持的全部 valuation metric_code（冻结顺序）。"""
    return tuple(_VALIDATION_METRIC_CODES)


@dataclass(frozen=True)
class ValuationMetricDraft:
    """调用方提交的估值倍数语义输入（构造时校验，不可变）。

    只允许提供：company_id / source_evidence_card_id / metric_code /
    metric_as_of / source_value_text。**不得**提供 metric_value / fingerprint /
    authority / provider / source_id / comparison result（由
    ValuationObservationService 从真实 Evidence 确定性派生）。

    - metric_as_of = **市场观测日**（该倍数对应的估值时点），不是来源发布时间；
    - source_value_text：trim 后非空；必须是 EvidenceCard.quote_text 的
      **exact 完整数字 token**（Service 校验，禁止 fuzzy / normalize / LLM 修正）。
    """

    company_id: UUID
    source_evidence_card_id: UUID
    metric_code: ValuationMetricCode
    metric_as_of: date
    source_value_text: str

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise ValuationInputError("company_id 必须是 UUID")
        if isinstance(self.source_evidence_card_id, bool) or not isinstance(
            self.source_evidence_card_id, UUID
        ):
            raise ValuationInputError("source_evidence_card_id 必须是 UUID")
        if not isinstance(self.metric_code, ValuationMetricCode):
            raise ValuationInputError("metric_code 必须是 ValuationMetricCode")
        if self.metric_code not in _VALIDATION_METRIC_CODES:
            raise ValuationInputError(f"不支持 metric_code: {self.metric_code}")
        if isinstance(self.metric_as_of, bool) or not isinstance(self.metric_as_of, date):
            raise ValuationInputError("metric_as_of 必须是 date")
        value_text = self.source_value_text.strip()
        if not value_text:
            raise ValuationInputError("source_value_text 不能为空（trim 后）")
        object.__setattr__(self, "source_value_text", value_text)


@dataclass(frozen=True)
class ValuationObservationResult:
    """一次 create_observation 的结果摘要（不含任何 evidence 正文 / 数值细节）。"""

    valuation_observation_id: UUID
    valuation_observation_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class ComparisonDraft:
    """调用方提交的相对估值比较语义输入（构造时校验，不可变）。

    只允许提供：target_company_id / target_observation_id /
    peer_observation_ids / analysis_as_of。**不得**提供 metric / metric_as_of /
    peer company id / median / min / max / premium_discount / 分类 / fingerprint
    ——全部由 RelativeValuationComparisonService 从真实 Observation 派生。

    - peer_observation_ids：显式 peer 集合（3..20，observation id 去重，
      target observation 不能在 peer 集合内）；peer 公司必须互不相同且不含
      target 公司（Service 校验真实 Observation 后确定）。
    - analysis_as_of：研究 cutoff（>= metric_as_of 的 no-lookahead 由 Service 校验）。
    """

    target_company_id: UUID
    target_observation_id: UUID
    peer_observation_ids: tuple[UUID, ...]
    analysis_as_of: date

    def __post_init__(self) -> None:
        if isinstance(self.target_company_id, bool) or not isinstance(self.target_company_id, UUID):
            raise ValuationInputError("target_company_id 必须是 UUID")
        if isinstance(self.target_observation_id, bool) or not isinstance(
            self.target_observation_id, UUID
        ):
            raise ValuationInputError("target_observation_id 必须是 UUID")
        if isinstance(self.peer_observation_ids, (list, tuple)):
            object.__setattr__(self, "peer_observation_ids", tuple(self.peer_observation_ids))
        if not isinstance(self.peer_observation_ids, tuple):
            raise ValuationInputError("peer_observation_ids 必须是序列")
        if not (MIN_PEER_COUNT <= len(self.peer_observation_ids) <= MAX_PEER_COUNT):
            raise ValuationInputError(
                f"peer_observation_ids 必须在 {MIN_PEER_COUNT}..{MAX_PEER_COUNT} 条"
            )
        if any(
            isinstance(pid, bool) or not isinstance(pid, UUID) for pid in self.peer_observation_ids
        ):
            raise ValuationInputError("peer_observation_ids 元素必须是 UUID")
        if len(set(self.peer_observation_ids)) != len(self.peer_observation_ids):
            raise ValuationInputError("peer_observation_ids 不能重复")
        if self.target_observation_id in self.peer_observation_ids:
            raise ValuationInputError("target observation 不能出现在 peer 集合内")
        if isinstance(self.analysis_as_of, bool) or not isinstance(self.analysis_as_of, date):
            raise ValuationInputError("analysis_as_of 必须是 date")


@dataclass(frozen=True)
class ComparisonResult:
    """一次 create_comparison 的结果摘要（不含任何 evidence 正文 / 数值细节）。"""

    comparison_id: UUID
    comparison_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class DerivedComparisonStats:
    """纯函数阶段派生的确定性比较统计（comparison_method=peer_median）。"""

    comparison_method: str
    peer_count: int
    peer_median: Decimal
    peer_min: Decimal
    peer_max: Decimal
    premium_discount_to_median: Decimal


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def compute_valuation_observation_fingerprint(
    *,
    valuation_observation_schema_version: int,
    company_id: UUID,
    source_evidence_card_id: UUID,
    metric_code: str,
    metric_as_of: date,
    source_value_text: str,
    metric_value: Decimal,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：schema version / company_id / source_evidence_card_id /
    metric_code / metric_as_of / source_value_text / metric_value（canonical
    decimal string）。**不得包含** valuation_observation_id / created_at。
    Decimal 用 str() 序列化（规范形式，无 float 精度歧义）。
    """
    payload = {
        "valuation_observation_schema_version": valuation_observation_schema_version,
        "company_id": str(company_id),
        "source_evidence_card_id": str(source_evidence_card_id),
        "metric_code": metric_code,
        "metric_as_of": metric_as_of.isoformat(),
        "source_value_text": source_value_text,
        "metric_value": str(metric_value),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_comparison_fingerprint(
    *,
    comparison_schema_version: int,
    formula_version: int,
    comparison_method: str,
    target_company_id: UUID,
    target_observation_id: UUID,
    target_observation_fingerprint: str,
    metric_code: str,
    metric_as_of: date,
    analysis_as_of: date,
    peers: list[dict],
    peer_median: Decimal,
    peer_min: Decimal,
    peer_max: Decimal,
    premium_discount_to_median: Decimal,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：comparison schema version / formula version / comparison_method /
    target_company_id / target_observation_id / **target observation fingerprint** /
    metric_code / metric_as_of / analysis_as_of / peer list（**按 peer_company_id
    排序**，每条含 peer_company_id / peer_observation_id / observation
    fingerprint）/ peer_median / peer_min / peer_max / premium_discount_to_median
    （canonical decimal string）。

    **不得包含** comparison_id / created_at。同一完全相同 comparison → 同一
    指纹 → replay 同一行；任一输入变化 → 新指纹 → 新 comparison，旧行保留
    （无 update API）。
    """
    payload = {
        "comparison_schema_version": comparison_schema_version,
        "formula_version": formula_version,
        "comparison_method": comparison_method,
        "target_company_id": str(target_company_id),
        "target_observation_id": str(target_observation_id),
        "target_observation_fingerprint": target_observation_fingerprint,
        "metric_code": metric_code,
        "metric_as_of": metric_as_of.isoformat(),
        "analysis_as_of": analysis_as_of.isoformat(),
        "peers": peers,
        "peer_median": str(peer_median),
        "peer_min": str(peer_min),
        "peer_max": str(peer_max),
        "premium_discount_to_median": str(premium_discount_to_median),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
