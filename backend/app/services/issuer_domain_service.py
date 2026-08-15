"""Issuer official-domain registry service (V1.1 final closure).

生产供给链（mirror company master 模式）：
    authoritative sources（SZSE 官方名录 + EM F10 ORGINFO）
    → versioned snapshot（仓库内 checked-in JSON，`app/issuer_domains/`）
    → 本服务幂等导入（bootstrap at startup / 显式 refresh via CLI）

语义与不变量：
- **bootstrap()**：issuer_domains 非空 → 跳过；空表 → 导入 bundled snapshot
  （insert-only）。启动可安全重复调用。
- **import_snapshot()**：同一 (snapshot_version, content_sha256) 已登记 →
  replay（0 写）；marker 存在但实际数据为空 → 一致性恢复（repair=True）；
  行按 (company_id, domain) ON CONFLICT DO NOTHING，绝不覆盖既有行；
  snapshot provenance 写 `issuer_domain_snapshots`（migration 0049）。
- **lookup / validate_issuer_url**：issuer_official 受控来源的动态域名校验
  ——URL hostname 必须匹配该公司在 registry 中登记的域名（company_id 绑定，
  不允许任意网站伪装 issuer）；registry 无该公司记录 → 拒绝（不降级到
  allowlist 之外）。
- **0 network / 0 LLM**：只消费本地 snapshot 文件；导入为批量 INSERT。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import get_logger
from app.db.models.company import CompanyModel
from app.db.models.issuer_domain import IssuerDomainModel
from app.db.models.issuer_domain_snapshot import IssuerDomainSnapshotRow
from app.issuer_domains.snapshot import (
    IssuerDomainSnapshot,
    LoadedIssuerDomainSnapshot,
    load_bundled_snapshot,
)
from app.source_registry.url_policy import _idna_host

logger = get_logger("app.issuer_domain")

_BATCH_SIZE = 1000


class IssuerDomainBootstrapError(RuntimeError):
    """稳定错误码（非 HTTP DomainError：启动/CLI 路径，不入 API 信封）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class IssuerDomainImportResult:
    snapshot_version: str
    content_sha256: str
    imported_domains: int
    imported_companies: int
    skipped: bool
    replayed: bool
    repair: bool = False
    error_code: str | None = None
    error_message: str | None = None


def issuer_domains_data_missing(domain_count: int) -> bool:
    """一致性判定（纯函数，可单元测试）：marker 存在时，实际数据为空表即缺失。"""
    return domain_count == 0


class IssuerDomainService:
    """启动 bootstrap + 显式 import + 运行时域名校验。"""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    # ------------------------------------------------------------- bootstrap

    async def bootstrap(self) -> IssuerDomainImportResult:
        """启动幂等导入：registry 非空 → skip；否则导入 bundled snapshot。"""
        async with self._sessionmaker() as session:
            domain_count = int(
                (
                    await session.execute(select(func.count(IssuerDomainModel.domain_id)))
                ).scalar_one()
            )
        if not issuer_domains_data_missing(domain_count):
            return IssuerDomainImportResult(
                snapshot_version="",
                content_sha256="",
                imported_domains=0,
                imported_companies=0,
                skipped=True,
                replayed=False,
                error_code="issuer_domains_skipped_nonempty",
                error_message="issuer domain registry already populated; skip bootstrap",
            )
        return await self.import_snapshot(load_bundled_snapshot(), insert_only=True)

    # ------------------------------------------------------------- import

    async def import_snapshot(
        self,
        loaded: LoadedIssuerDomainSnapshot,
        *,
        insert_only: bool = True,
    ) -> IssuerDomainImportResult:
        """导入一份 validated snapshot（幂等；Case A/B/C/D 语义同 company master）。"""
        snapshot = loaded.snapshot
        async with self._sessionmaker() as session:
            recorded = (
                await session.execute(
                    select(IssuerDomainSnapshotRow.snapshot_id).where(
                        IssuerDomainSnapshotRow.snapshot_version == snapshot.snapshot_version,
                        IssuerDomainSnapshotRow.content_sha256 == loaded.content_sha256,
                    )
                )
            ).first()
            domain_count = int(
                (
                    await session.execute(select(func.count(IssuerDomainModel.domain_id)))
                ).scalar_one()
            )
        data_missing = issuer_domains_data_missing(domain_count)
        if recorded is not None and not data_missing:
            # Case B：marker 存在且实际数据完整 → replay（0 写）。
            return IssuerDomainImportResult(
                snapshot_version=snapshot.snapshot_version,
                content_sha256=loaded.content_sha256,
                imported_domains=0,
                imported_companies=0,
                skipped=False,
                replayed=True,
            )
        repair = recorded is not None and data_missing
        if repair:
            logger.warning(
                "issuer_domain_bootstrap_repair_started",
                snapshot_version=snapshot.snapshot_version,
                content_sha256=loaded.content_sha256[:16],
                domain_count=domain_count,
            )
        async with self._sessionmaker() as session:
            company_id_by_key = await self._load_company_ids(
                session, [entry.security_code for entry in snapshot.domains]
            )
            rows = self._domain_rows(snapshot, company_id_by_key)
            for batch_start in range(0, len(rows), _BATCH_SIZE):
                batch = rows[batch_start : batch_start + _BATCH_SIZE]
                stmt = pg_insert(IssuerDomainModel).values(batch)
                if insert_only:
                    stmt = stmt.on_conflict_do_nothing(
                        constraint="uq_issuer_domains_company_domain"
                    )
                else:
                    stmt = stmt.on_conflict_do_update(
                        constraint="uq_issuer_domains_company_domain",
                        set_={
                            "source_url": stmt.excluded.source_url,
                            "provider_key": stmt.excluded.provider_key,
                            "verified_at": stmt.excluded.verified_at,
                        },
                    )
                await session.execute(stmt)
            await session.execute(
                pg_insert(IssuerDomainSnapshotRow)
                .values(
                    {
                        "snapshot_id": uuid.uuid4(),
                        "snapshot_version": snapshot.snapshot_version,
                        "content_sha256": loaded.content_sha256,
                        "company_count": len(company_id_by_key),
                        "domain_count": len(rows),
                        "sources": [source.model_dump(mode="json") for source in snapshot.sources],
                    }
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        IssuerDomainSnapshotRow.snapshot_version,
                        IssuerDomainSnapshotRow.content_sha256,
                    ]
                )
            )
            await session.commit()
        return IssuerDomainImportResult(
            snapshot_version=snapshot.snapshot_version,
            content_sha256=loaded.content_sha256,
            imported_domains=len(rows),
            imported_companies=len(company_id_by_key),
            skipped=False,
            replayed=False,
            repair=repair,
        )

    async def import_bundled(self, *, insert_only: bool = True) -> IssuerDomainImportResult:
        return await self.import_snapshot(load_bundled_snapshot(), insert_only=insert_only)

    # ------------------------------------------------------------- runtime

    async def lookup_domains(self, company_id: UUID) -> list[str]:
        """公司登记的全部官网域名（稳定排序）。"""
        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(IssuerDomainModel.domain)
                        .where(IssuerDomainModel.company_id == company_id)
                        .order_by(IssuerDomainModel.domain.asc())
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)

    async def match_issuer_url(self, company_id: UUID, url: str) -> bool:
        """URL hostname 是否匹配该公司登记的官网域名（resolve-provider 用，不抛错）。"""
        try:
            await self.validate_issuer_url(company_id, url)
            return True
        except IssuerDomainBootstrapError:
            return False

    async def validate_issuer_url(self, company_id: UUID, url: str) -> None:
        """issuer_official 受控来源的动态域名校验。

        URL hostname 必须匹配该公司在 issuer_domains registry 中登记的域名
        （company_id 绑定 + 真实验证 URL）；registry 无该公司记录或 hostname
        不匹配 → 拒绝（不降级、不放宽现有 allowlist / SSRF 策略）。
        """
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise IssuerDomainBootstrapError(
                code="issuer_url_not_https", message="issuer URL 必须是 https 且含 hostname"
            )
        if parsed.username is not None or parsed.password is not None:
            raise IssuerDomainBootstrapError(
                code="issuer_url_userinfo", message="issuer URL 不得包含 userinfo"
            )
        if parsed.port is not None:
            raise IssuerDomainBootstrapError(
                code="issuer_url_port", message="issuer URL 不得包含端口"
            )
        host = _idna_host(parsed.hostname)
        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(IssuerDomainModel.domain).where(
                            IssuerDomainModel.company_id == company_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            raise IssuerDomainBootstrapError(
                code="issuer_domain_not_registered",
                message="该公司未登记官网域名，无法以 issuer_official 来源校验",
            )
        for domain in rows:
            if host == domain or host.endswith("." + domain):
                return
        raise IssuerDomainBootstrapError(
            code="issuer_url_domain_mismatch",
            message="URL hostname 不在该公司登记的官网域名内",
        )

    # ------------------------------------------------------------- internal

    @staticmethod
    async def _load_company_ids(session, security_codes: list[str]) -> dict[str, UUID]:
        """security_code → 实际 company_id（按 identity_key 匹配，供 FK 引用）。"""
        codes = list(dict.fromkeys(security_codes))
        result: dict[str, UUID] = {}
        for start in range(0, len(codes), _BATCH_SIZE):
            batch = codes[start : start + _BATCH_SIZE]
            rows = (
                await session.execute(
                    select(CompanyModel.company_id, CompanyModel.security_code).where(
                        CompanyModel.security_code.in_(batch)
                    )
                )
            ).all()
            result.update({code: company_id for company_id, code in rows})
        return result

    @staticmethod
    def _domain_rows(
        snapshot: IssuerDomainSnapshot,
        company_id_by_code: dict[str, UUID],
    ) -> list[dict]:
        now = datetime.now(UTC)
        rows: list[dict] = []
        for entry in snapshot.domains:
            company_id = company_id_by_code.get(entry.security_code)
            if company_id is None:
                continue
            rows.append(
                {
                    "domain_id": uuid.uuid4(),
                    "company_id": company_id,
                    "domain": entry.domain,
                    "source_url": entry.source_url,
                    "provider_key": entry.provider_key,
                    "verified_at": datetime.fromisoformat(entry.verified_at).replace(tzinfo=UTC),
                    "created_at": now,
                }
            )
        return rows
