"""Auditor model protocol (stage 5D, spec P).

`AuditModel` 是 auditor 模型的可替换边界：`DeepSeekAuditModel`（production，
`deepseek:deepseek-v4-flash`）与 Fake 模型（自动化测试，0 real LLM）实现同一
协议。模型层只做结构化输出解析；**不做**校验 / 持久化 / 指纹（都在 service）。

协议约束（spec P）：thinking disabled、temperature=0、structured output、
无 tools / web / reasoning_content persistence。模型只输出
`AuditDecision`（reviewed_paragraph_refs + 0..50 issues），**不输出** overall
status / recommended route（程序确定性派生，spec O）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.audit.contracts import AuditDecision
from app.audit.packs import AuditPack


@runtime_checkable
class AuditModel(Protocol):
    """一次 `audit` 调用 = 一次模型审核（输入 pack → 结构化 AuditDecision）。"""

    model_id: str

    async def audit(self, pack: AuditPack) -> AuditDecision:
        """基于已验证 audit pack 生成结构化审核决策。

        - 返回必须可通过 pydantic 解析为 `AuditDecision`；解析失败 / 模型异常 →
          上层（adapters / service）捕获并归一化为 `ReportAuditModelUnavailable`
          或 `ReportAuditMalformedOutput`；
        - **不持久化**；persisted auditor_model_id 由 service 读取 `self.model_id`。
        """
        raise NotImplementedError
