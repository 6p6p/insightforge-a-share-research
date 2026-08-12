"""Research fulfillment error tree (stage 7A.2A spec G/H)。

`ResearchFulfillmentService.fulfill_research_needs` 的领域错误基类。executor 的
确定性路径**不抛异常**：补证据失败 → attempt.status=unresolved / error_code
（见 contracts.FulfillmentErrorCode），异常不进入 ResearchFulfillmentResult。

以下情况抛 `ResearchFulfillmentError`：
- plan / route verify 失败（ResearchPlanNotFound / IntegrityError 等上游
  research_planning 错误原样透出，不在此重定义）；
- 装配错误（executor 依赖缺失，如无 document extractor model 但尝试真实
  抽取）→ `ResearchFulfillmentUnavailable`。
"""

from app.core.errors import DomainError


class ResearchFulfillmentError(DomainError):
    """research fulfillment 领域错误基类。"""

    code = "research_fulfillment_error"
    http_status = 500
    message = "研究需求补全错误"


class ResearchFulfillmentUnavailable(ResearchFulfillmentError):
    """fulfillment 依赖装配错误（如 document executor 未提供 extractor model）。"""

    code = "research_fulfillment_unavailable"
    http_status = 502
    message = "研究需求补全不可用"
