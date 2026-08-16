"""Deterministic source router (stage 7A.1 spec J/K): 0 LLM.

把 ResearchPlan 的每个 need **确定性**映射为 route decision：
- `route_type`（SourceCapability 现有能力值）+ `expected_document_type`；
- `provider_keys`：**路由当时的** registry 快照（enabled + 该 capability 的
  provider）——保证 registry 后续变化（provider 被禁用 / 移除 / 新增）后仍可
  审计当时的 route decision（spec J）。

设计：
- **0 LLM / 0 web / 0 retrieval**：输入只有 plan payload + 现有
  `source_providers` 表；
- 同 (plan, router_version) → replay 同一行（`UNIQUE(research_plan_id,
  router_version)`）；bump router_version → 新 fingerprint → 新行，旧行保留；
- `route_fingerprint` = plan fingerprint + router 身份 + normalized route
  payload（spec K）；`verify_research_plan_route_integrity` 只重放 stored
  payload（不重新 route / 不查 registry）。
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.research_plan import ResearchPlanRouteModel
from app.domain.sources import SourceCapability
from app.repositories.source_provider_repository import SourceProviderRepository
from app.research_planning.contracts import (
    ResearchDocumentNeedType,
    ResearchPlanPayload,
)
from app.research_planning.errors import (
    ResearchPlanRouteIntegrityError,
    ResearchPlanRouteNotFound,
)
from app.research_planning.repository import (
    ResearchPlanRouteRepository,
)
from app.research_planning.service import ResearchPlanningService

# router 身份（persisted router_name / router_version / schema_version）。
SOURCE_ROUTE_SCHEMA_VERSION = 1
ROUTER_NAME = "source_router"
ROUTER_VERSION = 1


class SourceRouteType(StrEnum):
    """route_type：复用 SourceCapability 现有能力值（spec J：route_type 仅限现有能力）。

    只保留本阶段路由会用到的能力；ISSUER_IR 当前 registry 无 provider →
    provider_unavailable（不伪造可用性）。
    """

    COMPANY_ANNOUNCEMENT = SourceCapability.COMPANY_ANNOUNCEMENT.value
    DOCUMENT_DOWNLOAD = SourceCapability.DOCUMENT_DOWNLOAD.value
    COMPANY_SEARCH = SourceCapability.COMPANY_SEARCH.value
    REGULATION = SourceCapability.REGULATION.value
    ISSUER_IR = SourceCapability.ISSUER_IR.value
    MACRO_DATA = SourceCapability.MACRO_DATA.value
    NEWS_ARTICLE = SourceCapability.NEWS_ARTICLE.value


# ------------------------------------------------------------------ 映射（纯函数）

# document source_type → (route_type, expected_document_type)。
_DOCUMENT_ROUTE: dict[str, tuple[SourceRouteType, str | None]] = {
    ResearchDocumentNeedType.ANNUAL_REPORT.value: (
        SourceRouteType.COMPANY_ANNOUNCEMENT,
        "annual_report",
    ),
    ResearchDocumentNeedType.SEMIANNUAL_REPORT.value: (
        SourceRouteType.COMPANY_ANNOUNCEMENT,
        "semiannual_report",
    ),
    ResearchDocumentNeedType.QUARTERLY_REPORT.value: (
        SourceRouteType.COMPANY_ANNOUNCEMENT,
        "quarterly_report",
    ),
    ResearchDocumentNeedType.COMPANY_ANNOUNCEMENT.value: (
        SourceRouteType.COMPANY_ANNOUNCEMENT,
        "company_announcement",
    ),
    ResearchDocumentNeedType.PROSPECTUS.value: (
        SourceRouteType.COMPANY_ANNOUNCEMENT,
        "prospectus",
    ),
    ResearchDocumentNeedType.ISSUER_IR_MATERIAL.value: (
        SourceRouteType.ISSUER_IR,
        "issuer_ir_material",
    ),
    ResearchDocumentNeedType.NEWS_ARTICLE.value: (
        SourceRouteType.NEWS_ARTICLE,
        "news_article",
    ),
    ResearchDocumentNeedType.MACRO_DATASET.value: (
        SourceRouteType.MACRO_DATA,
        None,
    ),
    ResearchDocumentNeedType.OTHER.value: (SourceRouteType.DOCUMENT_DOWNLOAD, None),
}

# 非 document 的 need 类别 → route_type。
_NON_DOCUMENT_ROUTE: dict[str, SourceRouteType] = {
    "financial": SourceRouteType.COMPANY_ANNOUNCEMENT,  # 财务科目来自披露报表
    "macro": SourceRouteType.MACRO_DATA,
    "event": SourceRouteType.NEWS_ARTICLE,  # 公司事件通过新闻/公告
    "valuation": SourceRouteType.COMPANY_ANNOUNCEMENT,  # pe/pb/ps 来自财务披露
}

# context need（Final: Research Context Intelligence）→ route_type 映射。
# regulatory_policy 优先监管能力（csrc 等官方来源）；行业/商品/地缘走
# 新闻与搜索发现；公司 IR/ESG/投资者交流走 issuer IR 能力（官网 + eastmoney）。
_CONTEXT_ROUTE: dict[str, tuple[SourceRouteType, str | None]] = {
    "regulatory_policy": (SourceRouteType.REGULATION, None),
    "geopolitical_trade": (SourceRouteType.NEWS_ARTICLE, "news_article"),
    "industry_metric": (SourceRouteType.NEWS_ARTICLE, "news_article"),
    "commodity_market": (SourceRouteType.NEWS_ARTICLE, "news_article"),
    "macro_timeseries": (SourceRouteType.MACRO_DATA, None),
    "company_ir": (SourceRouteType.ISSUER_IR, "issuer_ir_material"),
    "esg": (SourceRouteType.ISSUER_IR, "issuer_ir_material"),
    "investor_presentation": (SourceRouteType.ISSUER_IR, "issuer_ir_material"),
}


def route_context_need(context_type: str) -> tuple[SourceRouteType, str | None]:
    """context need → (route_type, expected_document_type)（确定性）。"""
    return _CONTEXT_ROUTE[context_type]


def route_document_need(source_type: str) -> tuple[SourceRouteType, str | None]:
    """document need → (route_type, expected_document_type)（确定性）。"""
    return _DOCUMENT_ROUTE[source_type]


def route_need(need_kind: str, source_type: str | None) -> tuple[SourceRouteType, str | None]:
    """任意 need → (route_type, expected_document_type)。

    - document：按 source_type 查 _DOCUMENT_ROUTE；
    - financial / macro / event / valuation：按类别查 _NON_DOCUMENT_ROUTE。
    """
    if need_kind == "document":
        return route_document_need(source_type)  # type: ignore[arg-type]
    if need_kind == "context":
        return route_context_need(source_type)  # type: ignore[arg-type]
    return _NON_DOCUMENT_ROUTE[need_kind], None


def route_type_has_provider(route_type: SourceRouteType, provider_keys: list[str]) -> bool:
    """provider_keys 非空 ⇔ 路由当时有 enabled provider 能服务该 route_type。"""
    return bool(provider_keys)


# ------------------------------------------------------------------ payload


class SourceRouteEntry(BaseModel):
    """单条 route decision：need → route_type + expected_document_type + provider 快照。"""

    model_config = ConfigDict(frozen=True)

    need_code: str
    need_kind: str  # document / financial / macro / event / valuation
    route_type: SourceRouteType
    expected_document_type: str | None = None
    provider_keys: list[str] = Field(default_factory=list)

    @field_validator("need_code")
    @classmethod
    def _valid_need_code(cls, value: str) -> str:
        code = value.strip()
        if not code:
            raise ValueError("need_code 不能为空（trim 后）")
        return code

    @field_validator("provider_keys")
    @classmethod
    def _sorted_unique(cls, value: list[str]) -> list[str]:
        return sorted({key for key in value if key})


class SourceRoutePlan(BaseModel):
    """SourceRoutePlan schema v1（route_payload JSONB 内容）。

    entries 顺序 = plan 各 need 列表的确定性顺序（document → financial →
    macro → event → valuation），need_code 全局唯一（plan 已保证）。
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = SOURCE_ROUTE_SCHEMA_VERSION
    router_name: str = ROUTER_NAME
    router_version: int = ROUTER_VERSION
    entries: list[SourceRouteEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_entries(self) -> "SourceRoutePlan":
        seen: set[str] = set()
        for entry in self.entries:
            if entry.need_code in seen:
                raise ValueError(f"route entries need_code 必须唯一: {entry.need_code!r}")
            seen.add(entry.need_code)
        return self

    def normalized_payload(self) -> dict:
        """canonical JSON payload（route fingerprint 用）。"""
        return self.model_dump(mode="json")


# ------------------------------------------------------------------ fingerprint


def compute_route_fingerprint(
    *,
    plan_fingerprint: str,
    router_name: str,
    router_version: int,
    payload: dict,
) -> str:
    """route fingerprint（spec K）：plan fingerprint + router 身份 + normalized payload。"""
    canonical = json.dumps(
        {
            "plan_fingerprint": plan_fingerprint,
            "router": {"name": router_name, "version": router_version},
            "route": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_route_payload(raw: dict) -> SourceRoutePlan:
    """把 stored route_payload 还原为 validated SourceRoutePlan（失败 → IntegrityError）。"""
    from pydantic import ValidationError

    try:
        return SourceRoutePlan.model_validate(raw)
    except ValidationError as exc:
        raise ResearchPlanRouteIntegrityError("stored route payload invalid") from exc


# ------------------------------------------------------------------ service


@dataclass(frozen=True)
class ResearchRouteResult:
    """route_research_plan 的结果摘要。"""

    route_plan_id: UUID
    research_plan_id: UUID
    replayed: bool
    route_schema_version: int
    router_name: str
    router_version: int
    route_fingerprint: str
    route_payload: dict


class ResearchSourceRouter:
    """Deterministic SourceRouter（0 LLM；route 决策 + replay + integrity）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        plan_service: ResearchPlanningService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._plan_service = plan_service

    # ------------------------------------------------------------------ route

    async def route_research_plan(self, research_plan_id: UUID) -> ResearchRouteResult:
        """加载并 verify plan → 确定性映射每个 need → registry 快照 provider →
        SourceRoutePlan → fingerprint → create_or_get（同 plan 同 version → replay）。"""
        plan = await self._plan_service.verify_research_plan_integrity(research_plan_id)
        payload = ResearchPlanPayload.model_validate(plan.plan_payload)
        entries = await self._build_entries(payload)
        route_plan = SourceRoutePlan(entries=entries)
        route_fingerprint = compute_route_fingerprint(
            plan_fingerprint=plan.plan_fingerprint,
            router_name=ROUTER_NAME,
            router_version=ROUTER_VERSION,
            payload=route_plan.normalized_payload(),
        )
        route = ResearchPlanRouteModel(
            research_plan_id=plan.research_plan_id,
            route_schema_version=SOURCE_ROUTE_SCHEMA_VERSION,
            router_name=ROUTER_NAME,
            router_version=ROUTER_VERSION,
            route_payload=route_plan.normalized_payload(),
            route_fingerprint=route_fingerprint,
        )
        async with self._sessionmaker() as session:
            row, created = await ResearchPlanRouteRepository(session).create_or_get(route)
            await session.commit()
        return ResearchRouteResult(
            route_plan_id=row.route_plan_id,
            research_plan_id=row.research_plan_id,
            replayed=not created,
            route_schema_version=row.route_schema_version,
            router_name=row.router_name,
            router_version=row.router_version,
            route_fingerprint=row.route_fingerprint,
            route_payload=dict(row.route_payload),
        )

    async def _build_entries(self, payload: ResearchPlanPayload) -> list[SourceRouteEntry]:
        """对 plan 的每个 need 做确定性 route 决策 + registry provider 快照。

        entries 顺序 = document → financial → macro → event → valuation。
        """
        # 一次性把需要的 route_type 的 provider 快照读出来（同 route_type 合并）。
        provider_keys_by_type: dict[SourceRouteType, list[str]] = {}
        async with self._sessionmaker() as session:
            repo = SourceProviderRepository(session)
            for route_type in self._needed_route_types(payload):
                if route_type in provider_keys_by_type:
                    continue
                rows = await repo.list_providers(
                    authority_tier=None,
                    capability=SourceCapability(route_type.value),
                    acquisition_method=None,
                    exchange=None,
                    enabled_only=True,
                )
                provider_keys_by_type[route_type] = sorted({row.provider_key for row in rows})

        entries: list[SourceRouteEntry] = []
        for need in payload.document_needs:
            route_type, doc_type = route_document_need(need.source_type.value)
            entries.append(
                SourceRouteEntry(
                    need_code=need.need_code,
                    need_kind="document",
                    route_type=route_type,
                    expected_document_type=doc_type,
                    provider_keys=provider_keys_by_type.get(route_type, []),
                )
            )
        for need in payload.financial_needs:
            route_type, _ = route_need("financial", None)
            entries.append(
                SourceRouteEntry(
                    need_code=need.need_code,
                    need_kind="financial",
                    route_type=route_type,
                    provider_keys=provider_keys_by_type.get(route_type, []),
                )
            )
        for need in payload.macro_needs:
            route_type, _ = route_need("macro", None)
            entries.append(
                SourceRouteEntry(
                    need_code=need.need_code,
                    need_kind="macro",
                    route_type=route_type,
                    provider_keys=provider_keys_by_type.get(route_type, []),
                )
            )
        for need in payload.event_needs:
            route_type, _ = route_need("event", None)
            entries.append(
                SourceRouteEntry(
                    need_code=need.need_code,
                    need_kind="event",
                    route_type=route_type,
                    provider_keys=provider_keys_by_type.get(route_type, []),
                )
            )
        for need in payload.valuation_needs:
            route_type, _ = route_need("valuation", None)
            entries.append(
                SourceRouteEntry(
                    need_code=need.need_code,
                    need_kind="valuation",
                    route_type=route_type,
                    provider_keys=provider_keys_by_type.get(route_type, []),
                )
            )
        for need in payload.context_needs:
            route_type, doc_type = route_context_need(need.context_type.value)
            entries.append(
                SourceRouteEntry(
                    need_code=need.need_code,
                    need_kind="context",
                    route_type=route_type,
                    expected_document_type=doc_type,
                    provider_keys=provider_keys_by_type.get(route_type, []),
                )
            )
        return entries

    @staticmethod
    def _needed_route_types(payload: ResearchPlanPayload) -> set[SourceRouteType]:
        types: set[SourceRouteType] = set()
        for need in payload.document_needs:
            route_type, _ = route_document_need(need.source_type.value)
            types.add(route_type)
        for kind in ("financial", "macro", "event", "valuation"):
            route_type, _ = route_need(kind, None)
            types.add(route_type)
        for need in payload.context_needs:
            route_type, _ = route_context_need(need.context_type.value)
            types.add(route_type)
        return types

    # ------------------------------------------------------------------ verify

    async def verify_research_plan_route_integrity(self, research_plan_id: UUID):
        """spec K：重放 stored route payload + recompute fingerprint 比对。

        **不重新 route / 不查 registry**（provider 快照是路由当时的真实决策，
        不因 registry 变化而失效）。
        """
        plan = await self._plan_service.verify_research_plan_integrity(research_plan_id)
        async with self._sessionmaker() as session:
            route = await ResearchPlanRouteRepository(session).get_by_plan_and_router_version(
                research_plan_id, ROUTER_VERSION
            )
        if route is None:
            raise ResearchPlanRouteNotFound()

        payload = validate_route_payload(route.route_payload)
        recomputed = compute_route_fingerprint(
            plan_fingerprint=plan.plan_fingerprint,
            router_name=route.router_name,
            router_version=route.router_version,
            payload=payload.normalized_payload(),
        )
        if recomputed != route.route_fingerprint:
            raise ResearchPlanRouteIntegrityError(
                "research plan route fingerprint mismatch (payload/plan tampered)"
            )
        return route

    # ------------------------------------------------------------------ read

    async def get_route(self, research_plan_id: UUID) -> ResearchRouteResult:
        async with self._sessionmaker() as session:
            route = await ResearchPlanRouteRepository(session).get_by_plan_and_router_version(
                research_plan_id, ROUTER_VERSION
            )
        if route is None:
            raise ResearchPlanRouteNotFound()
        return ResearchRouteResult(
            route_plan_id=route.route_plan_id,
            research_plan_id=route.research_plan_id,
            replayed=False,
            route_schema_version=route.route_schema_version,
            router_name=route.router_name,
            router_version=route.router_version,
            route_fingerprint=route.route_fingerprint,
            route_payload=dict(route.route_payload),
        )
