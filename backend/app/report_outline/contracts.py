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
