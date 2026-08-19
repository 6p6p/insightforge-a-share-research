"""FakeAuditModel：自动化测试用的确定性 audit model（stage 5D）。

- 可配置固定输出（AuditDecision 或 dict；dict 模拟 malformed output）；
- 可配置抛错（模拟 provider 不可用）；
- `model_id` 稳定可断言（写入 report_audits.auditor_model_id）；
- 记录每次调用的 AuditPack（断言注入边界 / S/P/C/E/X/G alias 稳定性 /
  LLM 永不看 UUID / fingerprint）。
- `pass_decision(pack)`：reviewed = 全部 P refs + 0 issues（no-cherry-picking
  恰好满足，pass 语义）。

自动测试一律使用本 fake，不访问任何真实 LLM / 网络 / provider。
"""

from app.audit.contracts import AuditDecision
from app.audit.packs import AuditPack


def pass_decision(pack: AuditPack) -> AuditDecision:
    """全部 P refs + 0 issues（语义 case 1：段落与 Claim/Evidence 一致 → pass）。"""
    return AuditDecision(
        reviewed_paragraph_refs=[paragraph.paragraph_ref for paragraph in pack.paragraphs],
        issues=[],
    )


class FakeAuditModel:
    """Deterministic fake audit model（结构性满足 AuditModel protocol）。"""

    def __init__(
        self,
        *,
        decision: AuditDecision | dict | None = None,
        model_id: str = "deepseek:deepseek-v4-flash",
        error: type[Exception] | None = None,
        decision_factory=None,
    ) -> None:
        self._decision = decision
        self._model_id = model_id
        self._error = error
        self._decision_factory = decision_factory
        self.call_hints: list[str | None] = []
        self.calls: list[AuditPack] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def audit(self, pack: AuditPack, hint: str | None = None) -> AuditDecision | dict:
        self.calls.append(pack)
        self.call_hints.append(hint)
        if self._error is not None:
            raise self._error()
        if self._decision_factory is not None:
            return self._decision_factory(pack)
        return self._decision
