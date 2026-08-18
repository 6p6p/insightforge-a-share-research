"""Stage 4 workflow nodes (thin orchestration only; business logic lives in services).

Role boundaries (spec J-K):
- nodes 只协调 Application Services，不重写业务逻辑；
- worker dispatch：business/event/risk → ClaimAnalysisService；
  financial → FinancialAnalysisService；macro → MacroAnalysisService；
  valuation → ValuationAnalysisService；
- `fan_out_workers` 是 conditional-edge 函数（返回 Send list），dispatch node
  返回 `{}`——node 不能返回 `[Send(...)]`（LangGraph 1.x InvalidUpdateError）；
- `synthesize_claims` 只调用 SynthesisService + SynthesisAnalysisService，
  不让 graph 自行重做 synthesis validation。
"""

from datetime import date
from uuid import UUID

from langgraph.types import Send

from app.analysis.claims.contracts import ClaimAnalysisRequest
from app.analysis.financial.contracts import FinancialAnalysisRequest
from app.analysis.macro.contracts import MacroAnalysisRequest
from app.analysis.synthesis.contracts import SynthesisAnalysisRequest
from app.analysis.valuation.contracts import ValuationAnalysisRequest
from app.claims.contracts import ClaimAnalysisDomain
from app.stage4.analyst_error_policy import (
    MAX_ANALYST_RETRIES,
    classify_analyst_error,
)
from app.stage4.dependencies import Stage4AnalysisDependencies
from app.stage4.errors import (
    Stage4InsufficientClaims,
    Stage4InvalidPlan,
    Stage4UnknownWorkItemType,
)
from app.synthesis.contracts import MIN_SYNTHESIS_CLAIMS, SynthesisInputDraft

_MAX_ANALYSIS_WORK_ITEMS = 12
_VALID_ANALYSIS_TYPES = frozenset(
    {
        ClaimAnalysisDomain.BUSINESS.value,
        ClaimAnalysisDomain.EVENT.value,
        ClaimAnalysisDomain.RISK.value,
        ClaimAnalysisDomain.FINANCIAL.value,
        ClaimAnalysisDomain.MACRO.value,
        ClaimAnalysisDomain.VALUATION.value,
    }
)


def make_validate_analysis_plan_node():
    """validate_analysis_plan：结构性校验 state 中的计划（不重做业务规则）。

    请求构造（Stage4WorkflowRequest）已做完整校验；此处只防御性确认 graph
    state 形状：work items 1..12、item_id 唯一、类型合法、公司 / question /
    cutoff 齐备。任一失败 → Stage4InvalidPlan（稳定错误码）。
    """

    async def validate_analysis_plan(state) -> dict:
        plan = state.get("analysis_work_items")
        if not isinstance(plan, list) or not plan:
            raise Stage4InvalidPlan("analysis_work_items 必须 1..12")
        if len(plan) > _MAX_ANALYSIS_WORK_ITEMS:
            raise Stage4InvalidPlan(f"analysis_work_items 最多 {_MAX_ANALYSIS_WORK_ITEMS} 条")
        ids = [item.get("item_id") for item in plan]
        if len(ids) != len(set(ids)):
            raise Stage4InvalidPlan("analysis_work_items 的 item_id 必须唯一")
        for item in plan:
            if not isinstance(item.get("item_id"), str) or not item["item_id"]:
                raise Stage4InvalidPlan("item_id 不能为空")
            if item.get("analysis_type") not in _VALID_ANALYSIS_TYPES:
                raise Stage4InvalidPlan(f"analysis_type 必须是 {sorted(_VALID_ANALYSIS_TYPES)}")
        if not isinstance(state.get("company_id"), str) or not state["company_id"]:
            raise Stage4InvalidPlan("company_id 不能为空")
        if (
            not isinstance(state.get("research_question"), str)
            or not state["research_question"].strip()
        ):
            raise Stage4InvalidPlan("research_question 不能为空（trim 后）")
        try:
            date.fromisoformat(state["analysis_as_of"])
        except (KeyError, TypeError, ValueError):
            raise Stage4InvalidPlan("analysis_as_of 必须是 ISO date") from None
        return {}

    return validate_analysis_plan


def make_dispatch_parallel_analysis_node():
    """dispatch_parallel_analysis：路由节点。

    返回 `{}`——实际 fan-out 由 conditional-edge 函数 `fan_out_workers` 返回
    Send list 完成（node 不能返回 `[Send(...)]`）。
    """

    async def dispatch_parallel_analysis(state) -> dict:
        return {}

    return dispatch_parallel_analysis


def fan_out_workers(state):
    """conditional-edge 函数：每个 work item 发一个 Send 到 run_analysis_item。

    Send payload = 共享上下文（company_id / research_question / analysis_as_of
    = 都是 str）+ item 自身字段（全部 checkpoint-safe 的 str/list[str]）。
    未声明的 payload key（item_id / analysis_type / *_ids）会原样到达 worker
    node state。
    """
    common = {
        "company_id": state["company_id"],
        "research_question": state["research_question"],
        "analysis_as_of": state["analysis_as_of"],
    }
    return [
        Send("run_analysis_item", {**common, **dict(item)}) for item in state["analysis_work_items"]
    ]


def make_run_analysis_item_node(deps: Stage4AnalysisDependencies):
    """run_analysis_item：按 analysis_type dispatch 到对应 Analysis Service。

    只做 request 构造 + 调用；不写 prompt / 不计算 / 不写 SQL（服务层负责）。
    返回 `{"analysis_results": [{item_id, analysis_type, claim_ids}]}`——
    并发 worker 写同一 channel，由 reducer 去重合并。
    """

    async def run_analysis_item(state) -> dict:
        """Run one analysis item with bounded retry then graceful degradation.

        P3: Single analyst malformed/model errors are retried up to
        MAX_ANALYST_RETRIES; after exhaustion the item is marked degraded
        (empty claims + degraded record). Hard failures (integrity/data
        corruption) still propagate.
        """
        item_id = state["item_id"]
        analysis_type = state["analysis_type"]
        question = state["research_question"]
        company_id = UUID(state["company_id"])

        async def _run_once():
            if analysis_type in {
                ClaimAnalysisDomain.BUSINESS.value,
                ClaimAnalysisDomain.EVENT.value,
                ClaimAnalysisDomain.RISK.value,
            }:
                result = await deps.claim_analysis_service.analyze(
                    ClaimAnalysisRequest(
                        company_id=company_id,
                        research_question=question,
                        analysis_domain=ClaimAnalysisDomain(analysis_type),
                        evidence_card_ids=[UUID(c) for c in state["evidence_card_ids"]],
                    )
                )
                return [str(cid) for cid in result.claim_ids]
            elif analysis_type == ClaimAnalysisDomain.FINANCIAL.value:
                result = await deps.financial_analysis_service.analyze(
                    FinancialAnalysisRequest(
                        company_id=company_id,
                        research_question=question,
                        calculation_ids=[UUID(c) for c in state["calculation_ids"]],
                        additional_evidence_ids=[UUID(c) for c in state["additional_evidence_ids"]],
                    )
                )
                return [str(cid) for cid in result.claim_ids]
            elif analysis_type == ClaimAnalysisDomain.MACRO.value:
                result = await deps.macro_analysis_service.analyze(
                    MacroAnalysisRequest(
                        company_id=company_id,
                        research_question=question,
                        analysis_as_of=date.fromisoformat(state["analysis_as_of"]),
                        macro_driver_evidence_ids=[
                            UUID(c) for c in state["macro_driver_evidence_ids"]
                        ],
                        company_evidence_ids=[UUID(c) for c in state["company_evidence_ids"]],
                    )
                )
                return [str(cid) for cid in result.claim_ids]
            elif analysis_type == ClaimAnalysisDomain.VALUATION.value:
                result = await deps.valuation_analysis_service.analyze(
                    ValuationAnalysisRequest(
                        company_id=company_id,
                        research_question=question,
                        analysis_as_of=date.fromisoformat(state["analysis_as_of"]),
                        comparison_ids=[UUID(c) for c in state["comparison_ids"]],
                    )
                )
                return [str(result.claim_id)] if result.claim_id else []
            else:
                raise Stage4UnknownWorkItemType(f"unknown analysis_type: {analysis_type!r}")

        last_exc = None
        for _attempt in range(MAX_ANALYST_RETRIES):
            try:
                claim_ids = await _run_once()
                return {
                    "analysis_results": [
                        {
                            "item_id": item_id,
                            "analysis_type": analysis_type,
                            "claim_ids": claim_ids,
                        }
                    ]
                }
            except Exception as exc:
                last_exc = exc
                classification = classify_analyst_error(exc)
                if classification != "retryable":
                    # Hard failure -- propagate immediately (no retry)
                    raise
                # Retryable -- continue loop

        # All retries exhausted -> degrade this item
        return {
            "analysis_results": [
                {
                    "item_id": item_id,
                    "analysis_type": analysis_type,
                    "claim_ids": [],
                }
            ],
            "degraded_items": [
                {
                    "item_id": item_id,
                    "analysis_type": analysis_type,
                    "error_code": type(last_exc).__name__ if last_exc else "unknown",
                    "attempts": MAX_ANALYST_RETRIES,
                }
            ],
        }

    return run_analysis_item


def make_collect_claim_ids_node():
    """collect_claim_ids：canonical sort + dedupe 全部 worker Claim。

    - 与 worker 完成顺序无关（去重后按 str 排序）；
    - < 2 条去重 Claim → Stage4InsufficientClaims（spec R：<2 final claims
      稳定失败，不进入综合）。
    """

    async def collect_claim_ids(state) -> dict:
        """Collect and dedupe claims; tolerate partial degradation.

        If every analyst degraded (0 claims) -> Stage4InsufficientClaims
        (irrecoverable). If some analysts succeeded, proceed with available
        claims -- missing modules are honest gaps (P3 principle).
        """
        unique = {
            cid
            for result in state.get("analysis_results", [])
            for cid in result.get("claim_ids", [])
        }
        degraded = state.get("degraded_items", [])
        if len(unique) < MIN_SYNTHESIS_CLAIMS:
            if not degraded:
                # No degradation: genuinely insufficient data, must fail
                raise Stage4InsufficientClaims(
                    f"synthesis requires at least {MIN_SYNTHESIS_CLAIMS} claims, got {len(unique)}"
                )
            # Degraded modules present: honest gap, proceed if we have some claims
            if len(unique) == 0:
                raise Stage4InsufficientClaims(
                    f"synthesis requires at least {MIN_SYNTHESIS_CLAIMS} claims, "
                    f"got {len(unique)} ({len(degraded)} module(s) degraded)"
                )
        return {"claim_ids": sorted(unique)}

    return collect_claim_ids


def make_synthesize_claims_node(deps: Stage4AnalysisDependencies):
    """synthesize_claims：SynthesisService + SynthesisAnalysisService。

    只消费 collect 产出的 canonical claim_ids；幂等（fingerprint / replay），
    retry / resume 不产生重复 SynthesisRun / SynthesisResult。
    """

    async def synthesize_claims(state) -> dict:
        draft = SynthesisInputDraft(
            company_id=UUID(state["company_id"]),
            research_question=state["research_question"],
            analysis_as_of=date.fromisoformat(state["analysis_as_of"]),
            claim_ids=[UUID(c) for c in state["claim_ids"]],
        )
        run = await deps.synthesis_service.create_or_get_synthesis(draft)
        result = await deps.synthesis_analysis_service.analyze(
            SynthesisAnalysisRequest(synthesis_id=run.synthesis_id)
        )
        return {
            "synthesis_id": str(run.synthesis_id),
            "synthesis_result_id": str(result.synthesis_result_id),
            "claim_count": len(state["claim_ids"]),
        }

    return synthesize_claims
