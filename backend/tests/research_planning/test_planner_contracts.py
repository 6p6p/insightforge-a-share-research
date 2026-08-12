"""ResearchPlan schema v1 contracts tests (stage 7A.1 spec C/D/E, T).

纯 Pydantic 单测（无 DB / 无 LLM）：验证 bounded vocabulary 的硬约束。

- module / metric / valuation metric 只接受冻结集合；
- 各 need 列表有明确 max 数量；
- need_code 全局唯一；
- 自由文本（purpose / topic / focus）拒绝 UUID / 64-hex 等 internal ID 形态；
- `build_planner_messages` 不向模型泄漏 task_id / 任何内部 UUID（no tools /
  no internal IDs，spec F）。
"""

import json
import re
from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.research_planning.contracts import (
    CompanyIdentitySnapshot,
    ResearchPlannerRequest,
    ResearchPlanPayload,
)
from app.research_planning.planner import build_planner_messages

_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _payload(**overrides) -> dict:
    """合法的 ResearchPlanPayload JSON 形态（tests override 具体字段）。"""
    base = {
        "research_scope": ["business", "financial", "valuation"],
        "document_needs": [
            {
                "need_code": "annual_report_2024",
                "purpose": "需要 2024 年报",
                "source_type": "annual_report",
                "period": "2024",
            }
        ],
        "financial_needs": [
            {
                "need_code": "revenue_2024",
                "purpose": "需要营收数据",
                "metric_code": "revenue",
                "period": "2024",
            }
        ],
        "macro_needs": [],
        "event_needs": [],
        "valuation_needs": [{"need_code": "pe_ttm_valuation", "metric_code": "pe_ttm"}],
        "analysis_modules": ["business_event", "financial", "valuation"],
        "research_focus": ["经营质量", "估值水平"],
    }
    base.update(overrides)
    return base


def _request() -> ResearchPlannerRequest:
    return ResearchPlannerRequest(
        task_id=uuid4(),
        company=CompanyIdentitySnapshot(
            security_code="600519",
            official_name="贵州茅台",
            exchange="SSE",
            board="sse_main",
            aliases=["茅台"],
        ),
        research_question="分析贵州茅台的经营质量与估值水平。",
        analysis_as_of=date(2026, 8, 10),
    )


# ---------------------------------------------------------------- valid


def test_valid_payload_round_trip() -> None:
    payload = ResearchPlanPayload.model_validate(_payload())
    normalized = payload.normalized_payload()
    assert normalized["research_scope"] == ["business", "financial", "valuation"]
    assert normalized["valuation_needs"][0]["metric_code"] == "pe_ttm"
    # normalized 是可 JSON 序列化的纯 dict（plan fingerprint 输入）。
    json.dumps(normalized, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------- bounded vocabulary


def test_invalid_analysis_module_rejected() -> None:
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(_payload(analysis_modules=["trading"]))


def test_invalid_financial_metric_rejected() -> None:
    """financial_needs.metric_code 只接受 MetricCode 冻结集合。"""
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                financial_needs=[{"need_code": "pe", "purpose": "x", "metric_code": "pe_ratio"}]
            )
        )


def test_financial_metric_accepts_real_metric_code() -> None:
    payload = ResearchPlanPayload.model_validate(
        _payload(financial_needs=[{"need_code": "np", "purpose": "x", "metric_code": "net_profit"}])
    )
    assert payload.financial_needs[0].metric_code.value == "net_profit"


def test_valuation_metric_constraint() -> None:
    """valuation_needs.metric_code 只允许 pe_ttm / pb_mrq / ps_ttm。"""
    for valid in ("pe_ttm", "pb_mrq", "ps_ttm"):
        payload = ResearchPlanPayload.model_validate(
            _payload(valuation_needs=[{"need_code": f"v_{valid}", "metric_code": valid}])
        )
        assert payload.valuation_needs[0].metric_code.value == valid
    for invalid in ("peg", "ev_ebitda", "revenue", "net_profit"):
        with pytest.raises(ValidationError):
            ResearchPlanPayload.model_validate(
                _payload(valuation_needs=[{"need_code": "v_x", "metric_code": invalid}])
            )


def test_invalid_period_rejected() -> None:
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                document_needs=[
                    {
                        "need_code": "d1",
                        "purpose": "x",
                        "source_type": "annual_report",
                        "period": "24",
                    }
                ]
            )
        )


def test_invalid_source_type_rejected() -> None:
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                document_needs=[{"need_code": "d1", "purpose": "x", "source_type": "chat_log"}]
            )
        )


# ---------------------------------------------------------------- max counts


def test_max_need_counts() -> None:
    # document_needs ≤ 8。
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                document_needs=[
                    {"need_code": f"d{i}", "purpose": "x", "source_type": "news_article"}
                    for i in range(9)
                ]
            )
        )
    # financial_needs ≤ 12。
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                financial_needs=[
                    {"need_code": f"f{i}", "purpose": "x", "metric_code": "revenue"}
                    for i in range(13)
                ]
            )
        )
    # macro_needs ≤ 6。
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                macro_needs=[
                    {"need_code": f"m{i}", "purpose": "x", "topic_or_indicator": "GDP"}
                    for i in range(7)
                ]
            )
        )
    # event_needs ≤ 6。
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                event_needs=[
                    {"need_code": f"e{i}", "purpose": "x", "topic": "管理层变动"} for i in range(7)
                ]
            )
        )
    # valuation_needs ≤ 3。
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                valuation_needs=[{"need_code": f"v{i}", "metric_code": "pe_ttm"} for i in range(4)]
            )
        )
    # research_focus ≤ 5。
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(_payload(research_focus=[f"f{i}" for i in range(6)]))


def test_scope_and_modules_bounds() -> None:
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(_payload(research_scope=[]))
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(_payload(analysis_modules=[]))
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(_payload(research_scope=["macro"] * 7))


# ---------------------------------------------------------------- internal ID 拒绝


def test_forbidden_uuid_in_free_text() -> None:
    """自由文本（purpose/topic/focus）拒绝 UUID-like 内部 ID。"""
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                document_needs=[
                    {
                        "need_code": "d1",
                        "purpose": f"需要证据 {uuid4()}",
                        "source_type": "annual_report",
                    }
                ]
            )
        )
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(event_needs=[{"need_code": "e1", "purpose": "x", "topic": f"事件 {uuid4()}"}])
        )
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(_payload(research_focus=[str(uuid4())]))


def test_forbidden_hex64_in_free_text() -> None:
    hex64 = "a" * 64
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(_payload(research_focus=[hex64]))
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(macro_needs=[{"need_code": "m1", "purpose": "x", "topic_or_indicator": hex64}])
        )


def test_need_code_global_unique() -> None:
    """need_code 跨所有 need 列表必须全局唯一。"""
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                document_needs=[
                    {"need_code": "shared", "purpose": "x", "source_type": "annual_report"}
                ],
                financial_needs=[{"need_code": "shared", "purpose": "x", "metric_code": "revenue"}],
            )
        )


def test_need_code_pattern() -> None:
    with pytest.raises(ValidationError):
        ResearchPlanPayload.model_validate(
            _payload(
                document_needs=[
                    {"need_code": "9bad", "purpose": "x", "source_type": "annual_report"}
                ]
            )
        )


# ---------------------------------------------------------------- planner messages（no tools）


def test_planner_messages_no_internal_ids() -> None:
    """build_planner_messages 只发送语义输入：不含 task_id / 任何 UUID / tool 结构。"""
    request = _request()
    messages = build_planner_messages(request)
    assert [m["role"] for m in messages] == ["system", "user"]
    blob = json.dumps(messages, ensure_ascii=False)
    assert str(request.task_id) not in blob
    assert _UUID_PATTERN.search(blob) is None
    # 无 fingerprint 形态。
    assert re.search(r"\b[0-9a-f]{64}\b", blob) is None
    # 语义身份在场。
    assert "600519" in blob
    assert "贵州茅台" in blob
    assert "analysis_as_of" not in blob  # as-of 以人类可读 iso 进入 prompt，无内部字段名
