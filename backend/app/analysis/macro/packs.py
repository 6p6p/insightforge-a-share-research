"""MacroDriver/CompanyEvidence Pack builder + M/E ref resolution (stage 4C.1B).

- **MacroDriver Pack**：从真实 EvidenceCardModel + provenance（MacroObservation /
  MacroDatasetSnapshot / MacroSeries / SourceRecord）构造最小投影（M1..Mn 按
  str(evidence_card_id) 升序），**不发送** evidence UUID / snapshot UUID /
  observation UUID / series UUID / fingerprint / source UUID / DB IDs /
  RawArtifact / locator / Chroma / raw JSON；
- **CompanyEvidence Pack**：从真实 EvidenceCardModel + SourceRecord 构造最小投影
  （E1..En 按 str(evidence_card_id) 升序），两池 namespace **严格分离**，同样
  **不发送** UUID / fingerprint / locator / raw / Chroma；
- **macro numeric-literal guard v1**：statement 禁止 ASCII/full-width digits /
  % / 中文数字字符（零〇二两三四五六七八九十百千万亿兆）/ 定量短语（百分之 /
  千分之 / 万分之 / 倍 / 翻倍 / 翻番 / 过半 / 半数 / 一成 / 一半 / 一点 /
  基点 / 百分点）/ numeric-context 表达（第 X 季度 / 第 X 月 / 第 X 年 /
  第 X 期 / 第 X 日 / 第 X 号）。与 Financial guard 语义独立，**不 import
  Financial 模块**，各自维护；（不自动删数字、不改写、不让第二个 LLM 修正）；
  "一/点"本身允许（"一定/进一步/观点"等常用非数量词），但真正的量与数字仍由
  字符 / 短语 / numeric-context 规则零暴露；
- **M/E ref resolution**：M<number> → evidence_card_id、E<number> →
  evidence_card_id；未知引用 / 跨 relation 冲突 → 整次失败（0 写）；不做 fuzzy
  resolve、不自动猜 UUID。

所有 alias 全确定性：同 Evidence 集合 → 相同 M1..Mn / E1..En 映射，ref
resolution 可复现。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.analysis.macro.contracts import MacroAnalysisDecision
from app.analysis.macro.errors import (
    MacroAnalysisInputError,
    MacroAnalysisNumericLiteralForbidden,
    MacroAnalysisRelationConflict,
    MacroAnalysisUnknownRef,
)
from app.claims.contracts import ClaimKind
from app.claims.macro_contracts import (
    MacroChannelType,
    MacroClaimConfidence,
    MacroClaimImportance,
    MacroEffectDirection,
    MacroImpactStatus,
    MacroTimeAlignment,
)

_ASCII_DIGITS = frozenset("0123456789")
_FULLWIDTH_DIGITS = frozenset("０１２３４５６７８９")
# 中文数字字符：不含"一"与"点"。原因：与 Financial guard 相同——"一定/进一步/观点"
# 等常用非数量词必须可用；真正的量（两成 / 二〇二六 / 二十五基点 / 百亿 / 一成 /
# 一半 / 一点）仍由本字符集（零〇二两三四五六七八九十百千万亿兆）或下方定量短语捕获
# （required reject 用例全部仍命中）。
_CHINESE_NUMERIC_CHARS = frozenset("零〇二两兩三四五六七八九十百千万亿兆")
# statement 中禁止出现的字符：ASCII digits + full-width digits + %（含全角）+
# 中文数字字符。
_FORBIDDEN_CHARS = frozenset("%％") | _ASCII_DIGITS | _FULLWIDTH_DIGITS | _CHINESE_NUMERIC_CHARS
# 定量短语（不含数字也表达量）：百分之 / 千分之 / 万分之 / 倍 / 翻倍 / 翻番 /
# 过半 / 半数 / 一成 / 一半 / 一点（模糊量），以及利率语境下的单位：基点 / 百分点
# （"加息若干基点"中"基点"恒为定量单位）。
_FORBIDDEN_PHRASES = (
    "百分之",
    "千分之",
    "万分之",
    "翻倍",
    "翻番",
    "过半",
    "半数",
    "倍",
    "一成",
    "一半",
    "一点",
    "基点",
    "百分点",
)
# numeric-context：单独放开"一"是为了保留"一定/进一步/观点"等非数量词，但"一"在
# 期间/序数表达中仍是量词，必须拒绝（一季度 / 第一年度 / 一月份 / 一期 / 一日 / 一号）。
_NUMERIC_CONTEXT_RE = re.compile(r"(?:第)?一(?:季|月|年|期|日|号)")


def assert_macro_statement_has_no_numeric_literals(statement: str) -> None:
    """macro numeric-literal boundary v1：statement 禁止任何数字形式、定量短语与
    numeric-context 表达。

    - 字符：ASCII digits（0-9）/ full-width digits（０-９）/ %（%％）/ 中文数字
      （零〇二两三四五六七八九十百千万亿兆）；
    - 短语：百分之 / 千分之 / 万分之 / 倍 / 翻倍 / 翻番 / 过半 / 半数 / 一成 /
      一半 / 一点 / 基点 / 百分点；
    - numeric-context：第? + 一 + 季/月/年/期/日/号。
    "利率上调五十个基点" / "加息百分之十" / "二〇二六年需求改善" / "政策利率下降
    两档"（"两"）→ 拒绝；"利率上行可能推高公司融资成本" / "汇率波动影响海外收入" /
    "公司管理层观点"（"一/点"在非数量词中）允许。**不自动删数字 / 不改写 / 不让
    第二个 LLM 修正**——违反即整次分析失败（0 写）。
    """
    if any(ch in _FORBIDDEN_CHARS for ch in statement):
        raise MacroAnalysisNumericLiteralForbidden(
            "macro claim statement must not contain numeric literals"
        )
    if any(phrase in statement for phrase in _FORBIDDEN_PHRASES):
        raise MacroAnalysisNumericLiteralForbidden(
            "macro claim statement must not contain quantitative expressions"
        )
    if _NUMERIC_CONTEXT_RE.search(statement):
        raise MacroAnalysisNumericLiteralForbidden(
            "macro claim statement must not contain numeric-context period expressions"
        )


@dataclass(frozen=True)
class MacroDriverPackItem:
    """单条 Macro Evidence 在 MacroDriver Pack 中的最小投影（模型输入）。

    **只含必要字段**：macro_ref（M<number>）/ origin_type / evidence_statement /
    evidence_type / provider_key / authority_tier / availability_date /
    effective_period_summary（确定性）。macro_observation 卡再带：
    indicator_name / series_identity / observation_period / value_summary /
    indicator_unit；document_chunk 卡再带：quote_text / document_type /
    published_at / reporting_period_end（若存在）。
    **不发送**：UUID / fingerprint / locator_refs / raw / Chroma / DB IDs。
    """

    macro_ref: str
    origin_type: str
    evidence_statement: str
    evidence_type: str
    provider_key: str
    authority_tier: int
    availability_date: date
    effective_period_summary: str
    # macro_observation 详情。
    indicator_name: str | None = None
    series_identity: str | None = None
    observation_period: str | None = None
    value_summary: str | None = None
    indicator_unit: str | None = None
    # document_chunk 详情。
    quote_text: str | None = None
    document_type: str | None = None
    published_at: datetime | None = None
    reporting_period_end: date | None = None


@dataclass(frozen=True)
class MacroDriverPack:
    """本次分析的确定性 MacroDriver Pack（M1..Mn 局部 alias → evidence_card_id 双向映射）。

    - items：按 str(evidence_card_id) 升序编号 M1..Mn（确定性，与调用方提交顺序无关）；
    - ref_to_card_id：M ref → evidence_card_id（ref resolution 用）；
    - card_id_to_ref：evidence_card_id → M ref（调试 / 日志用）。
    """

    items: tuple[MacroDriverPackItem, ...]
    ref_to_card_id: dict[str, UUID]
    card_id_to_ref: dict[UUID, str]


@dataclass(frozen=True)
class CompanyEvidencePackItem:
    """单条 Company Evidence 在 CompanyEvidence Pack 中的最小投影（模型输入）。

    **只含必要字段**：evidence_ref（E<number>）/ evidence_statement /
    evidence_type / provider_key / authority_tier / availability_date /
    quote_text / published_at / reporting_period_end（若存在）。
    **不发送**：UUID / fingerprint / locator / raw / Chroma / DB IDs。
    """

    evidence_ref: str
    evidence_statement: str
    evidence_type: str
    provider_key: str
    authority_tier: int
    availability_date: date
    quote_text: str | None = None
    published_at: datetime | None = None
    reporting_period_end: date | None = None


@dataclass(frozen=True)
class CompanyEvidencePack:
    """本次分析的确定性 Company Evidence Pack（E1..En 局部 alias → evidence_card_id）。

    - items：按 str(evidence_card_id) 升序编号 E1..En（确定性，与调用方提交顺序无关）；
    - ref_to_card_id：E ref → evidence_card_id（ref resolution 用）；
    - card_id_to_ref：evidence_card_id → E ref（调试 / 日志用）。
    """

    items: tuple[CompanyEvidencePackItem, ...]
    ref_to_card_id: dict[str, UUID]
    card_id_to_ref: dict[UUID, str]


@dataclass(frozen=True)
class MacroDriverPackSource:
    """Macro Evidence 的最小来源投影（由 Service 从真实 PG 行映射）。

    纯 Python 对象即可（无需 SQLAlchemy session / DB），单元测试直接构造。
    availability 由 Service 用 `resolve_availability` 从真实 provenance 解析。
    """

    evidence_card_id: UUID
    origin_type: str
    evidence_statement: str
    evidence_type: str
    provider_key: str
    authority_tier_snapshot: int
    availability: datetime
    effective_period_summary: str
    indicator_name: str | None = None
    series_identity: str | None = None
    observation_period: str | None = None
    value_summary: str | None = None
    indicator_unit: str | None = None
    quote_text: str | None = None
    document_type: str | None = None
    published_at: datetime | None = None
    reporting_period_end: date | None = None


@dataclass(frozen=True)
class CompanyEvidencePackSource:
    """Company Evidence 的最小来源投影（由 Service 从真实 PG 行映射）。"""

    evidence_card_id: UUID
    evidence_statement: str
    evidence_type: str
    provider_key: str
    authority_tier_snapshot: int
    availability: datetime
    quote_text: str | None = None
    published_at: datetime | None = None
    reporting_period_end: date | None = None


def build_macro_driver_pack(sources: list[MacroDriverPackSource]) -> MacroDriverPack:
    """构造确定性 MacroDriver Pack（M1..Mn 按 str(evidence_card_id) 升序）。

    - 空包 → MacroAnalysisInputError（分析必须有 Macro Evidence）；
    - alias 编号稳定：同 Evidence 集合 → 相同 M1..Mn 映射，ref resolution 可复现。
    """
    if not sources:
        raise MacroAnalysisInputError("macro driver pack 不能为空")
    ordered = sorted(sources, key=lambda source: str(source.evidence_card_id))
    items: list[MacroDriverPackItem] = []
    ref_to_card_id: dict[str, UUID] = {}
    card_id_to_ref: dict[UUID, str] = {}
    for index, source in enumerate(ordered, start=1):
        ref = f"M{index}"
        items.append(
            MacroDriverPackItem(
                macro_ref=ref,
                origin_type=source.origin_type,
                evidence_statement=source.evidence_statement,
                evidence_type=source.evidence_type,
                provider_key=source.provider_key,
                authority_tier=int(source.authority_tier_snapshot),
                availability_date=source.availability.date(),
                effective_period_summary=source.effective_period_summary,
                indicator_name=source.indicator_name,
                series_identity=source.series_identity,
                observation_period=source.observation_period,
                value_summary=source.value_summary,
                indicator_unit=source.indicator_unit,
                quote_text=source.quote_text,
                document_type=source.document_type,
                published_at=source.published_at,
                reporting_period_end=source.reporting_period_end,
            )
        )
        ref_to_card_id[ref] = source.evidence_card_id
        card_id_to_ref[source.evidence_card_id] = ref
    return MacroDriverPack(
        items=tuple(items),
        ref_to_card_id=ref_to_card_id,
        card_id_to_ref=card_id_to_ref,
    )


def build_company_evidence_pack(
    sources: list[CompanyEvidencePackSource],
) -> CompanyEvidencePack:
    """构造确定性 Company Evidence Pack（E1..En 按 str(evidence_card_id) 升序）。

    - 空包 → MacroAnalysisInputError（分析必须有 Company Exposure Evidence）；
    - alias 编号稳定：同 Evidence 集合 → 相同 E1..En 映射，ref resolution 可复现。
    """
    if not sources:
        raise MacroAnalysisInputError("company evidence pack 不能为空")
    ordered = sorted(sources, key=lambda source: str(source.evidence_card_id))
    items: list[CompanyEvidencePackItem] = []
    ref_to_card_id: dict[str, UUID] = {}
    card_id_to_ref: dict[UUID, str] = {}
    for index, source in enumerate(ordered, start=1):
        ref = f"E{index}"
        items.append(
            CompanyEvidencePackItem(
                evidence_ref=ref,
                evidence_statement=source.evidence_statement,
                evidence_type=source.evidence_type,
                provider_key=source.provider_key,
                authority_tier=int(source.authority_tier_snapshot),
                availability_date=source.availability.date(),
                quote_text=source.quote_text,
                published_at=source.published_at,
                reporting_period_end=source.reporting_period_end,
            )
        )
        ref_to_card_id[ref] = source.evidence_card_id
        card_id_to_ref[source.evidence_card_id] = ref
    return CompanyEvidencePack(
        items=tuple(items),
        ref_to_card_id=ref_to_card_id,
        card_id_to_ref=card_id_to_ref,
    )


@dataclass(frozen=True)
class ResolvedMacroClaim:
    """解析完成、可直接构造 MacroClaimDraft 的 Claim 候选（M/E ref → UUID 已 resolve）。"""

    statement: str
    claim_kind: ClaimKind
    confidence: MacroClaimConfidence
    importance: MacroClaimImportance
    channel_type: MacroChannelType
    effect_direction: MacroEffectDirection
    impact_status: MacroImpactStatus
    time_alignment: MacroTimeAlignment
    macro_driver_ids: tuple[UUID, ...]
    company_exposure_ids: tuple[UUID, ...]
    observed_effect_ids: tuple[UUID, ...]
    additional_supports: tuple[UUID, ...]
    additional_contradicts: tuple[UUID, ...]
    additional_context: tuple[UUID, ...]


def resolve_decision_refs(
    decision: MacroAnalysisDecision,
    driver_pack: MacroDriverPack,
    company_pack: CompanyEvidencePack,
) -> list[ResolvedMacroClaim]:
    """把 decision 中全部 M/E ref 解析为 UUID；任一无效 → 抛错（0 写）。

    - M ref 必须存在 driver_pack → 否则 MacroAnalysisUnknownRef；
    - E ref 必须存在 company_pack → 否则 UnknownRef（M 编号混进 Evidence list
      因 E 格式校验失败被 schema 拒绝；E 编号混进 MacroDriver list 同理）；
    - 同一 M ref 跨 macro_driver、同一 E ref 跨 company_exposure / observed_effect
      / additional 各组 → MacroAnalysisRelationConflict（与 MacroClaimDraft 的
      跨 relation 不变量一致）；
    - 组内去重 + canonical 排序（与 MacroClaimDraft normalization 一致）。
    """
    if not decision.claims:
        return []
    resolved: list[ResolvedMacroClaim] = []
    for candidate in decision.claims:
        m_groups = {"macro_driver": candidate.macro_driver_refs}
        e_groups = {
            "company_exposure": candidate.company_exposure_refs,
            "observed_effect": candidate.observed_effect_refs,
            "supports": candidate.additional_support_evidence_refs,
            "contradicts": candidate.additional_contradict_evidence_refs,
            "context": candidate.additional_context_evidence_refs,
        }
        # 未知引用检查（ref 格式已在 MacroClaimCandidate schema 校验）。
        for ref in (ref for group in m_groups.values() for ref in group):
            if ref not in driver_pack.ref_to_card_id:
                raise MacroAnalysisUnknownRef(f"unknown macro driver ref: {ref}")
        for ref in (ref for group in e_groups.values() for ref in group):
            if ref not in company_pack.ref_to_card_id:
                raise MacroAnalysisUnknownRef(f"unknown evidence ref: {ref}")
        # 跨 relation 重复检查（同一 ref 出现在 ≥2 个 relation 组）。
        relation_by_ref: dict[str, str] = {}
        for relation, refs in m_groups.items():
            for ref in refs:
                if ref in relation_by_ref:
                    raise MacroAnalysisRelationConflict(
                        f"macro driver ref in multiple relations: {ref}"
                    )
                relation_by_ref[ref] = relation
        for relation, refs in e_groups.items():
            for ref in refs:
                if ref in relation_by_ref:
                    raise MacroAnalysisRelationConflict(
                        f"evidence ref in multiple relations: {ref}"
                    )
                relation_by_ref[ref] = relation
        # 组内去重 + canonical 排序（与 MacroClaimDraft normalization 一致）。
        macro_driver_ids = sorted(
            {driver_pack.ref_to_card_id[ref] for ref in m_groups["macro_driver"]}, key=str
        )
        company_exposure_ids = sorted(
            {company_pack.ref_to_card_id[ref] for ref in e_groups["company_exposure"]}, key=str
        )
        observed_effect_ids = sorted(
            {company_pack.ref_to_card_id[ref] for ref in e_groups["observed_effect"]}, key=str
        )
        additional_supports = sorted(
            {company_pack.ref_to_card_id[ref] for ref in e_groups["supports"]}, key=str
        )
        additional_contradicts = sorted(
            {company_pack.ref_to_card_id[ref] for ref in e_groups["contradicts"]}, key=str
        )
        additional_context = sorted(
            {company_pack.ref_to_card_id[ref] for ref in e_groups["context"]}, key=str
        )
        resolved.append(
            ResolvedMacroClaim(
                statement=candidate.statement,
                claim_kind=candidate.claim_kind,
                confidence=candidate.confidence,
                importance=candidate.importance,
                channel_type=candidate.channel_type,
                effect_direction=candidate.effect_direction,
                impact_status=candidate.impact_status,
                time_alignment=candidate.time_alignment,
                macro_driver_ids=tuple(macro_driver_ids),
                company_exposure_ids=tuple(company_exposure_ids),
                observed_effect_ids=tuple(observed_effect_ids),
                additional_supports=tuple(additional_supports),
                additional_contradicts=tuple(additional_contradicts),
                additional_context=tuple(additional_context),
            )
        )
    return resolved
