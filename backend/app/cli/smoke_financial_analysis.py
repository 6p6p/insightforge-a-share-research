"""Real DeepSeek smoke (stage 4B.2C.2): structured financial analysis — 受控一次。

用途：手动验证真实 DeepSeek V4 Flash 对真实 Calculation Pack 能返回符合
`FinancialAnalysisDecision` schema 的结构化输出，并走**完整生产链路**：
`FinancialAnalysisService.analyze` = 短 DB session 加载校验（verify_calculation_
integrity / inputs→Observations）→ 关闭 session → 构造 C/E alias → **生产适配器
`DeepSeekFinancialAnalysisModel`**（不直接调用 SDK）→ schema double-check →
numeric-literal guard → ref resolution → 构造 v3 FinancialClaimDraft →
`FinancialClaimService.create_claim_batch` 原子持久化。

seed（真实 HTML 链 → Observation → Calculation）：
- C1 yoy_growth_rate = 0.2 → 展示 "20.00%"（revenue 2024=12000000000 /
  revenue 2023=10000000000）；
- C2 operating_margin = 0.15 → 展示 "15.00%"（operating_profit 2024=1800000000 /
  revenue 2024=12000000000，共享 revenue 2024 obs）。

校验：numeric_guard_success（模型 statement 不含任何数字形式/定量短语——ASCII /
full-width digits / % / 中文数字（零〇二两三四五六七八九十百千万亿兆）/ 定量短语
（百分之 倍 翻倍 翻番 过半 半数 一成 一半 一点），违反则整次失败 0 写）、
ref_resolution_success（refs 全部落在 C1/C2，未知/跨 relation 则整次失败 0 写）。
打印 provider / model / latency_ms / claim_count / claim_kinds /
numeric_guard_success / ref_resolution_success / cleanup_success。

**不记录** API key / 完整 prompt / reasoning_content / raw provider response。
**不写正式业务 Claim**：清理删除 scratch 公司全部 seed 链路（含 smoke 期间创建的
Claims）。cleanup 后实际查询受影响表并打印 cleanup_success；cleanup 失败或残留
非 0 → 不声称成功（退出码 1）。

需要环境变量 `DEEPSEEK_API_KEY`。运行（insightforge Conda 环境）：
    python -m app.cli.smoke_financial_analysis
"""

import asyncio
import shutil
import sys
import tempfile
import time
import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from sqlalchemy import text

from app.analysis.financial.adapters import DeepSeekFinancialAnalysisModel
from app.analysis.financial.contracts import FinancialAnalysisRequest
from app.analysis.financial.errors import (
    FinancialAnalysisClaimKindPolicy,
    FinancialAnalysisMalformedOutput,
    FinancialAnalysisModelUnavailable,
    FinancialAnalysisNumericLiteralForbidden,
    FinancialAnalysisRelationConflict,
    FinancialAnalysisUnknownRef,
)
from app.analysis.financial.service import FinancialAnalysisService
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.financial_calculation import FinancialCalculationModel
from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
from app.financial.calculations.contracts import (
    CalculationCode,
    FinancialCalculationDraft,
    InputRole,
)
from app.financial.calculations.service import FinancialCalculationService
from app.financial.contracts import FINANCIAL_METRIC_SCHEMA_VERSION, compute_metric_fingerprint
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

_QUESTION = "公司的经营表现如何？"
# seed HTML / evidence statement 刻意不含数字与百分比：数据是模型输出的素材，
# 保持数字零暴露以降低模型把数字拷贝进 statement 触发 numeric guard 的风险
# （smoke 的目标是验证 guard 成功路径）。
_HTML = (
    "<html><head><title>2024年年度报告经营情况</title></head><body><article>"
    "<p>报告期内公司营业收入保持稳健增长，经营盈利能力持续提升，主要产品市场份额进一步巩固。</p>"
    "<p>管理层表示将继续深化渠道建设与产品创新，保障主业高质量发展。</p>"
    "</article></body></html>"
).encode()
_URL = "https://www.xinhuanet.com/2026/0809/smoke_financial.htm"
_SOURCE_TITLE = "2024年年度报告"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_EVIDENCE_STATEMENT = "报告期内公司营业收入保持稳健增长，经营盈利能力持续提升。"


def _display_value(result_value: Decimal, result_unit: str) -> str:
    """与 `app.analysis.financial.packs._display_value` 相同的确定性展示值
    （ratio → "20.00%" 用 ROUND_HALF_EVEN；cny → "<canonical> CNY"）。"""
    if result_unit == "ratio":
        percent = (result_value * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
        return f"{percent}%"
    return f"{result_value} CNY"


async def _cleanup(
    sessionmaker,
    *,
    company_id: uuid.UUID,
    artifact_id: uuid.UUID | None,
) -> None:
    """删除 scratch 公司全部 seed 链路（含 smoke 期间创建的 financial Claims），
    只删本 smoke 数据，不动其他数据。按 FK 依赖逆序删除。"""
    async with sessionmaker() as session:
        await session.execute(
            text(
                "DELETE FROM claim_financial_calculation_links WHERE claim_id IN "
                "(SELECT claim_id FROM claims WHERE company_id = :cid)"
            ).bindparams(cid=company_id)
        )
        await session.execute(
            text(
                "DELETE FROM claim_evidence_links WHERE claim_id IN "
                "(SELECT claim_id FROM claims WHERE company_id = :cid)"
            ).bindparams(cid=company_id)
        )
        await session.execute(
            text("DELETE FROM claims WHERE company_id = :cid").bindparams(cid=company_id)
        )
        await session.execute(
            text(
                "DELETE FROM financial_calculation_inputs WHERE calculation_id IN "
                "(SELECT calculation_id FROM financial_calculations WHERE company_id = :cid)"
            ).bindparams(cid=company_id)
        )
        await session.execute(
            text("DELETE FROM financial_calculations WHERE company_id = :cid").bindparams(
                cid=company_id
            )
        )
        await session.execute(
            text("DELETE FROM financial_metric_observations WHERE company_id = :cid").bindparams(
                cid=company_id
            )
        )
        await session.execute(
            text("DELETE FROM evidence_cards WHERE company_id = :cid").bindparams(cid=company_id)
        )
        chain_src = "SELECT source_id FROM source_records WHERE company_id = :cid"
        chain_parsed = (
            f"SELECT parsed_source_id FROM parsed_sources WHERE source_id IN ({chain_src})"
        )
        chain_chunkset = (
            f"SELECT chunk_set_id FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})"
        )
        await session.execute(
            text(
                f"DELETE FROM document_chunks WHERE chunk_set_id IN ({chain_chunkset})"
            ).bindparams(cid=company_id)
        )
        await session.execute(
            text(
                f"DELETE FROM chunk_vector_indexes WHERE chunk_set_id IN ({chain_chunkset})"
            ).bindparams(cid=company_id)
        )
        await session.execute(
            text(f"DELETE FROM chunk_sets WHERE parsed_source_id IN ({chain_parsed})").bindparams(
                cid=company_id
            )
        )
        await session.execute(
            text(
                f"DELETE FROM parsed_source_blocks WHERE parsed_source_id IN ({chain_parsed})"
            ).bindparams(cid=company_id)
        )
        await session.execute(
            text(f"DELETE FROM parsed_sources WHERE source_id IN ({chain_src})").bindparams(
                cid=company_id
            )
        )
        await session.execute(
            text("DELETE FROM source_records WHERE company_id = :cid").bindparams(cid=company_id)
        )
        if artifact_id is not None:
            await session.execute(
                text("DELETE FROM raw_artifacts WHERE artifact_id = :aid").bindparams(
                    aid=artifact_id
                )
            )
        await session.execute(
            text("DELETE FROM company_aliases WHERE company_id = :cid").bindparams(cid=company_id)
        )
        await session.execute(
            text("DELETE FROM companies WHERE company_id = :cid").bindparams(cid=company_id)
        )
        await session.commit()


async def _residual_counts(
    sessionmaker,
    *,
    company_id: uuid.UUID,
    artifact_id: uuid.UUID | None,
) -> dict[str, int]:
    """实际查询 scratch company 在受影响表中的残留行数（不猜测、不声称 0 残留）。

    FK 依赖保证子表不能独立于父行存在，因此逐表查询后 `all(count == 0)` 即真实的
    0 残留验证；链路表（claim_*_links / financial_calculation_inputs /
    source_records / parsed_sources / chunk_*）用 company_id 或 FK 子查询限定到
    scratch company 的父行；raw_artifacts 按 artifact_id 精确查询。
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
        "claim_financial_calculation_links": (
            "SELECT count(*) FROM claim_financial_calculation_links WHERE claim_id IN "
            "(SELECT claim_id FROM claims WHERE company_id = :cid)"
        ),
        "financial_calculations": (
            "SELECT count(*) FROM financial_calculations WHERE company_id = :cid"
        ),
        "financial_calculation_inputs": (
            "SELECT count(*) FROM financial_calculation_inputs WHERE calculation_id IN "
            "(SELECT calculation_id FROM financial_calculations WHERE company_id = :cid)"
        ),
        "financial_metric_observations": (
            "SELECT count(*) FROM financial_metric_observations WHERE company_id = :cid"
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
        if artifact_id is not None:
            counts["raw_artifacts"] = (
                await session.execute(
                    text("SELECT count(*) FROM raw_artifacts WHERE artifact_id = :aid").bindparams(
                        aid=artifact_id
                    )
                )
            ).scalar_one()
        return counts


async def _seed_document_evidence(sessionmaker, raw_store, company_id) -> dict:
    """真实 HTML 链（RawArtifact → SourceRecord → Parsing → Chunking →
    EvidenceCardService）→ 1 张 document EvidenceCard。"""
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
        evidence_statement=_EVIDENCE_STATEMENT,
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


async def _insert_observation(
    sessionmaker,
    *,
    company_id: uuid.UUID,
    card_id: uuid.UUID,
    metric_code: str,
    normalized: str,
    period_start: date,
    period_end: date,
) -> uuid.UUID:
    """直接插入一行满足全部 CK 约束的 FinancialMetricObservation（fingerprint 用
    生产函数生成；镜像 migration 0020 guard 的 seed 模式）。"""
    fingerprint = compute_metric_fingerprint(
        metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
        company_id=company_id,
        source_evidence_card_id=card_id,
        metric_code=metric_code,
        statement_scope="consolidated",
        period_start=period_start,
        period_end=period_end,
        period_kind="duration",
        source_value_text="123",
        raw_value=Decimal(normalized),
        raw_unit="yuan",
        normalized_value_cny=Decimal(normalized),
    )
    obs_id = uuid.uuid4()
    async with sessionmaker() as session:
        session.add(
            FinancialMetricObservationModel(
                metric_observation_id=obs_id,
                company_id=company_id,
                source_evidence_card_id=card_id,
                metric_code=metric_code,
                statement_scope="consolidated",
                period_start=period_start,
                period_end=period_end,
                period_kind="duration",
                source_value_text="123",
                raw_value=Decimal(normalized),
                raw_unit="yuan",
                normalized_value_cny=Decimal(normalized),
                metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
                metric_fingerprint=fingerprint,
            )
        )
        await session.commit()
    return obs_id


async def _create_calculation(
    sessionmaker, *, company_id: uuid.UUID, code: CalculationCode, inputs: dict
):
    draft = FinancialCalculationDraft(
        company_id=company_id,
        calculation_code=code,
        input_observation_ids=inputs,
    )
    return await FinancialCalculationService(sessionmaker).create_calculation(draft)


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
    smoke_root = Path(tempfile.mkdtemp(prefix="smoke_financial_analysis_"))
    raw_store = LocalRawArtifactStore(root=smoke_root / "raw", max_bytes=1024 * 1024)
    card: dict | None = None
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
        card = await _seed_document_evidence(sessionmaker, raw_store, company_id)

        # Observations（C1/C2 共享 revenue 2024）。
        revenue_2024 = await _insert_observation(
            sessionmaker,
            company_id=company_id,
            card_id=card["evidence_card_id"],
            metric_code="revenue",
            normalized="12000000000",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
        )
        revenue_2023 = await _insert_observation(
            sessionmaker,
            company_id=company_id,
            card_id=card["evidence_card_id"],
            metric_code="revenue",
            normalized="10000000000",
            period_start=date(2023, 1, 1),
            period_end=date(2023, 12, 31),
        )
        operating_profit_2024 = await _insert_observation(
            sessionmaker,
            company_id=company_id,
            card_id=card["evidence_card_id"],
            metric_code="operating_profit",
            normalized="1800000000",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
        )

        # Calculations：C1 yoy_growth_rate = 0.2 → "20.00%"，C2 operating_margin
        # = 0.15 → "15.00%"。
        calc1 = await _create_calculation(
            sessionmaker,
            company_id=company_id,
            code=CalculationCode.YOY_GROWTH_RATE,
            inputs={InputRole.CURRENT: revenue_2024, InputRole.BASELINE: revenue_2023},
        )
        calc2 = await _create_calculation(
            sessionmaker,
            company_id=company_id,
            code=CalculationCode.OPERATING_MARGIN,
            inputs={
                InputRole.REVENUE: revenue_2024,
                InputRole.OPERATING_PROFIT: operating_profit_2024,
            },
        )
        async with sessionmaker() as session:
            row1 = await session.get(FinancialCalculationModel, calc1.calculation_id)
            row2 = await session.get(FinancialCalculationModel, calc2.calculation_id)
        assert row1 is not None and row2 is not None
        print(
            f"C1 {row1.calculation_code} result_value={row1.result_value} "
            f"display={_display_value(row1.result_value, row1.result_unit)}"
        )
        print(
            f"C2 {row2.calculation_code} result_value={row2.result_value} "
            f"display={_display_value(row2.result_value, row2.result_unit)}"
        )

        request = FinancialAnalysisRequest(
            company_id=company_id,
            research_question=_QUESTION,
            calculation_ids=[calc1.calculation_id, calc2.calculation_id],
        )
        model = DeepSeekFinancialAnalysisModel(settings)
        service = FinancialAnalysisService(sessionmaker, model)
        print(f"provider = {settings.llm_provider}")
        print(f"model = {model.model_id}")

        start = time.perf_counter()
        try:
            result = await service.analyze(request)
        except FinancialAnalysisNumericLiteralForbidden:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = False")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model returned a statement containing numeric literals")
            return 1
        except FinancialAnalysisClaimKindPolicy:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = True")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model returned a claim_kind outside inference/risk")
            return 1
        except FinancialAnalysisUnknownRef:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = True")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model returned refs outside C1/C2")
            return 1
        except FinancialAnalysisRelationConflict:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = True")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model used the same ref across conflicting relations")
            return 1
        except FinancialAnalysisMalformedOutput:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = False")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: model output could not be parsed into FinancialAnalysisDecision")
            return 1
        except FinancialAnalysisModelUnavailable:
            print(f"latency_ms = {int((time.perf_counter() - start) * 1000)}")
            print("numeric_guard_success = False")
            print("ref_resolution_success = False")
            print("claim_count = 0")
            print("FAIL: DeepSeek provider/model unavailable")
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
        if result.claim_ids:
            from app.repositories.claim_repository import ClaimRepository

            async with sessionmaker() as session:
                for claim_id in result.claim_ids:
                    claim = await ClaimRepository(session).get_by_id(claim_id)
                    assert claim is not None
                    kinds.append(claim.claim_kind)
                    print(
                        f"claim: kind={claim.claim_kind} confidence={claim.confidence} "
                        f"importance={claim.importance} | {claim.statement}"
                    )
        print(f"claim_kinds = {kinds}")
        analyze_ok = True
    finally:
        artifact_id = card["artifact_id"] if card is not None else None
        cleanup_ok = False
        try:
            await _cleanup(
                sessionmaker,
                company_id=company_id,
                artifact_id=artifact_id,
            )
            residual = await _residual_counts(
                sessionmaker,
                company_id=company_id,
                artifact_id=artifact_id,
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
        print("OK: real DeepSeek structured financial analysis smoke passed")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
