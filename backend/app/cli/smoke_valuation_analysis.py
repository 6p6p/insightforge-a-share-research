"""Real DeepSeek smoke (stage 4C.2B.2): structured relative valuation analysis — 受控一次。

用途：手动验证真实 DeepSeek V4 Flash 对真实 Comparison Pack（V1）能返回符合
`ValuationAnalysisDecision` schema 的结构化输出，并走**完整生产链路**：
`ValuationAnalysisService.analyze` = 短 DB session 加载校验（verify_comparison_
integrity / comparison set policy）→ 关闭 session → 构造 V alias Pack →
**生产适配器 `DeepSeekValuationAnalysisModel`**（thinking disabled / temperature=0 /
structured output）→ schema double-check → V ref resolution（no-cherry-picking）→
direction policy → 确定性 statement 渲染 → v7 ValuationClaimDraft →
`ValuationClaimService.create_claim` 原子持久化。

seed（真实 HTML 链 → EvidenceCard → ValuationMetricObservation → Comparison）：
- target PE = 30，peers = 18 / 20 / 22（同一 pe_ttm / metric_as_of）→
  peer_median = 20，premium_discount_to_median = +0.5（+50.00%），
  position = above → **期望 assessment = relative_high**。

校验：provider / model / latency_ms / relevant / claim_id / replayed /
assessment / deterministic_statement（`render_valuation_claim_statement`）/
reason_code / analysis_as_of / analyst 身份（name/version/model_id）/
assessment_matches_expected / cleanup_success（只删 scratch 公司，实际查询受影响
表残余行，全部 0 才算 cleanup_success；残留非 0 → 不声称成功，退出码 1）。

**不记录** API key / 完整 prompt / reasoning_content / raw provider response。
**不写正式业务 Claim**：清理删除 scratch 公司（target + 3 peers）全部 seed 链路
（含 smoke 期间创建的 Valuation Claim / Comparison / Observations / Evidence）。

需要环境变量 `DEEPSEEK_API_KEY`。运行（insightforge Conda 环境）：
    python -m app.cli.smoke_valuation_analysis
"""

import asyncio
import shutil
import sys
import tempfile
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text

from app.analysis.valuation.adapters import DeepSeekValuationAnalysisModel
from app.analysis.valuation.contracts import ValuationAnalysisRequest
from app.analysis.valuation.errors import (
    ValuationAnalysisComparisonOmitted,
    ValuationAnalysisDirectionConflict,
    ValuationAnalysisMalformedOutput,
    ValuationAnalysisMixedEvidenceInsufficient,
    ValuationAnalysisModelUnavailable,
    ValuationAnalysisRelationConflict,
    ValuationAnalysisUncertainImportancePolicy,
    ValuationAnalysisUnknownRef,
)
from app.analysis.valuation.service import ValuationAnalysisService
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
from app.repositories.claim_repository import ClaimRepository
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.claim_contracts import render_valuation_claim_statement
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.contracts import ComparisonDraft, ValuationMetricCode, ValuationMetricDraft
from app.valuation.observation_service import ValuationObservationService

# Windows 上 psycopg async 需要 SelectorEventLoop；必须在 asyncio.run 之前设置。
configure_asyncio_runtime()

_QUESTION = "贵州茅台当前市盈率（PE-TTM）处于可比公司中的什么相对水平？"
_METRIC_AS_OF = date(2026, 8, 7)
_ANALYSIS_AS_OF = date(2026, 8, 10)

# 干净且方向明确的样本：target=30，peers=18/20/22 → median=20，premium=+0.5。
_TARGET_VALUE = "30"
_PEER_VALUES = ["18", "20", "22"]
_EXPECTED_ASSESSMENT = "relative_high"

_URL = "https://www.xinhuanet.com/2026/0810/smoke_valuation.htm"
_SOURCE_TITLE = "估值新闻"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)


def _fin_html(value_text: str) -> bytes:
    # value 内嵌进正文：observation 服务要求 value_text 是 quote_text 中一个完整
    # 数字 token（ValuationValueNotFound 否则）。statement 仍是程序确定性渲染。
    body = f"<p>报告期内公司市盈率{value_text}倍，估值水平合理，市场给予较高溢价。</p>"
    return (
        "<html><head><title>估值新闻</title></head><body><article>"
        + body
        + "</article></body></html>"
    ).encode()


async def _seed_company(sessionmaker, code: str) -> uuid.UUID:
    company_id = uuid.uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code=code,
                identity_key=f"SSE:{code}",
                board="sse_main",
                official_name=f"Smoke公司{code}",
                short_name=code,
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    return company_id


async def _seed_observation(
    sessionmaker,
    raw_store,
    company_id: uuid.UUID,
    value_text: str,
) -> dict:
    """真实 HTML 链（RawArtifact → SourceRecord → Parsing → Chunking →
    EvidenceCard → ValuationMetricObservation）。"""
    stored = raw_store.put_html_bytes(_fin_html(value_text))
    async with sessionmaker() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
        assert artifact is not None
        record = SourceRecordModel(
            company_id=company_id,
            provider_key="xinhuanet",
            artifact_id=artifact.artifact_id,
            document_type="news_article",
            title=_SOURCE_TITLE,
            published_at=_PUBLISHED_AT,
            reporting_period_end=None,
            source_url=_URL + f"?uid={uuid.uuid4().hex[:8]}",
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsing = SourceParsingService(sessionmaker, raw_store)
    parsed = await parsing.parse_source(source_id)
    result = await ChunkingService(sessionmaker).chunk_parsed_source(parsed.parsed_source_id)
    async with sessionmaker() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(result.chunk_set_id)
    assert chunks, "smoke seed must produce chunks"
    chunk = next(c for c in chunks if value_text in c.text)
    idx = chunk.text.index(value_text)
    card = await EvidenceCardService(sessionmaker).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement="市盈率为" + chunk.text[idx : idx + len(value_text)] + "倍",
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=idx,
            quote_end=idx + len(value_text),
            extractor_name="smoke-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    obs = await ValuationObservationService(sessionmaker).create_observation(
        ValuationMetricDraft(
            company_id=company_id,
            source_evidence_card_id=card.evidence_card_id,
            metric_code=ValuationMetricCode.PE_TTM,
            metric_as_of=_METRIC_AS_OF,
            source_value_text=value_text,
        )
    )
    return {
        "company_id": company_id,
        "valuation_observation_id": obs.valuation_observation_id,
        "evidence_card_id": card.evidence_card_id,
        "artifact_id": artifact.artifact_id,
        "source_id": source_id,
    }


async def _cleanup(
    sessionmaker,
    *,
    company_ids: list[uuid.UUID],
    artifact_ids: list[uuid.UUID],
) -> None:
    """只删 scratch 公司（target + peers）的 seed 链路 + smoke Claim，不动其他数据。"""
    cids = tuple(company_ids)  # UUID 对象直接绑定（psycopg 原生 uuid，禁止 str 化）
    in_c = f"IN ({','.join(':' + f'c{i}' for i in range(len(cids)))})"
    claim_sel = f"SELECT claim_id FROM claims WHERE company_id {in_c}"
    comp_sel = (
        f"SELECT comparison_id FROM relative_valuation_comparisons WHERE target_company_id {in_c}"
    )
    src_sel = f"SELECT source_id FROM source_records WHERE company_id {in_c}"
    parsed_sel = f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({src_sel})"
    chunkset_sel = f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({parsed_sel})"
    params = {f"c{i}": cid for i, cid in enumerate(cids)}
    async with sessionmaker() as session:
        await session.execute(
            text(
                f"DELETE FROM claim_relative_valuation_comparison_links "
                f"WHERE claim_id IN ({claim_sel})"
            ).bindparams(**params)
        )
        await session.execute(
            text(
                f"DELETE FROM relative_valuation_claim_profiles WHERE claim_id IN ({claim_sel})"
            ).bindparams(**params)
        )
        await session.execute(
            text(f"DELETE FROM claim_evidence_links WHERE claim_id IN ({claim_sel})").bindparams(
                **params
            )
        )
        await session.execute(
            text(f"DELETE FROM claims WHERE company_id {in_c}").bindparams(**params)
        )
        await session.execute(
            text(
                f"DELETE FROM relative_valuation_comparison_peers "
                f"WHERE comparison_id IN ({comp_sel})"
            ).bindparams(**params)
        )
        await session.execute(
            text(
                f"DELETE FROM relative_valuation_comparisons WHERE target_company_id {in_c}"
            ).bindparams(**params)
        )
        await session.execute(
            text(f"DELETE FROM valuation_metric_observations WHERE company_id {in_c}").bindparams(
                **params
            )
        )
        await session.execute(
            text(f"DELETE FROM evidence_cards WHERE company_id {in_c}").bindparams(**params)
        )
        await session.execute(
            text(f"DELETE FROM document_chunks WHERE chunk_set_id IN ({chunkset_sel})").bindparams(
                **params
            )
        )
        await session.execute(
            text(
                f"DELETE FROM chunk_vector_indexes WHERE chunk_set_id IN ({chunkset_sel})"
            ).bindparams(**params)
        )
        await session.execute(
            text(f"DELETE FROM chunk_sets WHERE parsed_source_id IN ({parsed_sel})").bindparams(
                **params
            )
        )
        await session.execute(
            text(
                f"DELETE FROM parsed_source_blocks WHERE parsed_source_id IN ({parsed_sel})"
            ).bindparams(**params)
        )
        await session.execute(
            text(f"DELETE FROM parsed_sources WHERE source_id IN ({src_sel})").bindparams(**params)
        )
        await session.execute(
            text(f"DELETE FROM source_records WHERE company_id {in_c}").bindparams(**params)
        )
        for aid in artifact_ids:
            await session.execute(
                text("DELETE FROM raw_artifacts WHERE artifact_id = :aid").bindparams(aid=aid)
            )
        await session.execute(
            text(f"DELETE FROM company_aliases WHERE company_id {in_c}").bindparams(**params)
        )
        await session.execute(
            text(f"DELETE FROM companies WHERE company_id {in_c}").bindparams(**params)
        )
        await session.commit()


async def _residual_counts(
    sessionmaker, *, company_ids: list[uuid.UUID], artifact_ids: list[uuid.UUID]
) -> dict[str, int]:
    """实际查询 scratch company 受影响表的残留行数（不猜测、不声称 0 残留）。"""
    cids = tuple(company_ids)  # UUID 对象直接绑定（psycopg 原生 uuid，禁止 str 化）
    in_c = f"IN ({','.join(':' + f'c{i}' for i in range(len(cids)))})"
    params = {f"c{i}": cid for i, cid in enumerate(cids)}
    claim_sel = f"SELECT claim_id FROM claims WHERE company_id {in_c}"
    comp_sel = (
        f"SELECT comparison_id FROM relative_valuation_comparisons WHERE target_company_id {in_c}"
    )
    src_sel = f"SELECT source_id FROM source_records WHERE company_id {in_c}"
    parsed_sel = f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({src_sel})"
    chunkset_sel = f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({parsed_sel})"
    scoped: dict[str, str] = {
        "companies": f"SELECT count(*) FROM companies WHERE company_id {in_c}",
        "company_aliases": f"SELECT count(*) FROM company_aliases WHERE company_id {in_c}",
        "claims": f"SELECT count(*) FROM claims WHERE company_id {in_c}",
        "claim_evidence_links": (
            f"SELECT count(*) FROM claim_evidence_links WHERE claim_id IN ({claim_sel})"
        ),
        "claim_relative_valuation_comparison_links": (
            f"SELECT count(*) FROM claim_relative_valuation_comparison_links "
            f"WHERE claim_id IN ({claim_sel})"
        ),
        "relative_valuation_claim_profiles": (
            f"SELECT count(*) FROM relative_valuation_claim_profiles "
            f"WHERE claim_id IN ({claim_sel})"
        ),
        "relative_valuation_comparisons": (
            f"SELECT count(*) FROM relative_valuation_comparisons WHERE target_company_id {in_c}"
        ),
        "relative_valuation_comparison_peers": (
            f"SELECT count(*) FROM relative_valuation_comparison_peers "
            f"WHERE comparison_id IN ({comp_sel})"
        ),
        "valuation_metric_observations": (
            f"SELECT count(*) FROM valuation_metric_observations WHERE company_id {in_c}"
        ),
        "evidence_cards": f"SELECT count(*) FROM evidence_cards WHERE company_id {in_c}",
        "source_records": f"SELECT count(*) FROM source_records WHERE company_id {in_c}",
        "parsed_sources": (f"SELECT count(*) FROM parsed_sources WHERE source_id IN ({src_sel})"),
        "parsed_source_blocks": (
            f"SELECT count(*) FROM parsed_source_blocks WHERE parsed_source_id IN ({parsed_sel})"
        ),
        "chunk_sets": (f"SELECT count(*) FROM chunk_sets WHERE parsed_source_id IN ({parsed_sel})"),
        "document_chunks": (
            f"SELECT count(*) FROM document_chunks WHERE chunk_set_id IN ({chunkset_sel})"
        ),
        "chunk_vector_indexes": (
            f"SELECT count(*) FROM chunk_vector_indexes WHERE chunk_set_id IN ({chunkset_sel})"
        ),
    }
    async with sessionmaker() as session:
        counts: dict[str, int] = {}
        for table, sql in scoped.items():
            counts[table] = (await session.execute(text(sql).bindparams(**params))).scalar_one()
        for aid in artifact_ids:
            counts[f"raw_artifacts:{aid.hex[:8]}"] = (
                await session.execute(
                    text("SELECT count(*) FROM raw_artifacts WHERE artifact_id = :aid").bindparams(
                        aid=aid
                    )
                )
            ).scalar_one()
        return counts


async def _main() -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    settings = get_settings()
    if settings.deepseek_api_key is None:
        print("DEEPSEEK_API_KEY 未配置，跳过真实 smoke（零真实 LLM）。", file=sys.stderr)
        return 2
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    sessionmaker = manager.session_factory()
    company_ids: list[uuid.UUID] = []
    artifact_ids: list[uuid.UUID] = []
    smoke_root = Path(tempfile.mkdtemp(prefix="smoke_valuation_analysis_"))
    raw_store = LocalRawArtifactStore(root=smoke_root / "raw", max_bytes=1024 * 1024)
    analyze_ok = False
    try:
        await SourceRegistryService(sessionmaker).seed_defaults()
        target_company_id = await _seed_company(sessionmaker, "600519")
        peer_ids = [await _seed_company(sessionmaker, f"6005{2 + i:02d}") for i in range(3)]
        company_ids = [target_company_id, *peer_ids]

        target_obs = await _seed_observation(
            sessionmaker, raw_store, target_company_id, _TARGET_VALUE
        )
        peer_obs = []
        for i, value in enumerate(_PEER_VALUES):
            peer_obs.append(await _seed_observation(sessionmaker, raw_store, peer_ids[i], value))
        artifact_ids = [
            target_obs["artifact_id"],
            *(p["artifact_id"] for p in peer_obs),
        ]

        comp = await RelativeValuationComparisonService(sessionmaker).create_comparison(
            ComparisonDraft(
                target_company_id=target_company_id,
                target_observation_id=target_obs["valuation_observation_id"],
                peer_observation_ids=tuple(p["valuation_observation_id"] for p in peer_obs),
                analysis_as_of=_ANALYSIS_AS_OF,
            )
        )
        # 用 Gate 0 的 verify_comparison_integrity 读取统计（同时走真实 replay）。
        async with sessionmaker() as session:
            verified = await RelativeValuationComparisonService(
                sessionmaker
            ).verify_comparison_integrity(session, comp.comparison_id)
        assert verified is not None
        print(
            f"seed: target_pe={verified.target_value} peers={_PEER_VALUES} "
            f"peer_median={verified.peer_median} premium={verified.premium_discount_to_median}"
        )

        request = ValuationAnalysisRequest(
            company_id=target_company_id,
            research_question=_QUESTION,
            analysis_as_of=_ANALYSIS_AS_OF,
            comparison_ids=[comp.comparison_id],
        )
        model = DeepSeekValuationAnalysisModel(settings)
        service = ValuationAnalysisService(sessionmaker, model)
        print(f"provider = {settings.llm_provider}")
        print(f"model = {model.model_id}")

        start = time.perf_counter()
        try:
            result = await service.analyze(request)
        except ValuationAnalysisModelUnavailable:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("FAIL: DeepSeek provider/model unavailable")
            return 1
        except ValuationAnalysisMalformedOutput:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("FAIL: model output could not be parsed into ValuationAnalysisDecision")
            return 1
        except (
            ValuationAnalysisUnknownRef,
            ValuationAnalysisRelationConflict,
            ValuationAnalysisComparisonOmitted,
        ):
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("FAIL: model refs failed resolution / no-cherry-picking coverage")
            return 1
        except (
            ValuationAnalysisDirectionConflict,
            ValuationAnalysisMixedEvidenceInsufficient,
            ValuationAnalysisUncertainImportancePolicy,
        ):
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("FAIL: assessment contradicts support premiums / policy")
            return 1
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        print(f"latency_ms = {elapsed_ms}")
        print(f"relevant = {result.relevant}")
        print(f"claim_id = {result.claim_id}")
        print(f"replayed = {result.replayed}")
        reason = result.reason_code.value if result.reason_code is not None else None
        print(f"reason_code = {reason}")
        if result.assessment is not None:
            print(f"assessment = {result.assessment.value}")
            print(
                "deterministic_statement = "
                f"{render_valuation_claim_statement(result.assessment, ('pe_ttm',))}"
            )
            print(f"expected = {_EXPECTED_ASSESSMENT}")
            print(
                f"assessment_matches_expected = {result.assessment.value == _EXPECTED_ASSESSMENT}"
            )

        if result.claim_id is not None:
            async with sessionmaker() as session:
                claim = await ClaimRepository(session).get_by_id(result.claim_id)
            assert claim is not None
            print(f"claim_kind = {claim.claim_kind}")
            print(f"claim_schema_version = {claim.claim_schema_version}")
            print(f"analysis_domain = {claim.analysis_domain}")
            print(f"analyst = {claim.analyst_name} v{claim.analyst_version}")
            print(f"analyst_model_id = {claim.analyst_model_id}")
        analyze_ok = True
    finally:
        cleanup_ok = False
        try:
            await _cleanup(
                sessionmaker,
                company_ids=company_ids,
                artifact_ids=artifact_ids,
            )
            residual = await _residual_counts(
                sessionmaker,
                company_ids=company_ids,
                artifact_ids=artifact_ids,
            )
            cleanup_ok = all(count == 0 for count in residual.values())
            print(f"cleanup_success = {cleanup_ok}")
            if not cleanup_ok:
                print(f"residual_rows = {sum(residual.values())}")
        except Exception as exc:  # noqa: BLE001
            print(f"cleanup_failure = {type(exc).__name__}")
        await manager.dispose()
        shutil.rmtree(smoke_root, ignore_errors=True)
    if analyze_ok and cleanup_ok:
        print("OK: real DeepSeek structured relative valuation analysis smoke passed")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
