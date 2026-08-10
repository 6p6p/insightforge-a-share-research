"""Deterministic report assembly contracts (stage 5C): constants + request + output + fingerprints.

角色边界（Report 是**确定性装配**产物，不是 LLM 判断）：
- 确定性代码负责：verify Outline + 逐个 verify DraftSection + coverage / identity
  校验（每 Outline section 恰好一个 DraftSection）+ 按 Outline order 拼装 payload +
  report_fingerprint + create_or_get 原子持久化；
- **不调用 LLM**（0 model identity）：没有 writer/planner 身份，Report 不重新生成
  summary / conclusion / investment recommendation（Outline 没有的 Report 不擅自
  增加）；
- caller 只提供 `outline_id` + `draft_section_ids`（**显式选择**，spec L），其余
  company / question / cutoff / section order / payload / fingerprint 全部从
  verified artifacts 派生。

冻结常量：
- `REPORT_SCHEMA_VERSION = 1`（reports.report_schema_version）；
- `REPORT_CHECK_SCHEMA_VERSION = 1`（report_check_results.check_schema_version）；
  均为 0 model identity。

指纹：
- `compute_report_fingerprint` = canonical JSON + SHA-256：含 report schema version /
  outline_id / outline_fingerprint / company_id / research_question_sha256 /
  analysis_as_of / selected draft sections（按 section_order：draft_section_id /
  section_fingerprint / writer_name / writer_version / writer_model_id）/
  normalized report payload。**不含** report_id / created_at。完全相同 → replay；
  任何 DraftSection 变化 → 新指纹 → 新 Report（旧行保留，无 update API）。
- `compute_check_fingerprint` = check schema version / report_id / report_fingerprint /
  normalized findings 的 SHA-256。相同 → replay；Report 改变（report_fingerprint 变）
  → 新 check_fingerprint → 新 CheckResult（旧行保留）。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.draft_section.contracts import VerifiedDraftSection
from app.report.errors import ReportInputError
from app.report_outline.contracts import VerifiedReportOutline

# reports.report_schema_version 的当前值（改名或换结构时递增；已有 Report 原样
# 保留，新语义 → 新 fingerprint → 新行）。
REPORT_SCHEMA_VERSION = 1

# report_check_results.check_schema_version 的当前值（v1 = 10 个确定性 checks）。
REPORT_CHECK_SCHEMA_VERSION = 1

# check status 枚举（reports 表的 CHECK 约束同步维护）。
CHECK_STATUS_PASS = "pass"
CHECK_STATUS_FAIL = "fail"


@dataclass(frozen=True)
class ReportAssemblyDraft:
    """调用方提交的装配请求。

    **只提供 outline_id + draft_section_ids**：company / question / cutoff /
    section order / payload / fingerprint 全部从 VerifiedReportOutline +
    VerifiedDraftSection 派生，调用方不得提供。
    """

    outline_id: UUID
    draft_section_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if isinstance(self.outline_id, bool) or not isinstance(self.outline_id, UUID):
            raise ReportInputError("outline_id 必须是 UUID")
        if not isinstance(self.draft_section_ids, tuple) or not self.draft_section_ids:
            raise ReportInputError("draft_section_ids 必须是非空 tuple")
        for value in self.draft_section_ids:
            if isinstance(value, bool) or not isinstance(value, UUID):
                raise ReportInputError("draft_section_ids 必须全部是 UUID")
        if len(set(self.draft_section_ids)) != len(self.draft_section_ids):
            raise ReportInputError("draft_section_ids 不能重复")


@dataclass(frozen=True)
class ReportResult:
    """一次装配的结果摘要（不含 report_payload 正文结构明细）。"""

    report_id: UUID
    outline_id: UUID
    company_id: UUID
    research_question_sha256: str
    analysis_as_of: date
    report_schema_version: int
    report_fingerprint: str
    replayed: bool
    section_count: int


@dataclass(frozen=True)
class VerifiedReport:
    """`verify_report_integrity` 的 read-side 产物（完整重建验证通过）。

    - report_payload：与 persisted 行一致的重派生 payload；
    - verified_outline：上游已验证 Outline（checks 用 allowed claim 集）；
    - verified_drafts：按 Outline section_order 排序的已验证 DraftSection 投影
      （只含身份 / 指纹 / 段落计数，不含正文段落）。
    """

    report_id: UUID
    outline_id: UUID
    company_id: UUID
    research_question_sha256: str
    analysis_as_of: date
    report_schema_version: int
    report_fingerprint: str
    report_payload: dict
    verified_outline: VerifiedReportOutline
    verified_drafts: tuple[VerifiedDraftSection, ...]


@dataclass(frozen=True)
class CheckFinding:
    """一次确定性 check 的结构化 finding（spec R：**不存长 prose**）。

    可空字段按需要省略；related_* 只存真实 ID（字符串 UUID）。
    """

    code: str
    section_id: str | None = None
    paragraph_index: int | None = None
    related_claim_ids: tuple[str, ...] = ()
    related_evidence_card_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """规范化 JSON（reports.report_check_results.findings 的元素）。

        只包含非空字段（可空字段按需要）；related_* 保持字符串 UUID 列表。
        """
        result: dict = {"code": self.code}
        if self.section_id is not None:
            result["section_id"] = self.section_id
        if self.paragraph_index is not None:
            result["paragraph_index"] = self.paragraph_index
        if self.related_claim_ids:
            result["related_claim_ids"] = list(self.related_claim_ids)
        if self.related_evidence_card_ids:
            result["related_evidence_card_ids"] = list(self.related_evidence_card_ids)
        return result


@dataclass(frozen=True)
class ReportCheckResult:
    """一次确定性报告检查的结果摘要。"""

    check_result_id: UUID
    report_id: UUID
    check_schema_version: int
    status: str  # pass / fail（无 findings → pass，有任何 finding → fail）
    findings: tuple[CheckFinding, ...]
    check_fingerprint: str
    replayed: bool


def compute_report_fingerprint(
    *,
    report_schema_version: int,
    outline_id: UUID,
    outline_fingerprint: str,
    company_id: UUID,
    research_question_sha256: str,
    analysis_as_of: date,
    draft_sections: list[dict],
    report_payload: dict,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：report_schema_version、outline_id、outline_fingerprint、company_id、
    research_question_sha256、analysis_as_of、selected draft sections（按
    section_order：draft_section_id / section_fingerprint / writer_name /
    writer_version / writer_model_id）、normalized report_payload。

    **不得包含** report_id / created_at。完全相同 → replay 同一行；任一
    DraftSection / Outline / schema / payload 变化 → 新指纹 → 新 Report
    （旧行保留，无 update API）。
    """
    payload = {
        "report_schema_version": report_schema_version,
        "outline_id": str(outline_id),
        "outline_fingerprint": outline_fingerprint,
        "company_id": str(company_id),
        "research_question_sha256": research_question_sha256,
        "analysis_as_of": analysis_as_of.isoformat(),
        "draft_sections": draft_sections,
        "report_payload": report_payload,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_check_fingerprint(
    *,
    check_schema_version: int,
    report_id: UUID,
    report_fingerprint: str,
    findings: list[dict],
) -> str:
    """确定性 SHA-256 指纹：check schema + report_id + report_fingerprint + findings。

    findings 必须已规范化（deterministic order + `CheckFinding.to_dict()` 只含非空
    字段）。相同 report + 同 schema + 同 findings → 同指纹 → replay 同一行；Report
    内容变化 → report_fingerprint 不同 → 新 check_fingerprint → 新 CheckResult
    （旧行保留，无 update API）。
    """
    payload = {
        "check_schema_version": check_schema_version,
        "report_id": str(report_id),
        "report_fingerprint": report_fingerprint,
        "findings": findings,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
