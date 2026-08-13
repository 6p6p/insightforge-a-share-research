"""Evaluation replay contracts (stage 7B.1.4B.1)。

定义 rehydration 的**确定性 policy** 与结果 dataclass。frozen bundle 只携带语义
运行时字段（Company 的 security_code/official_name/short_name/exchange/board/
aliases、Provider 的 provider_key/display_name/enabled/capabilities、Document 的
provenance），其余 persistence-only 字段（schema 约束要求 NOT NULL / FK / CHECK
但运行期不读取）由 `replay_v1` policy 确定性补全——**不散落在 rehydrator 里、
不读 DEFAULT_PROVIDER registry、不读 source/live PG**。
"""

from dataclasses import dataclass
from uuid import UUID

# 确定性 rehydration policy 版本：schema-only 字段的唯一脚手架来源。
EVAL_REHYDRATION_POLICY_VERSION = "replay_v1"

# ---------------------------------------------------------------- Company 脚手架
# （运行期 planner 不读取 listing_status / identity_source_*；见 FrozenCompanyIdentity）
REPLAY_COMPANY_LISTING_STATUS = "unknown"
REPLAY_IDENTITY_SOURCE_URL = "https://replay.invalid/identity-source"

# ---------------------------------------------------------------- Provider 脚手架
# FrozenSourceProviderRef 只冻结 provider_key/display_name/enabled/capabilities；
# 其余 schema-only 字段由 replay_v1 给出确定性中性值（不读 DEFAULT_PROVIDERS）。
REPLAY_PROVIDER_TYPE = "general_web"
REPLAY_PROVIDER_AUTHORITY_TIER = 4
REPLAY_PROVIDER_HOMEPAGE_URL = "https://replay.invalid/provider"
REPLAY_PROVIDER_ALLOWED_DOMAINS: tuple[str, ...] = ()
REPLAY_PROVIDER_ACQUISITION_METHODS: tuple[str, ...] = ("public_html",)
REPLAY_PROVIDER_EXCHANGE_SCOPE: tuple[str, ...] = ()
REPLAY_PROVIDER_REQUIRES_API_KEY = False
REPLAY_PROVIDER_CRITICAL_CLAIM_ELIGIBLE = False

# ---------------------------------------------------------------- SourceRecord 脚手架
# FrozenDocumentSourceRef 不含 acquisition_method / status / provider_capabilities_snapshot /
# external_document_id；前二者用 replay_v1 中性值，provider_capabilities_snapshot 由
# frozen provider 的 capabilities 确定性派生（bundle 自洽），external_document_id=None。
REPLAY_SOURCE_ACQUISITION_METHOD = "public_html"
REPLAY_SOURCE_STATUS = "available"

# ---------------------------------------------------------------- Alias 脚手架
# frozen aliases 只有字符串（无类型）；planner `select CompanyAliasModel` 按
# `row.alias` 稳定排序读取（不按 alias_type 过滤），故用一个中性 alias_type 落库。
REPLAY_ALIAS_TYPE = "former_name"


@dataclass(frozen=True)
class RehydratedDocument:
    """一条 document 的 rehydration 结果（source/raw 的精确 ID + 真实落盘信息）。"""

    source_record_id: UUID
    raw_artifact_id: UUID
    content_sha256: str
    storage_key: str
    byte_size: int
    media_type: str


@dataclass(frozen=True)
class RehydratedCase:
    """一个 case 的 rehydration 结果摘要（不含任何 raw bytes / payload 文本）。"""

    company_id: UUID
    provider_keys: tuple[str, ...]
    documents: tuple[RehydratedDocument, ...]
