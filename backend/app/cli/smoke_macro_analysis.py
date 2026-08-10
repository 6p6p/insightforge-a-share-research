"""Real DeepSeek smoke (stage 4C.1B): structured macro context analysis — 受控一次。

用途：手动验证真实 DeepSeek V4 Flash 对真实 Evidence Pack 能返回符合
`MacroAnalysisDecision` schema 的结构化输出，并走**完整生产链路**：
`MacroAnalysisService.analyze` = 短 DB session 加载校验（driver/company 资格、
availability no-lookahead、future-evidence）→ 关闭 session → 构造 M/E alias →
**生产适配器 `DeepSeekMacroAnalysisModel`**（不直接调用 SDK）→ schema
double-check → macro numeric-literal guard → M/E ref resolution → 构造 v6
MacroClaimDraft → `MacroClaimService.create_claim_batch` 原子持久化（含
analysis_as_of 查询列，Gate 0）。

seed（真实 HTML → SourceRecord → Parsing → Chunking → EvidenceCardService）：
- M1 宏观驱动卡：news_article + event 文档卡（v3 资格），定性货币政策事件；
- E1 公司暴露卡：news_article + statement 文档卡（critical-eligible），定性融资披露。
两卡 evidence_statement / quote_text 刻意不含数字与百分比——保持数字零暴露，
降低模型把数字拷进 statement 触发 numeric guard 的风险（smoke 的目标是验证
guard 成功路径；macro_observation 驱动的数字投影由单元/集成测试覆盖）。

校验：numeric_guard_success（模型 statement 不含任何数字形式/定量短语——ASCII /
full-width digits / % / 中文数字（零〇二两三四五六七八九十百千万亿兆）/ 定量短语
（百分之 倍 翻倍 翻番 过半 半数 一成 一半 一点），违反则整次失败 0 写）、
ref_resolution_success（refs 全部落在 M1/E1，未知/跨 relation 则整次失败 0 写）。
打印 provider / model / latency_ms / relevant / claim_count / created_count /
replayed_count / reason_code / claim_schema_version / transmission_schema_version /
analysis_as_of_persisted / numeric_guard_success / ref_resolution_success /
cleanup_success。

**不记录** API key / 完整 prompt / reasoning_content / raw provider response。
**不写正式业务 Claim**：清理删除 scratch 公司全部 seed 链路（含 smoke 期间创建的
Claims）。cleanup 后实际查询受影响表并打印 cleanup_success；cleanup 失败或残留
非 0 → 不声称成功（退出码 1）。

需要环境变量 `DEEPSEEK_API_KEY`。运行（insightforge Conda 环境）：
    python -m app.cli.smoke_macro_analysis
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

from app.analysis.macro.adapters import DeepSeekMacroAnalysisModel
from app.analysis.macro.contracts import MacroAnalysisRequest
from app.analysis.macro.errors import (
    MacroAnalysisClaimKindPolicy,
    MacroAnalysisEvidenceCompanyMismatch,
    MacroAnalysisEvidenceCorrupted,
    MacroAnalysisEvidenceNotFound,
    MacroAnalysisFutureEvidence,
    MacroAnalysisMalformedOutput,
    MacroAnalysisModelUnavailable,
    MacroAnalysisNumericLiteralForbidden,
    MacroAnalysisOriginViolation,
    MacroAnalysisOverclaimPolicy,
    MacroAnalysisRelationConflict,
    MacroAnalysisTemporalEvidenceInsufficient,
    MacroAnalysisUnknownRef,
)
from app.analysis.macro.service import MacroAnalysisService
from app.claims.macro_contracts import (
    MACRO_CLAIM_SCHEMA_VERSION,
    MACRO_TRANSMISSION_SCHEMA_VERSION,
)
from app.claims.macro_errors import MacroClaimCriticalEvidenceInsufficient
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

# Windows 上 psycopg async 需要 SelectorEventLoop；必须在 asyncio.run 之前设置。
configure_asyncio_runtime()

_QUESTION = "利率上行对贵州茅台融资成本的影响？"
_ANALYSIS_AS_OF = date(2026, 8, 10)
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

# seed HTML / evidence statement 刻意不含数字与百分比：数据是模型输出的素材，
# 保持数字零暴露以降低模型把数字拷进 statement 触发 numeric guard 的风险
# （smoke 的目标是验证 guard 成功路径）。
_DRIVER_HTML = (
    "<html><head><title>货币政策新闻</title></head><body><article>"
    "<p>中国人民银行宣布上调政策利率，市场流动性环境趋于收紧。多家市场机构认为"
    "货币政策取向保持稳健，资金成本中枢存在抬升可能。银行间市场利率随之走高，"
    "企业融资条件边际收紧。</p>"
    "<p>分析人士指出，利率上行环境将推升存量浮动利率债务的利息负担，对依赖"
    "外部融资的企业构成压力。后续走势仍取决于宏观基本面与政策节奏。</p>"
    "</article></body></html>"
).encode()
_DRIVER_STATEMENT = "央行上调政策利率，市场流动性环境趋于收紧。"
_DRIVER_URL = "https://www.xinhuanet.com/2026/0809/smoke_macro_driver.htm"

_COMPANY_HTML = (
    "<html><head><title>公司融资情况披露</title></head><body><article>"
    "<p>公司披露部分借款采用浮动利率计息，利息支出与市场利率变动直接相关。"
    "管理层表示将合理安排债务期限结构，关注利率环境变化对财务费用的影响。</p>"
    "<p>公司称，主要融资渠道保持畅通，融资安排以中长期资金为主，整体负债结构"
    "保持稳定。</p>"
    "</article></body></html>"
).encode()
_COMPANY_STATEMENT = "公司部分借款按浮动利率计息，利息支出与市场利率变动相关。"
_COMPANY_URL = "https://www.xinhuanet.com/2026/0809/smoke_macro_company.htm"


async def _cleanup(
    sessionmaker,
    *,
    company_id: uuid.UUID,
    artifact_ids: list[uuid.UUID],
) -> None:
    """删除 scratch 公司全部 seed 链路（含 smoke 期间创建的 macro Claims），
    只删本 smoke 数据，不动其他数据。按 FK 依赖逆序删除。"""
    cid = company_id
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
                "DELETE FROM macro_transmission_evidence_links WHERE transmission_id IN "
                "(SELECT transmission_id FROM macro_transmission_chains WHERE claim_id IN "
                "(SELECT claim_id FROM claims WHERE company_id = :cid))"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(
                "DELETE FROM macro_transmission_chains WHERE claim_id IN "
                "(SELECT claim_id FROM claims WHERE company_id = :cid)"
            ).bindparams(cid=cid)
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
            text(
                f"DELETE FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(
                f"DELETE FROM parsed_source_blocks WHERE parsed_source_id IN ({chain_parsed})"
            ).bindparams(cid=cid)
        )
        await session.execute(
            text(f"DELETE FROM parsed_sources WHERE source_id IN ({chain_src})").bindparams(
                cid=cid
            )
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
    """实际查询 scratch company 在受影响表中的残留行数（不猜测、不声称 0 残留）。

    FK 依赖保证子表不能独立于父行存在，因此逐表查询后 `all(count == 0)` 即真实的
    0 残留验证；链路表（macro_transmission_* / claim_*_links / source_records /
    parsed_sources / chunk_*）用 company_id 或 FK 子查询限定到 scratch company
    的父行；raw_artifacts 按 artifact_id 精确查询。
    """
    cid = company_id
    chain_src = "SELECT source_id FROM source_records WHERE company_id = :cid"
    chain_parsed = f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({chain_src})"
    chain_chunkset = (
        f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
    )
    scoped: dict[str, str] = {
        "companies": "SELECT count(*) FROM companies WHERE company_id = :cid",
        "company_aliases": "SELECT count(*) FROM company_aliases WHERE company_id = :cid",
        "claims": "SELECT count(*) FROM claims WHERE company_id = :cid",
        "claim_evidence_links": (
            "SELECT count(*) FROM claim_evidence_links WHERE claim_id IN "
            "(SELECT claim_id FROM claims WHERE company_id = :cid)"
        ),
        "macro_transmission_chains": (
            "SELECT count(*) FROM macro_transmission_chains WHERE claim_id IN "
            "(SELECT claim_id FROM claims WHERE company_id = :cid)"
        ),
        "macro_transmission_evidence_links": (
            "SELECT count(*) FROM macro_transmission_evidence_links WHERE transmission_id IN "
            "(SELECT transmission_id FROM macro_transmission_chains WHERE claim_id IN "
            "(SELECT claim_id FROM claims WHERE company_id = :cid))"
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
    EvidenceCardService）→ 1 张 news_article document EvidenceCard。
    两卡都 critical-eligible=True（v6 critical policy 需 eligible 双腿；
    模型可能输出 critical，受控 smoke 不因此失败）。"""
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
    smoke_root = Path(tempfile.mkdtemp(prefix="smoke_macro_analysis_"))
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

        driver_card = await _seed_document_card(
            sessionmaker,
            raw_store,
            company_id=company_id,
            html=_DRIVER_HTML,
            statement=_DRIVER_STATEMENT,
            evidence_type=EvidenceType.EVENT,
            source_url=_DRIVER_URL,
        )
        company_card = await _seed_document_card(
            sessionmaker,
            raw_store,
            company_id=company_id,
            html=_COMPANY_HTML,
            statement=_COMPANY_STATEMENT,
            evidence_type=EvidenceType.STATEMENT,
            source_url=_COMPANY_URL,
        )
        cards = [driver_card, company_card]

        request = MacroAnalysisRequest(
            company_id=company_id,
            research_question=_QUESTION,
            analysis_as_of=_ANALYSIS_AS_OF,
            macro_driver_evidence_ids=[driver_card["evidence_card_id"]],
            company_evidence_ids=[company_card["evidence_card_id"]],
        )
        model = DeepSeekMacroAnalysisModel(settings)
        service = MacroAnalysisService(sessionmaker, model)
        print(f"provider = {settings.llm_provider}")
        print(f"model = {model.model_id}")

        start = time.perf_counter()
        try:
            result = await service.analyze(request)
        except MacroAnalysisNumericLiteralForbidden:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = False")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model returned a statement containing numeric literals")
            return 1
        except MacroAnalysisClaimKindPolicy:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = True")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model returned a claim_kind outside inference/risk")
            return 1
        except MacroAnalysisUnknownRef:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = True")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model returned refs outside M1/E1")
            return 1
        except MacroAnalysisRelationConflict:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = True")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model used the same ref across conflicting relations")
            return 1
        except MacroAnalysisOverclaimPolicy:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = True")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model violated observed_impact/uncertain overclaim contract")
            return 1
        except MacroClaimCriticalEvidenceInsufficient:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = True")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: critical claim without eligible evidence legs")
            return 1
        except MacroAnalysisMalformedOutput:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = False")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model output could not be parsed into MacroAnalysisDecision")
            return 1
        except MacroAnalysisModelUnavailable:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = False")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: DeepSeek provider/model unavailable")
            return 1
        except (
            MacroAnalysisEvidenceNotFound,
            MacroAnalysisEvidenceCompanyMismatch,
            MacroAnalysisEvidenceCorrupted,
            MacroAnalysisOriginViolation,
            MacroAnalysisTemporalEvidenceInsufficient,
            MacroAnalysisFutureEvidence,
        ) as exc:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = False")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print(f"FAIL: evidence loading/validation ({type(exc).__name__})")
            return 1
        elapsed_ms = int((time.perf_counter() - start) * 1000)

        print(f"latency_ms = {elapsed_ms}")
        print(f"relevant = {result.relevant}")
        print(f"claim_count = {result.created_count + result.replayed_count}")
        print(f"created_count = {result.created_count}")
        print(f"replayed_count = {result.replayed_count}")
        reason = result.reason_code.value if result.reason_code is not None else None
        print(f"reason_code = {reason}")
        print("numeric_guard_success = True")
        print("ref_resolution_success = True")
        kinds: list[str] = []
        chain_row = None
        for claim_id in result.claim_ids:
            async with sessionmaker() as session:
                claim = await ClaimRepository(session).get_by_id(claim_id)
                if claim is not None:
                    kinds.append(claim.claim_kind)
                    print(
                        f"claim: kind={claim.claim_kind} confidence={claim.confidence} "
                        f"importance={claim.importance} | {claim.statement}"
                    )
                    # Gate 0：analysis_as_of 作为查询列持久化。
                    chain_row = (
                        await session.execute(
                            text(
                                "SELECT claim_schema_version, analysis_as_of FROM claims c "
                                "JOIN macro_transmission_chains t ON t.claim_id = c.claim_id "
                                "WHERE c.claim_id = :cid"
                            ).bindparams(cid=claim_id)
                        )
                    ).first()
        print(f"claim_schema_version = {MACRO_CLAIM_SCHEMA_VERSION}")
        print(f"transmission_schema_version = {MACRO_TRANSMISSION_SCHEMA_VERSION}")
        if chain_row is not None:
            print(f"analysis_as_of_persisted = {chain_row.analysis_as_of}")
        print(f"claim_kinds = {kinds}")
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
        print("OK: real DeepSeek structured macro context analysis smoke passed")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
