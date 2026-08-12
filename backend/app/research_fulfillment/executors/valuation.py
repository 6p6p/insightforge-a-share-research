"""Valuation need executor (stage 7A.2A spec O): manual peer set required。

valuation need 缺失 comparison 时**不自动选择 peer universe**（spec O 硬边界）：
估值相对比较依赖人工指定的可比公司集，本阶段不做自动 peer 选择 / 自动
comparison 创建。executor 确定性返回 `manual_required` +
`EXPLICIT_PEER_SET_REQUIRED`，由工作流转人工处理。

**0 LLM / 0 Retrieval / 0 Chroma / 0 Web**；无任何写操作。
"""

from app.research_fulfillment.contracts import (
    FulfillmentAttempt,
    FulfillmentErrorCode,
    FulfillmentStatus,
)
from app.research_fulfillment.service import FulfillmentContext
from app.research_planning.preparation import MissingResearchNeed
from app.research_planning.router import SourceRouteEntry


class ValuationNeedExecutor:
    """valuation need：manual_required + explicit_peer_set_required（确定性）。"""

    async def fulfill(
        self,
        *,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
    ) -> FulfillmentAttempt:
        del context
        return FulfillmentAttempt(
            need_code=need.need_code,
            need_type=need.need_kind,
            route_type=entry.route_type.value if entry is not None else "",
            status=FulfillmentStatus.MANUAL_REQUIRED,
            error_code=FulfillmentErrorCode.EXPLICIT_PEER_SET_REQUIRED,
        )
