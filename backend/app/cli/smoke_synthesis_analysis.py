"""Real DeepSeek smoke (stage 4D.1B): structured claim synthesis — 受控一次。

用途：手动验证真实 DeepSeek V4 Flash 对一组已验证 Claim 的 Claim Pack 能返回符合
`SynthesisAnalysisOutput` schema 的结构化综合（themes / claim_roles / conflicts /
evidence gaps），并走**完整生产链路**：`SynthesisAnalysisService.analyze` =
短 DB session 加载 SynthesisRun + input links + **ClaimIntegrityGateway** 完整性
校验（generic/financial/macro/valuation dispatch）→ 关闭 session → 构造
deterministic Claim Pack（C alias，LLM 永不看 UUID）→ **生产适配器
`DeepSeekSynthesisAnalysisModel`**（不直接调用 SDK）→ strict validation
（no-cherry-picking 硬边界）→ compute fingerprint → create_or_get result 原子持久化。

seed（真实 HTML → SourceRecord → Parsing → Chunking → EvidenceCardService）：
- 3 张 document EvidenceCard（business/event/risk 各驱动一条 generic v1 Claim），
  evidence_statement 与 claim statement 语义对应（营收增长 / 经营事件 / 风险）；
- 3 条 Claim 经 ClaimService.create_claim 登记 → SynthesisService 登记 1 个
  SynthesisRun（2..50 条 input，no-lookahead + gateway 校验）。
claim pack C alias 按 analysis_domain + claim_id canonical 排序：
C1=business、C2=event、C3=risk。

校验：no_cherry_picking_success（LLM claim_roles 恰好覆盖 C1..C3 各一次，违反 →
整次失败 0 写）、strict_validation_success（全部 C refs 已知 / 文本非空）。
打印 provider / model / latency_ms / claim_count / created_count / replayed /
result_fingerprint / themes_count / conflicts_count / evidence_gaps_count /
cleanup_success。

**不记录** API key / 完整 prompt / reasoning_content / raw provider response。
**不写正式业务数据**：清理删除 scratch 公司全部 seed 链路（含 synthesis run /
result / claims）。cleanup 后实际查询受影响表并打印 cleanup_success；cleanup
失败或残留非 0 → 不声称成功（退出码 1）。

需要环境变量 `DEEPSEEK_API_KEY`。运行（insightforge Conda 环境）：
    python -m app.cli.smoke_synthesis_analysis
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

from app.analysis.synthesis.contracts import (
    SYNTHESIS_RESULT_SCHEMA_VERSION,
    SynthesisAnalysisRequest,
)
from app.analysis.synthesis.errors import (
    SynthesisAnalysisMalformedOutput,
    SynthesisAnalysisModelUnavailable,
    SynthesisAnalysisNoCherryPicking,
    SynthesisAnalysisRunNotFound,
    SynthesisAnalysisUnknownRef,
)
from app.analysis.synthesis.factory import create_synthesis_analysis_model
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimDraft,
    ClaimImportance,
    ClaimKind,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
)
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.claim_service import ClaimService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.contracts import SynthesisInputDraft
from app.synthesis.service import SynthesisService

# Windows 上 psycopg async 需要 SelectorEventLoop；必须在 asyncio.run 之前设置。
configure_asyncio_runtime()

_QUESTION = "贵州茅台2026年营收增长、经营事件与风险是否协调一致？"
_ANALYSIS_AS_OF = date(2026, 8, 10)
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

_SEED_ROWS = [
    (
        "https://www.xinhuanet.com/2026/0809/smoke_synthesis_revenue.htm",
        "<html><head><title>营收增长</title></head><body><article><p>公司披露2026年"
        "上半年营业收入实现增长，主要来自核心产品量价齐升。管理层表示市场需求保持"
        "稳健，渠道动销正常，全年经营目标不变。</p></article></body></html>",
        "贵州茅台2026年上半年营业收入实现同比增长。",
        ClaimAnalysisDomain.BUSINESS,
        ClaimKind.FACT,
        EvidenceType.METRIC,
    ),
    (
        "https://www.xinhuanet.com/2026/0809/smoke_synthesis_event.htm",
        "<html><head><title>经营事件</title></head><body><article><p>公司公告其核心"
        "产品的市场指导价格保持不变，本轮渠道政策调整重点在于理顺批发环节价差，"
        "不涉及出厂价格变动。相关调整已于近期落地执行。</p></article></body></html>",
        "公司近期调整了核心产品渠道政策，理顺批发环节价差。",
        ClaimAnalysisDomain.EVENT,
        ClaimKind.FACT,
        EvidenceType.EVENT,
    ),
    (
        "https://www.xinhuanet.com/2026/0809/smoke_synthesis_risk.htm",
        "<html><head><title>经营风险</title></head><body><article><p>行业环境方面，"
        "高端白酒消费景气度存在不确定性，部分区域批价波动加剧。公司提示若需求端"
        "持续走弱，可能对短期动销与价格体系形成压力。</p></article></body></html>",
        "高端白酒消费景气度存在不确定性，可能对公司短期动销形成压力。",
        ClaimAnalysisDomain.RISK,
        ClaimKind.RISK,
        EvidenceType.STATEMENT,
    ),
]


async def _cleanup(sessionmaker, *, company_id: uuid.UUID, artifact_ids: list[uuid.UUID]) -> None:
    """删除 scratch 公司全部 seed 链路（含 smoke 期间创建的 synthesis run / result /
    claims / cards / source 链），只删本 smoke 数据，不动其他数据。按 FK 依赖逆序删除。"""
    cid = company_id
    chain_src = "SELECT source_id FROM source_records WHERE company_id = :cid"
    chain_parsed = f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({chain_src})"
    chain_chunkset = (
        f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
    )
    async with sessionmaker() as session:
        await session.execute(
            text(
                "DELETE FROM claim_synthesis_results WHERE synthesis_id IN "
                "(SELECT synthesis_id FROM claim_synthesis_runs WHERE company_id = :cid)"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(
                "DELETE FROM claim_synthesis_input_links WHERE synthesis_id IN "
                "(SELECT synthesis_id FROM claim_synthesis_runs WHERE company_id = :cid)"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text("DELETE FROM claim_synthesis_runs WHERE company_id = :cid").bindparams(cid=cid)
        )
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
            text(
                f"DELETE FROM document_chunks WHERE chunk_set_id IN ({chain_chunkset})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(
                f"DELETE FROM chunk_vector_indexes WHERE chunk_set_id IN ({chain_chunkset})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(f"DELETE FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})").bindparams(
                cid=cid
            )
        )
        await session.execute(
            text(
                f"DELETE FROM parsed_source_blocks WHERE parsed_source_id IN ({chain_parsed})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(f"DELETE FROM parsed_sources WHERE source_id IN ({chain_src})").bindparams(cid=cid)
        )
        await session.execute(
            text("DELETE FROM source_records WHERE company_id = :cid").bindparams(cid=cid)
        )
        for aid in artifact_ids:
            await session.execute(
                text("DELETE FROM raw_artifacts WHERE artifact_id = :aid").bindparams(aid=aid)
            )
        await session.execute(
            text("DELETE FROM company_aliases WHERE company_id = :cid").bindparams(cid=cid)
        )
        await session.execute(
            text("DELETE FROM companies WHERE company_id = :cid").bindparams(cid=cid)
        )
        await session.commit()


async def _residual_counts(
    sessionmaker,
    *,
    company_id: uuid.UUID,
    artifact_ids: list[uuid.UUID],
) -> dict[str, int]:
    """实际查询 scratch company 在受影响表中的残留行数（不猜测、不声称 0 残留）。"""
    cid = company_id
    chain_src = "SELECT source_id FROM source_records WHERE company_id = :cid"
    chain_parsed = f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({chain_src})"
    chain_chunkset = (
        f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
    )
    scoped: dict[str, str] = {
        "companies": "SELECT count(*) FROM companies WHERE company_id = :cid",
        "company_aliases": "SELECT count(*) FROM company_aliases WHERE company_id = :cid",
        "claim_synthesis_runs": (
            "SELECT count(*) FROM claim_synthesis_runs WHERE company_id = :cid"
        ),
        "claim_synthesis_input_links": (
            "SELECT count(*) FROM claim_synthesis_input_links WHERE synthesis_id IN "
            "(SELECT synthesis_id FROM claim_synthesis_runs WHERE company_id = :cid)"
        ),
        "claim_synthesis_results": (
            "SELECT count(*) FROM claim_synthesis_results WHERE synthesis_id IN "
            "(SELECT synthesis_id FROM claim_synthesis_runs WHERE company_id = :cid)"
        ),
        "claims": "SELECT count(*) FROM claims WHERE company_id = :cid",
        "claim_evidence_links": (
            "SELECT count(*) FROM claim_evidence_links WHERE claim_id IN "
            "(SELECT claim_id FROM claims WHERE company_id = :cid)"
        ),
        "evidence_cards": "SELECT count(*) FROM evidence_cards WHERE company_id = :cid",
        "source_records": "SELECT count(*) FROM source_records WHERE company_id = :cid",
        "parsed_sources": (
            "SELECT count(*) FROM parsed_sources WHERE source_id IN (" + chain_src + ")"
        ),
        "parsed_source_blocks": (
            "SELECT count(*) FROM parsed_source_blocks WHERE parsed_source_id IN ("
            + chain_parsed
            + ")"
        ),
        "chunk_sets": (
            "SELECT count(*) FROM chunk_sets WHERE parsed_source_id IN (" + chain_parsed + ")"
        ),
        "document_chunks": (
            "SELECT count(*) FROM document_chunks WHERE chunk_set_id IN (" + chain_chunkset + ")"
        ),
        "chunk_vector_indexes": (
            "SELECT count(*) FROM chunk_vector_indexes WHERE chunk_set_id IN ("
            + chain_chunkset
            + ")"
        ),
    }
    async with sessionmaker() as session:
        counts: dict[str, int] = {}
        for table, sql in scoped.items():
            counts[table] = (await session.execute(text(sql).bindparams(cid=cid))).scalar_one()
        for aid in artifact_ids:
            counts[f"raw_artifacts:{aid.hex[:8]}"] = (
                await session.execute(
                    text("SELECT count(*) FROM raw_artifacts WHERE artifact_id = :aid").bindparams(
                        aid=aid
                    )
                )
            ).scalar_one()
        return counts


async def _seed_document_card(
    sessionmaker,
    raw_store,
    *,
    company_id: uuid.UUID,
    html: bytes,
    statement: str,
    evidence_type: EvidenceType,
    source_url: str,
) -> dict:
    """真实 HTML 链（RawArtifact → SourceRecord → Parsing → Chunking →
    EvidenceCardService）→ 1 张 news_article document EvidenceCard。"""
    stored = raw_store.put_html_bytes(html)
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
            title="新闻标题",
            published_at=_PUBLISHED_AT,
            reporting_period_end=None,
            source_url=source_url,
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=1,
            critical_claim_eligible_snapshot=True,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
        artifact_id = artifact.artifact_id
    parsed_service = SourceParsingService(sessionmaker, raw_store)
    parsed = await parsed_service.parse_source(source_id)
    result = await ChunkingService(sessionmaker).chunk_parsed_source(parsed.parsed_source_id)
    async with sessionmaker() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(result.chunk_set_id)
    assert chunks, "smoke seed must produce chunks"
    chunk = chunks[0]
    draft = EvidenceCardDraft(
        research_question=_QUESTION,
        evidence_statement=statement,
        evidence_type=evidence_type,
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
        "artifact_id": artifact_id,
        "source_id": source_id,
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
    smoke_root = Path(tempfile.mkdtemp(prefix="smoke_synthesis_analysis_"))
    raw_store = LocalRawArtifactStore(root=smoke_root / "raw", max_bytes=1024 * 1024)
    cards: list[dict] = []
    analyze_ok = False
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

        card_docs = [
            await _seed_document_card(
                sessionmaker,
                raw_store,
                company_id=company_id,
                html=html.encode(),
                statement=statement,
                evidence_type=evidence_type,
                source_url=url,
            )
            for url, html, statement, _, _, evidence_type in _SEED_ROWS
        ]
        cards = card_docs

        # 3 条 generic Claim（business/event/risk）→ 1 个 SynthesisRun。
        claim_ids: list[uuid.UUID] = []
        for card_doc, (_, _, statement, domain, kind, _) in zip(card_docs, _SEED_ROWS, strict=True):
            result = await ClaimService(sessionmaker).create_claim(
                ClaimDraft(
                    company_id=company_id,
                    research_question=_QUESTION,
                    statement=statement,
                    analysis_domain=domain,
                    claim_kind=kind,
                    confidence=ClaimConfidence.HIGH,
                    importance=ClaimImportance.NORMAL,
                    support_evidence_ids=[card_doc["evidence_card_id"]],
                    contradict_evidence_ids=[],
                    context_evidence_ids=[],
                    analyst_name="smoke-synthesis-seeder",
                    analyst_version=1,
                    analyst_model_id="deepseek:deepseek-v4-flash",
                )
            )
            claim_ids.append(result.claim_id)

        run = await SynthesisService(sessionmaker).create_or_get_synthesis(
            SynthesisInputDraft(
                company_id=company_id,
                research_question=_QUESTION,
                analysis_as_of=_ANALYSIS_AS_OF,
                claim_ids=claim_ids,
            )
        )

        # 生产链路：Settings → factory → DeepSeekSynthesisAnalysisModel → Service。
        model = create_synthesis_analysis_model(settings)
        service = SynthesisAnalysisService(sessionmaker, model)
        print(f"provider = {settings.llm_provider}")
        print(f"model = {model.model_id}")

        start = time.perf_counter()
        try:
            result = await service.analyze(SynthesisAnalysisRequest(synthesis_id=run.synthesis_id))
        except SynthesisAnalysisNoCherryPicking:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("no_cherry_picking_success = False")
            print("strict_validation_success = False")
            print("claim_count = 0")
            print("FAIL: model did not cover every input claim exactly once")
            return 1
        except SynthesisAnalysisUnknownRef:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("no_cherry_picking_success = False")
            print("strict_validation_success = False")
            print("claim_count = 0")
            print("FAIL: model output referenced an unknown C alias")
            return 1
        except SynthesisAnalysisMalformedOutput:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("no_cherry_picking_success = False")
            print("strict_validation_success = False")
            print("claim_count = 0")
            print("FAIL: model output could not be parsed into SynthesisAnalysisOutput")
            return 1
        except SynthesisAnalysisModelUnavailable:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("no_cherry_picking_success = False")
            print("strict_validation_success = False")
            print("claim_count = 0")
            print("FAIL: DeepSeek provider/model unavailable")
            return 1
        except SynthesisAnalysisRunNotFound:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("no_cherry_picking_success = False")
            print("strict_validation_success = False")
            print("claim_count = 0")
            print("FAIL: synthesis run not found")
            return 1
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        print(f"latency_ms = {elapsed_ms}")
        print(f"claim_count = {result.claim_count}")
        print(f"created_count = {0 if result.replayed else 1}")
        print(f"replayed = {result.replayed}")
        print(f"result_fingerprint = {result.result_fingerprint}")
        print(f"result_schema_version = {SYNTHESIS_RESULT_SCHEMA_VERSION}")
        print("no_cherry_picking_success = True")
        print("strict_validation_success = True")

        async with sessionmaker() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT jsonb_array_length(themes) AS themes_count, "
                        "jsonb_array_length(conflicts) AS conflicts_count, "
                        "jsonb_array_length(evidence_gaps) AS evidence_gaps_count, "
                        "analyst_name, analyst_version, analyst_model_id "
                        "FROM claim_synthesis_results "
                        "WHERE synthesis_result_id = :rid"
                    ).bindparams(rid=result.synthesis_result_id)
                )
            ).first()
        if row is not None:
            print(f"themes_count = {row.themes_count}")
            print(f"conflicts_count = {row.conflicts_count}")
            print(f"evidence_gaps_count = {row.evidence_gaps_count}")
            print(f"persisted_analyst_name = {row.analyst_name}")
            print(f"persisted_analyst_version = {row.analyst_version}")
            print(f"persisted_analyst_model_id = {row.analyst_model_id}")
        analyze_ok = True
    finally:
        artifact_ids = [card["artifact_id"] for card in cards]
        cleanup_ok = False
        try:
            await _cleanup(
                sessionmaker,
                company_id=company_id,
                artifact_ids=artifact_ids,
            )
            residual = await _residual_counts(
                sessionmaker,
                company_id=company_id,
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
        print("OK: real DeepSeek structured claim synthesis smoke passed")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
