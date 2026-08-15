"""Calculation/Evidence Pack builder + C/E ref resolution (stage 4B.2C.2).

- **Calculation Pack**：从真实 FinancialCalculationModel + inputs 构造最小投影
  （C1..Cn 按 str(calculation_id) 升序），**不发送** calculation UUID /
  observation UUID / fingerprint / Evidence UUID / DB IDs / RawArtifact /
  locator / Chroma；
- **Evidence Pack**：复用 4B.1 的 EvidencePack（E1..En 按 str(evidence_card_id)
  升序），允许空包（additional evidence 0..20 条）；
- **numeric-literal guard**：statement 禁止 ASCII/full-width digits / % / 中文数字
  字符（零〇二两三四五六七八九十百千万亿兆）/ 定量短语（百分之 / 倍 / 翻倍 / 翻番 /
  过半 / 半数 / 一成 / 一半 / 一点）/ numeric-context 表达（第 X 季度 / 第 X 月 /
  第 X 年 / 第 X 期 / 第 X 日 / 第 X 号，如"一季度 / 第一年度 / 一月 / 一号"）
  （不自动删数字、不改写、不让第二个 LLM 修正）；
  "一/点"本身允许（"一定/进一步/观点"等常用非数量词），但真正的量与数字仍由字符 /
  短语 / numeric-context 规则零暴露；
- **C/E ref resolution**：C<number> → calculation_id、E<number> →
  evidence_card_id；未知引用 / 跨 relation 冲突 → 整次失败（0 写）；不做 fuzzy
  resolve、不自动猜 UUID。

所有 alias 全确定性：同 Calculation/Evidence 集合 → 相同 C1..Cn / E1..En 映射，
ref resolution 可复现。
"""

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID

from app.analysis.claims.contracts import EvidencePack
from app.analysis.claims.evidence_pack import EvidencePackSource, build_evidence_pack
from app.analysis.financial.contracts import (
    CalculationPack,
    CalculationPackItem,
    FinancialAnalysisDecision,
    InputSummaryItem,
)
from app.analysis.financial.errors import (
    FinancialAnalysisInputError,
    FinancialAnalysisNumericLiteralForbidden,
    FinancialAnalysisRelationConflict,
    FinancialAnalysisUnknownRef,
)
from app.claims.contracts import ClaimKind
from app.claims.financial_contracts import (
    FinancialClaimConfidence,
    FinancialClaimImportance,
)

_ASCII_DIGITS = frozenset("0123456789")
_FULLWIDTH_DIGITS = frozenset("０１２３４５６７８９")
# 中文数字字符：不含"一"与"点"。原因：spec D 明确允许"该指标反映公司存在一定盈利
# 空间"——"一定/进一步/观点"等常用非数量词必须可用；真正的量（两成 / 二〇二五 /
# 百亿 / 一成 / 一半 / 一点）仍由本字符集（零〇二两三四五六七八九十百千万亿兆）或
# 下方定量短语捕获（required reject 用例全部仍命中）。
_CHINESE_NUMERIC_CHARS = frozenset("零〇二两兩三四五六七八九十百千万亿兆")
# statement 中禁止出现的字符：ASCII digits + full-width digits + %（含全角）+
# 中文数字字符。
_FORBIDDEN_CHARS = frozenset("%％") | _ASCII_DIGITS | _FULLWIDTH_DIGITS | _CHINESE_NUMERIC_CHARS
# 定量短语（不含数字也表达量）：百分之 / 倍 / 翻倍 / 翻番 / 过半 / 半数，以及依赖
# "一"的量词表达：一成（10%）/ 一半 / 一点（"一点"为模糊量，一并拒绝）。
_FORBIDDEN_PHRASES = ("百分之", "翻倍", "翻番", "过半", "半数", "倍", "一成", "一半", "一点")
# numeric-context：单独放开"一"是为了保留"一定/进一步/观点"等非数量词，但"一"在
# 期间/序数表达中仍是量词，必须拒绝：第一季度收入改善 / 一季度收入改善 / 一月份需求
# 增加 / 第一期项目完成 / 第一年度经营改善 / 一日发生变化 / 一号项目。单个语义明确
# 的 pattern（第? + 一 + 季/月/年/期/日/号）覆盖全部 required 用例，不用大量硬编码
# if；"一"后接非上述量词语素（定/步/个/致等）不命中，继续允许。
# V1.1 closure：排除「上一季度/上一年度/下一季度/下一年度」等期间引用复合词
# （"上/下一年度"= 上一年度/下一年度，非数量表达；生产实测模型以「上一年度」
# 引用报告期，误命中导致整次分析失败）。
_NUMERIC_CONTEXT_RE = re.compile(r"(?<!上)(?<!下)(?:第)?一(?:季|月|年|期|日|号)")
# V1.1 closure：4 位年份（19xx/20xx）允许（期间引用，非定量事实）。
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def assert_statement_has_no_numeric_literals(statement: str) -> None:
    """numeric-literal boundary：statement 禁止任何数字形式、定量短语与 numeric-context 表达。

    - 字符：ASCII digits（0-9）/ full-width digits（０-９）/ %（%％）/ 中文数字
      （零〇二两三四五六七八九十百千万亿兆）；
    - 短语：百分之 / 倍 / 翻倍 / 翻番 / 过半 / 半数 / 一成 / 一半 / 一点；
    - numeric-context：第? + 一 + 季/月/年/期/日/号（含"季度/一季度/一月/一月份/
      一年/第一年度/一期/一日/一号"），此时"一"是量词而非非数量词语素。
    - **V1.1 closure：4 位年份（19xx/20xx）允许**——年份是期间引用（metadata），
      不是定量事实；生产实测模型系统性以年份引用报告期，硬性拒绝导致整次
      财务分析反复失败。百分比 / 金额 / 其他数字 / 中文数字仍全部禁止。
    "收入同比增长20%" / "营业收入增长两成" / "盈利能力提升一倍" / "利润实现翻倍" /
    "二〇二五年收入改善" / "利润增长一成" / "第一季度收入改善" / "一季度收入改善" /
    "一月份需求增加" / "第一期项目完成" / "第一年度经营改善" / "一日发生变化" /
    "一号项目" → 拒绝；"营业收入保持增长态势。" / "公司经营保持一定增长" /
    "管理层观点"（"一/点"在非数量词中）允许。**不自动删数字 / 不改写 / 不让第二个
    LLM 修正**——违反即整次分析失败（0 写）。
    """
    scrubbed = _YEAR_RE.sub("", statement)
    if any(ch in _FORBIDDEN_CHARS for ch in scrubbed):
        raise FinancialAnalysisNumericLiteralForbidden(
            "financial claim statement must not contain numeric literals"
        )
    if any(phrase in statement for phrase in _FORBIDDEN_PHRASES):
        raise FinancialAnalysisNumericLiteralForbidden(
            "financial claim statement must not contain quantitative expressions"
        )
    if _NUMERIC_CONTEXT_RE.search(statement):
        raise FinancialAnalysisNumericLiteralForbidden(
            "financial claim statement must not contain numeric-context period expressions"
        )


def _display_value(result_value: Decimal, result_unit: str) -> str:
    """deterministic display：ratio → "20.00%"，cny → "<canonical> CNY"。

    程序生成（ROUND_HALF_EVEN 与公式舍入一致）；模型不得改写。
    """
    if result_unit == "ratio":
        percent = (result_value * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
        return f"{percent}%"
    return f"{result_value} CNY"


def _period_summary(inputs: tuple["InputSummarySource", ...]) -> str:
    """确定性 period summary：所有输入同一期间 → 单期间；否则按 role 顺序拼接。"""
    periods: list[str] = []
    for source in inputs:
        start = source.period_start.isoformat() if source.period_start is not None else "None"
        periods.append(f"{source.period_kind} {start}~{source.period_end.isoformat()}")
    distinct = list(dict.fromkeys(periods))  # 保持顺序去重
    if len(distinct) == 1:
        return distinct[0]
    return " vs ".join(distinct)


@dataclass(frozen=True)
class InputSummarySource:
    """单条输入 Observation 的最小来源投影（由 Service 从真实 PG 行映射）。

    构造纯 Python 对象即可（无需 SQLAlchemy session / DB），单元测试直接构造；
    `from_model` 从 FinancialMetricObservationModel 行映射所需字段。
    """

    role: str  # input_role.value
    metric_code: str
    statement_scope: str
    period_start: date | None
    period_end: date
    period_kind: str
    normalized_value_cny: Decimal

    @classmethod
    def from_model(cls, role: str, obs) -> "InputSummarySource":
        """从真实 FinancialMetricObservationModel 行映射为最小投影。"""
        return cls(
            role=role,
            metric_code=obs.metric_code,
            statement_scope=obs.statement_scope,
            period_start=obs.period_start,
            period_end=obs.period_end,
            period_kind=obs.period_kind,
            normalized_value_cny=obs.normalized_value_cny,
        )


@dataclass(frozen=True)
class CalculationPackSource:
    """FinancialCalculation 在包中的最小来源投影（由 Service 从真实 PG 行映射）。

    `statement_scope` 从输入 Observation 派生（计算校验保证全部输入同 scope）。
    """

    calculation_id: UUID
    calculation_code: str
    result_value: Decimal
    result_unit: str
    formula_version: int
    inputs: tuple[InputSummarySource, ...]

    @property
    def statement_scope(self) -> str:
        return self.inputs[0].statement_scope

    @classmethod
    def from_model(cls, calc, inputs: list[InputSummarySource]) -> "CalculationPackSource":
        """从真实 FinancialCalculationModel 行 + 已校验 input 摘要映射为最小投影。"""
        return cls(
            calculation_id=calc.calculation_id,
            calculation_code=calc.calculation_code,
            result_value=calc.result_value,
            result_unit=calc.result_unit,
            formula_version=calc.formula_version,
            inputs=tuple(sorted(inputs, key=lambda item: item.role)),
        )


def build_calculation_pack(sources: list[CalculationPackSource]) -> CalculationPack:
    """构造确定性 Calculation Pack（C1..Cn 按 str(calculation_id) 升序）。

    - 空包 → FinancialAnalysisInputError（分析必须有 Calculation）；
    - alias 编号稳定：同 Calculation 集合 → 相同 C1..Cn 映射，ref resolution
      可复现。
    """
    if not sources:
        raise FinancialAnalysisInputError("calculation pack 不能为空")
    ordered = sorted(sources, key=lambda source: str(source.calculation_id))
    items: list[CalculationPackItem] = []
    ref_to_calc_id: dict[str, UUID] = {}
    calc_id_to_ref: dict[UUID, str] = {}
    for index, source in enumerate(ordered, start=1):
        ref = f"C{index}"
        items.append(
            CalculationPackItem(
                calculation_ref=ref,
                calculation_code=source.calculation_code,
                result_value=str(source.result_value),
                result_unit=source.result_unit,
                formula_version=source.formula_version,
                period_summary=_period_summary(source.inputs),
                statement_scope=source.statement_scope,
                deterministic_display_value=_display_value(source.result_value, source.result_unit),
                inputs=tuple(
                    InputSummaryItem(
                        role=item.role,
                        metric_code=item.metric_code,
                        period_start=(
                            item.period_start.isoformat() if item.period_start is not None else None
                        ),
                        period_end=item.period_end.isoformat(),
                        normalized_value_cny=str(item.normalized_value_cny),
                        unit="CNY",
                    )
                    for item in source.inputs
                ),
            )
        )
        ref_to_calc_id[ref] = source.calculation_id
        calc_id_to_ref[source.calculation_id] = ref
    return CalculationPack(
        items=tuple(items),
        ref_to_calc_id=ref_to_calc_id,
        calc_id_to_ref=calc_id_to_ref,
    )


def build_evidence_pack_allowing_empty(
    sources: list[EvidencePackSource],
) -> EvidencePack:
    """构造 Evidence Pack（E1..En 按 str(evidence_card_id) 升序）；允许空包。

    additional evidence 0..20 条；空包 → 空 EvidencePack（Analysis 仍可只基于
    Calculation 判断）。
    """
    if not sources:
        return EvidencePack(items=(), ref_to_card_id={}, card_id_to_ref={})
    return build_evidence_pack(sources)


@dataclass(frozen=True)
class ResolvedFinancialClaim:
    """解析完成、可直接构造 FinancialClaimDraft 的 Claim 候选（relation → UUID 已 resolve）。"""

    statement: str
    claim_kind: ClaimKind
    confidence: FinancialClaimConfidence
    importance: FinancialClaimImportance
    supports_calculations: tuple[UUID, ...]
    contradicts_calculations: tuple[UUID, ...]
    context_calculations: tuple[UUID, ...]
    additional_supports: tuple[UUID, ...]
    additional_contradicts: tuple[UUID, ...]
    additional_context: tuple[UUID, ...]


def resolve_decision_refs(
    decision: FinancialAnalysisDecision,
    calculation_pack: CalculationPack,
    evidence_pack: EvidencePack,
) -> list[ResolvedFinancialClaim]:
    """把 decision 中全部 C/E ref 解析为 UUID；任一无效 → 抛错（0 写）。

    - C ref 必须存在 calculation_pack（格式已在 schema 校验）→ 否则
      FinancialAnalysisUnknownRef；
    - E ref 必须存在 evidence_pack → 否则 UnknownRef（C 编号混进 Evidence list
      因 E 格式校验失败被 schema 拒绝；E 编号混进 Calculation list 同理）；
    - 同一 C ref 跨 support/contradict/context、同一 E ref 跨 additional
      relation → FinancialAnalysisRelationConflict；
    - 组内去重 + canonical 排序（与 FinancialClaimDraft normalization 一致）。
    """
    if not decision.claims:
        return []
    resolved: list[ResolvedFinancialClaim] = []
    for candidate in decision.claims:
        calc_groups = {
            "supports": candidate.support_calculation_refs,
            "contradicts": candidate.contradict_calculation_refs,
            "context": candidate.context_calculation_refs,
        }
        ev_groups = {
            "supports": candidate.additional_support_evidence_refs,
            "contradicts": candidate.additional_contradict_evidence_refs,
            "context": candidate.additional_context_evidence_refs,
        }
        # 未知引用检查（ref 格式已在 FinancialClaimCandidate schema 校验）。
        for ref in (ref for group in calc_groups.values() for ref in group):
            if ref not in calculation_pack.ref_to_calc_id:
                raise FinancialAnalysisUnknownRef(f"unknown calculation ref: {ref}")
        for ref in (ref for group in ev_groups.values() for ref in group):
            if ref not in evidence_pack.ref_to_card_id:
                raise FinancialAnalysisUnknownRef(f"unknown evidence ref: {ref}")
        # 跨 relation 重复检查（同一 ref 出现在 ≥2 个 relation 组）。
        relation_by_ref: dict[str, str] = {}
        for relation, refs in calc_groups.items():
            for ref in refs:
                if ref in relation_by_ref:
                    raise FinancialAnalysisRelationConflict(
                        f"calculation ref in multiple relations: {ref}"
                    )
                relation_by_ref[ref] = relation
        for relation, refs in ev_groups.items():
            for ref in refs:
                if ref in relation_by_ref:
                    raise FinancialAnalysisRelationConflict(
                        f"evidence ref in multiple relations: {ref}"
                    )
                relation_by_ref[ref] = relation
        # 组内去重 + canonical 排序（与 FinancialClaimDraft normalization 一致）。
        supports_calculations = sorted(
            {calculation_pack.ref_to_calc_id[ref] for ref in calc_groups["supports"]}, key=str
        )
        contradicts_calculations = sorted(
            {calculation_pack.ref_to_calc_id[ref] for ref in calc_groups["contradicts"]}, key=str
        )
        context_calculations = sorted(
            {calculation_pack.ref_to_calc_id[ref] for ref in calc_groups["context"]}, key=str
        )
        additional_supports = sorted(
            {evidence_pack.ref_to_card_id[ref] for ref in ev_groups["supports"]}, key=str
        )
        additional_contradicts = sorted(
            {evidence_pack.ref_to_card_id[ref] for ref in ev_groups["contradicts"]}, key=str
        )
        additional_context = sorted(
            {evidence_pack.ref_to_card_id[ref] for ref in ev_groups["context"]}, key=str
        )
        resolved.append(
            ResolvedFinancialClaim(
                statement=candidate.statement,
                claim_kind=candidate.claim_kind,
                confidence=candidate.confidence,
                importance=candidate.importance,
                supports_calculations=tuple(supports_calculations),
                contradicts_calculations=tuple(contradicts_calculations),
                context_calculations=tuple(context_calculations),
                additional_supports=tuple(additional_supports),
                additional_contradicts=tuple(additional_contradicts),
                additional_context=tuple(additional_context),
            )
        )
    return resolved
