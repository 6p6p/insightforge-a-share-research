"""CLI: import / refresh the A-share company master from a versioned snapshot.

用法（backend 目录，insightforge conda env）：
    python -m app.cli.import_company_master                 # 导入 bundled snapshot
    python -m app.cli.import_company_master --snapshot PATH # 显式 snapshot 文件
    python -m app.cli.import_company_master --force         # companies 非空时 refresh upsert

语义：
- 默认（companies 表非空 → 拒绝，除非 --force）：与启动 bootstrap 一致，
  保护既有 CompanyIdentity；
- --force：按 identity_key upsert 名称/板块/上市状态（不删除任何行），
  aliases 补缺不覆盖；
- 同一 (snapshot_version, content_sha256) 已登记 → replay（0 写）。
- 依赖 Source Registry 已 seed（sse/szse/bse provider 存在）。
"""

import argparse
import asyncio
from pathlib import Path

from app.companies.master.snapshot import BUNDLED_SNAPSHOT_PATH, load_bundled_snapshot
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.services.company_master_service import (
    CompanyMasterBootstrapError,
    CompanyMasterBootstrapService,
)

configure_asyncio_runtime()


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Import the A-share company master")
    parser.add_argument(
        "--snapshot",
        type=str,
        default=None,
        help=f"snapshot JSON path (default: {BUNDLED_SNAPSHOT_PATH.name})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="import even when companies table is non-empty (refresh upsert)",
    )
    args = parser.parse_args()

    settings = get_settings()
    database = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        service = CompanyMasterBootstrapService(database.session_factory())
        if args.snapshot:
            from app.companies.master.snapshot import load_snapshot_file

            loaded = load_snapshot_file(Path(args.snapshot))
        else:
            loaded = load_bundled_snapshot()

        async with database.session_factory()() as session:
            from sqlalchemy import select

            from app.db.models.company import CompanyModel

            existing = (await session.execute(select(CompanyModel.company_id).limit(1))).first()
        if existing is not None and not args.force:
            print(
                "companies table is not empty; refusing import without --force "
                "(existing CompanyIdentity must not be overwritten)"
            )
            return 2
        try:
            result = await service.import_snapshot(loaded, insert_only=not args.force)
        except CompanyMasterBootstrapError as exc:
            print(f"import failed [{exc.code}]: {exc.message}")
            return 2
        print(
            f"company master import: version={result.snapshot_version} "
            f"companies={result.imported_companies} aliases={result.imported_aliases} "
            f"skipped={result.skipped} replayed={result.replayed} repair={result.repair}"
        )
        return 0
    finally:
        await database.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
