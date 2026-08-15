"""FakeDraftSectionModel：自动化测试用的确定性 section writer（stage 5B）。

- 可配置固定输出（WriterDecision 或 dict；dict 用于模拟 malformed output）；
- 可配置抛错（模拟 provider 不可用 / 网络异常）；
- `model_id` 稳定可断言（写入 draft_sections.writer_model_id）；
- 记录每次调用的 SectionInputPack（断言注入边界 / C/E/X/G alias 稳定性 /
  LLM 永不看 UUID / fingerprint）。
- `valid_decision_for(pack)`：从真实 pack 派生合法决策（引用 pack 中真实绑定
  的 C/E，text 逐字引用 claim/evidence 陈述，保证 numeric grounding 通过）——
  E2E 用，让 fake 输出始终对当前输入合法。

自动测试一律使用本 fake，不访问任何真实 LLM / 网络 / provider。
"""

from app.draft_section.contracts import ParagraphCandidate, WriterDecision
from app.draft_section.packs import SectionInputPack


def valid_decision_for(pack: SectionInputPack) -> WriterDecision:
    """构造一个对该 pack 一定合法的 WriterDecision。

    - 取第一个 Claim 与其真实绑定的第一个 Evidence（claim_aliases 非空）；
    - text = claim.statement + evidence.evidence_statement（逐字引用，保证
      numeric grounding 通过；引用 X/G 若有）；
    - 额外段落：引用 X（冲突）与 G（缺口）对应 claim 的正文（仅当存在）。
    """
    claim = pack.claims[0]
    evidence = next(item for item in pack.evidence if claim.alias in item.claim_aliases)
    paragraphs = [
        ParagraphCandidate(
            text=f"{claim.statement} {evidence.evidence_statement}",
            claim_refs=[claim.alias],
            evidence_refs=[evidence.alias],
        )
    ]
    for conflict in pack.conflicts:
        if conflict.claim_aliases:
            c_alias = conflict.claim_aliases[0]
            c = next(item for item in pack.claims if item.alias == c_alias)
            ev = next(item for item in pack.evidence if c_alias in item.claim_aliases)
            paragraphs.append(
                ParagraphCandidate(
                    text=f"{conflict.description} {c.statement} {ev.evidence_statement}",
                    claim_refs=[c_alias],
                    evidence_refs=[ev.alias],
                    conflict_refs=[conflict.alias],
                )
            )
    for gap in pack.gaps:
        if gap.claim_aliases:
            c_alias = gap.claim_aliases[0]
            c = next(item for item in pack.claims if item.alias == c_alias)
            ev = next(item for item in pack.evidence if c_alias in item.claim_aliases)
            paragraphs.append(
                ParagraphCandidate(
                    text=f"{gap.description} {c.statement} {ev.evidence_statement}",
                    claim_refs=[c_alias],
                    evidence_refs=[ev.alias],
                    gap_refs=[gap.alias],
                )
            )
    return WriterDecision(paragraphs=paragraphs)


class FakeDraftSectionModel:
    """Deterministic fake section writer（结构性满足 DraftSectionModel）。"""

    def __init__(
        self,
        *,
        decision: WriterDecision | dict | None = None,
        model_id: str = "deepseek:deepseek-v4-flash",
        error: type[Exception] | None = None,
        decision_factory=None,
    ) -> None:
        self._decision = decision
        self._model_id = model_id
        self._error = error
        self._decision_factory = decision_factory
        self.calls: list[SectionInputPack] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def write(
        self, pack: SectionInputPack, correction_hint: str | None = None
    ) -> WriterDecision | dict:
        self.calls.append(pack)
        if correction_hint is not None:
            self.correction_hints = getattr(self, "correction_hints", [])
            self.correction_hints.append(correction_hint)
        if self._error is not None:
            raise self._error()
        if self._decision_factory is not None:
            return self._decision_factory(pack)
        return self._decision
