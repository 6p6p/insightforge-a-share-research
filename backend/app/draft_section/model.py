"""Writer model protocol (stage 5B, spec E/J).

`DraftSectionModel` 是 writer 模型的可替换边界：`DeepSeekDraftSectionModel`
（production，`deepseek:deepseek-v4-flash`）与 Fake 模型（自动化测试，0 real LLM）
实现同一协议。模型层只做结构化输出解析；**不做**校验 / 持久化 / 指纹（都在
service）。

协议约束（spec E）：thinking disabled、temperature=0、structured output、
无 tools / web / reasoning_content persistence。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.draft_section.contracts import WriterDecision
from app.draft_section.packs import SectionInputPack


@runtime_checkable
class DraftSectionModel(Protocol):
    """一次 `write` 调用 = 一次模型起草（输入 pack → 结构化 WriterDecision）。"""

    model_id: str

    async def write(
        self, pack: SectionInputPack, correction_hint: str | None = None
    ) -> WriterDecision:
        """基于已验证 section 输入包生成结构化草稿。

        - `correction_hint`（V1.1 closure，writer v4）：service 对首稿做 hard
          provenance validation，违规时带违规摘要重试一次（有界；仍违规才拒绝）；
        - 返回必须可通过 pydantic 解析为 `WriterDecision`；解析失败 / 模型异常 →
          上层（adapters / service）捕获并归一化为 `DraftSectionModelUnavailable` 或
          `DraftSectionMalformedOutput`；
        - **不持久化**；persisted writer_model_id 由 service 读取 `self.model_id`。
        """
        raise NotImplementedError
