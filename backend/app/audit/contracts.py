"""Evidence-bound report audit contracts (stage 5D): constants + request + output + fingerprints.

角色边界（Auditor 只判断"语义上是否真的成立"；IDs / fingerprints / numeric /
alias leak / 引用 closure / conflict/gap preservation 已由确定性 checks 检查）：
- 确定性代码负责：verify Report + verify CheckResult + 构造 Audit Pack（alias
  S/P/C/E/X/G，**LLM 永不看 UUID / fingerprint / provenance id**）+ audit_input
  fingerprint + replay（命中 → 0 model calls）+ structured output 的 hard
  validation（coverage / known / scope / enum）+ resolve aliases + normalize
  issues + deterministic status / route 派生 + create_or_get 原子持久化 +
  verify_audit_integrity（read-side）；
- LLM 负责：`AuditDecision`（reviewed_paragraph_refs + 0..50 issues）——判断正文
  是否忠实表达 Claims、Evidence 是否真正支持文字、是否过度推断 / 因果夸大 /
  遗漏反向证据 / 来源不足 / 未解决冲突；**不重新计算财务数字**；
- LLM **不输出** overall status / recommended route（程序确定性派生，spec O）。

冻结常量：
- `REPORT_AUDIT_SCHEMA_VERSION = 1`（report_audits.audit_schema_version）；
- `AUDITOR_NAME = "evidence_bound_report_auditor"`、`AUDITOR_VERSION = 1`（persisted
  auditor provenance）；production `AUDITOR_MODEL_ID = "deepseek:deepseek-v4-flash"`。

指纹：
- `compute_audit_input_fingerprint` = canonical JSON + SHA-256：audit schema version /
  report_id / report_fingerprint / check_result_id / check_fingerprint / auditor
  身份 / normalized audit pack 身份（section/paragraph 结构 + Claim fingerprints +
  Evidence fingerprints + ClaimEvidence relation mapping + synthesis
  conflict/gap 身份）。**不含** audit_id / created_at / model output。同输入 →
  replay 同一行（**0 model calls**）。
- `compute_audit_fingerprint` = audit_input_fingerprint + normalized resolved
  issues + status + route 的 SHA-256（NOT UNIQUE，唯一性由 input 指纹保证）。
"""

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.report.contracts import VerifiedReport, VerifiedReportCheckResult

# report_audits.audit_schema_version 的当前值（改名或换结构时递增；已有审计原样
# 保留，新语义 → 新 audit_input_fingerprint → 新行）。
REPORT_AUDIT_SCHEMA_VERSION = 1

# evidence-bound report auditor 的身份常量（persisted auditor_name / version）。
AUDITOR_NAME = "evidence_bound_report_auditor"
AUDITOR_VERSION = 1

# production auditor_model_id（adapter 用 settings.llm_provider:llm_model 派生，
# 与 Writer 约定一致；thinking disabled / temperature=0 / structured output /
# 0 tools / 0 web）。
AUDITOR_MODEL_ID = "deepseek:deepseek-v4-flash"

# audit_status 枚举（report_audits 表的 CHECK 约束同步维护）。
AUDIT_STATUS_PASS = "pass"
AUDIT_STATUS_FAIL = "fail"

# recommended_route 枚举（report_audits 表的 CHECK 约束同步维护）。
AUDIT_ROUTE_PASS = "pass"
AUDIT_ROUTE_REWRITE = "rewrite"
AUDIT_ROUTE_RESEARCH = "research"
AUDIT_ROUTE_HUMAN_REVIEW = "human_review"

# issue severity 枚举（review_issues 表的 CHECK 约束同步维护）。
AUDIT_SEVERITY_NORMAL = "normal"
AUDIT_SEVERITY_HIGH = "high"
AUDIT_SEVERITY_CRITICAL = "critical"
AUDIT_SEVERITIES = (
    AUDIT_SEVERITY_NORMAL,
    AUDIT_SEVERITY_HIGH,
    AUDIT_SEVERITY_CRITICAL,
)

# v1 issue_type 枚举（spec L；Auditor 只做语义判断，不重算数字）。
AUDIT_ISSUE_TYPES = (
    "unsupported_by_evidence",
    "evidence_mismatch",
    "claim_misrepresentation",
    "wording_overclaim",
    "omitted_counterevidence",
    "unresolved_conflict",
    "weak_source_quality",
    "stale_or_temporally_misaligned",
    "causal_overreach",
    "valuation_overreach",
    "insufficient_evidence",
)

# 单条 issue 数量硬边界（spec L：0..50）。
MAX_ISSUES = 50

# 单条 issue message 上限（spec L：<=300 chars）。
MAX_ISSUE_MESSAGE_LENGTH = 300

_REF_PATTERN = re.compile(r"^[PSCE][1-9]\d*$")


def valid_ref_format(ref: str) -> bool:
    """ref 是否匹配 `[PSCE]<1..N>`（0 / 前导零 / 超格式返回 False）。"""
    return isinstance(ref, str) and bool(_REF_PATTERN.fullmatch(ref))


# ------------------------------------------------------------------ 结构化输出


class AuditIssueCandidate(BaseModel):
    """一条审计 issue（模型结构化输出；spec L 字段）。

    schema 层只强制格式（ref 格式 / 枚举 / message 长度）；**known / scope /
    coverage 需要在服务层（Audit Pack 已知）校验**。
    """

    model_config = ConfigDict(frozen=True)

    issue_type: str
    severity: str
    section_ref: str
    paragraph_ref: str | None = None
    claim_refs: list[str]
    evidence_refs: list[str]
    message: str

    @model_validator(mode="after")
    def _validate(self) -> "AuditIssueCandidate":
        if self.issue_type not in AUDIT_ISSUE_TYPES:
            raise ValueError(f"issue_type 必须是已知类型之一: {AUDIT_ISSUE_TYPES}")
        if self.severity not in AUDIT_SEVERITIES:
            raise ValueError(f"severity 必须是已知级别之一: {AUDIT_SEVERITIES}")
        if not valid_ref_format(self.section_ref):
            raise ValueError("section_ref 必须是 S<number> 格式")
        if self.paragraph_ref is not None and not valid_ref_format(self.paragraph_ref):
            raise ValueError("paragraph_ref 必须是 P<number> 格式")
        for ref in self.claim_refs:
            if not valid_ref_format(ref):
                raise ValueError("claim_refs 必须是 C<number> 格式")
        for ref in self.evidence_refs:
            if not valid_ref_format(ref):
                raise ValueError("evidence_refs 必须是 E<number> 格式")
        message = self.message.strip()
        if not message:
            raise ValueError("message 不能为空（trim 后）")
        if len(message) > MAX_ISSUE_MESSAGE_LENGTH:
            raise ValueError(f"message 超长（>{MAX_ISSUE_MESSAGE_LENGTH} 字符）")
        object.__setattr__(self, "message", message)
        return self


class AuditDecision(BaseModel):
    """一次审计的结构化输出（模型生成）：reviewed coverage + 0..50 issues。

    schema 层只强制 ref 格式 / 数量边界；**no-cherry-picking（reviewed 恰好等于
    pack P refs）与 unknown / scope 需要在服务层（Audit Pack 已知）校验**。
    """

    model_config = ConfigDict(frozen=True)

    reviewed_paragraph_refs: list[str]
    issues: list[AuditIssueCandidate]

    @model_validator(mode="after")
    def _validate(self) -> "AuditDecision":
        if not isinstance(self.reviewed_paragraph_refs, list) or not self.reviewed_paragraph_refs:
            raise ValueError("reviewed_paragraph_refs 不能为空")
        for ref in self.reviewed_paragraph_refs:
            if not valid_ref_format(ref):
                raise ValueError("reviewed_paragraph_refs 必须是 P<number> 格式")
        if len(self.reviewed_paragraph_refs) != len(set(self.reviewed_paragraph_refs)):
            raise ValueError("reviewed_paragraph_refs 不能重复")
        if not isinstance(self.issues, list) or len(self.issues) > MAX_ISSUES:
            raise ValueError(f"issues 数量必须在 0..{MAX_ISSUES}")
        return self


# ------------------------------------------------------------------ request / result


@dataclass(frozen=True)
class ReportAuditRequest:
    """调用方提交的审计请求。

    **只提供 report_id + check_result_id**：company / claims / evidence /
    paragraphs / findings / auditor version 全部从 verified Report + verified
    CheckResult 派生，调用方不得提供。
    """

    report_id: UUID
    check_result_id: UUID

    def __post_init__(self) -> None:
        if isinstance(self.report_id, bool) or not isinstance(self.report_id, UUID):
            raise ValueError("report_id 必须是 UUID")
        if isinstance(self.check_result_id, bool) or not isinstance(self.check_result_id, UUID):
            raise ValueError("check_result_id 必须是 UUID")


@dataclass(frozen=True)
class ReportAuditResult:
    """一次审计的结果摘要（不含 issues 明细 / prompt / raw response）。"""

    audit_id: UUID
    report_id: UUID
    check_result_id: UUID
    audit_schema_version: int
    audit_status: str
    recommended_route: str
    issue_count: int
    audit_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class ReviewIssue:
    """一条持久化 audit issue（read-side 投影）。"""

    review_issue_id: UUID
    audit_id: UUID
    ordinal: int
    issue_type: str
    severity: str
    section_id: str
    paragraph_index: int | None
    message: str
    related_claim_ids: tuple[str, ...]
    related_evidence_card_ids: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedAuditIssue:
    """一条已解析到真实 ID 的 issue（persisted review_issues 的规范化数据）。"""

    issue_type: str
    severity: str
    section_id: str
    paragraph_index: int | None
    message: str
    related_claim_ids: tuple[str, ...]
    related_evidence_card_ids: tuple[str, ...]

    def to_fingerprint_dict(self) -> dict:
        return {
            "issue_type": self.issue_type,
            "severity": self.severity,
            "section_id": self.section_id,
            "paragraph_index": self.paragraph_index,
            "message": self.message,
            "related_claim_ids": sorted(self.related_claim_ids),
            "related_evidence_card_ids": sorted(self.related_evidence_card_ids),
        }


@dataclass(frozen=True)
class VerifiedReportAudit:
    """`verify_audit_integrity` 的 read-side 产物（完整重建验证通过）。

    - verified_report / verified_check：上游已验证 Report / CheckResult
      （Stage 5D 只能消费 `VerifiedReportCheckResult`，spec B）；
    - issues：从 persisted review_issues 重载并验证的 issue 序列。
    """

    audit_id: UUID
    report_id: UUID
    check_result_id: UUID
    audit_schema_version: int
    auditor_name: str
    auditor_version: int
    auditor_model_id: str
    audit_input_fingerprint: str
    audit_status: str
    recommended_route: str
    issue_count: int
    audit_fingerprint: str
    issues: tuple[ReviewIssue, ...]
    verified_report: VerifiedReport
    verified_check: VerifiedReportCheckResult


# ------------------------------------------------------------------ 指纹


def compute_audit_input_fingerprint(
    *,
    audit_schema_version: int,
    report_id: UUID,
    report_fingerprint: str,
    check_result_id: UUID,
    check_fingerprint: str,
    auditor_name: str,
    auditor_version: int,
    auditor_model_id: str,
    pack_identity: dict,
) -> str:
    """LLM 输入边界的确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：audit_schema_version、report_id、report_fingerprint、
    check_result_id、check_fingerprint、auditor 身份、normalized audit pack 身份
    （section/paragraph 结构 + Claim fingerprints + Evidence fingerprints +
    ClaimEvidence relation mapping + synthesis conflict/gap 身份）。

    **不得包含** audit_id / created_at / model output。完全相同 → replay 同一行
    （0 model calls）；report / check / claims / evidence / relations / conflicts /
    gaps / schema / auditor 任一变化 → 新指纹 → 新 Audit（旧行保留）。
    """
    payload = {
        "audit_schema_version": audit_schema_version,
        "report_id": str(report_id),
        "report_fingerprint": report_fingerprint,
        "check_result_id": str(check_result_id),
        "check_fingerprint": check_fingerprint,
        "auditor": {
            "auditor_name": auditor_name,
            "auditor_version": auditor_version,
            "auditor_model_id": auditor_model_id,
        },
        "audit_pack": pack_identity,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_audit_fingerprint(
    *,
    audit_input_fingerprint: str,
    issues: list[dict],
    status: str,
    route: str,
) -> str:
    """审计不可变指纹：audit_input_fingerprint + normalized resolved issues + status + route。

    issues 必须已规范化（deterministic ordinal 顺序 + `to_fingerprint_dict`）。
    **NOT UNIQUE**——同输入必须 replay 同一行，唯一性由 audit_input_fingerprint
    UNIQUE 保证；issue / status / route 变化 → 新审计指纹 → 完整性校验拒绝。
    """
    payload = {
        "audit_input_fingerprint": audit_input_fingerprint,
        "issues": issues,
        "status": status,
        "route": route,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
