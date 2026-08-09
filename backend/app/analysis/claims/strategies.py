"""Analyst strategies (stage 4B.1): business/event + risk。

4B.1 只有两个 strategy：
- `business_event_v1`：用于 business / event——关注业务结构 / 经营变化 / 重大
  事件 / 增长驱动 / 公司明确表述；
- `risk_skeptic_v1`：用于 risk——关注风险因素 / 反向证据 / 不利事件 / 信息缺口 /
  过度推断风险。

**不创建** financial / macro / valuation 的 strategy prompt（domain 未到验收
门槛：financial 需确定性财务计算、macro 需专用传导契约、valuation 依赖后续
财务/同业数据）。
"""

from app.analysis.claims.errors import ClaimAnalysisDomainNotReady
from app.claims.contracts import ClaimAnalysisDomain

ANALYST_STRATEGY_BUSINESS_EVENT = "business_event_v1"
ANALYST_STRATEGY_RISK = "risk_skeptic_v1"

_STRATEGY_FOCUS = {
    ANALYST_STRATEGY_BUSINESS_EVENT: (
        "分析重点：业务结构、经营变化、重大事件、增长驱动、公司明确表述；"
        "优先区分已确认事实与合理推断，可标注潜在风险。"
    ),
    ANALYST_STRATEGY_RISK: (
        "分析重点：风险因素、反向证据、不利事件、信息缺口、过度推断风险；"
        "对高置信声明保持怀疑，避免把有限证据过度概括为事实。"
    ),
}

_DOMAIN_STRATEGY = {
    ClaimAnalysisDomain.BUSINESS: ANALYST_STRATEGY_BUSINESS_EVENT,
    ClaimAnalysisDomain.EVENT: ANALYST_STRATEGY_BUSINESS_EVENT,
    ClaimAnalysisDomain.RISK: ANALYST_STRATEGY_RISK,
}


def strategy_for_domain(domain: ClaimAnalysisDomain) -> str:
    """把 analysis_domain 映射到 4B.1 strategy；未支持 domain → NotReady。"""
    strategy = _DOMAIN_STRATEGY.get(domain)
    if strategy is None:
        raise ClaimAnalysisDomainNotReady()
    return strategy


def strategy_focus(strategy: str) -> str:
    """返回 strategy 的分析重点描述（用于 user prompt，非 system）。"""
    return _STRATEGY_FOCUS.get(strategy, "")
