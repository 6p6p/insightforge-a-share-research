"""Revision input pack construction (stage 5E.2A, spec I): original text + feedback + data.

`RevisionInputPack` 把修订 writer 的一次输入完整封装：

- `input_pack`：**源 section 的确定性 Section Input Pack**（同一 C/E/X/G alias，
  修订 writer 不换 Claim/Evidence 集、不改变 section scope，spec J）；
- `original_paragraphs`：source draft 的正文段落文本（修订的"原文"，逐段顺序）；
- `revision_feedback`：section-normalized feedback（check codes / audit issues /
  human comment，全部视为 DATA，spec I）。

模型层只消费本 pack（prompt 渲染在 `app/revision/prompt.py`）；alias 解析 /
provenance 校验 / 指纹全部复用 5B 的确定性代码（validate_decision /
resolve_decision / compute_section_fingerprint）。
"""

from dataclasses import dataclass

from app.draft_section.packs import SectionInputPack
from app.revision.contracts import RevisionFeedbackItem


@dataclass(frozen=True)
class RevisionInputPack:
    """传给 Revision Writer 模型的一次性输入。

    不含 UUID / fingerprint / provenance id——`input_pack` 的 alias 投影已过滤，
    feedback 只含 issue_type/severity/paragraph_index/message（不含 issue id）。
    """

    input_pack: SectionInputPack
    original_paragraphs: tuple[str, ...]
    revision_feedback: tuple[RevisionFeedbackItem, ...]


def build_revision_input_pack(
    *,
    input_pack: SectionInputPack,
    original_paragraphs: tuple[str, ...],
    revision_feedback: tuple[RevisionFeedbackItem, ...],
) -> RevisionInputPack:
    """纯函数：把验证过的输入构造成 deterministic Revision Input Pack。

    feedback 顺序 = derive 派生的确定性顺序（check findings / ordinal issues /
    human comment 末尾），不在此重新排序，保证 prompt 与指纹完全一致。
    """
    if not original_paragraphs:
        raise ValueError("original_paragraphs 不能为空")
    return RevisionInputPack(
        input_pack=input_pack,
        original_paragraphs=original_paragraphs,
        revision_feedback=revision_feedback,
    )
