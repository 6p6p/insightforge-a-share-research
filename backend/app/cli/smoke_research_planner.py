"""Real DeepSeek research planner smoke (stage 7A.1 Gate E): 受控一次。

用途：手动验证真实 DeepSeek V4 Flash 对 600519（贵州茅台）研究问题
「分析公司的经营质量、主要风险和估值」能返回符合 `ResearchPlanPayload`（schema
v2）的结构化研究计划，并走**完整生产链路**：`ResearchPlanningService.create_plan`
= 真实 Company + ResearchTask → creation-time PlannerInputSnapshot（spec A）→
input fingerprint → 真实生产适配器 `DeepSeekResearchPlannerModel`（thinking
disabled + `with_structured_output(ResearchPlanPayload)`）→ persist
research_plans 行（schema v2）→ Deterministic Router（route_research_plan，0 LLM）。

校验（全部打印，任何一项不满足 → 退出码 1）：
- model：`deepseek-v4-flash`（settings.llm_model）+ model_id = provider:model；
- thinking disabled：记录子类捕获 ChatDeepSeek 构造参数 →
  `extra_body == {"thinking": {"type": "disabled"}}` 且 `temperature == 0.0`；
- structured output：捕获 `with_structured_output` 的 schema ==
  `ResearchPlanPayload`；
- plan schema v2：`plan_schema_version == RESEARCH_PLAN_SCHEMA_VERSION`；
- bounded needs：document ≤ 8 / financial ≤ 12 / macro ≤ 6 / event ≤ 6 /
  valuation ≤ 3 / focus ≤ 5，scope / modules ≥ 1；
- financial uses calculation_code：每条 financial need 都是受控
  `CalculationCode`（metric policy 由 schema 强制，不输出 observation/metric ID）；
- 无 UUID / 64-hex internal ID：schema 防御性拒绝 + smoke 对完整 payload 再扫描；
- 无买卖建议：完整 payload 文本扫描禁用交易建议词；
- verify_research_plan_integrity / verify_research_plan_route_integrity 通过
  （0 次额外 LLM）。

**不记录** API key / 完整 prompt / reasoning_content / raw provider response。
**不写正式业务数据**：cleanup 删除 smoke 创建的 route / plan / task / company，
cleanup 后实际查询受影响表打印 cleanup_success；cleanup 失败或残留非 0 → 不声称
成功（退出码 1）。source_providers 仅幂等 seed 默认值，不清理。

需要环境变量 `DEEPSEEK_API_KEY`。无凭证 → 打印 pending_credentials，退出码 2，
不重试，不阻塞确定性 Gate。运行（insightforge Conda 环境）：
    python -m app.cli.smoke_research_planner
"""

import asyncio
import re
import sys
import time
import uuid
from datetime import date
from uuid import UUID

from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.research_task import ResearchTaskModel
from app.db.session import DatabaseManager
from app.financial.calculations.contracts import CalculationCode
from app.repositories.company_repository import CompanyRepository
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_planning.contracts import (
    RESEARCH_PLAN_SCHEMA_VERSION,
    ResearchPlanPayload,
)
from app.research_planning.errors import (
    ResearchPlanIntegrityError,
    ResearchPlannerMalformedOutput,
    ResearchPlannerModelUnavailable,
    ResearchPlanRouteIntegrityError,
)
from app.research_planning.planner import (
    PLANNER_VERSION,
    create_research_planner_model,
)
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService

# Windows 上 psycopg async 需要 SelectorEventLoop；必须在 asyncio.run 之前设置。
configure_asyncio_runtime()

_QUESTION = "分析公司的经营质量、主要风险和估值。"
_AS_OF = "2026-08-10"

_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HEX64_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
# 禁止的交易建议词：A 股基本面研究不输出买卖建议 / 短期预测。
_FORBIDDEN_TRADING_TERMS = (
    "买入",
    "卖出",
    "增持",
    "减持",
    "建仓",
    "清仓",
    "加仓",
    "减仓",
    "目标价",
    "止盈",
    "止损",
    "抄底",
    "做多",
    "做空",
    "逢低买入",
    "获利了结",
)


def _iter_strings(node) -> list[str]:
    """递归收集 payload 内全部字符串（key + value），供 internal-ID / 建议扫描。"""
    strings: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            strings.append(key)
            strings.extend(_iter_strings(value))
    elif isinstance(node, list):
        for item in node:
            strings.extend(_iter_strings(item))
    elif isinstance(node, str):
        strings.append(node)
    return strings


def _find_internal_id(payload: dict) -> str | None:
    """返回首个 UUID / 64-hex 形态的内部 ID；无则 None。"""
    for value in _iter_strings(payload):
        if _UUID_PATTERN.search(value):
            return value
        if _HEX64_PATTERN.fullmatch(value):
            return value
    return None


def _find_trading_advice(payload: dict) -> str | None:
    """返回首个命中禁用交易建议词的文本；无则 None。"""
    for value in _iter_strings(payload):
        for term in _FORBIDDEN_TRADING_TERMS:
            if term in value:
                return f"{term} in {value!r}"
    return None


def _check_counts(payload: dict) -> bool:
    """bounded needs：各列表不超过 schema max 数量。"""
    checks = (
        len(payload.get("document_needs", [])) <= 8,
        len(payload.get("financial_needs", [])) <= 12,
        len(payload.get("macro_needs", [])) <= 6,
        len(payload.get("event_needs", [])) <= 6,
        len(payload.get("valuation_needs", [])) <= 3,
        len(payload.get("research_focus", [])) <= 5,
    )
    return all(checks)


def _check_financial(payload: dict) -> bool:
    """financial uses calculation_code：每条 need 都是受控 CalculationCode。"""
    for need in payload.get("financial_needs", []):
        code = need.get("calculation_code")
        if code is None:
            return False
        try:
            CalculationCode(code)
        except ValueError:
            return False
        # 不输出 observation/metric ID：need 内部不允许 UUID-like 形态。
        if _UUID_PATTERN.search(str(need)):
            return False
    return True


async def _cleanup(
    sessionmaker,
    *,
    task_id: UUID,
    company_id: UUID,
    plan_id: UUID | None,
) -> None:
    """删除 smoke 创建的 route / plan / task / company（FK 依赖逆序，只删本 smoke 数据）。"""
    async with sessionmaker() as session:
        if plan_id is not None:
            await session.execute(
                text("DELETE FROM research_plan_routes WHERE research_plan_id = :pid").bindparams(
                    pid=plan_id
                )
            )
            await session.execute(
                text("DELETE FROM research_plans WHERE research_plan_id = :pid").bindparams(
                    pid=plan_id
                )
            )
        await session.execute(
            text("DELETE FROM research_tasks WHERE task_id = :tid").bindparams(tid=task_id)
        )
        await session.execute(
            text("DELETE FROM company_aliases WHERE company_id = :cid").bindparams(cid=company_id)
        )
        await session.execute(
            text("DELETE FROM companies WHERE company_id = :cid").bindparams(cid=company_id)
        )
        await session.commit()


async def _residual_counts(
    sessionmaker,
    *,
    task_id: UUID,
    company_id: UUID,
    plan_id: UUID | None,
) -> dict[str, int]:
    """实际查询受影响表的残留行数（不猜测、不声称 0 残留）。"""
    scoped: list[tuple[str, str, dict]] = []
    if plan_id is not None:
        scoped.append(
            (
                "research_plan_routes",
                "SELECT count(*) FROM research_plan_routes WHERE research_plan_id = :pid",
                {"pid": plan_id},
            )
        )
        scoped.append(
            (
                "research_plans",
                "SELECT count(*) FROM research_plans WHERE research_plan_id = :pid",
                {"pid": plan_id},
            )
        )
    scoped.append(
        (
            "research_tasks",
            "SELECT count(*) FROM research_tasks WHERE task_id = :tid",
            {"tid": task_id},
        )
    )
    scoped.append(
        (
            "company_aliases",
            "SELECT count(*) FROM company_aliases WHERE company_id = :cid",
            {"cid": company_id},
        )
    )
    scoped.append(
        (
            "companies",
            "SELECT count(*) FROM companies WHERE company_id = :cid",
            {"cid": company_id},
        )
    )
    async with sessionmaker() as session:
        counts: dict[str, int] = {}
        for table, sql, params in scoped:
            counts[table] = (await session.execute(text(sql).bindparams(**params))).scalar_one()
        return counts


async def _main() -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    settings = get_settings()
    if settings.deepseek_api_key is None:
        # pending_credentials：不重试，不阻塞确定性 Gate。
        print("DEEPSEEK_API_KEY 未配置：pending_credentials。", file=sys.stderr)
        return 2
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    sessionmaker = manager.session_factory()
    company_id = uuid.uuid4()
    task_id = uuid.uuid4()
    plan_id: UUID | None = None
    smoke_ok = False
    try:
        await SourceRegistryService(sessionmaker).seed_defaults()
        async with sessionmaker() as session:
            await CompanyRepository(session).create(
                CompanyModel(
                    company_id=company_id,
                    exchange="SSE",
                    security_code="600519",
                    identity_key="SSE:600519",
                    board="sse_main",
                    official_name="贵州茅台",
                    short_name="贵州茅台",
                    listing_status="listed",
                    identity_source_provider_key="sse",
                    identity_source_url="https://www.sse.com.cn",
                )
            )
            await ResearchTaskRepository(session).create(
                ResearchTaskModel(
                    task_id=task_id,
                    company_query="600519",
                    research_start_date=date(2023, 1, 1),
                    research_end_date=date.fromisoformat(_AS_OF),
                    modules=["company_profile"],
                    questions=[_QUESTION],
                    require_plan_approval=False,
                )
            )
            await session.commit()

        model = create_research_planner_model(settings)
        print(f"provider = {settings.llm_provider}")
        print(f"model = {model.model_id}")
        plan_service = ResearchPlanningService(
            sessionmaker, model, CompanyIdentityService(sessionmaker)
        )
        router = ResearchSourceRouter(sessionmaker, plan_service)

        # 记录子类捕获 ChatDeepSeek 构造参数 + with_structured_output 链，
        # 但**真实调用**（真实 provider / 真实 key）。
        import langchain_deepseek as _lds

        captured: dict = {}
        original = _lds.ChatDeepSeek

        class _RecordingChatDeepSeek(_lds.ChatDeepSeek):
            def __init__(self, *args, **kwargs):
                captured["constructor_kwargs"] = kwargs
                super().__init__(*args, **kwargs)

            def with_structured_output(self, *args, **kwargs):
                captured["structured_output"] = {"args": args, "kwargs": kwargs}
                return super().with_structured_output(*args, **kwargs)

        _lds.ChatDeepSeek = _RecordingChatDeepSeek
        try:
            start = time.perf_counter()
            result = await plan_service.create_plan(task_id)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
        finally:
            _lds.ChatDeepSeek = original

        plan_id = result.research_plan_id
        print(f"latency_ms = {elapsed_ms}")
        print(f"replayed = {result.replayed}")
        print(f"plan_schema_version = {result.plan_schema_version}")
        print(f"planner_version = {result.planner_version}")

        # ---- adapter 配置：deepseek-v4-flash / thinking disabled / structured output。
        ckw = captured.get("constructor_kwargs", {})
        so = captured.get("structured_output", {})
        model_ok = (
            settings.llm_model == "deepseek-v4-flash" and "deepseek-v4-flash" in result.model_id
        )
        thinking_ok = ckw.get("extra_body") == {"thinking": {"type": "disabled"}} and (
            ckw.get("temperature") == 0.0
        )
        structured_ok = bool(so.get("args")) and so["args"][0] is ResearchPlanPayload
        print(f"adapter_model_deepseek_v4_flash = {model_ok}")
        print(f"adapter_thinking_disabled = {thinking_ok}")
        print(f"adapter_structured_output = {structured_ok}")

        # ---- plan schema v2 + bounded needs + financial calculation_code。
        payload = result.plan_payload
        schema_v2_ok = (
            result.plan_schema_version == RESEARCH_PLAN_SCHEMA_VERSION
            and result.planner_version == PLANNER_VERSION
        )
        counts_ok = _check_counts(payload)
        financial_ok = _check_financial(payload)
        uuid_ok = _find_internal_id(payload) is None
        advice = _find_trading_advice(payload)
        advice_ok = advice is None
        scope_modules_ok = (
            1 <= len(payload.get("research_scope", [])) <= 6
            and 1 <= len(payload.get("analysis_modules", [])) <= 5
        )
        print(
            f"needs_counts = document:{len(payload.get('document_needs', []))} "
            f"financial:{len(payload.get('financial_needs', []))} "
            f"macro:{len(payload.get('macro_needs', []))} "
            f"event:{len(payload.get('event_needs', []))} "
            f"valuation:{len(payload.get('valuation_needs', []))}"
        )
        print(f"research_focus = {payload.get('research_focus', [])}")
        print(f"analysis_modules = {payload.get('analysis_modules', [])}")
        print(f"plan_schema_v2 = {schema_v2_ok}")
        print(f"bounded_needs = {counts_ok}")
        print(f"financial_uses_calculation_code = {financial_ok}")
        print(f"no_internal_uuid = {uuid_ok}")
        print(f"no_trading_advice = {advice_ok}")

        # ---- route（0 LLM，确定性）→ verify（0 次额外 LLM）。
        routed = await router.route_research_plan(plan_id)
        print(f"route_replayed = {routed.replayed}")
        print(f"route_entries = {len(routed.route_payload.get('entries', []))}")
        await plan_service.verify_research_plan_integrity(plan_id)
        await router.verify_research_plan_route_integrity(plan_id)
        print("integrity_verify = True")

        smoke_ok = all(
            (
                model_ok,
                thinking_ok,
                structured_ok,
                schema_v2_ok,
                counts_ok,
                financial_ok,
                uuid_ok,
                advice_ok,
                scope_modules_ok,
            )
        )
        print(f"smoke_ok = {smoke_ok}")
    except ResearchPlannerMalformedOutput:
        print("FAIL: model output could not be parsed into ResearchPlanPayload")
        return 1
    except ResearchPlannerModelUnavailable as exc:
        print(f"FAIL: DeepSeek provider/model unavailable: {exc}")
        return 1
    except (ResearchPlanIntegrityError, ResearchPlanRouteIntegrityError) as exc:
        print(f"FAIL: integrity error: {exc}")
        return 1
    finally:
        cleanup_ok = False
        try:
            await _cleanup(
                sessionmaker,
                task_id=task_id,
                company_id=company_id,
                plan_id=plan_id,
            )
            residual = await _residual_counts(
                sessionmaker,
                task_id=task_id,
                company_id=company_id,
                plan_id=plan_id,
            )
            cleanup_ok = all(count == 0 for count in residual.values())
            print(f"cleanup_success = {cleanup_ok}")
            if not cleanup_ok:
                print(f"residual_rows = {sum(residual.values())}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup_failure = {type(exc).__name__}")
        await manager.dispose()
    if smoke_ok and cleanup_ok:
        print("OK: real DeepSeek research planner v2 smoke passed")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
