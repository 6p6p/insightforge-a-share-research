"""Research planning error tree (stage 7A.1).

Planner / Router / Preparation 是「确定性代码 + LLM 受控生成」的边界。异常分类：

- `ResearchPlanningError`：领域错误基类（继承 `app.core.errors.DomainError`）；
- `ResearchPlanNotFound`：research_plan 不存在（404）；
- `ResearchPlanIntegrityError`：plan 持久化后 tamper / replay 损坏（spec I）——
  planner output 一旦持久化即 immutable，verify 发现不一致 → 明确失败；
- `ResearchPlanRouteNotFound`：route plan 不存在；
- `ResearchPlanRouteIntegrityError`：route plan tamper；
- `ResearchPlannerModelUnavailable`：provider / API / 网络异常（planner LLM 调用）；
- `ResearchPlannerMalformedOutput`：模型输出无法通过 ResearchPlanPayload 校验。

Preparation 校验失败（company/task tamper、payload 无效）同样用 IntegrityError。
"""

from app.core.errors import DomainError


class ResearchPlanningError(DomainError):
    """research planning 领域错误基类。"""

    code = "research_planning_error"
    http_status = 500
    message = "研究规划错误"


class ResearchPlanNotFound(ResearchPlanningError):
    """research_plan 不存在。"""

    code = "research_plan_not_found"
    http_status = 404
    message = "研究计划不存在"


class ResearchPlanIntegrityError(ResearchPlanningError):
    """research_plan 完整性校验失败（immutable plan 被 tamper / 上游 mismatch）。"""

    code = "research_plan_integrity_error"
    http_status = 409
    message = "研究计划完整性校验失败"


class ResearchPlanLegacyExecutionUnsupported(ResearchPlanningError):
    """v1 legacy plan 无 frozen input snapshot，禁止进入自动执行。

    v1 行可 verify 历史完整性（replay stored payload），但没有
    `planner_input_payload` 派生执行语义（research_question / analysis_as_of）——
    **不用当前 Task 字段猜历史 v1 的 question/cutoff**。Preparation / Fulfillment
    应重新创建 v2 Plan。
    """

    code = "research_plan_legacy_execution_unsupported"
    http_status = 409
    message = "旧版研究计划不支持自动执行，请重新创建研究计划"


class ResearchPlanRouteNotFound(ResearchPlanningError):
    """research_plan_routes 不存在。"""

    code = "research_plan_route_not_found"
    http_status = 404
    message = "研究计划路由不存在"


class ResearchPlanRouteIntegrityError(ResearchPlanningError):
    """research_plan_routes 完整性校验失败。"""

    code = "research_plan_route_integrity_error"
    http_status = 409
    message = "研究计划路由完整性校验失败"


class ResearchPlannerModelUnavailable(ResearchPlanningError):
    """planner LLM provider / API / 网络异常。"""

    code = "research_planner_model_unavailable"
    http_status = 502
    message = "研究计划模型不可用"


class ResearchPlannerMalformedOutput(ResearchPlanningError):
    """planner 模型输出无法通过 ResearchPlanPayload 校验。"""

    code = "research_planner_malformed_output"
    http_status = 422
    message = "研究计划模型输出无法解析"
