"""Company query normalization and structured parsing."""

import re
import unicodedata
from dataclasses import dataclass

from app.core.errors import InvalidCompanyQuery
from app.domain.companies import ExchangeCode

_EXCHANGE_BY_SUFFIX = {
    "SH": ExchangeCode.SSE,
    "SZ": ExchangeCode.SZSE,
    "BJ": ExchangeCode.BSE,
}
_IDENTITY_KEY_RE = re.compile(r"^(?P<exchange>SSE|SZSE|BSE):(?P<code>\d{6})$")
_SYMBOL_RE = re.compile(r"^(?P<code>\d{6})\.(?P<suffix>SH|SZ|BJ)$")
_SECURITY_CODE_RE = re.compile(r"^\d{6}$")
_BAD_PREFIX_RE = re.compile(r"^[A-Za-z]+:\d+$")
_BAD_SUFFIX_RE = re.compile(r"^\d+\.[A-Za-z]{2}$")
_MAX_LENGTH = 200


@dataclass(frozen=True)
class ParsedCompanyQuery:
    original: str
    normalized: str
    explicit_exchange: ExchangeCode | None
    security_code: str | None
    identity_key: str | None
    explicit_symbol: bool


def normalize_company_text(value: str) -> str:
    if not isinstance(value, str):
        raise InvalidCompanyQuery()
    stripped = value.strip()
    if not stripped:
        raise InvalidCompanyQuery()
    if len(stripped) > _MAX_LENGTH:
        raise InvalidCompanyQuery()
    folded = unicodedata.normalize("NFKC", stripped)
    folded = re.sub(r"\s+", "", folded)
    return folded.casefold()


def _identity_key(exchange: ExchangeCode, code: str) -> str:
    return f"{exchange.value}:{code}"


def parse_company_query(value: str) -> ParsedCompanyQuery:
    if not isinstance(value, str):
        raise InvalidCompanyQuery()
    raw = value.strip()
    if not raw or len(raw) > _MAX_LENGTH:
        raise InvalidCompanyQuery()
    normalized = unicodedata.normalize("NFKC", raw).strip()

    key_match = _IDENTITY_KEY_RE.match(normalized)
    if key_match:
        exchange = ExchangeCode(key_match.group("exchange"))
        code = key_match.group("code")
        return ParsedCompanyQuery(
            original=value,
            normalized=normalized.casefold(),
            explicit_exchange=exchange,
            security_code=code,
            identity_key=_identity_key(exchange, code),
            explicit_symbol=False,
        )

    symbol_match = _SYMBOL_RE.match(normalized)
    if symbol_match:
        code = symbol_match.group("code")
        exchange = _EXCHANGE_BY_SUFFIX[symbol_match.group("suffix")]
        return ParsedCompanyQuery(
            original=value,
            normalized=normalized.casefold(),
            explicit_exchange=exchange,
            security_code=code,
            identity_key=_identity_key(exchange, code),
            explicit_symbol=True,
        )

    if _BAD_PREFIX_RE.match(normalized) or _BAD_SUFFIX_RE.match(normalized):
        raise InvalidCompanyQuery()

    if normalized.isdigit() and not _SECURITY_CODE_RE.match(normalized):
        raise InvalidCompanyQuery()

    if _SECURITY_CODE_RE.match(normalized):
        return ParsedCompanyQuery(
            original=value,
            normalized=normalized,
            explicit_exchange=None,
            security_code=normalized,
            identity_key=None,
            explicit_symbol=False,
        )

    return ParsedCompanyQuery(
        original=value,
        normalized=normalize_company_text(normalized),
        explicit_exchange=None,
        security_code=None,
        identity_key=None,
        explicit_symbol=False,
    )
