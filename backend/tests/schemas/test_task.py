"""Tests for the research task request/response schemas."""

import pytest
from pydantic import ValidationError

from app.domain.tasks import ResearchModule
from app.schemas.task import TaskCreateRequest


def _valid_payload() -> dict:
    return {
        "company_query": "600519",
        "research_start_date": "2023-01-01",
        "research_end_date": "2025-12-31",
        "modules": ["company_profile", "financial"],
        "questions": ["公司收入增长主要由哪些因素驱动？"],
    }


def test_valid_request() -> None:
    payload = TaskCreateRequest.model_validate(_valid_payload())
    assert payload.company_query == "600519"
    assert payload.include_relative_valuation is False
    assert payload.require_plan_approval is True
    assert payload.questions == ["公司收入增长主要由哪些因素驱动？"]


def test_company_query_is_stripped() -> None:
    payload = TaskCreateRequest.model_validate({**_valid_payload(), "company_query": "  600519  "})
    assert payload.company_query == "600519"


def test_company_query_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate({**_valid_payload(), "company_query": "   "})


def test_company_query_too_long_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate({**_valid_payload(), "company_query": "x" * 101})


def test_date_order_invalid_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate(
            {
                **_valid_payload(),
                "research_start_date": "2025-01-01",
                "research_end_date": "2023-01-01",
            }
        )


def test_modules_empty_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate({**_valid_payload(), "modules": []})


def test_modules_unknown_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate({**_valid_payload(), "modules": ["valuation"]})


def test_modules_deduplicated_preserving_order() -> None:
    payload = TaskCreateRequest.model_validate(
        {**_valid_payload(), "modules": ["financial", "company_profile", "financial"]}
    )
    assert payload.modules == [ResearchModule.FINANCIAL, ResearchModule.COMPANY_PROFILE]


def test_questions_deduplicated_and_stripped() -> None:
    payload = TaskCreateRequest.model_validate(
        {**_valid_payload(), "questions": ["  Q1  ", "Q1", "Q2"]}
    )
    assert payload.questions == ["Q1", "Q2"]


def test_questions_blank_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate({**_valid_payload(), "questions": ["   "]})
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate({**_valid_payload(), "questions": ["Q1", ""]})


def test_questions_count_limit_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate(
            {**_valid_payload(), "questions": [f"q{i}" for i in range(21)]}
        )


def test_question_length_limit_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate({**_valid_payload(), "questions": ["x" * 501]})


def test_default_flags() -> None:
    payload = TaskCreateRequest.model_validate(
        {k: v for k, v in _valid_payload().items() if k != "questions"}
    )
    assert payload.questions == []
    assert payload.include_relative_valuation is False
    assert payload.require_plan_approval is True
