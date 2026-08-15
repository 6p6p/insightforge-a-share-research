"""Exact quote resolver（stage 3C.2）。

resolve_exact_quote(chunk_text, quote_text) 把 LLM 返回的 quote_text 解析成
chunk.text 上的 Python 字符区间 [start, end)：
- quote_text.strip() 非空；
- 必须是 chunk.text 的**精确子串**：禁止 fuzzy match、禁止 normalize 后匹配、
  禁止自动修正标点/空白；
- 唯一出现一次 → (start, end)；0 次 → EvidenceExtractionQuoteNotFound；
  >1 次（含重叠）→ EvidenceExtractionQuoteAmbiguous。

**LLM 不返回 char offsets**：offsets 由本函数确定性推导。

`resolve_quote_whitespace_tolerant`（V1.1 closure，extractor v3）：PDF 解析
文本存在布局空白（换行/不规则空格，如「约 90%」vs 模型引用的「约90%」），
严格精确匹配在生产实测中高频失败。容差语义**只允许空白差异**：
- 非空白字符骨架（compact）必须逐字一致，在骨架序列上做唯一子串匹配；
- 命中后映射回**原始 chunk 字符区间**（卡内 quote_text = chunk.text 原文
  切片，逐字保留原始布局空白，不归一化存储）；
- 0 次 → NotFound；>1 次 → Ambiguous（唯一性不变量不变）。
"""

import re

from app.evidence.extractor.errors import (
    EvidenceExtractionQuoteAmbiguous,
    EvidenceExtractionQuoteNotFound,
)

_WS_RE = re.compile(r"[ \t\n\r\f\v]+")


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


def _normalize_whitespace(text: str) -> str:
    """空白折叠为单个空格（非空白字符逐字保留）。"""
    return _WS_RE.sub(" ", text)


def resolve_quote_whitespace_tolerant(chunk_text: str, quote_text: str) -> tuple[int, int]:
    """空白容差的确定性 quote 解析（extractor v3 语义；见模块 docstring）。

    算法（非空白字符骨架匹配）：
    1. 提取 chunk 的非空白字符序列 compact_chunk，并记录每个骨架字符的
       原始下标（origin[i]）；
    2. 提取 quote 的非空白字符序列 compact_quote（首尾空白自然忽略）；
    3. compact_quote 在 compact_chunk 中唯一出现 → 映射回原始区间
       [origin[cs], origin[ce-1] + 1)（区间内原始空白逐字保留）；
    4. 0 次 → NotFound；>1 次 → Ambiguous（唯一性不变量不变）。
    非空白字符必须逐字一致（仍禁止改写/纠错/增减标点）。
    """
    if not isinstance(chunk_text, str) or not isinstance(quote_text, str):
        raise EvidenceExtractionQuoteNotFound("chunk_text / quote_text 必须是 str")
    if not quote_text.strip():
        raise EvidenceExtractionQuoteNotFound("quote_text trim 后不能为空")

    compact_chunk: list[str] = []
    origin: list[int] = []
    for i, char in enumerate(chunk_text):
        if not _WS_RE.match(char):
            compact_chunk.append(char)
            origin.append(i)
    compact_quote = "".join(char for char in quote_text if not _WS_RE.match(char))
    if not compact_quote:
        raise EvidenceExtractionQuoteNotFound("quote_text 无有效内容")

    compact_text = "".join(compact_chunk)
    first = compact_text.find(compact_quote)
    if first == -1:
        raise EvidenceExtractionQuoteNotFound("quote_text 不是 chunk.text 的精确子串（含空白容差）")

    count = 1
    idx = compact_text.find(compact_quote, first + 1)
    while idx != -1:
        count += 1
        idx = compact_text.find(compact_quote, idx + 1)
    if count > 1:
        raise EvidenceExtractionQuoteAmbiguous("quote_text 在 chunk.text 中出现多次（含空白容差）")

    start = origin[first]
    end = origin[first + len(compact_quote) - 1] + 1
    return start, end
