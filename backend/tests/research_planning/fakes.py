"""Fake research planner models（0 real DeepSeek，spec T/U）。

确定性 fake：每次 `generate` 返回固定 `ResearchPlanPayload`（frozen，可安全重放），
可注入异常模拟 provider / malformed 失败；记录每次 request 供调用计数断言
（replay 必须 0 次额外 LLM 调用）。
"""

from app.research_planning.contracts import (
    ResearchPlannerRequest,
    ResearchPlanPayload,
)


class FakeResearchPlannerModel:
    """确定性 fake planner：固定 payload / 可注入异常 / 调用计数。"""

    def __init__(
        self,
        payload: ResearchPlanPayload,
        *,
        fail_with: BaseException | None = None,
        model_id: str = "test:fake-research-planner",
    ) -> None:
        self._payload = payload
        self._fail_with = fail_with
        self._model_id = model_id
        self.calls: list[ResearchPlannerRequest] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def generate(self, request: ResearchPlannerRequest) -> ResearchPlanPayload:
        self.calls.append(request)
        if self._fail_with is not None:
            raise self._fail_with
        return self._payload
