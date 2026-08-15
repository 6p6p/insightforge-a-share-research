"""Source preparation orchestration service (V1.1 P0-2).

把 Web upload / URL import 后停步在 `RawArtifact → SourceRecord` 的现状，补上
生产接线：

    RawArtifact → SourceRecord → ParsedSource → ParsedBlock
    → ChunkSet / DocumentChunk → Chroma derived index（chunk_vector_indexes）

**只协调既有正式实现**（SourceParsingService / ChunkingService /
VectorIndexService），不复制任何 parser / chunker / indexer 逻辑。

不变量：
- RawArtifact immutable；PostgreSQL 是 truth；Chroma 是 derived/rebuildable index；
- 每阶段短事务 / 文件 I/O 不持长 DB transaction（底层服务已保证）；
- 全部阶段幂等（parse/chunk 按 fingerprint replay；index ready replay）——
  重复调用 0 副作用；
- **失败不伪装 ready**：任一步失败 → 结果 status=failed + 稳定 error_code，
  不抛出（调用方据此保留 waiting_manual / INDEX_NOT_READY 语义）；
- 后台 `schedule_prepare`：同 source 至多一个 in-flight 任务（幂等调度），
  失败仅记录日志（resume 时 graph 内 fulfill 会再次自愈补建）。

Generic Source 与 Task-bound Source 的区别由下游负责：普通资料只 prepare
（parse/chunk/index）；**不创建 EvidenceCard**（EvidenceCard 需要 research
question，只由 document executor / 提取路径按需生成）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import get_logger
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.source_record import SourceRecordModel
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.storage.raw_store import LocalRawArtifactStore

logger = get_logger("app.source_preparation")


class SourcePreparationStatus(StrEnum):
    PREPARED = "prepared"
    FAILED = "failed"


class SourcePreparationErrorCode(StrEnum):
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_NOT_ARCHIVED = "source_not_archived"
    MEDIA_TYPE_UNSUPPORTED = "media_type_unsupported"
    PARSE_FAILED = "parse_failed"
    CHUNK_FAILED = "chunk_failed"
    INDEX_FAILED = "index_failed"
    EMBEDDING_NOT_CONFIGURED = "embedding_not_configured"


@dataclass(frozen=True)
class SourcePreparationResult:
    source_id: UUID
    status: str
    error_code: str | None = None
    error_message: str | None = None
    parsed: bool = False
    chunked: bool = False
    indexed: bool = False


@dataclass(frozen=True)
class CompanyPreparationResult:
    company_id: UUID
    prepared: int
    already_prepared: int
    failed: int
    error_codes: list[str]


class SourcePreparationService:
    """parse → chunk → index 的幂等编排（生产接线 / resume 自愈共用）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        raw_store: LocalRawArtifactStore,
        chunking: ChunkingService,
        indexing,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store
        self._parsing = SourceParsingService(sessionmaker, raw_store)
        self._chunking = chunking
        self._indexing = indexing
        self._in_flight: set[UUID] = set()
        self._company_in_flight: set[UUID] = set()

    @property
    def parsing_service(self) -> SourceParsingService:
        """只读暴露内部 parsing service（factory 复用同一实例，避免重复构造）。"""
        return self._parsing

    # ------------------------------------------------------------- 主入口

    async def prepare_source(self, source_id: UUID) -> SourcePreparationResult:
        """把单个 source 推进到可检索状态（parse → chunk → index）。

        - 幂等：已 parsed / chunked / indexed 的阶段自动复用（replay）；
        - 失败返回 status=failed + 稳定 error_code（不抛出）；调用方决定
          （如保留 waiting_manual / INDEX_NOT_READY）。
        """
        source, media_type, parsed = await self._load(source_id)
        if source is None:
            return SourcePreparationResult(
                source_id=source_id,
                status=SourcePreparationStatus.FAILED.value,
                error_code=SourcePreparationErrorCode.SOURCE_NOT_FOUND.value,
                error_message="source record not found",
            )
        if parsed is None:
            try:
                await self._parsing.parse_source(source_id)
                parsed = await self._parsed_source(source_id)
            except Exception as exc:  # noqa: BLE001 - 分类为稳定错误码
                return self._failure(
                    source_id,
                    self._parse_error_code(exc, media_type),
                    f"parse failed: {type(exc).__name__}",
                )
        parsed_id = parsed.parsed_source_id
        chunked = True
        try:
            chunk_result = await self._chunking.chunk_parsed_source(parsed_id)
            chunk_set_id = chunk_result.chunk_set_id
        except Exception as exc:  # noqa: BLE001
            return self._failure(
                source_id,
                SourcePreparationErrorCode.CHUNK_FAILED.value,
                f"chunk failed: {type(exc).__name__}",
                parsed=True,
            )
        try:
            index_result = await self._indexing.index_chunk_set(chunk_set_id)
            indexed = index_result.status == "ready"
        except Exception as exc:  # noqa: BLE001
            error_code = (
                SourcePreparationErrorCode.EMBEDDING_NOT_CONFIGURED.value
                if type(exc).__name__ == "EmbeddingModelNotConfigured"
                else SourcePreparationErrorCode.INDEX_FAILED.value
            )
            return self._failure(
                source_id,
                error_code,
                f"index failed: {type(exc).__name__}",
                parsed=True,
                chunked=True,
            )
        return SourcePreparationResult(
            source_id=source_id,
            status=SourcePreparationStatus.PREPARED.value,
            parsed=True,
            chunked=chunked,
            indexed=indexed,
        )

    async def ensure_indexed(self, source_id: UUID) -> bool:
        """兼容入口（executor IndexBuilder 协议）：prepare 全链，就绪返回 True。"""
        result = await self.prepare_source(source_id)
        return result.status == SourcePreparationStatus.PREPARED.value

    async def prepare_company_sources(self, company_id: UUID) -> CompanyPreparationResult:
        """补齐公司全部未 parse 的 source（幂等；逐条失败不中断）。"""
        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(SourceRecordModel.source_id).where(
                            SourceRecordModel.company_id == company_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        prepared = 0
        already = 0
        failed = 0
        error_codes: list[str] = []
        for source_id in rows:
            result = await self.prepare_source(source_id)
            if result.status == SourcePreparationStatus.PREPARED.value:
                prepared += 1
            elif result.error_code == SourcePreparationErrorCode.SOURCE_NOT_FOUND.value:
                already += 1
            else:
                failed += 1
                error_codes.append(result.error_code or "unknown")
        return CompanyPreparationResult(
            company_id=company_id,
            prepared=prepared,
            already_prepared=already,
            failed=failed,
            error_codes=sorted(set(error_codes)),
        )

    # ------------------------------------------------------------- background

    def schedule_prepare(self, source_id: UUID) -> bool:
        """后台准备（ingest 成功后调用）：同 source 至多一个 in-flight 任务。

        失败仅记录日志——resume 时编排图内 fulfill 的 index builder 会再次
        自愈补建（本服务同一实现）。
        """
        if source_id in self._in_flight:
            return False
        self._in_flight.add(source_id)

        async def _run() -> None:
            try:
                result = await self.prepare_source(source_id)
                if result.status != SourcePreparationStatus.PREPARED.value:
                    logger.warning(
                        "source_preparation_failed",
                        source_id=str(source_id),
                        error_code=result.error_code,
                    )
            except Exception as exc:  # noqa: BLE001 - 后台任务不抛出
                logger.warning(
                    "source_preparation_error",
                    source_id=str(source_id),
                    error_type=type(exc).__name__,
                )
            finally:
                self._in_flight.discard(source_id)

        asyncio.create_task(_run(), name=f"source-prepare-{source_id}")
        return True

    def schedule_prepare_company(self, company_id: UUID) -> bool:
        """后台补齐公司全部未 parse 的 source（resume 前预准备；best-effort）。"""
        if company_id in self._company_in_flight:
            return False
        self._company_in_flight.add(company_id)

        async def _run() -> None:
            try:
                result = await self.prepare_company_sources(company_id)
                logger.info(
                    "company_source_preparation_completed",
                    company_id=str(company_id),
                    prepared=result.prepared,
                    already_prepared=result.already_prepared,
                    failed=result.failed,
                    error_codes=result.error_codes,
                )
            except Exception as exc:  # noqa: BLE001 - 后台任务不抛出
                logger.warning(
                    "company_source_preparation_error",
                    company_id=str(company_id),
                    error_type=type(exc).__name__,
                )
            finally:
                self._company_in_flight.discard(company_id)

        asyncio.create_task(_run(), name=f"company-prepare-{company_id}")
        return True

    # ------------------------------------------------------------- internal

    async def _load(self, source_id: UUID):
        async with self._sessionmaker() as session:
            source = await session.get(SourceRecordModel, source_id)
            if source is None:
                return None, None, None
            from app.db.models.raw_artifact import RawArtifactModel

            artifact = await session.get(RawArtifactModel, source.artifact_id)
            media_type = artifact.media_type if artifact is not None else None
            parsed = (
                (
                    await session.execute(
                        select(ParsedSourceModel).where(ParsedSourceModel.source_id == source_id)
                    )
                )
                .scalars()
                .first()
            )
        return source, media_type, parsed

    async def _parsed_source(self, source_id: UUID) -> ParsedSourceModel | None:
        async with self._sessionmaker() as session:
            return (
                (
                    await session.execute(
                        select(ParsedSourceModel).where(ParsedSourceModel.source_id == source_id)
                    )
                )
                .scalars()
                .first()
            )

    @staticmethod
    def _parse_error_code(exc: Exception, media_type: str | None) -> str:
        name = type(exc).__name__
        if name == "UnsupportedParseMediaType":
            return SourcePreparationErrorCode.MEDIA_TYPE_UNSUPPORTED.value
        if name in ("SourceRecordNotFound", "RawArtifactNotFound"):
            return SourcePreparationErrorCode.SOURCE_NOT_ARCHIVED.value
        return SourcePreparationErrorCode.PARSE_FAILED.value

    @staticmethod
    def _failure(
        source_id: UUID,
        error_code: str,
        message: str,
        *,
        parsed: bool = False,
        chunked: bool = False,
    ) -> SourcePreparationResult:
        return SourcePreparationResult(
            source_id=source_id,
            status=SourcePreparationStatus.FAILED.value,
            error_code=error_code,
            error_message=message,
            parsed=parsed,
            chunked=chunked,
        )
