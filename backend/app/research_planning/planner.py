"""Research planner model adapter (stage 7A.1 spec F): LLM → ResearchPlanPayload.

生产 adapter：`ChatDeepSeek` + `with_structured_output(ResearchPlanPayload)`。

- **thinking disabled**（`extra_body={"thinking": {"type": "disabled"}}`）：
  DeepSeek V4 Flash 默认 thinking，但 planner 需要稳定、低成本的受约束输出且
  不产生 `reasoning_content`；`temperature=0` 不等于关闭 thinking，必须显式传参；
- **0 tools / 0 web / 0 retrieval**：只启用 structured-output，不绑定任何工具；
- `model_id = {provider}:{model}`（如 `deepseek:deepseek-v4-flash`）；
- 异常映射：provider / API / 网络 → `ResearchPlannerModelUnavailable`；输出
  无法解析为 `ResearchPlanPayload`（含 schema 校验失败）→
  `ResearchPlannerMalformedOutput`；
- **不泄露** raw provider response / key / 完整 prompt。

自动测试一律用 FakeResearchPlannerModel；真实调用只用于受控 smoke。
"""

from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from app.core.config import Settings
from app.llm.components import COMPONENT_RESEARCH_PLANNER
from app.llm.contracts import LLM_PROVIDER_DEEPSEEK
from app.llm.errors import UnsupportedLLMProviderError
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage
from app.research_planning.contracts import (
    ResearchPlannerRequest,
    ResearchPlanPayload,
)
from app.research_planning.errors import (
    ResearchPlannerMalformedOutput,
    ResearchPlannerModelUnavailable,
)

# planner prompt / 策略版本（spec H：进入 input fingerprint）。
PLANNER_STRATEGY_VERSION = 1

# planner 身份（persisted planner_name / planner_version）。
# v2 = 冻结 creation-time PlannerInputSnapshot（spec A）——新 create 只产生 v2；
# v1 行原样保留（legacy verifier 显式处理）。
PLANNER_NAME = "research_planner"
PLANNER_VERSION = 2


@runtime_checkable
class ResearchPlannerModel(Protocol):
    """LLM abstraction：request → 结构化 ResearchPlanPayload。

    - `model_id`：稳定 identifier（provider:model，不伪造 revision）；
    - `generate`：接收 ResearchPlannerRequest，返回 ResearchPlanPayload；
      provider 失败翻译为 ResearchPlannerModelUnavailable；
    - 实现不得启用 tools / web search / retrieval / function side effects。
    """

    @property
    def model_id(self) -> str: ...

    async def generate(self, request: ResearchPlannerRequest) -> ResearchPlanPayload: ...


def create_research_planner_model(
    settings: Settings, usage_observer: LlmUsageObserver | None = None
) -> ResearchPlannerModel:
    """根据 Settings.llm_provider 构造 ResearchPlannerModel（可选注入 usage_observer）。"""
    provider = (settings.llm_provider or "").strip().lower()
    if provider == LLM_PROVIDER_DEEPSEEK:
        return DeepSeekResearchPlannerModel(settings, usage_observer=usage_observer)
    raise UnsupportedLLMProviderError(f"unsupported llm_provider: {provider or '<empty>'}")


_SYSTEM_RULES = """你是 InsightForge 的研究计划制定器。任务：为一个 A 股上市公司研究问题生成
**研究计划**（semantic research needs），而不是做研究本身。

硬性规则：
1. 只做 A 股基本面研究；禁止交易建议、技术分析、短期预测。
2. 不输出事实结论（不写「公司业绩增长」等断言），只输出需要研究什么。
3. 不假设任何数据已经存在——计划只声明需要哪些资料。
4. 不输出任何内部 ID / URL / fingerprint / SQL / filter / Chroma metadata。
   所有 need_code 用语义短标识（小写字母开头，如 annual_report_2023、pe_valuation）。
5. 自由文本（purpose / topic / focus）保持简洁，禁止编造数字与事实。
6. research_scope 只允许：business / event / risk / financial / macro / valuation。
7. analysis_modules 只允许当前已实现的分析模块：business_event / risk /
   financial / macro / valuation（每个模块都需要有对应资料支撑，不强制跑全部）。
8. document_needs.source_type 只允许：annual_report / semiannual_report /
   quarterly_report / company_announcement / issuer_ir_material / prospectus /
   news_article / macro_dataset / other。
9. financial_needs 只声明财务**派生计算**（calculation_code 只允许：
   absolute_change_cny / yoy_growth_rate / qoq_growth_rate / gross_margin /
   operating_margin / net_margin_parent / debt_to_assets_ratio）。growth 类
   （absolute_change / yoy / qoq）必须同时指定目标 metric_code（revenue /
   operating_cost / operating_profit / profit_before_tax / net_profit /
   net_profit_parent / net_profit_parent_excl_nonrecurring /
   operating_cash_flow_net / total_assets / total_liabilities / equity_parent）；
   margin / ratio 类（gross_margin / operating_margin / net_margin_parent /
   debt_to_assets_ratio）不需要 metric_code。不输出 observation ID / metric ID。
10. valuation_needs.metric_code 只允许：pe_ttm / pb_mrq / ps_ttm；
    peer_policy 只允许 peer_median。
11. period 只允许 4 位年度（如 2023）或省略。
12. 数量上限：document_needs ≤ 8、financial_needs ≤ 12、macro_needs ≤ 6、
    event_needs ≤ 6、valuation_needs ≤ 3、research_focus ≤ 5 条（每条 ≤ 40 字符）。
13. 通用「全面分析基本面」问题时，至少考虑 business / risk / financial，
    并视行业与数据可得性选择 macro / valuation——不要为跑全部而列全部。

输出必须是完整的 ResearchPlanPayload JSON。"""


def build_planner_messages(request: ResearchPlannerRequest) -> list[dict]:
    """构造 planner 模型消息（system + 只含语义输入 user）。

    不发送内部 UUID（task_id 是结果归属，不进入 prompt）、不发送 fingerprint /
    storage metadata / prompt history。company 只发语义身份。
    """
    company = request.company
    company_lines = "\n".join(
        (
            f"- security_code: {company.security_code}",
            f"- official_name: {company.official_name}",
            f"- exchange: {company.exchange}",
            f"- board: {company.board}",
        )
        + (tuple(f"- alias: {alias}" for alias in company.aliases) if company.aliases else ())
    )
    user = (
        f"研究问题：{request.research_question}\n"
        f"分析基准日：{request.analysis_as_of.isoformat()}\n"
        f"公司身份：\n{company_lines}\n"
        "请给出研究计划。"
    )
    return [
        {"role": "system", "content": _SYSTEM_RULES},
        {"role": "user", "content": user},
    ]


class DeepSeekResearchPlannerModel:
    """把官方 `langchain_deepseek.ChatDeepSeek` 包装为 ResearchPlannerModel。

    langchain SDK 只在 `generate()` 真正调用时懒加载（import 本模块 / 构造
    adapter 不依赖 langchain 已安装）。
    """

    def __init__(self, settings: Settings, usage_observer: LlmUsageObserver | None = None) -> None:
        self._settings = settings
        self._model_id = f"{settings.llm_provider}:{settings.llm_model}"
        self._usage_observer = usage_observer

    @property
    def model_id(self) -> str:
        return self._model_id

    async def generate(self, request: ResearchPlannerRequest) -> ResearchPlanPayload:
        try:
            from langchain_deepseek import ChatDeepSeek
        except ImportError as exc:
            raise ResearchPlannerModelUnavailable("langchain-deepseek 未安装") from exc

        messages = build_planner_messages(request)
        api_key = self._settings.deepseek_api_key
        llm = ChatDeepSeek(
            model=self._settings.llm_model,
            temperature=0.0,
            timeout=self._settings.llm_timeout_seconds,
            max_retries=self._settings.llm_max_retries,
            api_key=api_key.get_secret_value() if api_key is not None else None,
            # 显式关闭 thinking（同现有 analyst adapter 约定）。
            extra_body={"thinking": {"type": "disabled"}},
        )
        try:
            return await invoke_structured_with_usage(
                llm,
                ResearchPlanPayload,
                messages,
                component_name=COMPONENT_RESEARCH_PLANNER,
                provider=self._settings.llm_provider,
                model_id=self._settings.llm_model,
                usage_observer=self._usage_observer,
            )
        except ValidationError as exc:
            raise ResearchPlannerMalformedOutput() from exc
        except Exception as exc:
            # 含 OutputParserException（输出无法解析）与 provider/API 异常。
            raise ResearchPlannerModelUnavailable("LLM structured-output 调用失败") from exc
