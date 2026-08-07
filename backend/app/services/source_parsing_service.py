"""Deterministic source parsing service (stage 2E.1).

parse_source(source_id) 把已归档的 text/html SourceRecord 确定性解析为
ParsedSource + ParsedSourceBlock 快照：

1. 短 DB session 读 SourceRecord + RawArtifact metadata → 关闭 session；
   仅允许 artifact.media_type == text/html；
2. 文件 I/O（从 LocalRawArtifactStore 读 raw bytes）**不持 DB transaction**；
3. Parser 解析 + 计算 parse_fingerprint；
4. 短 DB transaction：create-or-get ParsedSource →（created 时）bulk insert
   Blocks → commit；并发相同 parse 最终只能 1 个 ParsedSource + 一套 Blocks；
5. replay：fingerprint 命中已有快照时不重复插 Blocks，校验
   artifact/source 一致、block_count 一致、block hash/ordinal 完整；
   损坏抛 ParsedSourceIntegrityError，**不自动修复**。

本 Service **不更新** SourceRecord.title / SourceRecord.published_at：解析出的
metadata 只写入 ParsedSource，SourceRecord 保持原始 provenance 不可变。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import RawArtifactNotFound, SourceRecordNotFound
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.parsed_source_block import ParsedSourceBlockModel
from app.parsing.contracts import ParsedDocument, compute_parse_fingerprint
from app.parsing.errors import (
    ParsedSourceIntegrityError,
    ParsedSourcePersistenceFailed,
    UnsupportedParseMediaType,
)
from app.parsing.html_parser import parse_html_bytes
from app.repositories.parsed_source_block_repository import (
    ParsedSourceBlockRepository,
)
from app.repositories.parsed_source_repository import ParsedSourceRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.storage.raw_store import LocalRawArtifactStore

_MEDIA_TYPE_HTML = "text/html"


@dataclass(frozen=True)
class ParsedSourceResult:
    """一次 parse_source 的结果摘要（不含任何 HTML 正文 / block 文本）。"""

    parsed_source_id: UUID
    source_id: UUID
    artifact_id: UUID
    parser_name: str
    parser_version: int
    raw_content_sha256: str
    parse_fingerprint: str
    extracted_title: str | None
    extracted_published_at: datetime | None
    block_count: int
    replayed: bool


class SourceParsingService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        raw_store: LocalRawArtifactStore,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store

    async def parse_source(self, source_id: UUID) -> ParsedSourceResult:
        # 1. 短 DB session：读 SourceRecord + RawArtifact metadata。
        async with self._sessionmaker() as session:
            source_repo = SourceRecordRepository(session)
            artifact_repo = RawArtifactRepository(session)
            source = await source_repo.get_by_id(source_id)
            if source is None:
                raise SourceRecordNotFound()
            artifact = await artifact_repo.get_by_id(source.artifact_id)
            if artifact is None:
                raise RawArtifactNotFound()
            if artifact.media_type != _MEDIA_TYPE_HTML:
                raise UnsupportedParseMediaType()
            storage_key = artifact.storage_key
            expected_content_sha256 = artifact.content_sha256
            artifact_id = artifact.artifact_id
        # session 已关闭；以下为文件 I/O + 纯函数解析，不持有 DB 连接。

        # 2. 文件 I/O：从内容寻址存储读原始字节（不持 DB transaction）。
        with self._raw_store.open(storage_key) as handle:
            raw = handle.read()

        # 3. 解析 + 内容寻址一致性 + fingerprint。
        document = parse_html_bytes(raw)
        if document.raw_content_sha256 != expected_content_sha256:
            # 存储内容与 artifact 登记的 SHA 不一致 = 内容寻址存储被篡改/损坏。
            raise ParsedSourceIntegrityError()
        fingerprint = compute_parse_fingerprint(source_id, document)

        # 4. 短 DB transaction：create-or-get → blocks → commit。
        async with self._sessionmaker() as session:
            try:
                parsed_source_repo = ParsedSourceRepository(session)
                block_repo = ParsedSourceBlockRepository(session)

                existing = await parsed_source_repo.get_by_fingerprint(fingerprint)
                if existing is not None:
                    await self._verify_replay(session, existing, source_id, artifact_id, document)
                    return self._to_result(existing, replayed=True)

                snapshot = ParsedSourceModel(
                    parsed_source_id=uuid.uuid4(),
                    source_id=source_id,
                    artifact_id=artifact_id,
                    parser_name=document.parser_name,
                    parser_version=document.parser_version,
                    raw_content_sha256=document.raw_content_sha256,
                    parse_fingerprint=fingerprint,
                    extracted_title=document.extracted_title,
                    extracted_published_at=document.extracted_published_at,
                    block_count=len(document.blocks),
                    parsed_at=datetime.now(UTC),
                )
                snapshot, created = await parsed_source_repo.create_or_get(snapshot)
                if not created:
                    # 并发输家：不插 Blocks，replay 校验后复用既有快照。
                    await self._verify_replay(session, snapshot, source_id, artifact_id, document)
                    return self._to_result(snapshot, replayed=True)

                blocks = [
                    ParsedSourceBlockModel(
                        block_id=uuid.uuid4(),
                        parsed_source_id=snapshot.parsed_source_id,
                        ordinal=block.ordinal,
                        block_type=block.block_type.value,
                        text=block.text,
                        text_sha256=block.text_sha256,
                        locator=block.locator,
                    )
                    for block in document.blocks
                ]
                await block_repo.bulk_insert(blocks)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ParsedSourcePersistenceFailed() from exc

        return self._to_result(snapshot, replayed=False)

    # ------------------------------------------------------------------ 内部

    async def _verify_replay(
        self,
        session,
        existing: ParsedSourceModel,
        source_id: UUID,
        artifact_id: UUID,
        document: ParsedDocument,
    ) -> None:
        """已有快照 replay 完整性校验；任何不一致抛 ParsedSourceIntegrityError。

        校验项：source/artifact 一致、raw sha 一致、parser 身份一致、
        block_count 一致、block hash/ordinal/type/locator 完整。发现损坏
        只抛错，**不自动修复**（证据链快照不可静默重建）。
        """
        if existing.source_id != source_id:
            raise ParsedSourceIntegrityError()
        if existing.artifact_id != artifact_id:
            raise ParsedSourceIntegrityError()
        if existing.raw_content_sha256 != document.raw_content_sha256:
            raise ParsedSourceIntegrityError()
        if (
            existing.parser_name != document.parser_name
            or existing.parser_version != document.parser_version
        ):
            raise ParsedSourceIntegrityError()
        if existing.block_count != len(document.blocks):
            raise ParsedSourceIntegrityError()
        block_repo = ParsedSourceBlockRepository(session)
        db_blocks = await block_repo.list_for_parsed_source(existing.parsed_source_id)
        if len(db_blocks) != len(document.blocks):
            raise ParsedSourceIntegrityError()
        for db_block, doc_block in zip(db_blocks, document.blocks, strict=True):
            if (
                db_block.ordinal != doc_block.ordinal
                or db_block.block_type != doc_block.block_type.value
                or db_block.text_sha256 != doc_block.text_sha256
                or db_block.locator != doc_block.locator
            ):
                raise ParsedSourceIntegrityError()

    @staticmethod
    def _to_result(snapshot: ParsedSourceModel, *, replayed: bool) -> ParsedSourceResult:
        return ParsedSourceResult(
            parsed_source_id=snapshot.parsed_source_id,
            source_id=snapshot.source_id,
            artifact_id=snapshot.artifact_id,
            parser_name=snapshot.parser_name,
            parser_version=snapshot.parser_version,
            raw_content_sha256=snapshot.raw_content_sha256,
            parse_fingerprint=snapshot.parse_fingerprint,
            extracted_title=snapshot.extracted_title,
            extracted_published_at=snapshot.extracted_published_at,
            block_count=snapshot.block_count,
            replayed=replayed,
        )
