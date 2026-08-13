"""Replay bundle builder（stage 7B.1.4B.1 测试共享）。

与 tests/eval/conftest.py 的 `build_bundle` 不同：这里 document 是 **有效 HTML**
（media_type=text/html），可被 `LocalRawArtifactStore.put_html_bytes` 落盘，也能被
`SourceParsingService.parse_html_bytes` 解析 + `ChunkingService` 分块，从而支撑
rehydrate → parse → chunk 的端到端证明。

`doc_provider_key` / `doc_media_type` 可参数化，供 tamper / 负例测试构造自洽但
语义不一致的 bundle（snapshot fingerprint 始终与 case 引用闭合）。
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.eval.bundle.writer import EvaluationBundleWriter
from app.eval.contracts import (
    EvalCase,
    EvalDatasetCaseRef,
    EvalDatasetManifest,
    FrozenCompanyIdentity,
    FrozenDocumentSourceRef,
    FrozenSourceProviderRef,
    FrozenSourceSnapshot,
)
from app.eval.fingerprints import (
    compute_eval_case_fingerprint,
    compute_source_snapshot_fingerprint,
)

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
SOURCE_RECORD_ID = UUID("22222222-2222-2222-2222-222222222222")
RAW_ARTIFACT_ID = UUID("33333333-3333-3333-3333-333333333333")
CASE_ID = "test-replay-fundamentals"
CASE_VERSION = 1
DATASET_ID = "insightforge_eval_replay_test"

# 与 materializer 测试的 _doc_html 同构，保证 parse_html_bytes 能产生 >=1 个 block。
DOC_HTML = (
    "<html><head><title>研究新闻</title></head><body><article>"
    "<p>2024年贵州茅台营业收入123,456万元，净利润同比增长。</p>"
    "</article></body></html>"
).encode()
DOC_SHA256 = hashlib.sha256(DOC_HTML).hexdigest()

_ACQUIRED_AT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_PUBLISHED_AT = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ReplayBundleSpec:
    manifest: EvalDatasetManifest
    case: EvalCase
    snapshot: FrozenSourceSnapshot
    document_content: bytes
    document_sha256: str
    snapshot_fingerprint: str


def build_replay_bundle(
    root: str | Path,
    *,
    doc_provider_key: str = "xinhuanet",
    doc_media_type: str = "text/html",
    company_exchange: str = "SSE",
) -> ReplayBundleSpec:
    """写入一个可 rehydrate 的 bundle，返回其 frozen spec（确定性）。

    `company_exchange` 用于构造「通过 frozen 契约但违反目标 schema CHECK」的
    case（如 exchange='NASDAQ'），触发 rehydrator 的 `EvalReplayIntegrityError`。
    """
    doc = FrozenDocumentSourceRef(
        source_record_id=SOURCE_RECORD_ID,
        raw_artifact_id=RAW_ARTIFACT_ID,
        content_sha256=DOC_SHA256,
        provider_key=doc_provider_key,
        document_type="news_article",
        media_type=doc_media_type,
        title="研究新闻",
        source_url="https://www.xinhuanet.com/2026/0809/0001.htm",
        acquired_at=_ACQUIRED_AT,
        authority_tier_snapshot=3,
        critical_claim_eligible_snapshot=False,
        published_at=_PUBLISHED_AT,
    )
    providers = (
        FrozenSourceProviderRef(
            provider_key="xinhuanet",
            display_name="新华网",
            enabled=True,
            capabilities=("news_article",),
        ),
        FrozenSourceProviderRef(
            provider_key="sse",
            display_name="上海证券交易所",
            enabled=True,
            capabilities=("company_announcement", "document_download"),
        ),
    )
    snapshot = FrozenSourceSnapshot(
        document_sources=(doc,),
        source_providers=providers,
    )
    snapshot_fp = compute_source_snapshot_fingerprint(snapshot)
    case = EvalCase(
        case_id=CASE_ID,
        case_version=CASE_VERSION,
        company_id=COMPANY_ID,
        company=FrozenCompanyIdentity(
            security_code="600519",
            official_name="公司600519",
            short_name="600519",
            exchange=company_exchange,
            board="sse_main",
            aliases=("贵州茅台", "茅台股份"),
        ),
        research_question="贵州茅台 2024 年基本面是否支撑当前估值？",
        analysis_as_of=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        source_snapshot_fingerprint=snapshot_fp,
    )
    case_fp = compute_eval_case_fingerprint(case)
    manifest = EvalDatasetManifest(
        dataset_id=DATASET_ID,
        dataset_version=1,
        cases=(
            EvalDatasetCaseRef(
                case_id=CASE_ID,
                case_version=CASE_VERSION,
                case_fingerprint=case_fp,
            ),
        ),
    )

    writer = EvaluationBundleWriter(root)
    writer.write_document_blob(DOC_SHA256, DOC_HTML)
    writer.write_snapshot(snapshot)
    writer.write_case(case)
    writer.write_manifest(manifest)

    return ReplayBundleSpec(
        manifest=manifest,
        case=case,
        snapshot=snapshot,
        document_content=DOC_HTML,
        document_sha256=DOC_SHA256,
        snapshot_fingerprint=snapshot_fp,
    )
