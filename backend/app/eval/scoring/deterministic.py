"""Deterministic cross-variant metrics (stage 7B.1.2A).

只实现「真正公平」的确定性指标：`citation_validity` 与 `citation_coverage`。
二者只依赖 variant 产出的 normalized structure（claims / citations）+ frozen
source snapshot，不需要 human label / judge / DB / LLM / network，因此可在三路
variant 上公平比较。

缺陷二分（7B.1.2A Gate 冻结）：
- **hard structural**（identity 歧义，denominator 不可靠）：duplicate claim_id /
  duplicate citation_id → `verify_variant_output_identity` 抛
  `EvalOutputStructureError`（runner 的 fail-fast 前置检查）。
- **scorable**（可量化质量下降，不 hard fail）：citation.source_fingerprint 未命中
  snapshot / citation.claim_ids 含 unknown claim / claim.citation_ids 含 unknown
  citation / citation↔claim 反向不闭合 → 进入 `citation_validity` /
  `citation_coverage` 的 ratio。

定义（v1）：
- `citation_validity`：分母 = 全部 citation；一条 citation「valid」当且仅当
  (1) `source_fingerprint` 命中 frozen snapshot 且 (2) `claim_ids` 非空 且
  (3) 所有 `claim_ids` 指向真实 claim 且 (4) 每个被引用 claim 反向包含该
  citation_id（citation↔claim 闭合）。0 citation → `not_applicable`。
- `citation_coverage`：分母 = 全部 claim；一条 claim「covered」当且仅当其拥有
  ≥1 条 citation_id，且该 citation 真实存在、符合 citation_validity 单条 valid
  规则、且其 `claim_ids` 包含该 claim_id。0 claim → `not_applicable`。

两个 metric 复用同一 validity 定义（`analyze_citations`），不复制规则。
未实现的 deterministic-kind 指标（claim_support_rate / unsupported_claim_ratio /
conflict_preservation 等）由 registry 暴露为 unavailable。
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.eval.contracts import FrozenSourceSnapshot
from app.eval.errors import EvalOutputStructureError
from app.eval.metrics import MetricName, MetricStatus, MetricValue
from app.eval.scoring.context import EvalScoringContext

_METRIC_VERSION = 1


def valid_source_fingerprints(snapshot: FrozenSourceSnapshot) -> frozenset[str]:
    """frozen snapshot 中可作为 citation `source_fingerprint` 的语义身份集合。

    document 用 `content_sha256`、macro 用 `snapshot_fingerprint`、structured 用
    `artifact_fingerprint`（与 snapshot 的 duplicate identity 口径一致）。
    """
    return frozenset(
        [ref.content_sha256 for ref in snapshot.document_sources]
        + [ref.snapshot_fingerprint for ref in snapshot.macro_snapshots]
        + [ref.artifact_fingerprint for ref in snapshot.structured_artifacts]
    )


@dataclass(frozen=True)
class CitationAnalysis:
    """一次 variant output 的 citation 分析结果（只读）。

    `claim_citation_ids` / `citation_claim_ids` 是 claim↔citation 双向索引
    （结构索引，不是 validity 规则）。
    """

    claim_ids: frozenset[str]
    citation_ids: frozenset[str]
    valid_citation_ids: frozenset[str]
    claim_citation_ids: dict[str, frozenset[str]]
    citation_claim_ids: dict[str, frozenset[str]]


def analyze_citations(context: EvalScoringContext) -> CitationAnalysis:
    """对 variant output 做 citation validity 分析（validity / coverage 共享）。

    单条 citation「valid」规则（v1）：
    1. `source_fingerprint` 命中 frozen snapshot；
    2. `claim_ids` 非空；
    3. 所有 `claim_ids` 都指向真实 claim；
    4. 每个被引用 claim 反向包含该 citation_id（citation↔claim 闭合）。
    """
    output = context.variant_output
    sources = valid_source_fingerprints(context.source_snapshot)
    claims = output.claims
    claim_ids = {claim.claim_id for claim in claims}
    claim_citation_ids = {claim.claim_id: frozenset(claim.citation_ids) for claim in claims}
    citation_claim_ids = {
        citation.citation_id: frozenset(citation.claim_ids) for citation in output.citations
    }
    valid: set[str] = set()
    for citation in output.citations:
        if citation.source_fingerprint not in sources:
            continue
        if not citation.claim_ids:
            continue
        if any(cid not in claim_ids for cid in citation.claim_ids):
            continue
        if any(citation.citation_id not in claim_citation_ids[cid] for cid in citation.claim_ids):
            continue
        valid.add(citation.citation_id)
    return CitationAnalysis(
        claim_ids=frozenset(claim_ids),
        citation_ids=frozenset(citation_claim_ids),
        valid_citation_ids=frozenset(valid),
        claim_citation_ids=claim_citation_ids,
        citation_claim_ids=citation_claim_ids,
    )


def verify_variant_output_identity(context: EvalScoringContext) -> None:
    """Hard structural 校验：identity 歧义（duplicate id）→ 抛 `EvalOutputStructureError`。

    只检查 duplicate claim_id / citation_id（identity 歧义导致 denominator 不可靠）。
    scorable defects（unknown source / dangling ref / 非闭合）**不**在此 raise，
    进入 citation_validity / citation_coverage 的 ratio。
    """
    output = context.variant_output
    if len({claim.claim_id for claim in output.claims}) != len(output.claims):
        raise EvalOutputStructureError("duplicate claim_id")
    if len({citation.citation_id for citation in output.citations}) != len(output.citations):
        raise EvalOutputStructureError("duplicate citation_id")


class DeterministicMetricCalculator(Protocol):
    """一个 deterministic 指标的计算器（无 DB / LLM / network）。"""

    name: MetricName

    def calculate(self, context: EvalScoringContext) -> MetricValue: ...


class CitationValidityCalculator:
    """`citation_validity` v1：valid citation 占比（higher_is_better）。"""

    name = MetricName.CITATION_VALIDITY

    def calculate(self, context: EvalScoringContext) -> MetricValue:
        citations = context.variant_output.citations
        if not citations:
            return MetricValue(
                metric_name=self.name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.NOT_APPLICABLE,
                reason_code="no_citations",
            )
        analysis = analyze_citations(context)
        valid = len(analysis.valid_citation_ids)
        total = len(citations)
        return MetricValue(
            metric_name=self.name,
            metric_version=_METRIC_VERSION,
            status=MetricStatus.COMPUTED,
            value=Decimal(valid) / Decimal(total),
            numerator=Decimal(valid),
            denominator=Decimal(total),
            sample_count=total,
        )


class CitationCoverageCalculator:
    """`citation_coverage` v1：被 valid real citation 覆盖的 claim 占比。"""

    name = MetricName.CITATION_COVERAGE

    def calculate(self, context: EvalScoringContext) -> MetricValue:
        claims = context.variant_output.claims
        if not claims:
            return MetricValue(
                metric_name=self.name,
                metric_version=_METRIC_VERSION,
                status=MetricStatus.NOT_APPLICABLE,
                reason_code="no_claims",
            )
        analysis = analyze_citations(context)
        valid = analysis.valid_citation_ids
        citation_claim_ids = analysis.citation_claim_ids
        covered = sum(
            1
            for claim in claims
            if any(
                cid in valid and claim.claim_id in citation_claim_ids.get(cid, ())
                for cid in claim.citation_ids
            )
        )
        total = len(claims)
        return MetricValue(
            metric_name=self.name,
            metric_version=_METRIC_VERSION,
            status=MetricStatus.COMPUTED,
            value=Decimal(covered) / Decimal(total),
            numerator=Decimal(covered),
            denominator=Decimal(total),
            sample_count=total,
        )
