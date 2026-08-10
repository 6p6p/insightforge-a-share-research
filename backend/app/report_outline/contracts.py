"""Report outline contracts (stage 5A): constants + fingerprint + result summary.

角色边界（提纲是确定性派生产物，不是 LLM 判断）：
- 确定性代码负责：从已验证 SynthesisResult 机械映射 theme → theme section、
  conflicts/gaps → risks_and_gaps section、coverage 硬边界、outline_fingerprint、
  create_or_get 原子持久化；
- **不调用 LLM 规划**（0 planner model / 0 analyst version）：没有
  planner_model_id，没有 analyst 版本。

冻结常量：
- `REPORT_OUTLINE_SCHEMA_VERSION = 1`（report_outlines.outline_schema_version）；
- section_type：`theme` / `risks_and_gaps`；risks_and_gaps 固定标题。

`compute_outline_fingerprint` = canonical JSON + SHA-256（sort_keys + 固定
separators + UTF-8）：含 outline_schema_version / synthesis_result_id /
synthesis result_fingerprint / company_id / research_question_sha256 /
analysis_as_of / normalized payload。**不含** outline_id / created_at。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.analysis.synthesis.contracts import VerifiedSynthesisResult
from app.report_outline.errors import ReportOutlineIntegrityError

# report_outlines.outline_schema_version 的当前值（改名或换结构时递增；
# 已有提纲原样保留，新语义 → 新 fingerprint → 新行）。
REPORT_OUTLINE_SCHEMA_VERSION = 1

# v1 payload 的 section_type 枚举。
SECTION_TYPE_THEME = "theme"
SECTION_TYPE_RISKS_AND_GAPS = "risks_and_gaps"

# risks_and_gaps section 的固定标题（不重写、不生成解释正文）。
OUTLINE_RISKS_AND_GAPS_TITLE = "风险、冲突与证据缺口"


def compute_outline_fingerprint(
    *,
    outline_schema_version: int,
    synthesis_result_id: UUID,
    synthesis_result_fingerprint: str,
    company_id: UUID,
    research_question_sha256: str,
    analysis_as_of: date,
    outline_payload: dict,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：outline_schema_version、synthesis_result_id、
    synthesis_result_fingerprint（= result 的完整输入边界）、company_id、
    research_question_sha256、analysis_as_of、normalized outline_payload。

    **不得包含** outline_id / created_at。同 result + 同 schema + 同 payload →
    同指纹 → replay 同一行；SynthesisResult / schema / payload 任一变化 →
    新指纹 → 新提纲（旧行保留，无 update API）。
    """
    payload = {
        "outline_schema_version": outline_schema_version,
        "synthesis_result_id": str(synthesis_result_id),
        "synthesis_result_fingerprint": synthesis_result_fingerprint,
        "company_id": str(company_id),
        "research_question_sha256": research_question_sha256,
        "analysis_as_of": analysis_as_of.isoformat(),
        "outline_payload": outline_payload,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class ReportOutlineResult:
    """一次提纲派生的结果摘要（不含 outline_payload 正文结构明细）。"""

    outline_id: UUID
    synthesis_result_id: UUID
    company_id: UUID
    research_question_sha256: str
    analysis_as_of: date
    outline_schema_version: int
    outline_fingerprint: str
    replayed: bool
    section_count: int


@dataclass(frozen=True)
class OutlineSection:
    """一条已验证 outline section（从 re-derived payload 解析，不可变）。

    - claim_ids 已解析回真实 claim UUID（从 v1 payload 的字符串投影）；
    - conflict_indexes / evidence_gap_indexes 指向 VerifiedSynthesisResult 的
      conflicts / evidence_gaps 数组（risks_and_gaps 恢复用）。
    """

    section_id: str
    section_order: int
    section_type: str
    title: str
    claim_ids: tuple[UUID, ...]
    conflict_indexes: tuple[int, ...]
    evidence_gap_indexes: tuple[int, ...]


@dataclass(frozen=True)
class VerifiedReportOutline:
    """经 `verify_outline_integrity` 完整校验后的 Outline 投影（不可变）。

    **Stage 5B Writer 唯一可信输入**：Writer 不直接相信
    `report_outlines.outline_payload`，只消费本投影派生 section / claim set。
    校验覆盖：row 存在 + 上游 SynthesisResult 完整校验
    （`SynthesisAnalysisService.verify_result_integrity`）+ 重派生 payload +
    重算 fingerprint 与 persisted 7 字段逐一对比；任一损坏 →
    `ReportOutlineIntegrityError`（**不自动 repair**）。

    - sections：从**重派生** payload 解析（等于 persisted 时才通过校验）；
    - verified_synthesis_result：上游已验证综合结果（Writer 用它恢复
      risks_and_gaps 的 conflict / gap Claims）。
    """

    outline_id: UUID
    synthesis_result_id: UUID
    company_id: UUID
    research_question_sha256: str
    analysis_as_of: date
    outline_schema_version: int
    outline_fingerprint: str
    sections: tuple[OutlineSection, ...]
    verified_synthesis_result: VerifiedSynthesisResult


def parse_outline_sections(payload: dict) -> tuple[OutlineSection, ...]:
    """纯函数：v1 outline payload → 规范化 OutlineSection 元组（防御性解析）。

    校验通过后调用（payload 与重派生一致）；这里只做结构防御：sections 必须是
    非空 list，每条 section 字段完整且 claim_ids 是合法 UUID。损坏 →
    `ReportOutlineIntegrityError`（Writer 不消费损坏投影）。
    """
    sections_raw = payload.get("sections")
    if not isinstance(sections_raw, list) or not sections_raw:
        raise ReportOutlineIntegrityError(
            "report outline payload sections must be a non-empty list"
        )

    sections: list[OutlineSection] = []
    for index, item in enumerate(sections_raw):
        if not isinstance(item, dict):
            raise ReportOutlineIntegrityError(f"report outline section[{index}] must be an object")
        section_id = item.get("section_id")
        section_order = item.get("section_order")
        section_type = item.get("section_type")
        title = item.get("title")
        claim_ids_raw = item.get("claim_ids")
        conflict_indexes_raw = item.get("conflict_indexes")
        evidence_gap_indexes_raw = item.get("evidence_gap_indexes")
        if not isinstance(section_id, str) or not section_id:
            raise ReportOutlineIntegrityError(f"report outline section[{index}] section_id invalid")
        if not isinstance(section_type, str) or not section_type:
            raise ReportOutlineIntegrityError(
                f"report outline section[{index}] section_type invalid"
            )
        if not isinstance(title, str) or not title:
            raise ReportOutlineIntegrityError(f"report outline section[{index}] title invalid")
        if (
            isinstance(section_order, bool)
            or not isinstance(section_order, int)
            or section_order < 1
        ):
            raise ReportOutlineIntegrityError(
                f"report outline section[{index}] section_order invalid"
            )
        if not isinstance(claim_ids_raw, list):
            raise ReportOutlineIntegrityError(f"report outline section[{index}] claim_ids invalid")
        if not isinstance(conflict_indexes_raw, list) or not isinstance(
            evidence_gap_indexes_raw, list
        ):
            raise ReportOutlineIntegrityError(
                f"report outline section[{index}] conflict/gap indexes invalid"
            )
        claim_ids: list[UUID] = []
        for raw_id in claim_ids_raw:
            if isinstance(raw_id, bool) or not isinstance(raw_id, str):
                raise ReportOutlineIntegrityError(
                    f"report outline section[{index}] claim_id not a string"
                )
            try:
                claim_ids.append(UUID(raw_id))
            except ValueError:
                raise ReportOutlineIntegrityError(
                    f"report outline section[{index}] claim_id not a UUID"
                ) from None
        sections.append(
            OutlineSection(
                section_id=section_id,
                section_order=section_order,
                section_type=section_type,
                title=title,
                claim_ids=tuple(claim_ids),
                conflict_indexes=tuple(int(i) for i in conflict_indexes_raw),
                evidence_gap_indexes=tuple(int(i) for i in evidence_gap_indexes_raw),
            )
        )
    return tuple(sections)
