"""Deterministic cross-variant metrics (stage 7B.1.2A).

只实现「真正公平」的确定性指标：`citation_validity` 与 `citation_coverage`。
二者只依赖 variant 产出的 normalized structure（claims / citations）+ frozen
source snapshot，不需要 human label / judge / DB / LLM / network，因此可在三路
variant 上公平比较。

定义（v1）：
- `citation_validity`：分母 = 全部 citation；一条 citation「valid」当且仅当
  `source_fingerprint` 命中 frozen snapshot 且 `claim_ids` 全部指向真实 claim 且
  `citation_id` 唯一（不重复）。0 citation → `not_applicable`。
- `citation_coverage`：分母 = 全部 claim；一条 claim「covered」当且仅当其拥有
  ≥1 条 valid 且 real 的 citation（`citation_id` 命中 `valid` 集合）。0 claim →
  `not_applicable`。

`verify_variant_output_structure` 是**结构性**校验（unique ids / closure /
source membership），与跨 variant 指标分离：前者是 runner 的 fail-fast 前置检查，
后者才进入三路比较。未实现的 deterministic-kind 指标（claim_support_rate /
unsupported_claim_ratio / conflict_preservation 等）由 registry 暴露为
unavailable，本模块不复制其公式。
"""

from collections import Counter
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


def _compute_valid_citation_ids(context: EvalScoringContext) -> frozenset[str]:
    """返回 valid 的 citation_id 集合（citation_validity / coverage 共享口径）。"""
    output = context.variant_output
    sources = valid_source_fingerprints(context.source_snapshot)
    claim_ids = {claim.claim_id for claim in output.claims}
    citation_id_counts = Counter(citation.citation_id for citation in output.citations)
    valid: set[str] = set()
    for citation in output.citations:
        if citation_id_counts[citation.citation_id] != 1:
            continue
        if citation.source_fingerprint not in sources:
            continue
        if any(claim_id not in claim_ids for claim_id in citation.claim_ids):
            continue
        valid.add(citation.citation_id)
    return frozenset(valid)


def verify_variant_output_structure(context: EvalScoringContext) -> None:
    """结构校验：unique ids / closure（双向无悬空引用）/ source membership。

    违反时抛 `EvalOutputStructureError`（不返回 violation 列表）。这是 runner 的
    前置检查，不是跨 variant 指标。
    """
    output = context.variant_output
    sources = valid_source_fingerprints(context.source_snapshot)

    claim_ids = {claim.claim_id for claim in output.claims}
    if len(claim_ids) != len(output.claims):
        raise EvalOutputStructureError("duplicate claim_id")

    citation_ids = {citation.citation_id for citation in output.citations}
    if len(citation_ids) != len(output.citations):
        raise EvalOutputStructureError("duplicate citation_id")

    for citation in output.citations:
        if citation.source_fingerprint not in sources:
            raise EvalOutputStructureError("citation source_fingerprint not in snapshot")
        for claim_id in citation.claim_ids:
            if claim_id not in claim_ids:
                raise EvalOutputStructureError("citation claim_id not in claims")

    for claim in output.claims:
        for citation_id in claim.citation_ids:
            if citation_id not in citation_ids:
                raise EvalOutputStructureError("claim citation_id not in citations")


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
        valid = _compute_valid_citation_ids(context)
        total = len(citations)
        return MetricValue(
            metric_name=self.name,
            metric_version=_METRIC_VERSION,
            status=MetricStatus.COMPUTED,
            value=Decimal(len(valid)) / Decimal(total),
            numerator=Decimal(len(valid)),
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
        valid = _compute_valid_citation_ids(context)
        covered = sum(1 for claim in claims if any(cid in valid for cid in claim.citation_ids))
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
