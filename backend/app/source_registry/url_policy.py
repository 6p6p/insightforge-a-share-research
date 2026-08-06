"""URL safety policy for source providers."""

import re
from urllib.parse import urlparse

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not domain:
        raise ValueError("empty domain")
    return domain


def _idna_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return host.lower()


def is_url_allowed(url: str, allowed_domains: list[str]) -> bool:
    """Allow only https URLs whose host equals an allowed domain or a real subdomain."""
    if not allowed_domains:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = parsed.hostname
    if not host:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.port is not None:
        return False
    host = _idna_host(host)
    for allowed in allowed_domains:
        normalized = _idna_host(normalize_domain(allowed))
        if host == normalized or host.endswith("." + normalized):
            return True
    return False


def validate_provider_definition(definition) -> None:
    homepage = urlparse(definition.homepage_url)
    if homepage.scheme != "https" or not homepage.hostname:
        raise ValueError("homepage_url must be https with a hostname")
    if homepage.username is not None or homepage.password is not None:
        raise ValueError("homepage_url must not contain userinfo")
    for domain in definition.allowed_domains:
        normalized = normalize_domain(domain)
        if not _HOST_RE.match(normalized):
            raise ValueError(f"invalid allowed domain: {domain}")
    if not is_url_allowed(definition.homepage_url, list(definition.allowed_domains)):
        raise ValueError("homepage_url is not within allowed_domains")
