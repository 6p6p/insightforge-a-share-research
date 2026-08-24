"""Search discovery model unit tests (P2 Model Assisted Discovery).

- SearchCandidate / SearchDiscoveryOutput 校验（https / userinfo / 有界）；
- build_search_messages 语义（只含语义输入，无内部 ID）；
- create_search_query_model settings 开关；
- match_provider_domain 域名 allowlist 纯函数（issuer / registry / 拒绝）。
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services.source_discovery.contracts import SourceDiscoveryRequest
from app.services.source_discovery.providers.search import match_provider_domain
from app.services.source_discovery.search_model import (
    MAX_SEARCH_CANDIDATES,
    SearchCandidate,
    SearchDiscoveryOutput,
    build_search_messages,
    create_search_query_model,
)


def _request(**overrides) -> SourceDiscoveryRequest:
    base = dict(
        company_id="00000000-0000-0000-0000-000000000001",
        security_code="600519",
        need_kind="document",
        source_type="other",
    )
    base.update(overrides)
    return SourceDiscoveryRequest(**base)


# ---------------------------------------------------------------- candidate 校验


def test_candidate_accepts_https_url() -> None:
    c = SearchCandidate(url="https://www.sse.com.cn/a.pdf", title="年报")
    assert c.url == "https://www.sse.com.cn/a.pdf"


def test_candidate_rejects_http() -> None:
    with pytest.raises(ValidationError):
        SearchCandidate(url="http://www.sse.com.cn/a.pdf", title="t")


def test_candidate_rejects_userinfo() -> None:
    with pytest.raises(ValidationError):
        SearchCandidate(url="https://user:pass@www.sse.com.cn/a.pdf", title="t")


def test_candidate_rejects_blank_title() -> None:
    with pytest.raises(ValidationError):
        SearchCandidate(url="https://www.sse.com.cn/a.pdf", title="  ")


def test_candidate_rejects_oversized_title() -> None:
    with pytest.raises(ValidationError):
        SearchCandidate(url="https://www.sse.com.cn/a.pdf", title="长" * 301)


def test_output_bounded_candidates() -> None:
    with pytest.raises(ValidationError):
        SearchDiscoveryOutput(
            candidates=[
                SearchCandidate(url=f"https://x.example.com/{i}.pdf", title=f"t{i}")
                for i in range(MAX_SEARCH_CANDIDATES + 1)
            ]
        )


def test_output_allows_empty() -> None:
    assert SearchDiscoveryOutput(candidates=[]).candidates == []


# ---------------------------------------------------------------- messages


def test_build_search_messages_semantic_only() -> None:
    messages = build_search_messages(_request(research_question="分析经营质量", topic="行业格局"))
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "600519" in messages[1]["content"]
    assert "分析经营质量" in messages[1]["content"]
    assert "行业格局" in messages[1]["content"]
    # 禁止生成 evidence / 财务数字的规则在 system prompt。
    assert "evidence" in messages[0]["content"]
    assert "financial numbers" in messages[0]["content"]


# ---------------------------------------------------------------- factory


def test_factory_disabled_by_default() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5433/db",
        search_discovery_llm_enabled=False,
    )
    assert create_search_query_model(settings) is None


def test_factory_enabled_for_deepseek() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5433/db",
        search_discovery_llm_enabled=True,
        llm_provider="deepseek",
        llm_model="deepseek-v4-flash",
    )
    model = create_search_query_model(settings)
    assert model is not None
    assert model.model_id == "deepseek:deepseek-v4-flash"


def test_factory_accepts_any_nonempty_provider() -> None:
    # v1.2.8：非空 provider 直接视为 wrapper（openai-compatible 语义）。
    settings = Settings(
        database_url="postgresql+psycopg://u:p@127.0.0.1:5433/db",
        search_discovery_llm_enabled=True,
        llm_provider="unknown",
    )
    model = create_search_query_model(settings)
    assert model is not None
    assert model.model_id == "unknown:deepseek-v4-flash"


# ---------------------------------------------------------------- 域名 allowlist 纯函数


def test_match_provider_domain_issuer_exact() -> None:
    assert (
        match_provider_domain(
            "www.catl.com",
            registry_domains={"sse.com.cn": "sse"},
            issuer_domains={"www.catl.com"},
        )
        == "issuer_official"
    )


def test_match_provider_domain_registry_exact_and_subdomain() -> None:
    registry = {"sse.com.cn": "sse", "eastmoney.com": "eastmoney"}
    assert (
        match_provider_domain("sse.com.cn", registry_domains=registry, issuer_domains=set())
        == "sse"
    )
    assert (
        match_provider_domain(
            "np-anotice-stock.eastmoney.com", registry_domains=registry, issuer_domains=set()
        )
        == "eastmoney"
    )


def test_match_provider_domain_rejects_unknown() -> None:
    assert (
        match_provider_domain(
            "evil.example.com",
            registry_domains={"sse.com.cn": "sse"},
            issuer_domains=set(),
        )
        is None
    )
    # 子域模拟（evil-sse.com.cn 不是 sse.com.cn 的子域）。
    assert (
        match_provider_domain(
            "evil-sse.com.cn",
            registry_domains={"sse.com.cn": "sse"},
            issuer_domains=set(),
        )
        is None
    )
