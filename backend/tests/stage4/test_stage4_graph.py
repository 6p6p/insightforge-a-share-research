"""Stage 4 graph topology tests (spec J-K + spec R: graph).

无 DB / 无真实 LLM：用 stub Application Services + MemorySaver 编译真实
LangGraph graph。覆盖：
- Send fan-out：N 个 work item → N 次 worker 调用，全部到达对应 Service；
- worker dispatch：business/event/risk → claim stub；financial / macro /
  valuation → 各自 stub（验证 request 字段）；
- claim canonicalization：最终 claim_ids 去重 + sorted；
- reducer order independence（spec Q）：item 提交顺序 A..C vs C..A → 相同
  claim_ids；worker 完成顺序（慢/快交换）→ 相同 claim_ids；
- synthesis 恰好一次：collect → synthesize → END，create_or_get_synthesis 只
  调一次，返回 synthesis_id + synthesis_result_id；
- errors：worker 失败传播；<2 final claims → Stage4InsufficientClaims；
  invalid plan → Stage4InvalidPlan。
"""

import asyncio
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.analysis.claims.contracts import ClaimAnalysisDomain
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.dependencies import Stage4AnalysisDependencies
from app.stage4.errors import Stage4InsufficientClaims, Stage4InvalidPlan
from app.stage4.graph import build_stage4_analysis_graph
from app.stage4.runner import Stage4WorkflowRunner

pytestmark = pytest.mark.asyncio

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_CUTOFF = "2026-08-10"


def _uid() -> str:
    return str(uuid4())


class StubClaimService:
    """按 evidence_card_id（worker 标识）返回 claim ids。"""

    def __init__(self, ids_by_card: dict[str, list[str]]):
        self.calls: list = []
        self._ids = ids_by_card

    async def analyze(self, request):
        self.calls.append(request)
        key = str(request.evidence_card_ids[0])
        ids = self._ids.get(key, [])
        return SimpleNamespace(claim_ids=[UUID(c) for c in ids], relevant=bool(ids))


class StubFinancialService:
    def __init__(self, ids_by_calc: dict[str, list[str]]):
        self.calls: list = []
        self._ids = ids_by_calc

    async def analyze(self, request):
        self.calls.append(request)
        key = str(request.calculation_ids[0])
        ids = self._ids.get(key, [])
        return SimpleNamespace(claim_ids=[UUID(c) for c in ids], relevant=True)


class StubMacroService:
    def __init__(self, ids_by_driver: dict[str, list[str]]):
        self.calls: list = []
        self._ids = ids_by_driver

    async def analyze(self, request):
        self.calls.append(request)
        key = str(request.macro_driver_evidence_ids[0])
        ids = self._ids.get(key, [])
        return SimpleNamespace(claim_ids=[UUID(c) for c in ids], relevant=True)


class StubValuationService:
    def __init__(self, claim_id: str | None):
        self.calls: list = []
        self._claim_id = claim_id

    async def analyze(self, request):
        self.calls.append(request)
        claim_id = UUID(self._claim_id) if self._claim_id else None
        return SimpleNamespace(claim_id=claim_id, relevant=True)


class StubSynthesisService:
    def __init__(self):
        self.calls: list = []
        self._synthesis_id = uuid4()

    async def create_or_get_synthesis(self, draft):
        self.calls.append(draft)
        return SimpleNamespace(synthesis_id=self._synthesis_id, replayed=False)


class StubSynthesisAnalysisService:
    def __init__(self):
        self.calls: list = []
        self._result_id = uuid4()

    async def analyze(self, request):
        self.calls.append(request)
        return SimpleNamespace(synthesis_result_id=self._result_id, replayed=False)


def build_deps(
    *,
    claim_ids: dict[str, list[str]] | None = None,
    financial_ids: dict[str, list[str]] | None = None,
    macro_ids: dict[str, list[str]] | None = None,
    valuation_claim_id: str | None = None,
    claim_cls=StubClaimService,
) -> tuple[Stage4AnalysisDependencies, dict[str, object]]:
    stubs = {
        "claim": claim_cls(claim_ids or {}),
        "financial": StubFinancialService(financial_ids or {}),
        "macro": StubMacroService(macro_ids or {}),
        "valuation": StubValuationService(valuation_claim_id),
        "synthesis": StubSynthesisService(),
        "synthesis_analysis": StubSynthesisAnalysisService(),
    }
    deps = Stage4AnalysisDependencies(
        sessionmaker=object(),  # type: ignore[arg-type]
        claim_analysis_service=stubs["claim"],
        financial_analysis_service=stubs["financial"],
        macro_analysis_service=stubs["macro"],
        valuation_analysis_service=stubs["valuation"],
        synthesis_service=stubs["synthesis"],
        synthesis_analysis_service=stubs["synthesis_analysis"],
    )
    return deps, stubs


def _request(items: list[dict]) -> Stage4WorkflowRequest:
    return Stage4WorkflowRequest(
        task_id=uuid4(),
        company_id=uuid4(),
        research_question=_QUESTION,
        analysis_as_of=date.fromisoformat(_CUTOFF),
        analysis_work_items=items,
    )


def _state(request: Stage4WorkflowRequest) -> dict:
    return Stage4WorkflowRunner._build_initial_state(request)


async def _invoke(deps: Stage4AnalysisDependencies, state: dict) -> dict:
    graph = build_stage4_analysis_graph(deps, MemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    async for _ in graph.astream(state, config, stream_mode="updates"):
        pass
    final = await graph.aget_state(config)
    return dict(final.values)


def _item(item_id: str, analysis_type: str, **kw) -> dict:
    values = {"item_id": item_id, "analysis_type": analysis_type}
    values.update(kw)
    return values


# ---------------------------------------------------------------- happy path


async def test_send_fanout_dispatch_and_synthesis_once() -> None:
    c_a, c_b, c_c, c_d, c_e = (_uid() for _ in range(5))
    card_a, card_r = uuid4(), uuid4()
    calc = uuid4()
    driver, company = uuid4(), uuid4()
    comparison = uuid4()
    items = [
        _item("biz", "business", evidence_card_ids=[card_a]),
        _item("fin", "financial", calculation_ids=[calc], additional_evidence_ids=[]),
        _item("mac", "macro", macro_driver_evidence_ids=[driver], company_evidence_ids=[company]),
        _item("val", "valuation", comparison_ids=[comparison]),
        _item("rsk", "risk", evidence_card_ids=[card_r]),
    ]
    deps, stubs = build_deps(
        claim_ids={str(card_a): [c_a], str(card_r): [c_e]},
        financial_ids={str(calc): [c_b]},
        macro_ids={str(driver): [c_c]},
        valuation_claim_id=c_d,
    )
    final = await _invoke(deps, _state(_request(items)))

    # 每个 worker 恰好一次，dispatch 到正确 Service。
    assert [call.analysis_domain for call in stubs["claim"].calls] == [
        ClaimAnalysisDomain.BUSINESS,
        ClaimAnalysisDomain.RISK,
    ]
    assert len(stubs["financial"].calls) == 1
    assert stubs["financial"].calls[0].calculation_ids == [calc]
    assert len(stubs["macro"].calls) == 1
    assert stubs["macro"].calls[0].macro_driver_evidence_ids == [driver]
    assert len(stubs["valuation"].calls) == 1
    assert stubs["valuation"].calls[0].comparison_ids == [comparison]

    # claim canonicalization：去重 + sorted。
    assert final["claim_ids"] == sorted([c_a, c_b, c_c, c_d, c_e])

    # synthesis 恰好一次，返回 business IDs。
    assert len(stubs["synthesis"].calls) == 1
    assert len(stubs["synthesis_analysis"].calls) == 1
    assert final["synthesis_id"] == str(stubs["synthesis"]._synthesis_id)
    assert final["synthesis_result_id"] == str(stubs["synthesis_analysis"]._result_id)


async def test_duplicate_claims_canonicalized() -> None:
    # worker A → [c1, c2]，worker B → [c2] → collect 去重后 [c1, c2]。
    c1, c2 = _uid(), _uid()
    card_a, card_b = uuid4(), uuid4()
    items = [
        _item("a", "business", evidence_card_ids=[card_a]),
        _item("b", "business", evidence_card_ids=[card_b]),
    ]
    deps, _ = build_deps(claim_ids={str(card_a): [c1, c2], str(card_b): [c2]})
    final = await _invoke(deps, _state(_request(items)))
    assert final["claim_ids"] == sorted([c1, c2])


async def test_item_order_does_not_affect_final_claims() -> None:
    # spec Q：item 提交顺序 A..C vs C..A → 相同 claim_ids。
    ids = [c_a, c_b, c_c] = [_uid(), _uid(), _uid()]
    card_a, card_b = uuid4(), uuid4()
    calc = uuid4()
    items_abc = [
        _item("a", "business", evidence_card_ids=[card_a]),
        _item("b", "financial", calculation_ids=[calc], additional_evidence_ids=[]),
        _item("c", "business", evidence_card_ids=[card_b]),
    ]
    items_cba = list(reversed(items_abc))
    kwargs = dict(
        claim_ids={str(card_a): [c_a], str(card_b): [c_c]},
        financial_ids={str(calc): [c_b]},
    )
    deps_abc, _ = build_deps(**kwargs)
    deps_cba, _ = build_deps(**kwargs)
    first = await _invoke(deps_abc, _state(_request(items_abc)))
    second = await _invoke(deps_cba, _state(_request(items_cba)))
    assert first["claim_ids"] == second["claim_ids"] == sorted(ids)


# ---------------------------------------------------------------- errors


async def test_worker_failure_propagates() -> None:
    class Boom(Exception):
        pass

    class FailingClaimService(StubClaimService):
        async def analyze(self, request):
            raise Boom("worker boom")

    deps, _ = build_deps(claim_cls=FailingClaimService)
    items = [_item("a", "business", evidence_card_ids=[uuid4()])]
    with pytest.raises(Boom):
        await _invoke(deps, _state(_request(items)))


async def test_insufficient_claims_rejected() -> None:
    # 只有一个 worker 且产出 1 条 Claim → 无法综合。
    card = uuid4()
    items = [_item("a", "business", evidence_card_ids=[card])]
    deps, _ = build_deps(claim_ids={str(card): [_uid()]})
    with pytest.raises(Stage4InsufficientClaims):
        await _invoke(deps, _state(_request(items)))


async def test_invalid_plan_rejected() -> None:
    request = _request([_item("a", "business", evidence_card_ids=[uuid4()])])
    state = _state(request)
    state["analysis_work_items"] = []  # 绕过 request 校验，模拟损坏 state
    deps, _ = build_deps()
    with pytest.raises(Stage4InvalidPlan):
        await _invoke(deps, state)


async def test_synthesis_failure_propagates() -> None:
    class Boom(Exception):
        pass

    class FailingSynthesis(StubSynthesisService):
        async def create_or_get_synthesis(self, draft):
            raise Boom("synthesis boom")

    card_a, card_b = uuid4(), uuid4()
    deps = Stage4AnalysisDependencies(
        sessionmaker=object(),  # type: ignore[arg-type]
        claim_analysis_service=StubClaimService({str(card_a): [_uid()], str(card_b): [_uid()]}),
        financial_analysis_service=StubFinancialService({}),
        macro_analysis_service=StubMacroService({}),
        valuation_analysis_service=StubValuationService(None),
        synthesis_service=FailingSynthesis(),
        synthesis_analysis_service=StubSynthesisAnalysisService(),
    )
    items = [
        _item("a", "business", evidence_card_ids=[card_a]),
        _item("b", "business", evidence_card_ids=[card_b]),
    ]
    with pytest.raises(Boom):
        await _invoke(deps, _state(_request(items)))


# ---------------------------------------------------------------- worker completion order


async def test_concurrent_worker_completion_order_does_not_matter() -> None:
    """两个 worker 并发，慢 worker 分别压在 A / B 上 → 最终 claim_ids 相同。"""

    class SlowClaimService(StubClaimService):
        def __init__(self, ids_by_card: dict[str, list[str]], slow_card: str):
            super().__init__(ids_by_card)
            self._slow_card = slow_card

        async def analyze(self, request):
            if str(request.evidence_card_ids[0]) == self._slow_card:
                await asyncio.sleep(0.05)
            return await super().analyze(request)

    c_x, c_y = _uid(), _uid()
    card_x, card_y = uuid4(), uuid4()
    kwargs = dict(claim_ids={str(card_x): [c_x], str(card_y): [c_y]})
    items = [
        _item("x", "business", evidence_card_ids=[card_x]),
        _item("y", "business", evidence_card_ids=[card_y]),
    ]
    deps_slow_x, _ = build_deps(claim_cls=lambda m: SlowClaimService(m, str(card_x)), **kwargs)
    deps_slow_y, _ = build_deps(claim_cls=lambda m: SlowClaimService(m, str(card_y)), **kwargs)

    first = await _invoke(deps_slow_x, _state(_request(items)))
    second = await _invoke(deps_slow_y, _state(_request(items)))
    assert first["claim_ids"] == second["claim_ids"] == sorted([c_x, c_y])
