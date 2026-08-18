"""Company identity resolution service."""

from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.companies.normalization import parse_company_query
from app.core.errors import (
    CompanyIdentityAmbiguous,
    CompanyIdentityNotFound,
)
from app.db.models.company import CompanyModel
from app.domain.companies import CompanyAliasType, CompanyMatchType
from app.repositories.company_repository import CompanyRepository
from app.schemas.company import CompanyIdentityResponse, CompanyResolutionResponse

_ALIAS_MATCH_TYPE = {
    CompanyAliasType.OFFICIAL_NAME.value: CompanyMatchType.OFFICIAL_NAME,
    CompanyAliasType.SHORT_NAME.value: CompanyMatchType.SHORT_NAME,
    CompanyAliasType.FORMER_NAME.value: CompanyMatchType.FORMER_NAME,
    CompanyAliasType.ENGLISH_NAME.value: CompanyMatchType.ENGLISH_NAME,
}

_MATCH_PRIORITY = (
    CompanyMatchType.OFFICIAL_NAME,
    CompanyMatchType.SHORT_NAME,
    CompanyMatchType.FORMER_NAME,
    CompanyMatchType.ENGLISH_NAME,
)


class CompanyIdentityService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def resolve(self, query: str) -> CompanyResolutionResponse:
        parsed = parse_company_query(query)
        async with self._sessionmaker() as session:
            repo = CompanyRepository(session)
            if parsed.identity_key is not None:
                company = await repo.get_by_identity_key(parsed.identity_key)
                if company is None:
                    raise CompanyIdentityNotFound()
                match_type = (
                    CompanyMatchType.EXPLICIT_SYMBOL
                    if parsed.explicit_symbol
                    else CompanyMatchType.IDENTITY_KEY
                )
                matched_value = parsed.normalized if parsed.explicit_symbol else parsed.identity_key
                return self._response(company, match_type, matched_value)
            if parsed.security_code is not None:
                companies = await repo.find_by_security_code(parsed.security_code)
                return self._resolve_many(
                    companies,
                    CompanyMatchType.SECURITY_CODE,
                    parsed.security_code,
                )
            rows = await repo.find_by_normalized_alias(parsed.normalized)
            if not rows:
                # P1 generalization: fallback — direct match on short_name /
                # official_name when alias table has no normalized match.
                # Catches cases like whitespace artifacts in source data.
                rows = await repo.find_by_direct_name(parsed.normalized)
            return self._resolve_by_alias(rows, parsed.normalized)

    async def get_company(self, company_id: UUID) -> CompanyIdentityResponse:
        async with self._sessionmaker() as session:
            company = await CompanyRepository(session).get_by_id(company_id)
        if company is None:
            raise CompanyIdentityNotFound()
        return CompanyIdentityResponse.model_validate(company)

    @staticmethod
    def _resolve_many(
        companies: list[CompanyModel],
        match_type: CompanyMatchType,
        matched_value: str,
    ) -> CompanyResolutionResponse:
        if not companies:
            raise CompanyIdentityNotFound()
        if len(companies) > 1:
            raise CompanyIdentityAmbiguous()
        return CompanyIdentityService._response(companies[0], match_type, matched_value)

    @staticmethod
    def _resolve_by_alias(
        rows: list[tuple[CompanyModel, str]],
        matched_value: str,
    ) -> CompanyResolutionResponse:
        if not rows:
            raise CompanyIdentityNotFound()
        unique: list[CompanyModel] = []
        for company, _alias_type in rows:
            if not any(c.company_id == company.company_id for c in unique):
                unique.append(company)
        if len(unique) > 1:
            raise CompanyIdentityAmbiguous()
        match_type = CompanyIdentityService._pick_alias_match(rows)
        return CompanyIdentityService._response(unique[0], match_type, matched_value)

    @staticmethod
    def _pick_alias_match(rows: list[tuple[CompanyModel, str]]) -> CompanyMatchType:
        for match in _MATCH_PRIORITY:
            for _company, alias_type in rows:
                if _ALIAS_MATCH_TYPE.get(alias_type) == match:
                    return match
        return CompanyMatchType.OFFICIAL_NAME

    @staticmethod
    def _response(
        company: CompanyModel,
        match_type: CompanyMatchType,
        matched_value: str,
    ) -> CompanyResolutionResponse:
        return CompanyResolutionResponse(
            company=CompanyIdentityResponse.model_validate(company),
            match_type=match_type,
            matched_value=matched_value,
        )
