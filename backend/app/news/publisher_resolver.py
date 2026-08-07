"""Original publisher resolution (stage 2D.2A).

把 Discovery Candidate 的 normalized URL 解析为 Source Registry 中登记的
Original Publisher（Invariant B：Candidate URL 不直接请求，必须经过
Resolver → Registry → SafeHtmlFetcher）。纯函数、零网络请求。

资格（Invariant C）：Provider 必须 enabled + 含 news_article capability +
    含 public_html acquisition_method。
URL 规则：可解析、仅 https、无 userinfo、无非默认端口（显式 :443 视为
    等价无端口）、hostname 非 IP 字面量。
hostname 归属：复用 Source Registry URL policy 的"等于 allowed domain 或
    真实子域"语义（非 substring），防止 evilcnstock.com 命中 cnstock.com。
"""

import ipaddress
from urllib.parse import urlsplit

from app.db.models.source_provider import SourceProviderModel
from app.domain.sources import AcquisitionMethod, SourceCapability
from app.news.errors import NewsPublisherAmbiguous, NewsPublisherUnsupported
from app.source_registry.url_policy import is_url_allowed


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _capability_values(provider: SourceProviderModel) -> set[str]:
    return {_value_of(c) for c in provider.capabilities}


def _method_values(provider: SourceProviderModel) -> set[str]:
    return {_value_of(m) for m in provider.acquisition_methods}


def _value_of(item: object) -> str:
    # JSONB 列从 DB 加载出来是裸字符串；测试里可能直接给枚举值。
    return item.value if hasattr(item, "value") else str(item)


def _is_eligible_publisher(provider: SourceProviderModel) -> bool:
    if not provider.enabled:
        return False
    if SourceCapability.NEWS_ARTICLE.value not in _capability_values(provider):
        return False
    if AcquisitionMethod.PUBLIC_HTML.value not in _method_values(provider):
        return False
    return True


class OriginalPublisherResolver:
    """从 eligible Original Publishers 中把 normalized URL 解析为单个 Publisher。"""

    @staticmethod
    def resolve(
        normalized_url: str,
        providers: list[SourceProviderModel],
    ) -> SourceProviderModel:
        try:
            parts = urlsplit(normalized_url)
        except ValueError:
            raise NewsPublisherUnsupported() from None
        if parts.scheme != "https":
            raise NewsPublisherUnsupported()
        if parts.username is not None or parts.password is not None:
            raise NewsPublisherUnsupported()
        try:
            port = parts.port
        except ValueError:
            # urlparse 对畸形 netloc（如无括号 IPv6 拼 host:port）抛异常
            raise NewsPublisherUnsupported() from None
        if port is not None:
            if port != 443:
                # 显式非默认端口拒绝
                raise NewsPublisherUnsupported()
            # 显式 :443 视为等价无端口，去掉后交给 is_url_allowed（其要求无 port）
            parts = parts._replace(netloc=parts.hostname or "")
        host = parts.hostname
        if not host or _is_ip_address(host):
            raise NewsPublisherUnsupported()
        match_url = parts.geturl()

        eligible = [p for p in providers if _is_eligible_publisher(p)]
        matches = [p for p in eligible if is_url_allowed(match_url, list(p.allowed_domains))]
        if not matches:
            raise NewsPublisherUnsupported()
        if len(matches) > 1:
            raise NewsPublisherAmbiguous()
        return matches[0]
