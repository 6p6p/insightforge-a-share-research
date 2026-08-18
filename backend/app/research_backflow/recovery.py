"""P1 evidence recovery: gap classification + existing-source / financial quote recovery.

确定性核心（0 LLM）：`GapClass` 分类、第二遍来源检索与**真实原文引用**的
财务数字恢复。价值链仍为 Source -> Evidence -> Observation/Claim -> Report：

- **分类**：区分缺口性质，决定下一步去哪找（RETRIEVAL_MISS 查索引 / EXTRACTION_MISS
  重提取 / SOURCE_GAP 补来源 / CONFLICT 走冲突仲裁），全部失败才落 TRUE_MISSING；
- **财务恢复**：模型只负责"去哪找"（alias 术语），本模块在**真实来源块**中定位
  含 alias + 数字的原文片段并**确定性解析**，`VALUE` 必须来自真实 quote，
  绝不接受模型输出的数字；
- **标记**：`MODEL_ASSISTED_RECOVERY_MARKER` 仅内部使用，**不能替代 provenance**。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class GapClass(StrEnum):
    """证据缺口分类（P1）。"""

    # 数据根本不存在（三类恢复全部失败后判定）。
    TRUE_MISSING = "true_missing"
    # 来源存在但检索/索引未命中（第一遍检索漏网，不等于数据不存在）。
    RETRIEVAL_MISS = "retrieval_miss"
    # 来源 + 块存在但证据提取未产出（重提取 / 定向 alias 二次抽取）。
    EXTRACTION_MISS = "extraction_miss"
    # 该公司没有任何可用来源记录（需补来源 / 手动上传）。
    SOURCE_GAP = "source_gap"
    # 指标存在冲突（走确定性/LLM 仲裁，不在此分类）。
    CONFLICT = "conflict"


def classify_gap(*, has_source: bool, has_chunk: bool, has_evidence: bool) -> GapClass:
    """按"已有什么"确定性分类（证据链逐级下钻，绝不臆断不存在）。"""
    if has_evidence:
        # 已有证据 → 缺口已解决；调用方不应再分类（防御性）。
        raise ValueError("gap already resolved: evidence exists")
    if not has_source:
        return GapClass.SOURCE_GAP
    if not has_chunk:
        return GapClass.RETRIEVAL_MISS
    return GapClass.EXTRACTION_MISS


def recovery_exhausted(
    attempted: Sequence[tuple[str, bool]],
    *,
    required: Sequence[str] = ("existing_source", "financial", "supplementary"),
) -> bool:
    """三类恢复全部**尝试过**且全部失败 → TRUE_MISSING。

    attempted = (method, succeeded)；任一方法未尝试或成功 → 未穷尽（继续找，
    不臆断数据不存在）。
    """
    attempted_map = dict(attempted)
    if not all(method in attempted_map for method in required):
        return False
    return all(not succeeded for succeeded in attempted_map.values())


# ---------------------------------------------------------------------- financial

MODEL_ASSISTED_RECOVERY_MARKER = "model_assisted_recovery"

_UNIT_TERMS: tuple[tuple[str, str], ...] = (
    ("亿元", "hundred_million_yuan"),
    ("万元", "ten_thousand_yuan"),
    ("千元", "thousand_yuan"),
    ("元", "yuan"),
)

_NUMBER_RE = re.compile(r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?")

_QUOTE_WINDOW = 40  # quote 前后保留字符数（真实原文切片）


@dataclass(frozen=True)
class QuoteValue:
    """确定性解析出的数值（VALUE 只来自真实 quote 原文）。"""

    number: float
    raw_unit: str | None  # None = 无单位（比率/百分比）
    matched_text: str


@dataclass(frozen=True)
class FinancialRecoveryCandidate:
    """一条真实来源块内、含 alias + 可解析数字的候选。"""

    block_index: int
    alias_term: str
    quote: str  # 真实原文切片（含数字窗口）
    value: QuoteValue


def parse_quote_value(quote: str) -> QuoteValue | None:
    """从原文中确定性解析一个数值（+ 可选中文单位）。无数字 → None。"""
    matches = list(_NUMBER_RE.finditer(quote))
    if not matches:
        return None
    chosen = None
    # 单位优先：优先取"数字+紧跟中文单位"的匹配（年份等裸数字不误判为指标值）。
    for match in matches:
        after = quote[match.end() : match.end() + 4]
        for term, raw_unit in _UNIT_TERMS:
            if after.startswith(term):
                chosen = (match, raw_unit)
                break
        if chosen is not None:
            break
    if chosen is None:
        chosen = (matches[0], None)
    match, unit = chosen
    number_text = match.group(0).replace(",", "")
    number = float(number_text)
    return QuoteValue(number=number, raw_unit=unit, matched_text=match.group(0))


def alias_matches(text: str, alias_terms: Sequence[str]) -> bool:
    """alias 术语命中（大小写不敏感，中文无需分词）。"""
    lowered = text.lower()
    return any(term.lower() in lowered for term in alias_terms)


def locate_candidates(
    alias_terms: Sequence[str],
    text_blocks: Sequence[str],
    *,
    max_candidates: int = 5,
) -> list[FinancialRecoveryCandidate]:
    """在真实来源块中定位"alias + 可解析数字"的原文片段（不生成任何数字）。"""
    candidates: list[FinancialRecoveryCandidate] = []
    for block_index, block in enumerate(text_blocks):
        if not block:
            continue
        for term in alias_terms:
            if not term or not alias_matches(block, [term]):
                continue
            for match in _NUMBER_RE.finditer(block):
                start = max(0, match.start() - _QUOTE_WINDOW)
                end = min(len(block), match.end() + _QUOTE_WINDOW)
                quote = block[start:end].strip()
                value = parse_quote_value(quote)
                if value is None:
                    continue
                candidates.append(
                    FinancialRecoveryCandidate(
                        block_index=block_index,
                        alias_term=term,
                        quote=quote,
                        value=value,
                    )
                )
                if len(candidates) >= max_candidates:
                    return candidates
            break  # 每块只取第一个命中 alias 术语，避免重复候选
    return candidates
