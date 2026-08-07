"""CLI: fetch a World Bank indicator for one country (development diagnostic).

调用方式：
    conda run -n insightforge python -m app.cli.fetch_world_bank_macro \\
        --country CHN --indicator SP.POP.TOTL \\
        --start-year 2020 --end-year 2024

行为：
- 构造 MacroQuery 并调用 WorldBankProvider.fetch；
- JSON 报告输出到 stdout，日志输出到 stderr（ensure_ascii=true，兼容 Windows conda run）；
- Decimal 输出为字符串，禁止转 float；datetime/date 输出 ISO 格式；
- 不写数据库、不保存响应正文、不写本地文件；
- 失败输出稳定错误 code，不向 stdout 输出 traceback。

退出码：
0 成功；2 输入错误；3 Provider 配置错误；4 API/网络/响应错误。
"""

import argparse
import asyncio
import dataclasses
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

import structlog

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.macro.contracts import MacroFetchResult, MacroQuery
from app.macro.world_bank.errors import WorldBankError, WorldBankProviderNotReady
from app.macro.world_bank.provider import WorldBankProvider

configure_asyncio_runtime()


def _configure_macro_logging() -> None:
    """探测日志走 stderr，保证 stdout 只输出 JSON 报告。"""
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def _jsonable(value: object) -> object:
    """递归转换为 JSON 可序列化对象：Decimal → str，date/datetime → ISO，枚举 → value。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    return value


def _report(result: MacroFetchResult) -> dict:
    payload = dataclasses.asdict(result)
    payload["provider_snapshot"] = {
        "acquisition_method": result.acquisition_method.value,
        "authority_tier": int(result.authority_tier),
        "critical_claim_eligible": result.critical_claim_eligible,
        "capabilities": [cap.value for cap in result.provider_capabilities],
        "source_id": result.source_id,
    }
    return _jsonable(payload)


def _emit_error(code: str, message: str) -> None:
    print(json.dumps({"error": code, "message": message}, ensure_ascii=True))


async def _run(query: MacroQuery) -> int:
    settings = get_settings()
    database = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        provider = WorldBankProvider(database.session_factory())
        try:
            result = await provider.fetch(query)
        except WorldBankProviderNotReady as exc:
            _emit_error(exc.code, str(exc))
            return 3
        except WorldBankError as exc:
            _emit_error(exc.code, str(exc))
            return 4
        except Exception as exc:  # noqa: BLE001
            _emit_error("unexpected_error", type(exc).__name__)
            return 4
    finally:
        await database.dispose()
    print(json.dumps(_report(result), ensure_ascii=True, indent=2))
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="World Bank 宏观指标获取（开发期诊断）")
    parser.add_argument("--country", required=True, help="国家代码（ISO2 或 ISO3，如 CHN）")
    parser.add_argument("--indicator", required=True, help="指标代码（如 SP.POP.TOTL）")
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    args = parser.parse_args(argv)

    try:
        query = MacroQuery(
            provider_key="world_bank",
            indicator_code=args.indicator,
            country_code=args.country,
            start_year=args.start_year,
            end_year=args.end_year,
        )
    except ValueError as exc:
        _emit_error("invalid_input", str(exc))
        return 2
    return asyncio.run(_run(query))


if __name__ == "__main__":
    _configure_macro_logging()
    raise SystemExit(_main(sys.argv[1:]))
