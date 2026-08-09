"""Real DeepSeek smoke (stage 4B.1): structured claim analysis — 不持久化业务 Claim。

用途：手动验证真实 DeepSeek V4 Flash 对真实 Evidence Pack 能返回符合
`ClaimAnalysisDecision` schema 的结构化输出（thinking 关闭、temperature=0、
无 tools/web、Evidence 数据定界）。**不调用** `ClaimAnalysisService.analyze`
的持久化路径——本 smoke 只跑"Evidence Pack → 模型 → 结构化决策"这一段，
**不写入任何正式业务 Claim**。

流程：
1. 连真实 PostgreSQL；seed 默认 source registry；
2. 创建 scratch 公司 + 1 张真实 document EvidenceCard（真实 HTML 链：
   SourceRecord → ParsingService → ChunkingService → EvidenceCardService）；
3. 构造 ClaimAnalysisRequest(business) → 加载 Evidence Pack（E1..En 最小投影）；
4. 调 `DeepSeekClaimAnalysisModel.analyze` → 校验返回的 ClaimAnalysisDecision；
5. 打印结果摘要（relevant / claim 数 / statement / refs / reason_code）；
6. 清理：删除 evidence / source 链 / scratch 公司，**0 业务 Claim 残留**。

需要环境变量 `DEEPSEEK_API_KEY`。运行（insightforge Conda 环境）：
    python -m app.cli.smoke_structured_claim_analysis
"""

import asyncio
import shutil
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.analysis.claims.adapters import DeepSeekClaimAnalysisModel
from app.analysis.claims.contracts import (
    ClaimAnalysisContext,
    ClaimAnalysisDecision,
    ClaimAnalysisRequest,
)
from app.analysis.claims.evidence_pack import EvidencePackSource, build_evidence_pack
from app.analysis.claims.strategies import strategy_for_domain
from app.claims.contracts import ClaimAnalysisDomain
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore

# Windows 上 psycopg async 需要 SelectorEventLoop；必须在 asyncio.run 之前设置。
configure_asyncio_runtime()

_QUESTION = "2024年公司海外业务增长情况？"
_HTML = (
    "<html><head><title>年报披露</title></head><body><article>"
    "<p>公司2024年海外业务收入同比增长31.4%，占总收入比重持续提升；"
    "公司表示海外市场是未来收入增长的重要驱动因素，并将继续加大投入。</p>"
    "<p>管理层预计2025年海外收入仍将保持两位数增长，并将进一步拓展东南亚市场。</p>"
    "</article></body></html>"
).encode()
_URL = "https://www.xinhuanet.com/2026/0809/smoke001.htm"
_SOURCE_TITLE = "2024年年度报告"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)


async def _cleanup(
    sessionmaker,
    *,
    company_id,
    artifact_id,
    source_id,
    parsed_source_id,
    chunk_set_id,
    chunk_id,
    evidence_card_id,
) -> None:
    """定向删除本 smoke seed 的链路行（只删 scratch 数据，不动其他数据）。

    按 FK 依赖逆序删除：link → claims → evidence_cards → document_chunks →
    chunk_vector_indexes → chunk_sets → parsed_source_blocks → parsed_sources →
    source_records → raw_artifacts → company_aliases → companies。
    """
    async with sessionmaker() as session:
        await _delete(
            session,
            "DELETE FROM claim_evidence_links WHERE claim_id IN "
            "(SELECT claim_id FROM claims WHERE company_id = :cid)",
            cid=company_id,
        )
        await _delete(session, "DELETE FROM claims WHERE company_id = :cid", cid=company_id)
        await _delete(
            session,
            "DELETE FROM evidence_cards WHERE evidence_card_id = :eid",
            eid=evidence_card_id,
        )
        await _delete(session, "DELETE FROM document_chunks WHERE chunk_id = :kid", kid=chunk_id)
        await _delete(
            session,
            "DELETE FROM chunk_vector_indexes WHERE chunk_set_id = :sid",
            sid=chunk_set_id,
        )
        await _delete(session, "DELETE FROM chunk_sets WHERE chunk_set_id = :sid", sid=chunk_set_id)
        await _delete(
            session,
            "DELETE FROM parsed_source_blocks WHERE parsed_source_id = :pid",
            pid=parsed_source_id,
        )
        await _delete(
            session,
            "DELETE FROM parsed_sources WHERE parsed_source_id = :pid",
            pid=parsed_source_id,
        )
        await _delete(session, "DELETE FROM source_records WHERE source_id = :src", src=source_id)
        await _delete(
            session, "DELETE FROM raw_artifacts WHERE artifact_id = :aid", aid=artifact_id
        )
        await _delete(
            session, "DELETE FROM company_aliases WHERE company_id = :cid", cid=company_id
        )
        await _delete(session, "DELETE FROM companies WHERE company_id = :cid", cid=company_id)
        await session.commit()


async def _delete(session, sql: str, **params) -> None:
    """执行带参数绑定的 DELETE（scoped cleanup 辅助）。"""
    await session.execute(text(sql).bindparams(**params))


async def _seed_document_evidence(sessionmaker, raw_store, company_id) -> dict:
    stored = raw_store.put_html_bytes(_HTML)
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
            source_url=_URL,
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
    parsed_service = SourceParsingService(sessionmaker, raw_store)
    parsed = await parsed_service.parse_source(source_id)
    result = await ChunkingService(sessionmaker).chunk_parsed_source(parsed.parsed_source_id)
    async with sessionmaker() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(result.chunk_set_id)
    assert chunks, "smoke seed must produce chunks"
    chunk = chunks[0]
    draft = EvidenceCardDraft(
        research_question=_QUESTION,
        evidence_statement="2024年公司海外业务收入同比增长31.4%，占总收入比重持续提升。",
        evidence_type=EvidenceType.METRIC,
        chunk_id=chunk.chunk_id,
        quote_start=0,
        quote_end=20,
        extractor_name="smoke-extractor",
        extractor_version=1,
        extractor_model_id="test-model",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    result_card = await EvidenceCardService(sessionmaker).create_card(draft)
    return {
        "evidence_card_id": result_card.evidence_card_id,
        "source_id": source_id,
        "parsed_source_id": parsed.parsed_source_id,
        "chunk_set_id": result.chunk_set_id,
        "chunk_id": chunk.chunk_id,
        "artifact_id": artifact.artifact_id,
    }


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
    company_id = uuid.uuid4()
    smoke_root = Path(tempfile.mkdtemp(prefix="smoke_claim_analysis_"))
    raw_store = LocalRawArtifactStore(root=smoke_root / "raw", max_bytes=1024 * 1024)
    card: dict | None = None
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
                    official_name="Smoke测试公司",
                    short_name="Smoke",
                    listing_status="listed",
                    identity_source_provider_key="sse",
                    identity_source_url="https://www.sse.com.cn",
                )
            )
            await session.commit()
        card = await _seed_document_evidence(sessionmaker, raw_store, company_id)

        request = ClaimAnalysisRequest(
            company_id=company_id,
            research_question=_QUESTION,
            analysis_domain=ClaimAnalysisDomain.BUSINESS,
            evidence_card_ids=[card["evidence_card_id"]],
        )
        # 加载真实 EvidenceCard → 最小投影 Evidence Pack。
        async with sessionmaker() as session:
            row = await session.get(EvidenceCardModel, card["evidence_card_id"])
            assert row is not None
        source = EvidencePackSource.from_model(row)
        pack = build_evidence_pack([source])
        context = ClaimAnalysisContext(
            research_question=request.research_question,
            analysis_domain=request.analysis_domain,
            strategy=strategy_for_domain(request.analysis_domain),
        )
        model = DeepSeekClaimAnalysisModel(settings)
        print(f"model_id = {model.model_id}")
        print(f"evidence pack: {[item.evidence_ref for item in pack.items]}")

        raw = await model.analyze(context, pack)
        decision = ClaimAnalysisDecision.model_validate(raw)
        print(f"relevant = {decision.relevant}")
        if decision.reason_code is not None:
            print(f"reason_code = {decision.reason_code.value}")
        for claim in decision.claims:
            print(
                f"claim: kind={claim.claim_kind.value} "
                f"confidence={claim.confidence.value} "
                f"importance={claim.importance.value} "
                f"supports={claim.support_refs} "
                f"contradicts={claim.contradict_refs} "
                f"context={claim.context_refs} | {claim.statement}"
            )
        print("OK: real DeepSeek structured claim analysis smoke passed")
        return 0
    finally:
        if card is not None:
            await _cleanup(
                sessionmaker,
                company_id=company_id,
                artifact_id=card["artifact_id"],
                source_id=card["source_id"],
                parsed_source_id=card["parsed_source_id"],
                chunk_set_id=card["chunk_set_id"],
                chunk_id=card["chunk_id"],
                evidence_card_id=card["evidence_card_id"],
            )
        else:
            # seed 中途失败：按 scratch 公司作用域清理（source-chain 子查询）。
            cid = str(company_id)
            chain_src = "SELECT source_id FROM source_records WHERE company_id = :cid"
            chain_parsed = (
                f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({chain_src})"
            )
            chain_chunkset = (
                f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
            )
            async with sessionmaker() as session:
                await session.execute(
                    text(
                        "DELETE FROM claim_evidence_links WHERE claim_id IN "
                        "(SELECT claim_id FROM claims WHERE company_id = :cid)"
                    ).bindparams(cid=cid)
                )
                await session.execute(
                    text("DELETE FROM claims WHERE company_id = :cid").bindparams(cid=cid)
                )
                await session.execute(
                    text("DELETE FROM evidence_cards WHERE company_id = :cid").bindparams(cid=cid)
                )
                await session.execute(
                    text(f"DELETE FROM document_chunks WHERE chunk_set_id IN ({chain_chunkset})")
                ).bindparams(cid=cid)
                await session.execute(
                    text(
                        f"DELETE FROM chunk_vector_indexes WHERE chunk_set_id IN ({chain_chunkset})"
                    )
                ).bindparams(cid=cid)
                await session.execute(
                    text(f"DELETE FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})")
                ).bindparams(cid=cid)
                await session.execute(
                    text(
                        "DELETE FROM parsed_source_blocks "
                        f"WHERE parsed_source_id IN ({chain_parsed})"
                    )
                ).bindparams(cid=cid)
                await session.execute(
                    text(f"DELETE FROM parsed_sources WHERE source_id IN ({chain_src})")
                ).bindparams(cid=cid)
                await session.execute(
                    text("DELETE FROM source_records WHERE company_id = :cid").bindparams(cid=cid)
                )
                await session.execute(
                    text("DELETE FROM company_aliases WHERE company_id = :cid").bindparams(cid=cid)
                )
                await session.execute(
                    text("DELETE FROM companies WHERE company_id = :cid").bindparams(cid=cid)
                )
                await session.commit()
        await manager.dispose()
        shutil.rmtree(smoke_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
