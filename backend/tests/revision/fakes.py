"""FakeRevisionWriterModel：自动化测试用的确定性 revision writer（stage 5E.2A）。

- 可配置固定输出（WriterDecision 或 dict）/ 抛错 / decision_factory；记录每次
  调用的 `RevisionInputPack`（断言输入边界 / 同一 C/E/X/G alias / 原正文 /
  feedback / 0 UUID / 0 fingerprint）；
- `revision_decision_for(pack)`：从真实 RevisionInputPack 派生合法修订决策——
  复用 `valid_decision_for(pack.input_pack)` 的合法段落（引用 pack 中真实绑定的
  C/E/X/G，spec J：不添加新 Claim/Evidence、不改变 section scope），并在每段
  开头加"修订版[…]"标记（取自第一条 feedback 的 trigger_type/code，无数字 /
  UUID / alias 模式）使修订正文与 source 不同（section_fingerprint 变化），同时
  保证 numeric grounding / forbidden language / inline alias leak 全部通过。

自动测试一律使用本 fake，不访问任何真实 LLM / 网络 / provider。
"""

from app.draft_section.contracts import ParagraphCandidate, WriterDecision
from app.revision.packs import RevisionInputPack


def revision_decision_for(pack: RevisionInputPack) -> WriterDecision:
    """构造一个对该 RevisionInputPack 一定合法的修订决策。

    - 复用 `valid_decision_for`（同一 section scope 的合法 C/E/X/G 引用）；
    - 每段正文加"修订版[…]·原文"前缀（标记无数字 / UUID / alias，保证 numeric
      grounding 与 inline alias leak 通过）→ 修订正文与 source 不同。
    """
    from tests.draft_section.fakes import valid_decision_for

    base = valid_decision_for(pack.input_pack)
    marker = "修订版"
    if pack.revision_feedback:
        item = pack.revision_feedback[0]
        marker = f"修订版[{item.trigger_type}:{item.code}]"
    paragraphs = [
        ParagraphCandidate(
            text=f"{marker}。{p.text}",
            claim_refs=list(p.claim_refs),
            evidence_refs=list(p.evidence_refs),
            conflict_refs=list(p.conflict_refs),
            gap_refs=list(p.gap_refs),
        )
        for p in base.paragraphs
    ]
    return WriterDecision(paragraphs=paragraphs)


class FakeRevisionWriterModel:
    """Deterministic fake revision writer（结构性满足 RevisionWriterModel）。"""

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
        self.calls: list[RevisionInputPack] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def rewrite(self, pack: RevisionInputPack) -> WriterDecision | dict:
        self.calls.append(pack)
        if self._error is not None:
            raise self._error()
        if self._decision_factory is not None:
            return self._decision_factory(pack)
        return self._decision
