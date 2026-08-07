"""Deterministic document chunking service (stage 3A).

chunk_parsed_source(parsed_source_id) 把 ParsedSource + ordered ParsedBlocks
确定性切分为 ChunkSet + DocumentChunk 快照：

1. 短 DB session 读 ParsedSource + blocks（ordered by ordinal）→ 关闭 session；
2. 纯函数 chunking（block_window v1）+ 计算 chunk_set_fingerprint；
3. 短 DB transaction：create-or-get ChunkSet →（created 时）bulk insert
   chunks → commit；并发相同 chunking 最终只能 1 个 ChunkSet + 一套 chunks；
4. replay：fingerprint 命中已有 ChunkSet 时不重复插 chunks，校验
   parsed_source 一致、source_parse_fingerprint 一致、chunk_count 一致、
   chunk hash/ordinal/char_count/locator_refs 完整；损坏抛
   ChunkSetIntegrityError，**不自动修复**。

本 Service **不修改** SourceRecord / ParsedSource：分块只是 ParsedSource
快照的下游只读消费，不重新读取 RawArtifact、不解析、不回写任何元数据。
"""

import uuid
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.chunking.chunker import chunk_parsed_document
from app.chunking.contracts import (
    ChunkBlockRef,
    ChunkSetDocument,
    compute_chunk_set_fingerprint,
)
from app.chunking.errors import (
    ChunkSetIntegrityError,
    ChunkSetPersistenceFailed,
    ParsedSourceNotFound,
)
from app.db.models.chunk_set import ChunkSetModel
from app.db.models.document_chunk import DocumentChunkModel
from app.db.models.parsed_source_block import ParsedSourceBlockModel
from app.domain.parsing import ParsedBlockType
from app.parsing.contracts import ParsedBlock, ParsingContractViolation
from app.repositories.chunk_set_repository import ChunkSetRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.parsed_source_block_repository import ParsedSourceBlockRepository
from app.repositories.parsed_source_repository import ParsedSourceRepository


@dataclass(frozen=True)
class ChunkSetResult:
    """一次 chunk_parsed_source 的结果摘要（不含任何正文文本 / chunk 内容）。"""

    chunk_set_id: UUID
    parsed_source_id: UUID
    chunker_name: str
    chunker_version: int
    source_parse_fingerprint: str
    chunk_count: int
    chunk_set_fingerprint: str
    replayed: bool


class ChunkingService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def chunk_parsed_source(self, parsed_source_id: UUID) -> ChunkSetResult:
        # 1. 短 DB session：读 ParsedSource + ordered ParsedBlocks（metadata）。
        async with self._sessionmaker() as session:
            parsed_repo = ParsedSourceRepository(session)
            parsed = await parsed_repo.get_by_id(parsed_source_id)
            if parsed is None:
                raise ParsedSourceNotFound()
            block_repo = ParsedSourceBlockRepository(session)
            db_blocks = await block_repo.list_for_parsed_source(parsed_source_id)
            source_parse_fingerprint = parsed.parse_fingerprint
        # session 已关闭；以下为纯函数 chunking，不持有 DB 连接。

        # 2. 纯函数 chunking + 确定性指纹。
        blocks = tuple(self._to_parsed_block(db_block) for db_block in db_blocks)
        document = chunk_parsed_document(parsed_source_id, source_parse_fingerprint, blocks)
        fingerprint = compute_chunk_set_fingerprint(document)

        # 3. 短 DB transaction：create-or-get ChunkSet → chunks → commit。
        async with self._sessionmaker() as session:
            try:
                set_repo = ChunkSetRepository(session)
                chunk_repo = DocumentChunkRepository(session)

                existing = await set_repo.get_by_fingerprint(fingerprint)
                if existing is not None:
                    await self._verify_replay(session, existing, document)
                    return self._to_result(existing, replayed=True)

                chunk_set = ChunkSetModel(
                    chunk_set_id=uuid.uuid4(),
                    parsed_source_id=parsed_source_id,
                    chunker_name=document.chunker_name,
                    chunker_version=document.chunker_version,
                    source_parse_fingerprint=document.source_parse_fingerprint,
                    chunk_count=len(document.chunks),
                    chunk_set_fingerprint=fingerprint,
                )
                chunk_set, created = await set_repo.create_or_get(chunk_set)
                if not created:
                    # 并发输家：不插 chunks，replay 校验后复用既有 ChunkSet。
                    await self._verify_replay(session, chunk_set, document)
                    return self._to_result(chunk_set, replayed=True)

                chunks = [
                    DocumentChunkModel(
                        chunk_id=uuid.uuid4(),
                        chunk_set_id=chunk_set.chunk_set_id,
                        ordinal=chunk.ordinal,
                        text=chunk.text,
                        text_sha256=chunk.text_sha256,
                        char_count=chunk.char_count,
                        locator_refs=_refs_to_json(chunk.locator_refs),
                    )
                    for chunk in document.chunks
                ]
                await chunk_repo.bulk_insert(chunks)
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ChunkSetPersistenceFailed() from exc

        return self._to_result(chunk_set, replayed=False)

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _to_parsed_block(db_block: ParsedSourceBlockModel) -> ParsedBlock:
        """DB ParsedSourceBlock → ParsedBlock；输入损坏统一为 ChunkSetIntegrityError。

        上游 ParsedSource 已由 SourceParsingService 保证完整；此处仍防御性
        校验 block_type / text hash / locator，任何不一致视为输入损坏。
        """
        try:
            block_type = ParsedBlockType(db_block.block_type)
            return ParsedBlock(
                ordinal=db_block.ordinal,
                block_type=block_type,
                text=db_block.text,
                text_sha256=db_block.text_sha256,
                locator=db_block.locator,
            )
        except (ValueError, ParsingContractViolation) as exc:
            raise ChunkSetIntegrityError() from exc

    async def _verify_replay(
        self,
        session,
        existing: ChunkSetModel,
        document: ChunkSetDocument,
    ) -> None:
        """已有 ChunkSet replay 完整性校验；任何不一致抛 ChunkSetIntegrityError。

        校验项：parsed_source 一致、source_parse_fingerprint 一致、chunker
        身份一致、chunk_count 一致、chunk hash/ordinal/char_count/locator_refs
        完整。发现损坏只抛错，**不自动修复**（ChunkSet 不可静默重建）。
        """
        if existing.parsed_source_id != document.parsed_source_id:
            raise ChunkSetIntegrityError()
        if existing.source_parse_fingerprint != document.source_parse_fingerprint:
            raise ChunkSetIntegrityError()
        if (
            existing.chunker_name != document.chunker_name
            or existing.chunker_version != document.chunker_version
        ):
            raise ChunkSetIntegrityError()
        if existing.chunk_count != len(document.chunks):
            raise ChunkSetIntegrityError()
        chunk_repo = DocumentChunkRepository(session)
        db_chunks = await chunk_repo.list_for_chunk_set(existing.chunk_set_id)
        if len(db_chunks) != len(document.chunks):
            raise ChunkSetIntegrityError()
        for db_chunk, chunk in zip(db_chunks, document.chunks, strict=True):
            if (
                db_chunk.ordinal != chunk.ordinal
                or db_chunk.text != chunk.text
                or db_chunk.text_sha256 != chunk.text_sha256
                or db_chunk.char_count != chunk.char_count
                or db_chunk.locator_refs != _refs_to_json(chunk.locator_refs)
            ):
                raise ChunkSetIntegrityError()

    @staticmethod
    def _to_result(chunk_set: ChunkSetModel, *, replayed: bool) -> ChunkSetResult:
        return ChunkSetResult(
            chunk_set_id=chunk_set.chunk_set_id,
            parsed_source_id=chunk_set.parsed_source_id,
            chunker_name=chunk_set.chunker_name,
            chunker_version=chunk_set.chunker_version,
            source_parse_fingerprint=chunk_set.source_parse_fingerprint,
            chunk_count=chunk_set.chunk_count,
            chunk_set_fingerprint=chunk_set.chunk_set_fingerprint,
            replayed=replayed,
        )


def _refs_to_json(refs: tuple[ChunkBlockRef, ...]) -> list[dict]:
    """ChunkBlockRef → JSON 可序列化结构（与 fingerprint / DB 存储一致）。"""
    return [
        {
            "block_ordinal": ref.block_ordinal,
            "char_start": ref.char_start,
            "char_end": ref.char_end,
            "locator": ref.locator,
        }
        for ref in refs
    ]
