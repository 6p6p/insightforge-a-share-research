"""Official disclosure discovery contracts.

Discovery 契约只描述"发现候选"，不下载、不落库、不校验内容。
Candidate 不是 SourceRecord：它只代表"官方页面上可能存在这份披露"，
是否采纳由调用方决定，采纳后再走 SourceIngestionService.ingest_url。
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.domain.companies import ExchangeCode
from app.domain.source_records import SourceDocumentType
from app.domain.sources import AcquisitionMethod

_MAX_DATE_RANGE_DAYS = 366
_MAX_KEYWORDS = 10
_MAX_KEYWORD_LENGTH = 100
_MIN_LIMIT = 1
_MAX_LIMIT = 100
_SECURITY_CODE_RE = re.compile(r"^\d{6}$")

_ERROR_MSG = {
    "code_pattern": "security_code 必须是六位数字",
    "date_order": "start_date 必须不晚于 end_date",
    "date_range": "日期范围最多 366 天",
    "doc_types": "document_types 不能为空",
    "limit_range": "limit 必须在 1—100 之间",
    "keyword_length": "keyword 单项最长 100 字符",
    "keyword_count": "keyword 最多 10 个",
    "title_blank": "title 不能为空",
    "url_scheme": "source_url 与 discovery_url 必须使用 https",
}


def _validate_security_code(security_code: str) -> None:
    if not isinstance(security_code, str) or not _SECURITY_CODE_RE.match(security_code):
        raise ValueError(_ERROR_MSG["code_pattern"])


def _validate_date_range(start_date: date, end_date: date) -> None:
    if start_date > end_date:
        raise ValueError(_ERROR_MSG["date_order"])
    if (end_date - start_date).days > _MAX_DATE_RANGE_DAYS:
        raise ValueError(_ERROR_MSG["date_range"])


@dataclass(frozen=True)
class DisclosureSearchRequest:
    """一次官方披露发现查询。

    - company_id / exchange / security_code 锁定目标公司；
    - start_date / end_date 限定披露窗口（最多 366 天）；
    - document_types 必填；keywords 与 limit 为可选过滤。
    """

    company_id: UUID
    exchange: ExchangeCode
    security_code: str
    start_date: date
    end_date: date
    document_types: tuple[SourceDocumentType, ...]
    keywords: tuple[str, ...] = ()
    limit: int = 50

    def __post_init__(self) -> None:
        _validate_security_code(self.security_code)
        _validate_date_range(self.start_date, self.end_date)
        if not isinstance(self.document_types, tuple) or not self.document_types:
            raise ValueError(_ERROR_MSG["doc_types"])
        if any(not isinstance(dt, SourceDocumentType) for dt in self.document_types):
            raise ValueError(_ERROR_MSG["doc_types"])
        if not isinstance(self.limit, int) or not (_MIN_LIMIT <= self.limit <= _MAX_LIMIT):
            raise ValueError(_ERROR_MSG["limit_range"])
        if len(self.keywords) > _MAX_KEYWORDS:
            raise ValueError(_ERROR_MSG["keyword_count"])
        for keyword in self.keywords:
            if not isinstance(keyword, str) or len(keyword) > _MAX_KEYWORD_LENGTH:
                raise ValueError(_ERROR_MSG["keyword_length"])
        if not isinstance(self.exchange, ExchangeCode):
            raise ValueError("exchange 必须是 ExchangeCode")
        if not isinstance(self.company_id, UUID):
            raise ValueError("company_id 必须是 UUID")
        if not isinstance(self.start_date, date) or not isinstance(self.end_date, date):
            raise ValueError("start_date/end_date 必须是 date")


def _validate_candidate_url(url: str) -> None:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(_ERROR_MSG["url_scheme"])


@dataclass(frozen=True)
class DisclosureCandidate:
    """官方页面上发现的一条披露候选。

    不是 SourceRecord；不代表文件已下载或已验证；不保存 HTML 正文；
    provider_metadata 只保存少量非敏感标识（不含 Cookie/Header/selector/内部参数）。
    """

    provider_key: str
    title: str
    source_url: str
    discovery_url: str
    published_at: datetime
    document_type: SourceDocumentType
    company_security_code: str
    acquisition_method: AcquisitionMethod
    external_document_id: str | None = None
    reporting_period_end: date | None = None
    provider_metadata: dict[str, str | int | bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError(_ERROR_MSG["title_blank"])
        _validate_candidate_url(self.source_url)
        _validate_candidate_url(self.discovery_url)
        if not isinstance(self.provider_key, str) or not self.provider_key:
            raise ValueError("provider_key 不能为空")
        if not isinstance(self.company_security_code, str):
            raise ValueError("company_security_code 必须是字符串")
        if not isinstance(self.document_type, SourceDocumentType):
            raise ValueError("document_type 必须是 SourceDocumentType")
        if not isinstance(self.acquisition_method, AcquisitionMethod):
            raise ValueError("acquisition_method 必须是 AcquisitionMethod")


class DisclosureDiscoveryProvider(Protocol):
    """官方披露发现 Provider 契约。

    - 只负责发现候选；
    - 不写数据库；
    - 不下载完整 PDF；
    - 不调用 SourceIngestionService；
    - 调用方确认 Candidate 后再执行 ingest_url。
    """

    provider_key: str

    async def search(
        self,
        request: DisclosureSearchRequest,
    ) -> list[DisclosureCandidate]: ...
