"""Research fulfillment contracts (stage 7A.2A spec G/I): schema v1。

`fulfill_research_needs(research_plan_id)` 只消费 Preparation 的 `missing_needs`
（自动补证据），产出 `ResearchFulfillmentResult` —— 这是 **workflow / application
output**，**不持久化到 DB、不建表**。绝不持久化 raw exception / prompt /
API response / reasoning_content。

角色边界（spec G/I）：
- `ResearchFulfillmentService`：verify Plan + verify Route + `prepare_research()`
  → 对每个 missing need 分发到 executor → 重跑 `prepare_research()` → 组装结果；
- executor（document / financial / macro / valuation）：自动补证据（Retrieval →
  Evidence / calculation → re-preparation / macro Evidence replay / valuation
  manual_required）。**不**做全网无限搜索 / 复杂浏览器 agent / 自动 peer
  选择 / Top-level Graph；
- 调用方**不获取** Evidence / Source IDs / query / provider URL —— 只能看到
  attempt 摘要（created / existing artifact ids 之外不暴露中间产物细节）。

status 语义（spec I）：
- `resolved`：executor 成功补证据（created 或 existing artifacts）；
- `unresolved`：补证据失败（缺底层数据 / 无法确定性建索引等）；
- `manual_required`：需要人工决策（如 valuation 的显式 peer 集合）；
- `provider_unavailable`：路由当时无 provider 或数据源无数据（不 live fetch）；
- `unsupported`：need 类型 / calculation_code 不受支持。

`FulfillmentErrorCode` 区分「Source absent」vs「Source 存在但 Evidence 未抽取」
（spec P）：SOURCE_NOT_FOUND（无 source）vs INDEX_NOT_READY /
EVIDENCE_NOT_EXTRACTED（source 存在但无法得到证据）。
"""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

RESEARCH_FULFILLMENT_SCHEMA_VERSION = 1


class FulfillmentStatus(StrEnum):
    """一条 need 的 fulfillment 终态（spec I 固定集合）。"""

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    MANUAL_REQUIRED = "manual_required"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNSUPPORTED = "unsupported"


class FulfillmentErrorCode(StrEnum):
    """attempt.error_code 受控值（区分 source absent vs evidence 未抽取，spec P）。

    - document / event：
      SOURCE_NOT_FOUND（无匹配 source）vs INDEX_NOT_READY（source 存在但无
      ready index 且无法确定性补建）vs EVIDENCE_NOT_EXTRACTED（source 存在且
      已检索但抽取 0 证据）vs EXTRACTOR_UNAVAILABLE；
    - financial：MISSING_UNDERLYING_OBSERVATION（缺底层观测）/
      OBSERVATION_INSUFFICIENT（观测存在但不满足 calculation 输入语义）/
      CALCULATION_INPUT_ERROR（draft 构造失败）；
    - macro：MACRO_DATA_UNAVAILABLE（无可用 macro 观测数据，不 live fetch）/
      MACRO_EVIDENCE_MISSING（观测存在但无法创建 macro Evidence）；
    - valuation：EXPLICIT_PEER_SET_REQUIRED（需要人工提供 peer 集合，
      不自动 peer 宇宙）；
    - 通用：PROVIDER_UNAVAILABLE（路由当时无 provider 或 provider 不可用）/
      UNSUPPORTED_NEED（need 类型 / 参数不受支持）。
    """

    SOURCE_NOT_FOUND = "source_not_found"
    INDEX_NOT_READY = "index_not_ready"
    EVIDENCE_NOT_EXTRACTED = "evidence_not_extracted"
    EXTRACTOR_UNAVAILABLE = "extractor_unavailable"
    MISSING_UNDERLYING_OBSERVATION = "missing_underlying_observation"
    OBSERVATION_INSUFFICIENT = "observation_insufficient"
    CALCULATION_INPUT_ERROR = "calculation_input_error"
    MACRO_DATA_UNAVAILABLE = "macro_data_unavailable"
    MACRO_EVIDENCE_MISSING = "macro_evidence_missing"
    EXPLICIT_PEER_SET_REQUIRED = "explicit_peer_set_required"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNSUPPORTED_NEED = "unsupported_need"


class FulfillmentAttempt(BaseModel):
    """一条 missing need 的 fulfillment 摘要（spec I）。

    - need_code / need_type：来自 Plan（need_type = need_kind：
      document / financial / macro / event / valuation）；
    - route_type：来自 stored SourceRoutePlan 的对应 entry
      （SourceRouteType 值，route 决策时的能力值）；
    - status / error_code：终态 + 失败原因（受控枚举）；
    - created_artifact_ids / existing_artifact_ids：本次执行产生的 / 复用的
      artifact ids（evidence_card / calculation / macro_driver_evidence /
      comparison）。**不含**任何中间产物（query / hit / prompt / raw）。
    """

    model_config = ConfigDict(frozen=True)

    need_code: str
    need_type: str
    route_type: str
    status: FulfillmentStatus
    created_artifact_ids: list[UUID] = Field(default_factory=list)
    existing_artifact_ids: list[UUID] = Field(default_factory=list)
    error_code: FulfillmentErrorCode | None = None


class MissingNeedSummary(BaseModel):
    """Preparation missing need 的摘要（用于 before/after 快照，不泄漏正文）。"""

    model_config = ConfigDict(frozen=True)

    need_code: str
    need_kind: str
    reason_code: str
    detail: str


class FulfillmentPreparation(BaseModel):
    """一次 `prepare_research()` 的摘要快照（spec I：preparation_before/after）。

    只投影 need_code / need_kind / reason_code / detail 与 readiness；**不含**
    resolved 中间产物、stage4 正文。
    """

    model_config = ConfigDict(frozen=True)

    missing_need_codes: list[str] = Field(default_factory=list)
    missing_needs: list[MissingNeedSummary] = Field(default_factory=list)
    ready_for_analysis: bool = False


class ResearchFulfillmentResult(BaseModel):
    """`fulfill_research_needs` 的结果契约（schema v1，仅 application output）。

    - research_plan_id / route_plan_id：plan 与 route 身份（调用方不接触
      Evidence / Source ids 以外的内部查询细节）；
    - attempts：只对 missing needs 的逐条结果（已 resolved 的 need 不出现）；
    - preparation_before / preparation_after：执行前后两次 prepare 的摘要；
    - ready_for_analysis：执行后全部 need 是否就绪；
    - stage4_request：ready 时的有效 Stage4WorkflowRequest 序列化（否则 None）。
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = RESEARCH_FULFILLMENT_SCHEMA_VERSION
    research_plan_id: UUID
    route_plan_id: UUID
    attempts: list[FulfillmentAttempt] = Field(default_factory=list)
    preparation_before: FulfillmentPreparation
    preparation_after: FulfillmentPreparation
    ready_for_analysis: bool = False
    stage4_request: dict | None = None
