"""Versioned pricing snapshot (stage 7B.1.3D).

Vendor pricing 会变化：**不**把易变 API price 硬编码进 semantic execution logic。
成本估算只走 `PricingSnapshot`（versioned，每 1M token 的价格），并显式记录
`pricing_version`。语义执行（variant runner）不依赖本模块。
"""

from dataclasses import dataclass

from app.llm.instrumentation import LlmCallUsageRecord, UsageStatus

# 当前定价快照版本（pricing 调整 → 递增版本；旧快照保留用于审计）。
PRICING_VERSION = 1

# provider:model → (input USD / 1M tokens, output USD / 1M tokens)。
# v1：DeepSeek 公开价格（人民币折算为 USD 近似；仅用于相对成本比较，
# 不是财务结算依据）。
_PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("deepseek", "deepseek-v4-flash"): (0.27, 1.10),
    ("deepseek", "deepseek-chat"): (0.27, 1.10),
}


@dataclass(frozen=True)
class PricingSnapshot:
    """versioned pricing：`estimate_cost(usage)` 用（相对成本，非结算）。"""

    version: int = PRICING_VERSION

    def estimate_cost(self, records: tuple[LlmCallUsageRecord, ...]) -> float | None:
        """全部 reported usage 的估算成本（USD）；无 reported 记录 → None。

        `pricing_version` 随 snapshot 版本记录；未知 (provider, model) → None
        （不猜测价格）。
        """
        total = 0.0
        any_reported = False
        for record in records:
            if record.usage_status != UsageStatus.REPORTED:
                continue
            if (
                record.input_tokens is None
                or record.output_tokens is None
                or record.total_tokens is None
            ):
                continue
            price = _PRICES.get((record.provider, record.model_id))
            if price is None:
                return None
            input_price, output_price = price
            total += (
                record.input_tokens / 1_000_000 * input_price
                + record.output_tokens / 1_000_000 * output_price
            )
            any_reported = True
        return total if any_reported else None
