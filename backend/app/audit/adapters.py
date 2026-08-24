"""Production LLM adapter (stage 5D): ChatDeepSeek → AuditModel.

- 复用 5B 的 DeepSeek runtime（ChatDeepSeek + with_structured_output）；
- `model_id = {provider}:{model}`（如 `deepseek:deepseek-v4-flash`）；provider 无
  immutable revision 时**不伪造 revision**；
- **显式关闭 thinking**（`extra_body={"thinking": {"type": "disabled"}}`）：
  DeepSeek V4 Flash 默认开启 thinking，但 Evidence-bound Auditor 需要稳定、
  低成本的受约束输出且不产生 `reasoning_content`（spec P：不持久化
  reasoning_content）；`temperature=0` 不等于关闭 thinking，必须显式传参
  （`thinking` 非标准 OpenAI 参数，经 `extra_body` 传递）；
- 只启用 structured-output 机制，**不绑定 tools / 不开 web search**（非 agentic，
  spec P：Auditor 不 retrieval / 不联网 / 不调函数 / 不重写正文）；
- 异常映射：provider / API / 网络异常 → `ReportAuditModelUnavailable`；输出
  无法解析为 `AuditDecision` → `ReportAuditMalformedOutput`；
- **不泄露** raw provider response / key / 完整 prompt / reasoning_content。

自动测试仍用 Fake 模型；真实调用只用于受控 smoke。
"""

from app.audit.contracts import AuditDecision
from app.audit.errors import (
    ReportAuditMalformedOutput,
    ReportAuditModelUnavailable,
)
from app.audit.packs import AuditPack
from app.audit.prompt import build_audit_messages
from app.core.config import Settings
from app.llm.components import COMPONENT_AUDIT
from app.llm.base import get_active_llm
from app.llm.instrumentation import LlmUsageObserver, invoke_structured_with_usage


class DeepSeekAuditModel:
    """把官方 `langchain_deepseek.ChatDeepSeek` 包装为 AuditModel。

    langchain SDK 只在 `audit()` 真正调用时懒加载（import 本模块 / 构造
    adapter 不依赖 langchain 已安装）。
    """

    def __init__(self, settings: Settings, usage_observer: LlmUsageObserver | None = None) -> None:
        self._settings = settings
        self._model_id = f"{settings.llm_provider}:{settings.llm_model}"
        self._usage_observer = usage_observer

    @property
    def model_id(self) -> str:
        """稳定 identifier：provider:model（无 immutable revision，不伪造 @rev）。"""
        return self._model_id

    async def audit(self, pack: AuditPack, hint: str | None = None) -> AuditDecision:
        try:
            from langchain_core.exceptions import OutputParserException  # noqa: F401
        except ImportError as exc:
            raise ReportAuditModelUnavailable("langchain-deepseek 未安装") from exc

        messages = build_audit_messages(pack, hint=hint)
        llm = get_active_llm(self._settings, temperature=0.0)
        try:
            return await invoke_structured_with_usage(
                llm,
                AuditDecision,
                messages,
                component_name=COMPONENT_AUDIT,
                provider=self._settings.llm_provider,
                model_id=self._settings.llm_model,
                usage_observer=self._usage_observer,
            )
        except OutputParserException as exc:
            raise ReportAuditMalformedOutput() from exc
        except Exception as exc:
            raise ReportAuditModelUnavailable("LLM structured-output 调用失败") from exc
