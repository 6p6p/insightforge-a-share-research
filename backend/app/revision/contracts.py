"""Evidence-bound section revision contracts (stage 5E.2A): constants + trigger + fingerprint.

角色边界（Revision Writer 是**证据约束**的正文修订器，spec G/I/J）：
- 代码确定性负责：verify source DraftSection（递归，v2 原始或 v1 修订输出）→
  verify trigger artifact（check / action / decision 三选一，spec H）→ 派生
  revision feedback（spec I，全部视为 DATA）→ 构造 Revision Input Pack（复用
  源 section 的 C/E/X/G alias + 原正文段落 + feedback）→ revision input
  fingerprint（spec K）→ replay（同输入 → **0 model calls**）→ 复用 Writer v2
  hard validation（Claim scope / Evidence binding / numeric / forbidden / inline
  alias，spec J）→ 同短事务原子持久化 draft_sections + draft_section_revisions
  （spec L）→ `verify_revision_integrity`（spec M，**不自动 repair**）；
- Revision Writer LLM 负责：根据反馈修订一**节**正文草稿（1..10 段），只输出
  `WriterDecision`（与 5B Writer 同一结构化输出契约）——**不添加新 Claim /
  Evidence、不改变 section scope、不 retrieval / 不联网 / 不写数据库**。

冻结常量：
- `DRAFT_SECTION_REVISION_SCHEMA_VERSION = 1`（draft_section_revisions.
  revision_schema_version）；
- `REVISION_WRITER_NAME = "evidence_bound_section_rewriter"`、
  `REVISION_WRITER_VERSION = 1`（persisted 修订 writer 身份；修订正文进入
  draft_sections，writer_name/version 用它区别于原始 5B writer v2）；
- production `REVISION_WRITER_MODEL_ID = "deepseek:deepseek-v4-flash"`；
  thinking disabled / temperature=0 / structured output / 0 tools / 0 web。

指纹（spec K）：
- `compute_revision_input_fingerprint` = canonical JSON + SHA-256：含
  revision_schema_version / source draft section id + section fingerprint /
  outline_fingerprint / section 身份 / exact allowed Claim/Evidence
  fingerprints / Evidence–Claim relation mapping / conflict-gap 数据 /
  trigger_type + trigger artifact id/fingerprint / 该 section normalized
  feedback / writer 身份。**不含** revision_id / created_at / 正文 payload。
  同输入 → 同指纹 → replay 同一行；修订正文本身（payload）由 revised
  DraftSection 的 `section_fingerprint` 绑定（`compute_section_fingerprint`
  复用 5B 契约）。

revision_fingerprint（draft_section_revisions 列）== revision input
fingerprint（迁移 0037 语义：派生输入 SHA-256），并在
`verify_revision_integrity` 中与 revised draft 的 section_fingerprint 一起
重算比对——二者共同把 source + trigger + feedback + 修订正文全部绑定。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.draft_section.packs import (
    LoadedClaim,
    LoadedEvidence,
    ResolvedConflict,
    ResolvedGap,
    SectionInputPack,
)
from app.revision.errors import RevisionInputError

# draft_section_revisions.revision_schema_version 的当前值（改名或换结构时递增）。
DRAFT_SECTION_REVISION_SCHEMA_VERSION = 1

# evidence-bound section rewriter 的身份常量（persisted writer_name / version）。
REVISION_WRITER_NAME = "evidence_bound_section_rewriter"
REVISION_WRITER_VERSION = 1

# production revision writer_model_id（与 Writer / Auditor 约定一致）。
REVISION_WRITER_MODEL_ID = "deepseek:deepseek-v4-flash"

# trigger_type 枚举（draft_section_revisions 表的 CHECK 约束同步维护）。
TRIGGER_TYPE_DETERMINISTIC_CHECK = "deterministic_check"
TRIGGER_TYPE_AUDIT_REWRITE = "audit_rewrite"
TRIGGER_TYPE_HUMAN_REWRITE = "human_rewrite"
TRIGGER_TYPES = (
    TRIGGER_TYPE_DETERMINISTIC_CHECK,
    TRIGGER_TYPE_AUDIT_REWRITE,
    TRIGGER_TYPE_HUMAN_REWRITE,
)

# human_rewrite feedback 中人工 comment 的稳定 code（区别于 issue_type）。
FEEDBACK_CODE_HUMAN_COMMENT = "human_comment"


@dataclass(frozen=True)
class RevisionTrigger:
    """一次修订的 trigger：check_result / review_action / human_decision 三选一。

    - `check_result_id`（deterministic_check）：CheckResult.findings 标记的 section；
    - `review_action_id`（audit_rewrite）：ReviewAction.action_type 必须为 rewrite；
    - `human_decision_id`（human_rewrite）：HumanDecision.decision 必须为 rewrite。
    恰好一个非空（spec G discriminated union）。
    """

    check_result_id: UUID | None = None
    review_action_id: UUID | None = None
    human_decision_id: UUID | None = None

    def __post_init__(self) -> None:
        for name in ("check_result_id", "review_action_id", "human_decision_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or (value is not None and not isinstance(value, UUID)):
                raise RevisionInputError(f"{name} 必须是 UUID 或 None")
        present = [
            name
            for name in ("check_result_id", "review_action_id", "human_decision_id")
            if getattr(self, name) is not None
        ]
        if len(present) != 1:
            raise RevisionInputError("trigger 必须恰好一个非空（check/action/decision 三选一）")


@dataclass(frozen=True)
class RevisionFeedbackItem:
    """一条 revision feedback（spec I，全部视为 DATA；确定性派生，供 writer 与指纹）。

    - deterministic_check：code（check code）+ paragraph_index；
    - audit_rewrite：code=issue_type + severity + paragraph_index + message；
    - human_rewrite：underlying issues（同上）+ 末尾一条 human comment
      （code=`human_comment`）。
    """

    trigger_type: str
    code: str
    severity: str | None = None
    paragraph_index: int | None = None
    message: str | None = None

    def to_fingerprint_dict(self) -> dict:
        """规范化 JSON（canonical 指纹输入 + prompt 渲染共用同一投影）。"""
        result: dict = {
            "trigger_type": self.trigger_type,
            "code": self.code,
        }
        if self.severity is not None:
            result["severity"] = self.severity
        if self.paragraph_index is not None:
            result["paragraph_index"] = self.paragraph_index
        if self.message is not None:
            result["message"] = self.message
        return result


@dataclass(frozen=True)
class RevisionRequest:
    """调用方提交的修订请求（spec G：caller 只提供 source + trigger + round）。

    **不传** outline / section / claims / evidence / title / writer identity——
    全部从 VerifiedSourceDraft + verified trigger artifact 派生。
    """

    source_draft_section_id: UUID
    trigger: RevisionTrigger
    revision_round: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.source_draft_section_id, bool) or not isinstance(
            self.source_draft_section_id, UUID
        ):
            raise RevisionInputError("source_draft_section_id 必须是 UUID")
        if not isinstance(self.trigger, RevisionTrigger):
            raise RevisionInputError("trigger 必须是 RevisionTrigger")
        if isinstance(self.revision_round, bool) or not isinstance(self.revision_round, int):
            raise RevisionInputError("revision_round 必须是整数")
        if self.revision_round < 1:
            raise RevisionInputError("revision_round 必须 >= 1")


# ------------------------------------------------------------------ result 投影


@dataclass(frozen=True)
class RevisionResult:
    """一次修订的结果摘要（不含正文段落 / prompt / raw response）。"""

    revision_id: UUID
    source_draft_section_id: UUID
    revised_draft_section_id: UUID
    revision_round: int
    trigger_type: str
    revision_schema_version: int
    revision_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class VerifiedSourceDraft:
    """已验证的 source draft（v2 原始或 v1 修订输出，递归重建，不可变）。

    - pack：**同一 section scope** 的确定性 Section Input Pack（修订 writer 复用
      原 C/E/X/G alias，不能换 Claim/Evidence 集）；
    - original_paragraphs：source draft 正文段落文本（修订的"原文"）。
    """

    draft_section_id: UUID
    section_fingerprint: str
    outline_id: UUID
    outline_fingerprint: str
    section_id: str
    section_order: int
    section_type: str
    title: str
    total_claim_count: int
    claims: list[LoadedClaim]
    evidence: list[LoadedEvidence]
    conflicts: list[ResolvedConflict]
    gaps: list[ResolvedGap]
    pack: SectionInputPack
    original_paragraphs: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedTrigger:
    """已验证的 trigger artifact + 其目标 section 集 + section-normalized feedback。

    - target_section_ids：spec H——audit_rewrite 取 action_payload.target_section_ids；
      human_rewrite 经 HumanDecision→HumanRequest→ReviewAction 恢复；deterministic_check
      取 CheckResult.findings 出现的 section 集；
    - feedback：过滤到 source section 的确定性反馈（spec I）。
    """

    trigger_type: str
    artifact_id: UUID
    artifact_fingerprint: str
    target_section_ids: tuple[str, ...]
    feedback: tuple[RevisionFeedbackItem, ...]


@dataclass(frozen=True)
class VerifiedRevisedDraft:
    """已验证的 revised draft 投影（身份 + 指纹 + 段落计数，不含正文段落）。"""

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


@dataclass(frozen=True)
class VerifiedRevision:
    """`verify_revision_integrity` 的 read-side 产物（完整重建验证通过）。

    - source：递归验证的 source draft（section input + 原正文）；
    - trigger：验证的 trigger artifact + feedback；
    - verified_revised：验证的 revised draft（身份 / payload contracts /
      section_fingerprint 全部重放通过）。
    """

    revision_id: UUID
    source_draft_section_id: UUID
    revised_draft_section_id: UUID
    revision_round: int
    trigger_type: str
    revision_schema_version: int
    revision_fingerprint: str
    created_at: datetime
    source: VerifiedSourceDraft
    trigger: VerifiedTrigger
    verified_revised: VerifiedRevisedDraft


# ------------------------------------------------------------------ 指纹


def compute_revision_input_fingerprint(
    *,
    revision_schema_version: int,
    source_draft_section_id: UUID,
    source_section_fingerprint: str,
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
    trigger_type: str,
    trigger_artifact_id: UUID,
    trigger_artifact_fingerprint: str,
    feedback: list[dict],
    writer_name: str,
    writer_version: int,
    writer_model_id: str,
) -> str:
    """修订输入边界的确定性 SHA-256 指纹（spec K，sort_keys + 固定 separators + UTF-8）。

    至少覆盖：revision_schema_version、source draft section id +
    section fingerprint、outline_fingerprint、section 身份（id/order/type/title）、
    exact allowed Claim/Evidence fingerprints（canonical 排序）、Evidence–Claim
    relation mapping、conflict/gap 数据、trigger_type + trigger artifact
    id/fingerprint、该 section normalized feedback、writer 身份。

    **不得包含** revision_id / created_at / 修订正文 payload。同 source + 同
    trigger + 同 feedback + 同 writer → 同指纹 → replay 同一行；任一输入变化 →
    新指纹 → 新修订（旧行保留，无 update API）。修订正文由 revised DraftSection
    的 `section_fingerprint` 另行绑定。

    `feedback`：`RevisionFeedbackItem.to_fingerprint_dict()` 的确定性列表
    （derive 函数按 trigger 类型 + section 过滤派生，顺序稳定）。
    """
    payload = {
        "revision_schema_version": revision_schema_version,
        "source_draft_section_id": str(source_draft_section_id),
        "source_section_fingerprint": source_section_fingerprint,
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
        "trigger": {
            "trigger_type": trigger_type,
            "artifact_id": str(trigger_artifact_id),
            "artifact_fingerprint": trigger_artifact_fingerprint,
        },
        "feedback": feedback,
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
