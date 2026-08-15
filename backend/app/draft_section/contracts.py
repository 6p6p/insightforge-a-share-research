"""Evidence-bound section writer contracts (stage 5B): constants + request + output + fingerprint.

角色边界（Writer 只做**证据约束**的正文起草，判断与综合交给确定性代码验证）：
- LLM 负责：基于**已验证**的 section 输入包（C/E/X/G alias + 最小字段），写一节
  中文正文草稿（1..10 个段落），每段至少引用 1 个 C 与 1 个 E；
- 确定性代码负责：C1..Cn / E1..En / X1..Xn / G1..Gn alias 构造（**LLM 永不看
  UUID / fingerprint / provenance id**）、hard provenance validation（known /
  cross-section / unbound）、numeric grounding guard、forbidden investment
  language、writer_input_fingerprint + replay（同输入 → **0 model calls**）、
  section_fingerprint、create_or_get 原子持久化 / replay 校验；
- LLM **不负责**：Retrieval / Chroma / web search / 计算 / 写数据库。

冻结常量：
- `DRAFT_SECTION_SCHEMA_VERSION = 1`（draft_sections.section_schema_version；
  persisted payload shape 未变化，保持 1）；
- `WRITER_NAME = "evidence_bound_section_writer"`；`WRITER_VERSION = 4`（v4 =
  V1.1 closure：hard provenance validation 违规 → 带违规摘要**有界重试一次**
  （correction_hint）；v3 = 数字逐字核查清单；production writer_model_id =
  `deepseek:deepseek-v4-flash`。

v2 Writer contract（本轮 Gate 0）：
- **inline alias policy**：paragraph.text 禁止出现任何合法 C/E/X/G<number>
  transport alias token（`DraftSectionInlineAliasLeak`）——别名只是模型通信
  标识，不得进入正式报告正文；
- **risks_and_gaps section-aware paragraph contract**：Pydantic 只强制 text
  非空 + 四类 ref 都是 list（允许空）；真正 required policy 由 Section-aware
  validation 决定（theme：每段 >=1 C + >=1 E；risks_and_gaps：每段 >=1 的
  C / X / G 之一，evidence 可空）；
- **per-Claim evidence relation**：同一 Evidence 可绑定多个 Claim 且 relation
  不同（DB UNIQUE=(claim_id, evidence_card_id) 只约束单 claim 内），pack /
  prompt / writer input fingerprint 均按 (claim, relation) 投影，不折叠。

strict validation（服务层 `validate_decision`）：
- ref 格式 C/E/X/G<number>；全部 ref 必须是已知 alias；claim ref 超出 section
  但属合成输入集 → CrossSection；Evidence 必须绑定于段落引用的至少一个 Claim；
- Section-aware paragraph contract（见上）；inline alias leak policy；
- numeric grounding + forbidden language 逐段检查。

指纹：
- `compute_writer_input_fingerprint` = canonical JSON + SHA-256：含
  section_schema_version / outline_fingerprint / section 身份 / allowed
  Claim/Evidence fingerprints / **Evidence–Claim relation mapping** /
  conflict-gap 数据 / writer 身份。**不含** draft_section_id / created_at /
  payload。同输入 → 同指纹 → replay 同一行（relation mapping 变化 → 新指纹）。
- `compute_section_fingerprint` = writer_input_fingerprint + normalized resolved
  payload 的 SHA-256。
"""

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.draft_section.errors import DraftSectionInputError

# draft_sections.section_schema_version 的当前值（改名或换结构时递增；已有草稿
# 原样保留，新语义 → 新 fingerprint → 新行）。
DRAFT_SECTION_SCHEMA_VERSION = 1

# evidence-bound section writer 的身份常量（persisted writer_name / version）。
WRITER_NAME = "evidence_bound_section_writer"
# v2：inline alias policy + risks_and_gaps output semantics + per-Claim evidence
# relation 改变 Writer contract。v1 冻结不再使用；旧 v1 rows 不修改 / 不 backfill
# （同 section 用 v2 → writer_input_fingerprint 不同 → 新 DraftSection）。
WRITER_VERSION_V1 = 1
WRITER_VERSION = 4

# 段落数量边界（spec J：1..10）。
MIN_PARAGRAPHS = 1
MAX_PARAGRAPHS = 10

# 单段 text 上限（字符）。
MAX_PARAGRAPH_TEXT_LENGTH = 2000

# 被禁止的投资语言（spec M：不写买入/卖出建议、目标价、收益承诺）。
FORBIDDEN_INVESTMENT_PHRASES = (
    "建议买入",
    "建议卖出",
    "买入评级",
    "卖出评级",
    "增持评级",
    "减持评级",
    "推荐买入",
    "推荐卖出",
    "强烈推荐",
    "目标价",
    "收益承诺",
    "保证收益",
    "看涨",
    "看跌",
    "抄底",
    "追高",
)


def contains_forbidden_language(text: str) -> str | None:
    """返回 text 中命中的第一个被禁止投资语言片段；未命中返回 None。"""
    for phrase in FORBIDDEN_INVESTMENT_PHRASES:
        if phrase in text:
            return phrase
    return None


@dataclass(frozen=True)
class DraftSectionRequest:
    """调用方提交的起草请求。

    **只提供 outline_id + section_id**：title / claims / evidence / company /
    question / cutoff / writer version 全部从 VerifiedReportOutline +
    VerifiedSynthesisResult 派生，调用方不得提供。
    """

    outline_id: UUID
    section_id: str

    def __post_init__(self) -> None:
        if isinstance(self.outline_id, bool) or not isinstance(self.outline_id, UUID):
            raise DraftSectionInputError("outline_id 必须是 UUID")
        section_id = self.section_id.strip()
        if not section_id:
            raise DraftSectionInputError("section_id 不能为空（trim 后）")
        object.__setattr__(self, "section_id", section_id)


class ParagraphCandidate(BaseModel):
    """一个段落草稿（模型结构化输出，**不渲染进 prompt**）。

    schema 层只强制 text 非空 / 有上限、四类 ref 都是 list（允许空）——
    **真正 required policy 由 Section-aware validation 决定**（spec B）：
    - theme section：claim_refs >= 1 且 evidence_refs >= 1；
    - risks_and_gaps：claim_refs / conflict_refs / gap_refs 至少 1（evidence 可空）。
    ref 格式与作用域仍在服务层校验（known / cross-section / unbound / numeric /
    forbidden / inline alias）。
    """

    model_config = ConfigDict(frozen=True)

    text: str
    claim_refs: list[str]
    evidence_refs: list[str]
    conflict_refs: list[str] = []
    gap_refs: list[str] = []

    @model_validator(mode="after")
    def _validate(self) -> "ParagraphCandidate":
        text = self.text.strip()
        if not text:
            raise ValueError("paragraph.text 不能为空（trim 后）")
        if len(text) > MAX_PARAGRAPH_TEXT_LENGTH:
            raise ValueError(f"paragraph.text 超长（>{MAX_PARAGRAPH_TEXT_LENGTH} 字符）")
        object.__setattr__(self, "text", text)
        for field in ("claim_refs", "evidence_refs", "conflict_refs", "gap_refs"):
            if not isinstance(getattr(self, field), list):
                raise ValueError(f"paragraph {field} 必须是 list")
        return self


class WriterDecision(BaseModel):
    """一次起草的结构化输出（模型生成，1..10 个段落）。"""

    model_config = ConfigDict(frozen=True)

    paragraphs: list[ParagraphCandidate]

    @model_validator(mode="after")
    def _validate(self) -> "WriterDecision":
        if not (MIN_PARAGRAPHS <= len(self.paragraphs) <= MAX_PARAGRAPHS):
            raise ValueError(f"paragraphs 数量必须在 {MIN_PARAGRAPHS}..{MAX_PARAGRAPHS}")
        return self


@dataclass(frozen=True)
class DraftSectionResult:
    """一次起草的结果摘要（不含正文段落 / prompt / raw response）。"""

    draft_section_id: UUID
    outline_id: UUID
    section_id: str
    section_fingerprint: str
    writer_input_fingerprint: str
    replayed: bool
    paragraph_count: int


@dataclass(frozen=True)
class VerifiedDraftSection:
    """`verify_draft_section_integrity` 的 read-side 产物（完整重建验证通过）。

    只含可公开的身份 / 指纹 / 段落计数，不含正文段落 / prompt / raw response。
    """

    draft_section_id: UUID
    outline_id: UUID
    section_id: str
    section_order: int
    section_type: str
    title: str
    section_schema_version: int
    writer_name: str
    writer_version: int
    writer_model_id: str
    writer_input_fingerprint: str
    section_fingerprint: str
    paragraph_count: int


def compute_writer_input_fingerprint(
    *,
    section_schema_version: int,
    outline_fingerprint: str,
    section_id: str,
    section_order: int,
    section_type: str,
    title: str,
    claim_fingerprints: list[str],
    evidence_fingerprints: list[str],
    evidence_claim_relations: list[dict],
    conflicts: list[dict],
    gaps: list[dict],
    writer_name: str,
    writer_version: int,
    writer_model_id: str,
) -> str:
    """LLM 输入边界的确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：section_schema_version、outline_fingerprint、section 身份
    （id/order/type/title）、exact allowed Claim fingerprints（canonical 排序）、
    exact allowed Evidence fingerprints（canonical 排序）、**Evidence–Claim
    relation mapping**（每张 Evidence 绑定哪些 Claim、relation 各是什么——
    同一 Evidence 可对不同 Claim 有不同 relation，prompt 按 per-Claim 投影，
    指纹必须反映该 mapping）、selected conflict/gap 数据、writer 身份。

    **不得包含** draft_section_id / created_at / section_payload。同 outline +
    同 section + 同 Claim/Evidence 集 + 同 relation mapping + 同 conflict/gap
    数据 + 同 writer → 同指纹 → replay 同一行（**0 model calls**）；任一输入变化
    → 新指纹 → 新草稿（旧行保留，无 update API）。

    `evidence_claim_relations`：canonical 排序的
    `{"evidence": <str evidence_card_id>, "claim": <str claim_id>,
      "relation": <str>}` 列表（服务层由 LoadedEvidence.claim_relations 派生）。
    """
    payload = {
        "section_schema_version": section_schema_version,
        "outline_fingerprint": outline_fingerprint,
        "section": {
            "section_id": section_id,
            "section_order": section_order,
            "section_type": section_type,
            "title": title,
        },
        "allowed_claims": sorted(claim_fingerprints),
        "allowed_evidence": sorted(evidence_fingerprints),
        "evidence_claim_relations": evidence_claim_relations,
        "conflicts": conflicts,
        "gaps": gaps,
        "writer": {
            "writer_name": writer_name,
            "writer_version": writer_version,
            "writer_model_id": writer_model_id,
        },
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_section_fingerprint(*, writer_input_fingerprint: str, section_payload: dict) -> str:
    """草稿不可变指纹：writer_input_fingerprint + normalized resolved payload。

    **不得包含** draft_section_id / created_at。同输入 + 同正文 → 同指纹 →
    replay 校验；payload / 输入任一变化 → 新指纹（旧行保留，无 update API）。
    """
    payload = {
        "writer_input_fingerprint": writer_input_fingerprint,
        "section_payload": section_payload,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


_REF_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _ref_pattern(prefix: str) -> re.Pattern:
    pattern = _REF_PATTERN_CACHE.get(prefix)
    if pattern is None:
        # alias 从 1 起（C1..Cn）；0 或前导零不是合法编号。
        pattern = re.compile(rf"^{prefix}[1-9]\d*$")
        _REF_PATTERN_CACHE[prefix] = pattern
    return pattern


def valid_ref_format(ref: str, prefix: str) -> bool:
    """ref 是否匹配 `{prefix}<1..N>`（C/E/X/G），0 / 前导零 / 超格式返回 False。"""
    return isinstance(ref, str) and bool(_ref_pattern(prefix).fullmatch(ref))
