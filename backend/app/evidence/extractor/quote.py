"""Exact quote resolver（stage 3C.2）。

resolve_exact_quote(chunk_text, quote_text) 把 LLM 返回的 quote_text 解析成
chunk.text 上的 Python 字符区间 [start, end)：
- quote_text.strip() 非空；
- 必须是 chunk.text 的**精确子串**：禁止 fuzzy match、禁止 normalize 后匹配、
  禁止自动修正标点/空白；
- 唯一出现一次 → (start, end)；0 次 → EvidenceExtractionQuoteNotFound；
  >1 次（含重叠）→ EvidenceExtractionQuoteAmbiguous。

**LLM 不返回 char offsets**：offsets 由本函数确定性推导。
"""

from app.evidence.extractor.errors import (
    EvidenceExtractionQuoteAmbiguous,
    EvidenceExtractionQuoteNotFound,
)


def resolve_exact_quote(chunk_text: str, quote_text: str) -> tuple[int, int]:
    """把 quote_text 解析为 chunk.text 的唯一精确子串区间 [start, end)。"""
    if not isinstance(chunk_text, str) or not isinstance(quote_text, str):
        raise EvidenceExtractionQuoteNotFound("chunk_text / quote_text 必须是 str")
    if not quote_text.strip():
        raise EvidenceExtractionQuoteNotFound("quote_text trim 后不能为空")

    first = chunk_text.find(quote_text)
    if first == -1:
        raise EvidenceExtractionQuoteNotFound("quote_text 不是 chunk.text 的精确子串")

    # 统计全部出现次数（含重叠：步长 +1，而非 +len(quote)）。
    count = 1
    idx = chunk_text.find(quote_text, first + 1)
    while idx != -1:
        count += 1
        idx = chunk_text.find(quote_text, idx + 1)
    if count > 1:
        raise EvidenceExtractionQuoteAmbiguous("quote_text 在 chunk.text 中出现多次")

    return first, first + len(quote_text)
