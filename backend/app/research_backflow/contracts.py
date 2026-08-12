"""Research backflow contracts (stage 5E.2B): constants + result projections + fingerprints.

角色边界（Backflow 只做可验证的研究交接 / 消费 upstream 返回的新综合结论；不执行
Stage2/3/4 research）：
- 确定性代码负责：verify source Stage 5 run（graph_name + 真实 terminal=
  research_required）→ verify review action（± human decision）→ verify source
  Report → 从 Report→Outline→Synthesis chain 恢复身份 / cutoff → derive 结构化
  request payload → request fingerprint → create_or_get 原子持久化；fulfillment：
  verify request → verify 新 SynthesisResult → continuation identity / no-progress
  政策 → fulfillment fingerprint → create_or_get；
- **request/fulfillment 不调用 LLM / 不检索 Chroma / 不执行 Stage2/3/4**——只产生
  可验证 research handoff 并消费 upstream 返回的 `new_synthesis_result_id`；
  supplemental plan 只做 **0 LLM** 确定性派生（见下）；
- **execution（7A.2B.3 `ResearchBackflowExecutor`）单独检索已有 Source Library**：
  真实 RetrievalService→Chroma→PG hydrate→EvidenceExtraction→EvidenceCard；无
  满足 source → manual_required（不假装完成）。

冻结常量：
- `RESEARCH_BACKFLOW_REQUEST_SCHEMA_VERSION = 1`、
  `RESEARCH_BACKFLOW_FULFILLMENT_SCHEMA_VERSION = 1`、
  `RESEARCH_BACKFLOW_PLAN_SCHEMA_VERSION = 1`（0 model identity）；
- `RESEARCH_NEED_CODE_HUMAN_REQUESTED_RESEARCH = "human_requested_research"`
  （human research 的 request 恒含此 code）；
- `SUPPLEMENTAL_RESEARCH_NEED_CODES` / `RESEARCH_NEED_DESCRIPTIONS` /
  `MAX_QUERIES_PER_NEED`（stage 7A.2B.3：补充计划 need_specs 的受控词表与
  deterministic query 模板上限）。

指纹（canonical JSON + SHA-256，**均不含 row id / created_at**）：
- `compute_research_backflow_request_fingerprint` = schema + source_stage5_run_id +
  review_action id+fingerprint + human_decision id+fingerprint（可选）+
  source_report id+fingerprint + company + question hash + analysis_as_of +
  normalized request_payload（spec J：同 run → replay，不 update）；
- `compute_research_backflow_fulfillment_fingerprint` = schema + research_request_id
  + request_fingerprint + new_synthesis_result_id + result_fingerprint +
  new_synthesis_run_id + synthesis_fingerprint（spec N：同 request+result →
  replay；不同 result → AlreadyFulfilled，不覆盖）；
- `compute_research_backflow_plan_fingerprint` = schema + request id+fingerprint +
  strategy + normalized plan_payload（spec K：同 request → replay 同一行）。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.analysis.synthesis.contracts import VerifiedSynthesisResult
from app.report.contracts import VerifiedReport
from app.review.contracts import (
    VerifiedHumanReviewDecision,
    VerifiedReviewAction,
)

# research_backflow_requests.request_schema_version 的当前值（改名或换结构时递增；
# 已有请求原样保留，新语义 → 新 request_fingerprint → 新行）。
RESEARCH_BACKFLOW_REQUEST_SCHEMA_VERSION = 1

# research_backflow_fulfillments.fulfillment_schema_version 的当前值。
RESEARCH_BACKFLOW_FULFILLMENT_SCHEMA_VERSION = 1

# research_backflow_plans.plan_schema_version 的当前值（改 payload 结构时递增；
# 已有 plan 原样保留，新语义 → 新 fingerprint → 新行）。
RESEARCH_BACKFLOW_PLAN_SCHEMA_VERSION = 1

# v1 确定性补充研究策略（stage 7A.2B.3 spec K：**0 LLM** 计划生成；只研究已有
# Source Library，不自动获取新源）。`strategy_name` 在 research_backflow_plans
# 表存 String(64)。
SUPPLEMENTAL_RESEARCH_STRATEGY_NAME = "existing_source_library"
SUPPLEMENTAL_RESEARCH_STRATEGY_VERSION = 1

# 补充研究 need codes（= AUDIT_ISSUE_TYPES 中触发补充研究的子集，spec K 冻结）。
# 每个 code 映射一个确定性 strategy（v1 全部 → existing_source_library）。issue
# 其余类型（evidence_mismatch / claim_misrepresentation / wording_overclaim /
# omitted_counterevidence / unresolved_conflict / valuation_overreach）属于措辞 /
# 表示问题，不是补充研究需求 → 不进 need_specs。
SUPPLEMENTAL_RESEARCH_NEED_CODES = frozenset(
    (
        "unsupported_by_evidence",
        "weak_source_quality",
        "stale_or_temporally_misaligned",
        "causal_overreach",
        "insufficient_evidence",
    )
)

# need code → 受控中文描述（确定性 query 模板的固定短语，**非 LLM 生成**）。
RESEARCH_NEED_DESCRIPTIONS = {
    "unsupported_by_evidence": "核实证据支持",
    "weak_source_quality": "核实来源质量",
    "stale_or_temporally_misaligned": "核实时间对齐与时效性",
    "causal_overreach": "核实因果推论",
    "insufficient_evidence": "补充证据",
}

# query 模板冻结上限：每个 need_spec 派生 retrieval_queries 数量上限（spec K：
# max query 冻结，禁止 LLM 自由生成 web query）。
MAX_QUERIES_PER_NEED = 3

# human research 的 request 恒含的 research_need_code（spec H：至少
# human_requested_research + issue_type 映射 codes）。
RESEARCH_NEED_CODE_HUMAN_REQUESTED_RESEARCH = "human_requested_research"

# 补充研究执行状态（spec K-X：不假装完成）。
RESEARCH_BACKFLOW_NEED_STATUS_RESOLVED = "resolved"
RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED = "manual_required"

# manual_required 的稳定 reason（spec K：无满足 source → source_acquisition_required）。
RESEARCH_BACKFLOW_MANUAL_REASON_SOURCE_ACQUISITION = "source_acquisition_required"
RESEARCH_BACKFLOW_MANUAL_REASON_INDEX_NOT_READY = "index_not_ready"
RESEARCH_BACKFLOW_MANUAL_REASON_EVIDENCE_NOT_EXTRACTED = "evidence_not_extracted"
# 7A.2B.3 scope 冻结：structured financial/macro/valuation refresh 不在 automatic
# 文档补充研究范围（需 provider/network 或新数据）→ 明确 manual_required，
# **不误报 research_backflow_no_progress**。
RESEARCH_BACKFLOW_MANUAL_REASON_STRUCTURED_DATA_REFRESH = "structured_data_refresh_required"


def _canonical_dumps(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


# ------------------------------------------------------------------ result 投影


@dataclass(frozen=True)
class ResearchBackflowRequestResult:
    """一次 research request 的摘要（不含 issue 明细 / 任何正文）。"""

    research_request_id: UUID
    source_stage5_run_id: UUID
    review_action_id: UUID
    human_decision_id: UUID | None
    source_report_id: UUID
    company_id: UUID
    research_question_sha256: str
    analysis_as_of: date
    request_schema_version: int
    request_payload: dict
    request_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class ResearchBackflowFulfillmentResult:
    """一次 fulfillment 的摘要（consumed upstream SynthesisResult 的身份）。"""

    fulfillment_id: UUID
    research_request_id: UUID
    new_synthesis_result_id: UUID
    fulfillment_schema_version: int
    fulfillment_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class ResearchBackflowPlanResult:
    """一次补充研究计划的摘要（stage 7A.2B.3，**不含** model reasoning / prompt）。

    plan_payload 是结构化的 `need_specs[]`（need_code / target_section_ids /
    related_claim_ids / related_evidence_card_ids / retrieval_queries[] /
    allowed_source_types[] / manual_required_reason?），query 由确定性模板派生。
    """

    backflow_plan_id: UUID
    research_backflow_request_id: UUID
    plan_schema_version: int
    strategy_name: str
    strategy_version: int
    plan_payload: dict
    plan_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class ResearchBackflowNeedExecution:
    """一条 need_spec 的补充研究执行结果（spec K-X）。

    - status：`RESEARCH_BACKFLOW_NEED_STATUS_RESOLVED`（至少创建/复用一张相关
      EvidenceCard）或 `RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED`；
    - manual_required_reason：manual_required 时的稳定原因（source_acquisition_required
      / index_not_ready / evidence_not_extracted），resolved 时 None；
    - created / replayed：本次执行创建的 / fingerprint replay 复用的证据卡 id
      （canonical 排序）。
    """

    need_code: str
    status: str
    created_evidence_card_ids: tuple[UUID, ...]
    replayed_evidence_card_ids: tuple[UUID, ...]
    manual_required_reason: str | None = None


@dataclass(frozen=True)
class ResearchBackflowExecutionResult:
    """`execute_supplemental_research` 的结果（**仅 application output**）。

    不保存 model reasoning / prompt / query 明细；只投影每条 need 的执行摘要 +
    全量 created/replayed 证据卡 id（供 prepare_updated_analysis 组装新 Stage4
    输入 + verify_progress 判定新增）。
    """

    attempts: tuple[ResearchBackflowNeedExecution, ...]
    new_evidence_card_ids: tuple[UUID, ...]
    replayed_evidence_card_ids: tuple[UUID, ...]
    all_manual_required: bool
    resolved_need_codes: tuple[str, ...]


# ------------------------------------------------------------------ verified 产物


@dataclass(frozen=True)
class VerifiedResearchBackflowRequest:
    """`verify_research_request_integrity` 的 read-side 产物（完整重建验证通过）。

    - verified_action / verified_decision：上游已验证 ReviewAction（± human
      decision），legal trigger 已重放（research 无 decision / human_review +
      research decision）；
    - verified_report：source Report 已验证（→ verified_outline →
      verified_source_synthesis，身份 / cutoff 从该 chain 恢复）；
    - verified_source_synthesis：source Report 链上的 SynthesisResult
      （no-progress 政策的 source 边界）。
    """

    research_request_id: UUID
    source_stage5_run_id: UUID
    review_action_id: UUID
    human_decision_id: UUID | None
    source_report_id: UUID
    company_id: UUID
    research_question_sha256: str
    analysis_as_of: date
    request_schema_version: int
    request_payload: dict
    request_fingerprint: str
    created_at: datetime
    verified_action: VerifiedReviewAction
    verified_decision: VerifiedHumanReviewDecision | None
    verified_report: VerifiedReport
    verified_source_synthesis: VerifiedSynthesisResult


@dataclass(frozen=True)
class VerifiedResearchBackflowFulfillment:
    """`verify_research_fulfillment_integrity` 的 read-side 产物。

    - verified_request：完整校验的 research request（continuation identity /
      no-progress 的基准）；
    - verified_new_synthesis：consumed upstream 新 SynthesisResult（identity /
      cutoff 已与 request 比对）。
    """

    fulfillment_id: UUID
    research_request_id: UUID
    new_synthesis_result_id: UUID
    fulfillment_schema_version: int
    fulfillment_fingerprint: str
    created_at: datetime
    verified_request: VerifiedResearchBackflowRequest
    verified_new_synthesis: VerifiedSynthesisResult


# ------------------------------------------------------------------ 指纹


def compute_research_backflow_request_fingerprint(
    *,
    request_schema_version: int,
    source_stage5_run_id: UUID,
    review_action_id: UUID,
    review_action_fingerprint: str,
    human_decision_id: UUID | None,
    human_decision_fingerprint: str | None,
    source_report_id: UUID,
    report_fingerprint: str,
    company_id: UUID,
    research_question_sha256: str,
    analysis_as_of: date,
    request_payload: dict,
) -> str:
    """ResearchBackflowRequest 指纹（spec J）：schema + source run + action（±
    decision）+ report + 身份/cutoff + normalized payload。

    **不得包含** research_request_id / created_at。同 run → replay 同一行
    （不 update）；request payload / 上游 action / decision / report / 身份 /
    cutoff 任一变化 → 新指纹 → 新请求（旧行保留）。
    """
    payload = {
        "request_schema_version": request_schema_version,
        "source_stage5_run_id": str(source_stage5_run_id),
        "review_action_id": str(review_action_id),
        "review_action_fingerprint": review_action_fingerprint,
        "human_decision_id": str(human_decision_id) if human_decision_id is not None else None,
        "human_decision_fingerprint": human_decision_fingerprint,
        "source_report_id": str(source_report_id),
        "report_fingerprint": report_fingerprint,
        "company_id": str(company_id),
        "research_question_sha256": research_question_sha256,
        "analysis_as_of": analysis_as_of.isoformat(),
        "request_payload": request_payload,
    }
    return hashlib.sha256(_canonical_dumps(payload)).hexdigest()


def compute_research_backflow_fulfillment_fingerprint(
    *,
    fulfillment_schema_version: int,
    research_request_id: UUID,
    request_fingerprint: str,
    new_synthesis_result_id: UUID,
    new_synthesis_result_fingerprint: str,
    new_synthesis_run_id: UUID,
    new_synthesis_run_fingerprint: str,
) -> str:
    """ResearchBackflowFulfillment 指纹（spec N）：schema + request id+fingerprint +
    new synthesis result id+result fingerprint + new synthesis run id+run fingerprint。

    **不得包含** fulfillment_id / created_at。同 request+result → replay 同一行；
    request 已 fulfilled 且 result 不同 → `ResearchBackflowAlreadyFulfilled`
    （不覆盖历史，无 update API）。
    """
    payload = {
        "fulfillment_schema_version": fulfillment_schema_version,
        "research_request_id": str(research_request_id),
        "request_fingerprint": request_fingerprint,
        "new_synthesis_result_id": str(new_synthesis_result_id),
        "new_synthesis_result_fingerprint": new_synthesis_result_fingerprint,
        "new_synthesis_run_id": str(new_synthesis_run_id),
        "new_synthesis_run_fingerprint": new_synthesis_run_fingerprint,
    }
    return hashlib.sha256(_canonical_dumps(payload)).hexdigest()


def compute_research_backflow_plan_fingerprint(
    *,
    plan_schema_version: int,
    research_backflow_request_id: UUID,
    request_fingerprint: str,
    strategy_name: str,
    strategy_version: int,
    plan_payload: dict,
) -> str:
    """ResearchBackflowPlan 指纹（spec K）：schema + request id+fingerprint +
    strategy + normalized plan_payload。

    **不得包含** backflow_plan_id / created_at。同 request → replay 同一行；
    request payload / request fingerprint / strategy / plan_payload 任一变化 →
    新指纹 → 新 plan（旧行保留，无 update API）。
    """
    payload = {
        "plan_schema_version": plan_schema_version,
        "research_backflow_request_id": str(research_backflow_request_id),
        "request_fingerprint": request_fingerprint,
        "strategy_name": strategy_name,
        "strategy_version": strategy_version,
        "plan_payload": plan_payload,
    }
    return hashlib.sha256(_canonical_dumps(payload)).hexdigest()


def canonical_payload_equals(left: dict, right: dict) -> bool:
    """JSONB 规范化比较（忽略 key 顺序；None / 空集合等边界差异）。

    与 review service 的 `_same_payload` 同构；request_payload 全部派生字段为
    排序后的小对象，canonical JSON 相等即逐字段相等。
    """
    return _canonical_dumps(left) == _canonical_dumps(right)
