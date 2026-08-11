"""Revision writer model protocol (stage 5E.2A, spec I/J).

`RevisionWriterModel` 是修订 writer 模型的可替换边界：`DeepSeekRevisionWriterModel`
（production，`deepseek:deepseek-v4-flash`）与 Fake 模型（自动化测试，0 real LLM）
实现同一协议。模型层只做结构化输出解析；**不做**校验 / 持久化 / 指纹（都在
service）。

协议约束（spec F）：thinking disabled、temperature=0、structured output、
无 tools / web / reasoning_content persistence。输出复用 5B `WriterDecision`
（与 Evidence-bound Section Writer 同一结构化输出契约，spec J）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.draft_section.contracts import WriterDecision
from app.revision.packs import RevisionInputPack


@runtime_checkable
class RevisionWriterModel(Protocol):
    """一次 `rewrite` 调用 = 一次模型修订（输入 pack → 结构化 WriterDecision）。"""

    model_id: str

    async def rewrite(self, pack: RevisionInputPack) -> WriterDecision:
        """基于修订输入包生成结构化草稿（1..10 段，同一 section scope）。

        - 返回必须可通过 pydantic 解析为 `WriterDecision`；解析失败 / 模型异常 →
          上层（adapters / service）捕获并归一化为 `RevisionWriterModelUnavailable`
          或 `RevisionWriterMalformedOutput`；
        - **不持久化**；persisted writer_model_id 由 service 读取 `self.model_id`。
        """
        raise NotImplementedError
