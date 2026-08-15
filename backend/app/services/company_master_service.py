"""Company master bootstrap / import service (V1.1 P0-1).

生产 Company Master 供给链：
    authoritative sources（SSE/SZSE 官方 + BSE 记录降级）
    → versioned snapshot（仓库内 checked-in JSON，`app/companies/master/`）
    → 本服务幂等导入（bootstrap at startup / 显式 refresh via CLI）

语义与不变量：
- **bootstrap()**：companies 表非空 → 跳过（不覆盖已有 CompanyIdentity）；
  空表 → 导入 bundled snapshot（insert-only）。启动可安全重复调用。
- **import_snapshot()**：
  - `insert_only=True`（bootstrap）：ON CONFLICT DO NOTHING，绝不修改既有行；
  - `insert_only=False`（CLI refresh，--force）：按 identity_key upsert 名称/
    板块/上市状态（不删除任何行），aliases 仍 insert-only（补缺不覆盖）；
  - 同一 (snapshot_version, content_sha256) 已登记 → replay（0 写，幂等）；
  - snapshot provenance 写 `company_master_snapshots`（migration 0048）。
- **Provider 依赖**：identity_source_provider_key 指向 source_providers 中
  已存在的 sse/szse/bse（Source Registry bootstrap 必须先于 Company Master
  bootstrap）；缺失 → `CompanyMasterProviderMissing`（稳定错误码）。
- **0 network / 0 LLM**：只消费本地 snapshot 文件；导入为批量 INSERT。

数据规模：~5500 家公司 + ~11000 别名，批量导入（每批 1000 行）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.companies.master.snapshot import (
    CompanyMasterEntry,
    CompanyMasterSnapshot,
    LoadedSnapshot,
    load_bundled_snapshot,
)
from app.companies.normalization import normalize_company_text
from app.core.logging import get_logger
from app.db.models.company import CompanyModel
from app.db.models.company_alias import CompanyAliasModel
from app.db.models.company_master_snapshot import CompanyMasterSnapshotRow

logger = get_logger("app.company_master")

_BATCH_SIZE = 1000
_PROVIDER_BY_EXCHANGE = {"SSE": "sse", "SZSE": "szse", "BSE": "bse"}


class CompanyMasterBootstrapError(RuntimeError):
    """稳定错误码（非 HTTP DomainError：启动/CLI 路径，不入 API 信封）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CompanyMasterImportResult:
    snapshot_version: str
    content_sha256: str
    imported_companies: int
    imported_aliases: int
    skipped: bool  # bootstrap：company master 数据已存在，未执行
    replayed: bool  # marker 存在且实际数据完整（0 写）
    repair: bool = False  # marker 存在但实际数据缺失 → 一致性恢复（重新 import）
    error_code: str | None = None
    error_message: str | None = None


def master_data_missing(company_count: int, alias_count: int) -> bool:
    """一致性判定（纯函数，可单元测试）：marker 存在时，实际 Company Master
    数据为**空表**即视为缺失——不允许 replay 掩盖数据丢失。

    非空（即使 count 与 snapshot 不完全一致）→ 尊重现有数据（Case D），
    不 repair、不重灌、不覆盖。
    """
    return company_count == 0 or alias_count == 0


class CompanyMasterBootstrapService:
    """启动 bootstrap + 显式 import 的幂等导入服务。"""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    # ------------------------------------------------------------- bootstrap

    async def bootstrap(self) -> CompanyMasterImportResult:
        """启动幂等导入：company master 数据存在（companies 与 aliases 均非空）
        → skip；否则导入 bundled snapshot（marker 存在但数据缺失时自动恢复）。

        判定依据是**实际数据状态**，不是 marker 历史——marker 存在 + 空表
        的 inconsistent state 会走一致性恢复而非 replay（见 import_snapshot）。
        """
        async with self._sessionmaker() as session:
            company_count = int(
                (await session.execute(select(func.count(CompanyModel.company_id)))).scalar_one()
            )
            alias_count = int(
                (await session.execute(select(func.count(CompanyAliasModel.alias_id)))).scalar_one()
            )
        if not master_data_missing(company_count, alias_count):
            return CompanyMasterImportResult(
                snapshot_version="",
                content_sha256="",
                imported_companies=0,
                imported_aliases=0,
                skipped=True,
                replayed=False,
                error_code="company_master_skipped_nonempty",
                error_message="company master data already populated; skip bootstrap",
            )
        loaded = load_bundled_snapshot()
        return await self.import_snapshot(loaded, insert_only=True)

    # ------------------------------------------------------------- import

    async def import_snapshot(
        self,
        loaded: LoadedSnapshot,
        *,
        insert_only: bool = True,
    ) -> CompanyMasterImportResult:
        """导入一份 validated snapshot（幂等）。insert_only=False 为 refresh upsert。

        一致性规则（marker 与实际数据状态联合判定）：
        - **Case A**：marker 不存在 + 数据缺失 → 首次导入 → 记录 marker；
        - **Case B**：marker 存在 + **实际数据完整** → replay（0 写）；
        - **Case C**：marker 存在 + 实际数据缺失（空表）→ **一致性恢复**：
          重新执行安全 import（insert-only），恢复到 snapshot 对应 master，
          marker 不重复（ON CONFLICT DO NOTHING），结果 repair=True；
        - **Case D**：数据非空（即使 count 与 snapshot 不一致）→ 尊重现有数据：
          marker 存在 → replay；bootstrap → skip。**不 DELETE、不覆盖**已有
          CompanyIdentity；ON CONFLICT DO NOTHING / upsert 语义保持不变。
        """
        snapshot = loaded.snapshot
        await self._ensure_providers()
        async with self._sessionmaker() as session:
            recorded = (
                await session.execute(
                    select(CompanyMasterSnapshotRow.snapshot_id).where(
                        CompanyMasterSnapshotRow.snapshot_version == snapshot.snapshot_version,
                        CompanyMasterSnapshotRow.content_sha256 == loaded.content_sha256,
                    )
                )
            ).first()
            company_count = int(
                (await session.execute(select(func.count(CompanyModel.company_id)))).scalar_one()
            )
            alias_count = int(
                (await session.execute(select(func.count(CompanyAliasModel.alias_id)))).scalar_one()
            )
        data_missing = master_data_missing(company_count, alias_count)
        if recorded is not None and not data_missing:
            # Case B：marker 存在且实际数据完整 → replay（0 写）。
            return CompanyMasterImportResult(
                snapshot_version=snapshot.snapshot_version,
                content_sha256=loaded.content_sha256,
                imported_companies=0,
                imported_aliases=0,
                skipped=False,
                replayed=True,
            )
        repair = recorded is not None and data_missing
        if repair:
            # Case C：marker 存在但实际数据缺失 → 一致性恢复（不掩盖数据丢失）。
            logger.warning(
                "company_master_bootstrap_repair_started",
                snapshot_version=snapshot.snapshot_version,
                content_sha256=loaded.content_sha256[:16],
                company_count=company_count,
                alias_count=alias_count,
            )
        company_rows = self._company_rows(snapshot)
        async with self._sessionmaker() as session:
            for batch_start in range(0, len(company_rows), _BATCH_SIZE):
                batch = company_rows[batch_start : batch_start + _BATCH_SIZE]
                stmt = pg_insert(CompanyModel).values(batch)
                if insert_only:
                    stmt = stmt.on_conflict_do_nothing(constraint="uq_companies_identity_key")
                else:
                    stmt = stmt.on_conflict_do_update(
                        constraint="uq_companies_identity_key",
                        set_={
                            "official_name": stmt.excluded.official_name,
                            "short_name": stmt.excluded.short_name,
                            "board": stmt.excluded.board,
                            "listing_status": stmt.excluded.listing_status,
                            "listing_date": stmt.excluded.listing_date,
                            "identity_source_url": stmt.excluded.identity_source_url,
                            "source_updated_at": stmt.excluded.source_updated_at,
                            "updated_at": stmt.excluded.updated_at,
                        },
                    )
                await session.execute(stmt)
            # aliases 必须引用**实际落库**的 company_id（既有行冲突时保留原 id，
            # 以 identity_key 回查，避免派生 id 造成 FK 悬挂）。
            company_id_by_key = await self._load_company_ids(
                session, [entry.identity_key for entry in snapshot.companies]
            )
            alias_rows = self._alias_rows(snapshot, company_id_by_key)
            for batch_start in range(0, len(alias_rows), _BATCH_SIZE):
                batch = alias_rows[batch_start : batch_start + _BATCH_SIZE]
                stmt = (
                    pg_insert(CompanyAliasModel)
                    .values(batch)
                    .on_conflict_do_nothing(constraint="uq_company_aliases_company_alias_type")
                )
                await session.execute(stmt)
            await session.execute(
                pg_insert(CompanyMasterSnapshotRow)
                .values(
                    {
                        "snapshot_id": uuid.uuid4(),
                        "snapshot_version": snapshot.snapshot_version,
                        "content_sha256": loaded.content_sha256,
                        "company_count": len(snapshot.companies),
                        "alias_count": len(alias_rows),
                        "sources": [source.model_dump(mode="json") for source in snapshot.sources],
                    }
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        CompanyMasterSnapshotRow.snapshot_version,
                        CompanyMasterSnapshotRow.content_sha256,
                    ]
                )
            )
            await session.commit()
        return CompanyMasterImportResult(
            snapshot_version=snapshot.snapshot_version,
            content_sha256=loaded.content_sha256,
            imported_companies=len(company_rows),
            imported_aliases=len(alias_rows),
            skipped=False,
            replayed=False,
            repair=repair,
        )

    async def import_bundled(self, *, insert_only: bool = True) -> CompanyMasterImportResult:
        return await self.import_snapshot(load_bundled_snapshot(), insert_only=insert_only)

    # ------------------------------------------------------------- internal

    async def _ensure_providers(self) -> None:
        """identity_source_provider_key 依赖 Source Registry 的 sse/szse/bse。"""
        from app.db.models.source_provider import SourceProviderModel

        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(SourceProviderModel.provider_key).where(
                            SourceProviderModel.provider_key.in_(
                                list(_PROVIDER_BY_EXCHANGE.values())
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        missing = set(_PROVIDER_BY_EXCHANGE.values()) - set(rows)
        if missing:
            raise CompanyMasterBootstrapError(
                code="company_master_provider_missing",
                message=(
                    "source registry providers missing: "
                    + ", ".join(sorted(missing))
                    + "; seed source registry before company master bootstrap"
                ),
            )

    @staticmethod
    def _source_url_for(snapshot: CompanyMasterSnapshot, exchange: str) -> str:
        for source in snapshot.sources:
            if source.exchange == exchange:
                return source.url
        return ""

    def _company_rows(self, snapshot: CompanyMasterSnapshot) -> list[dict]:
        as_of = datetime.now(UTC)
        url_by_exchange = {
            exchange: self._source_url_for(snapshot, exchange) for exchange in _PROVIDER_BY_EXCHANGE
        }
        rows: list[dict] = []
        for entry in snapshot.companies:
            rows.append(
                {
                    "company_id": uuid.uuid4(),
                    "exchange": entry.exchange,
                    "security_code": entry.security_code,
                    "identity_key": entry.identity_key,
                    "board": entry.board,
                    "official_name": entry.official_name,
                    "short_name": entry.short_name,
                    "listing_status": entry.listing_status,
                    "listing_date": entry.listing_date,
                    "identity_source_provider_key": _PROVIDER_BY_EXCHANGE[entry.exchange],
                    "identity_source_url": url_by_exchange[entry.exchange],
                    "source_updated_at": as_of,
                }
            )
        return rows

    @staticmethod
    async def _load_company_ids(session, identity_keys: Iterable[str]) -> dict[str, UUID]:
        """identity_key → 实际 company_id（批量回查，供 alias 行引用）。"""
        keys = list(identity_keys)
        result: dict[str, UUID] = {}
        for start in range(0, len(keys), _BATCH_SIZE):
            batch = keys[start : start + _BATCH_SIZE]
            rows = (
                await session.execute(
                    select(CompanyModel.company_id, CompanyModel.identity_key).where(
                        CompanyModel.identity_key.in_(batch)
                    )
                )
            ).all()
            result.update({identity_key: company_id for company_id, identity_key in rows})
        return result

    def _alias_rows(
        self,
        snapshot: CompanyMasterSnapshot,
        company_id_by_key: dict[str, UUID],
    ) -> list[dict]:
        url_by_exchange = {
            exchange: self._source_url_for(snapshot, exchange) for exchange in _PROVIDER_BY_EXCHANGE
        }
        rows: list[dict] = []
        for entry in snapshot.companies:
            company_id = company_id_by_key.get(entry.identity_key)
            if company_id is None:
                continue
            provider_key = _PROVIDER_BY_EXCHANGE[entry.exchange]
            source_url = url_by_exchange[entry.exchange]
            rows.extend(self._aliases_for_entry(entry, provider_key, source_url, company_id))
        return rows

    @staticmethod
    def _aliases_for_entry(
        entry: CompanyMasterEntry,
        provider_key: str,
        source_url: str,
        company_id: UUID | None,
    ) -> list[dict]:
        aliases: list[tuple[str, str]] = [
            (entry.official_name, "official_name"),
            (entry.short_name, "short_name"),
        ]
        aliases.extend((former, "former_name") for former in entry.former_names)
        seen: set[str] = set()
        rows: list[dict] = []
        for alias, alias_type in aliases:
            normalized = normalize_company_text(alias)
            if normalized in seen:
                continue
            seen.add(normalized)
            rows.append(
                {
                    "alias_id": uuid.uuid4(),
                    "company_id": company_id,
                    "alias": alias,
                    "normalized_alias": normalized,
                    "alias_type": alias_type,
                    "source_provider_key": provider_key,
                    "source_url": source_url,
                }
            )
        return rows


# 说明：CompanyMasterSnapshotRow 在模块顶部导入（db.models.company_master_snapshot
# 无循环依赖）。
