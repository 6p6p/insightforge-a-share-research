"""Quantitative token grounding guard (stage 5B, spec L).

确定性代码从 paragraph.text 提取 quantitative tokens（ASCII / full-width digits、
percentage、decimal、Chinese numeric expression、明显 ratio / 倍数表达），每个
token 必须**逐字**出现在该段落引用的 Claim statement 或 Evidence statement /
quote 至少一处；否则 `DraftSectionNumericGroundingError`（**不自动改写 / 不二次
LLM**）。

v1 保守策略：宁可拒绝，不猜测——凡是段落里出现的数字，都必须能在所引用的
Claim / Evidence 原文中逐字找到。提取器的 token 顺序固定（先百分数 / 小数格式，
再裸数字），保证 "15%" 同时产出 "15%" 与 "15" 两个 token（"15" 是 "15%" 的
子串，corpus 校验时天然通过）。
"""

import re

from app.draft_section.errors import DraftSectionNumericGroundingError

# 中文数字字符集（含「两」「〇」）。
_CN = "零〇一二三四五六七八九十百千万亿两"

# 提取顺序固定：先百分比 / 小数复合格式，再裸数字。`[%％]` 同时覆盖半/全角百分号。
_ARABIC_PERCENT = re.compile(r"\d+(?:[.,]\d+)?[%％]")
_FULLWIDTH_PERCENT = re.compile(r"[０-９]+(?:[．][０-９]+)?[％%]")
_CN_PERCENT = re.compile(rf"[{_CN}]+[％%]")
_ARABIC_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_FULLWIDTH_NUMBER = re.compile(r"[０-９]+(?:[．][０-９]+)?")
_CN_NUMBER = re.compile(rf"[{_CN}]{{2,}}")

# 明显 ratio / 倍数表达（无数字的量词）。
_MULTIPLE_EXPRESSIONS = ("翻倍", "翻一番", "翻两番", "减半")

_PATTERNS = (
    _FULLWIDTH_PERCENT,
    _CN_PERCENT,
    _ARABIC_PERCENT,
    _ARABIC_NUMBER,
    _FULLWIDTH_NUMBER,
    _CN_NUMBER,
)

# 内联别名引用（C3 / E1 / X1 / G1）里的编号是**标签**而非 quantitative token：
# 段落正文常以「（C3）」「详见G1」等形式内联引用 C/E/X/G 编号，其数字必须剥离，
# 否则会被误判为 quantitative token（如「（C3）」→ token '3'）并错误拒绝
# grounded 稿件。
# 不能用 `\b`：Python re 的 `\w` 包含中文字符，中文与拉丁字母之间没有词边界
# （「详见G1」中「见」与「G」都是 `\w`）。改为只要求左右不是 ASCII 字母/数字/
# 下划线——中文（如「见/（」）作为前缀不阻断匹配。
_ALIAS_REF = re.compile(r"(?<![A-Za-z0-9_])[CEXG][1-9]\d*(?![A-Za-z0-9_])", re.IGNORECASE)


def _strip_alias_refs(text: str) -> str:
    """把内联别名引用替换为空白（不产生 token，也不改变相邻词的粘连）。"""
    return _ALIAS_REF.sub(" ", text)


def extract_quantitative_tokens(text: str) -> list[str]:
    """提取 text 的全部 quantitative tokens（去重、保序）。

    先剥离内联 C/E/X/G 别名引用（标签编号不是数字），再按固定顺序提取复合
    百分比 / 小数 / 裸数字 / 中文数字 / 倍数表达。
    """
    text = _strip_alias_refs(text)
    tokens: list[str] = []
    for pattern in _PATTERNS:
        tokens.extend(pattern.findall(text))
    for expression in _MULTIPLE_EXPRESSIONS:
        if expression in text:
            tokens.append(expression)
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered


def assert_numeric_grounding(*, paragraph_text: str, grounding_texts: list[str]) -> None:
    """paragraph_text 的每个 quantitative token 必须逐字出现在 grounding_texts。

    grounding_texts = 该段落引用的全部 Claim statements + Evidence statements +
    quotes。任一 token 缺失 → DraftSectionNumericGroundingError（0 写）。
    """
    corpus = "\n".join(grounding_texts)
    for token in extract_quantitative_tokens(paragraph_text):
        if token not in corpus:
            raise DraftSectionNumericGroundingError(
                f"paragraph introduces ungrounded quantitative token: {token!r}"
            )
