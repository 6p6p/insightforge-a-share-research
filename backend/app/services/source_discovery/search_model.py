"""Model-assisted search discovery model adapter (P2).

定位：**Discovery only**——LLM（deepseek-v4-flash）只做：
- 生成受控搜索意图 / 候选 URL / 推荐公开来源；

**禁止**：
- 生成 evidence / 财务数字 / 事实断言（system 规则显式禁止）；
- bypass provenance（候选 URL 必须经 provider 域名 allowlist 校验 +
  SafeFetcher 抓取验证后才由 SourceIngestionService 落库）。

`DeepSeekSearchQueryModel` 输出结构化 `SearchDiscoveryOutput`（候选 URL +
title，≤ 5 条）；输出校验强制 https + 合法 hostname（完整域名边界由
SearchDiscoveryProvider 校验）。任何失败 → `SearchDiscoveryUnavailable`
（调用方降级 exhausted，绝不编造来源）。
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import Settings
from app.llm.components import COMPONENT_SEARCH_DISCOVERY
from app.llm.errors import UnsupportedLLMProviderError
from app.llm.base import get_active_llm
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage
from app.services.source_discovery.contracts import SourceDiscoveryRequest

# 单次模型辅助发现最多候选数（有界：不无限抓取）。
MAX_SEARCH_CANDIDATES = 5
_MAX_TITLE_LENGTH = 300
_MAX_GAP_REASON_LENGTH = 200
# 迭代发现最大轮数（P6：LLM 判断缺口 → 工具验证 → 再探索，严格有界）。
MAX_SEARCH_ROUNDS = 3


class SearchCandidate(BaseModel):
    """模型推荐的一个公开来源候选（只是线索，不是 SourceRecord）。"""

    model_config = ConfigDict(frozen=True)

    url: str
    title: str

    @field_validator("url", mode="before")
    @classmethod
    def _valid_url(cls, value: object) -> str:
        from urllib.parse import urlparse

        if not isinstance(value, str):
            raise ValueError("url 必须是字符串")
        text = value.strip()
        parsed = urlparse(text)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("候选 url 必须是 https 且含 hostname")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("候选 url 不允许 userinfo")
        return text

    @field_validator("title", mode="before")
    @classmethod
    def _valid_title(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("title 必须是字符串")
        text = value.strip()
        if not text:
            raise ValueError("title 不能为空（trim 后）")
        if len(text) > _MAX_TITLE_LENGTH:
            raise ValueError(f"title 最长 {_MAX_TITLE_LENGTH} 字符")
        return text


class SearchDiscoveryOutput(BaseModel):
    """模型辅助发现的受控输出（候选列表 + 缺口判断，有界）。

    gap_remaining / gap_reason 是下一轮查询构造的信号：模型只判断
    “是否仍存在证据缺口 / 该往哪里看”，绝不生成 evidence / 数字 / 事实。
    """

    model_config = ConfigDict(frozen=True)

    candidates: list[SearchCandidate] = Field(default_factory=list)
    # LLM 判断：现有候选尝试后是否仍存在缺口（True → 进入下一轮迭代）。
    gap_remaining: bool = False
    # 受控短文本：缺口描述 / 下一轮建议查询方向（仅查询构造用）。
    gap_reason: str = ""

    @field_validator("candidates")
    @classmethod
    def _bounded(cls, value: list[SearchCandidate]) -> list[SearchCandidate]:
        if len(value) > MAX_SEARCH_CANDIDATES:
            raise ValueError(f"candidates 最多 {MAX_SEARCH_CANDIDATES} 条")
        return value

    @field_validator("gap_reason")
    @classmethod
    def _bounded_reason(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("gap_reason 必须是字符串")
        text = value.strip()
        if len(text) > _MAX_GAP_REASON_LENGTH:
            raise ValueError(f"gap_reason 最长 {_MAX_GAP_REASON_LENGTH} 字符")
        return text


class SearchDiscoveryUnavailable(RuntimeError):
    """模型辅助发现不可用 / 失败 / 输出非法（调用方降级 exhausted）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SearchQueryModel(Protocol):
    """LLM abstraction：need + 前轮真实结果 → 受控候选来源列表 + 缺口判断。

    - `model_id`：稳定 identifier（provider:model）；
    - `generate(request, round_history=None)`：`round_history` 是**代码构造的
      前轮真实执行摘要**（allowlist 拒绝数 / 抓取失败数 / 落库标题——全部
      来自真实工具结果，模型不参与编造）；返回已通过
      `SearchDiscoveryOutput` 校验的输出；任何失败 →
      `SearchDiscoveryUnavailable`；
    - 实现不得启用 tools / web search / retrieval / function side effects
      （模型只输出候选 URL + 缺口判断，不自行联网、不生成证据）。
    """

    @property
    def model_id(self) -> str: ...

    async def generate(
        self,
        request: SourceDiscoveryRequest,
        round_history: str | None = None,
    ) -> SearchDiscoveryOutput: ...


def build_search_messages(
    request: SourceDiscoveryRequest,
    round_history: str | None = None,
) -> list[dict]:
    """构造模型消息（system + 只含语义输入 user；不发送内部 ID）。

    round_history：前轮真实工具结果摘要（allowlist 拒绝 / 抓取失败 /
    已存在 / 落库标题），模型据此判断缺口是否仍在（gap_remaining）并给出
    下一轮查询方向（gap_reason）——P6 工具使用循环的"观察"输入。
    """
    user_lines = [
        f"security_code: {request.security_code}",
        f"need_kind: {request.need_kind}",
        f"source_type: {request.source_type or '未指定'}",
        f"period: {request.period or '未指定'}",
        f"research_question: {request.research_question or '未指定'}",
        f"topic: {request.topic or '未指定'}",
    ]
    if round_history:
        user_lines.append("\n前几轮工具执行结果（全部为真实抓取/校验结果）：")
        user_lines.append(round_history)
        user_lines.append(
            "请基于上述真实结果判断：证据缺口是否仍存在（gap_remaining）。"
            "若仍存在，给出下一轮查询方向（gap_reason，简短），并推荐新的"
            "候选来源。"
        )
    else:
        user_lines.append("请推荐公开来源候选。")
    return [
        {
            "role": "system",
            "content": (
                "你是 InsightForge 的公开资料发现助手。为一个 A 股上市公司推荐"
                "可能存在的**公开来源资料**（年报、公告、IR 材料、行业/宏观资料"
                "等）。\n"
                "硬性规则：\n"
                "1. 只输出候选 URL + 标题 + 缺口判断，**绝不输出任何研究结论、"
                "数字或事实**（不允许生成 evidence / financial numbers / "
                "observations）。\n"
                "2. URL 必须是 https 且来自公开官方网站（公司官网 / 交易所 / "
                "监管 / 权威数据平台），不要编造 URL——不确定就少给。\n"
                "3. 最多 5 条候选；title 简洁；gap_reason 最多 200 字符。\n"
                "4. 前一轮结果由真实工具执行产生（候选被 allowlist 拒绝、抓取"
                "失败、已存在、落库成功），如实据此判断是否仍有缺口；不要声称"
                "前轮产生了任何你未看到的具体内容。"
            ),
        },
        {"role": "user", "content": "\n".join(user_lines)},
    ]


class DeepSeekSearchQueryModel:
    """把官方 `langchain_deepseek.ChatDeepSeek` 包装为 SearchQueryModel。

    langchain SDK 只在 `generate()` 真正调用时懒加载；thinking 显式关闭。
    """

    def __init__(self, settings: Settings, usage_observer: LlmUsageObserver | None = None) -> None:
        self._settings = settings
        self._model_id = f"{settings.llm_provider}:{settings.llm_model}"
        self._usage_observer = usage_observer

    @property
    def model_id(self) -> str:
        return self._model_id

    async def generate(
        self,
        request: SourceDiscoveryRequest,
        round_history: str | None = None,
    ) -> SearchDiscoveryOutput:
        messages = build_search_messages(request, round_history=round_history)
        llm = get_active_llm(self._settings, temperature=0.0)
        try:
            output = await invoke_structured_with_usage(
                llm,
                SearchDiscoveryOutput,
                messages,
                component_name=COMPONENT_SEARCH_DISCOVERY,
                provider=self._settings.llm_provider,
                model_id=self._settings.llm_model,
                usage_observer=self._usage_observer,
            )
        except Exception as exc:
            raise SearchDiscoveryUnavailable("search discovery 调用失败") from exc
        return output


def create_search_query_model(
    settings: Settings,
    usage_observer: LlmUsageObserver | None = None,
) -> SearchQueryModel | None:
    """settings.search_discovery_llm_enabled 开关 → model（False → None）。"""
    if not settings.search_discovery_llm_enabled:
        return None
    provider = (settings.llm_provider or "").strip().lower()
    if not provider:
        raise UnsupportedLLMProviderError("llm_provider is not configured")

    return DeepSeekSearchQueryModel(settings, usage_observer=usage_observer)
