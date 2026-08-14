"""Structured remap contracts (stage 7B.1.4C.3).

`RemappedObservation` / `StructuredRemapResult`：一次 structured remap 的
deterministic 结果摘要（只含 identity / fingerprint，不含正文 / prompt / 原始
response）。`StructuredRemapResult` 可被 attempt runner 计入输出、被测试断言。
"""

from dataclasses import dataclass, field
from uuid import UUID

from app.eval.contracts import StructuredArtifactType


@dataclass(frozen=True)
class RemappedObservation:
    """一条 remap 后的 structured observation 摘要。

    `semantic_key` = (metric_code, as_of, source_value_text) 的确定性身份，
    用于跨 artifact 去重（comparison 的 target/peer 观测可能独立出现在
    structured_artifacts 中）。
    """

    artifact_type: StructuredArtifactType
    semantic_key: str
    observation_id: UUID
    fingerprint: str
    source_evidence_card_id: UUID
    replayed: bool


@dataclass(frozen=True)
class StructuredRemapResult:
    """一次 `remap_case` 的确定性结果（不含正文 / prompt / 原始数据）。"""

    financial_observations: tuple[RemappedObservation, ...] = ()
    valuation_observations: tuple[RemappedObservation, ...] = ()
    comparisons: tuple[tuple[UUID, str], ...] = ()  # (comparison_id, fingerprint)
    created_peer_companies: tuple[UUID, ...] = ()

    @property
    def total_remapped(self) -> int:
        return (
            len(self.financial_observations)
            + len(self.valuation_observations)
            + len(self.comparisons)
        )


@dataclass(frozen=True)
class _RemapAccumulator:
    """remap 过程中的累积状态（服务内部用；确定性排序后投影为结果）。"""

    financial: list[RemappedObservation] = field(default_factory=list)
    valuation: list[RemappedObservation] = field(default_factory=list)
    comparisons: list[tuple[UUID, str]] = field(default_factory=list)
    peer_companies: list[UUID] = field(default_factory=list)

    def result(self) -> StructuredRemapResult:
        return StructuredRemapResult(
            financial_observations=tuple(
                sorted(
                    self.financial,
                    key=lambda item: (item.semantic_key, str(item.observation_id)),
                )
            ),
            valuation_observations=tuple(
                sorted(
                    self.valuation,
                    key=lambda item: (item.semantic_key, str(item.observation_id)),
                )
            ),
            comparisons=tuple(sorted(self.comparisons, key=lambda item: (str(item[0]), item[1]))),
            created_peer_companies=tuple(sorted({cid for cid in self.peer_companies}, key=str)),
        )
