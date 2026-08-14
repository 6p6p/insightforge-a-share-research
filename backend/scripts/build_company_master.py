"""Build the versioned A-share company master snapshot (V1.1 P0-1).

数据来源（公开、合规、可追溯）：
- SSE（上海证券交易所）：官方 query API
  `http://query.sse.com.cn/sseQuery/commonQuery.do?sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L`
  （A 股列表；含证券代码 / 简称 / 公司全称 / 上市日期 / 退市日期）；
- SZSE（深圳证券交易所）：官方 ShowReport xlsx
  `http://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110`
  （主板 + 创业板 A 股列表；含 A 股代码 / 简称 / 公司全称 / 上市日期）；
- BSE（北京证券交易所）：官网 `bse.cn` 在构建环境不可达（TLS 阻断），
  使用公开证券数据终端（East Money）作为**已记录的降级来源**：
  - 北交所实时列表 `push2.eastmoney.com/api/qt/clist/get?fs=m:0+t:81+s:2048`
    （证券代码 / 简称 / 上市日期，用于**在板成员**判定）；
  - F10 基础信息 `datacenter.eastmoney.com/securities/api/data/v1/get?
    reportName=RPT_F10_BASIC_ORGINFO`（公司全称 / 曾用名）。
  权威性降级在 snapshot `sources[]` 与文档中显式记录。

板块由证券代码前缀确定性推导（不依赖各源板块字段）：
  600/601/603/605 -> sse_main；688/689 -> star；
  000/001/002/003 -> szse_main；300/301/302 -> chinext；
  43x/8xx/920xxx -> bse。

输出：`app/companies/master/company_master_v1.json`（versioned snapshot，随仓库提交）。
用法（backend 目录，insightforge conda env）：
    python -m scripts.build_company_master [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from zipfile import ZipFile

_SSE_QUERY_URL = (
    "http://query.sse.com.cn/sseQuery/commonQuery.do"
    "?sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L"
    "&pageHelp.pageSize=10000&pageHelp.pageNo=1&pageHelp.cacheSize=1"
    "&pageHelp.beginPage=1&pageHelp.endPage=1"
)
_SZSE_XLSX_URL = "http://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110"
_EM_ORGINFO_URL = (
    "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
    "reportName=RPT_F10_BASIC_ORGINFO"
    "&columns=SECURITY_CODE,SECURITY_NAME_ABBR,ORG_NAME,FORMERNAME,SECURITY_TYPE"
    "&pageSize=600&pageNumber={page}&sortColumns=SECURITY_CODE&sortTypes=1"
)
_EM_ORGINFO_FILTER = (
    "&filter=(SECURITY_TYPE%3D%22%E5%8C%97%E4%BA%AC%E8%AF%81%E5%88%B8%E4%BA%A4"
    "%E6%98%93%E6%89%80A%E8%82%A1%22)"
)
_SINA_HS_A_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a"
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = BACKEND_ROOT / "app" / "companies" / "master" / "company_master_v1.json"

_SSL_CTX = ssl.create_default_context()

_BOARD_BY_PREFIX: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(688|689)"), "star"),
    (re.compile(r"^(600|601|603|605)"), "sse_main"),
    (re.compile(r"^(300|301|302)"), "chinext"),
    (re.compile(r"^(000|001|002|003)"), "szse_main"),
]
_EXCHANGE_BY_PREFIX: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(600|601|603|605|688|689)"), "SSE"),
    (re.compile(r"^(000|001|002|003|300|301|302)"), "SZSE"),
]
_BSE_PREFIX = re.compile(r"^(4[0-9]{2}|8[0-9]{2}|920)")


def _http_get(url: str, referer: str, timeout: int = 60, retries: int = 6) -> bytes:
    import time

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "Chrome/120.0 Safari/537.36"
                    ),
                    "Referer": referer,
                    "Accept": "*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - 官方站点偶发瞬断，重试后仍失败才上抛
            last_error = exc
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"fetch failed after {retries} attempts: {last_error}") from last_error


def _board_for(code: str, exchange: str) -> str:
    if exchange == "BSE":
        return "bse"
    for pattern, board in _BOARD_BY_PREFIX:
        if pattern.match(code):
            return board
    raise ValueError(f"code {code} has no known board")


def _exchange_for(code: str) -> str:
    for pattern, exchange in _EXCHANGE_BY_PREFIX:
        if pattern.match(code):
            return exchange
    if _BSE_PREFIX.match(code):
        return "BSE"
    raise ValueError(f"code {code} does not match any A-share exchange prefix")


# ------------------------------------------------------------------ fetch


def fetch_sse() -> tuple[list[dict], dict]:
    raw = json.loads(_http_get(_SSE_QUERY_URL, "http://www.sse.com.cn/"))
    rows = raw.get("result") or []
    companies: list[dict] = []
    skipped: dict[str, int] = {}
    # A/B 股双重上市同代码会出现两行（STOCK_TYPE=1 为 A 股行，优先保留）。
    best_by_code: dict[str, dict] = {}
    for row in rows:
        code = str(row.get("A_STOCK_CODE") or "")
        if not re.fullmatch(r"\d{6}", code):
            skipped["bad_code"] = skipped.get("bad_code", 0) + 1
            continue
        try:
            exchange = _exchange_for(code)
        except ValueError:
            # 900xxx（沪 B 股）等非 A 股前缀：排除。
            skipped["non_a_share"] = skipped.get("non_a_share", 0) + 1
            continue
        if exchange != "SSE":
            skipped["non_sse_row"] = skipped.get("non_sse_row", 0) + 1
            continue
        if str(row.get("DELIST_DATE") or "-") not in ("-", ""):
            skipped["delisted"] = skipped.get("delisted", 0) + 1
            continue
        existing = best_by_code.get(code)
        if existing is not None and str(existing.get("STOCK_TYPE")) == "1":
            continue
        best_by_code[code] = row
    for row in best_by_code.values():
        code = str(row.get("A_STOCK_CODE"))
        short = str(row.get("COMPANY_ABBR") or "").strip()
        full = str(row.get("FULL_NAME") or "").strip()
        if not short or not full or short == "-":
            skipped["missing_name"] = skipped.get("missing_name", 0) + 1
            continue
        listing_date = str(row.get("LIST_DATE") or "").strip()
        listing_date = (
            f"{listing_date[:4]}-{listing_date[4:6]}-{listing_date[6:8]}"
            if re.fullmatch(r"\d{8}", listing_date)
            else None
        )
        companies.append(
            {
                "security_code": code,
                "exchange": "SSE",
                "board": _board_for(code, "SSE"),
                "listing_status": "listed",
                "listing_date": listing_date,
                "official_name": full,
                "short_name": short,
            }
        )
    return companies, skipped


def _xlsx_inline_strings(path: Path) -> tuple[list[list[str]], dict]:
    with ZipFile(path) as zf:
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    rows: list[list[str]] = []
    for row_xml in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
        cells = re.findall(
            r'<c[^>]*?(?:t="(\w+)")?[^>]*>(?:<v>(.*?)</v>|<is><t[^>]*>(.*?)</t></is>)?</c>',
            row_xml,
        )
        values: list[str] = []
        for cell_type, value, inline in cells:
            if cell_type == "inlineStr" or inline:
                values.append(inline or "")
            elif value is not None:
                values.append(value)
            else:
                values.append("")
        rows.append(values)
    return rows, {}


def fetch_szse(tmp_dir: Path) -> tuple[list[dict], dict]:
    xlsx_path = tmp_dir / "szse_list.xlsx"
    data = _http_get(_SZSE_XLSX_URL, "http://www.szse.cn/market/product/stock/list/index.html")
    xlsx_path.write_bytes(data)
    rows, _ = _xlsx_inline_strings(xlsx_path)
    companies: list[dict] = []
    skipped: dict[str, int] = {}
    for values in rows[1:]:
        if len(values) < 7:
            continue
        board_raw = values[0].strip()
        full = values[1].strip()
        code = values[4].strip()
        short = values[5].strip()
        listing_date = values[6].strip()
        if not re.fullmatch(r"\d{6}", code):
            skipped["bad_code"] = skipped.get("bad_code", 0) + 1
            continue
        if board_raw not in ("主板", "创业板"):
            skipped["other_board"] = skipped.get("other_board", 0) + 1
            continue
        if not full or not short:
            skipped["missing_name"] = skipped.get("missing_name", 0) + 1
            continue
        companies.append(
            {
                "security_code": code,
                "exchange": "SZSE",
                "board": _board_for(code, "SZSE"),
                "listing_status": "listed",
                "listing_date": listing_date or None,
                "official_name": full,
                "short_name": short,
            }
        )
    return companies, skipped


def fetch_bse() -> tuple[list[dict], dict]:
    """北交所成员 = 新浪全 A 节点（hs_a，含 bj 前缀，实时行情节点 = 在板）；
    全称/曾用名来自 East Money F10 ORGINFO（datacenter API）。"""
    members: dict[str, dict] = {}
    page = 1
    while page <= 80:
        rows = json.loads(
            _http_get(_SINA_HS_A_URL.format(page=page), "https://finance.sina.com.cn/")
        )
        if not rows:
            break
        for item in rows:
            symbol = str(item.get("symbol") or "")
            code = str(item.get("code") or "")
            if not symbol.startswith("bj") or not re.fullmatch(r"\d{6}", code):
                continue
            members[code] = {"short_name": str(item.get("name") or "").strip()}
        if len(rows) < 100:
            break
        page += 1

    orginfo: dict[str, dict] = {}
    page = 1
    while True:
        url = _EM_ORGINFO_URL.format(page=page) + _EM_ORGINFO_FILTER
        payload = json.loads(_http_get(url, "https://data.eastmoney.com/"))
        result = payload.get("result") or {}
        rows = result.get("data") or []
        for row in rows:
            code = str(row.get("SECURITY_CODE") or "")
            orginfo[code] = {
                "official_name": str(row.get("ORG_NAME") or "").strip(),
                "former_names": [
                    name.strip()
                    for name in re.split(r"[,，、;；]", str(row.get("FORMERNAME") or ""))
                    if name.strip()
                ],
            }
        if len(rows) < 600 or page >= 5:
            break
        page += 1

    companies: list[dict] = []
    skipped: dict[str, int] = {}
    for code, member in sorted(members.items()):
        info = orginfo.get(code) or {}
        short = member["short_name"]
        full = info.get("official_name") or ""
        if not full or not short:
            skipped["missing_name"] = skipped.get("missing_name", 0) + 1
            continue
        companies.append(
            {
                "security_code": code,
                "exchange": "BSE",
                "board": "bse",
                "listing_status": "listed",
                "listing_date": None,
                "official_name": full,
                "short_name": short,
                "former_names": info.get("former_names") or [],
            }
        )
    return companies, skipped


# ------------------------------------------------------------------ validate


def _validate(companies: list[dict]) -> None:
    seen_keys: set[str] = set()
    seen_codes: set[str] = set()
    for company in companies:
        code = company["security_code"]
        exchange = company["exchange"]
        identity_key = f"{exchange}:{code}"
        if identity_key in seen_keys:
            raise ValueError(f"duplicate identity_key {identity_key}")
        seen_keys.add(identity_key)
        if (exchange, code) in seen_codes:
            raise ValueError(f"duplicate (exchange, code) {(exchange, code)}")
        seen_codes.add((exchange, code))
        actual_exchange = _exchange_for(code)
        if actual_exchange != exchange:
            raise ValueError(f"code {code} claimed {exchange} but prefix implies {actual_exchange}")
        if company["board"] != _board_for(code, exchange):
            raise ValueError(f"code {code} board mismatch")
        if not company["official_name"] or not company["short_name"]:
            raise ValueError(f"code {code} missing name")
        if company["listing_status"] != "listed":
            raise ValueError(f"code {code} unexpected status")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the A-share company master snapshot")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    tmp_dir = Path(tempfile.mkdtemp(prefix="company_master_build_"))
    try:
        sse, sse_skipped = fetch_sse()
        szse, szse_skipped = fetch_szse(tmp_dir)
        bse, bse_skipped = fetch_bse()
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
    all_companies = sse + szse + bse
    _validate(all_companies)

    as_of = date.today().isoformat()
    sources = [
        {
            "exchange": "SSE",
            "source_name": "sse_official_query_api",
            "url": _SSE_QUERY_URL,
            "fetched_at": datetime.now(UTC).isoformat(),
            "row_count": len(sse),
            "skipped": sse_skipped,
            "authority_tier": 1,
            "note": "上海证券交易所官方 A 股列表",
        },
        {
            "exchange": "SZSE",
            "source_name": "szse_official_showreport_xlsx",
            "url": _SZSE_XLSX_URL,
            "fetched_at": datetime.now(UTC).isoformat(),
            "row_count": len(szse),
            "skipped": szse_skipped,
            "authority_tier": 1,
            "note": "深圳证券交易所官方上市公司列表（主板+创业板）",
        },
        {
            "exchange": "BSE",
            "source_name": "sina_hs_a_membership_eastmoney_orginfo_fallback",
            "url": _SINA_HS_A_URL.format(page=1).split("?")[0],
            "fetched_at": datetime.now(UTC).isoformat(),
            "row_count": len(bse),
            "skipped": bse_skipped,
            "authority_tier": 3,
            "note": (
                "BSE 官网（bse.cn）在本构建环境 TLS 阻断；在板成员来自新浪全 A 实时"
                "行情节点（hs_a，bj 前缀），公司全称/曾用名来自 East Money F10 "
                "ORGINFO（datacenter API），已显式降级记录"
            ),
        },
    ]
    alias_count = sum(2 + len(company.get("former_names") or []) for company in all_companies)
    snapshot = {
        "schema_version": 1,
        "snapshot_version": f"company-master-v1-{as_of}",
        "as_of": as_of,
        "sources": sources,
        "companies": all_companies,
        "counts": {
            "companies": len(all_companies),
            "aliases": alias_count,
            "by_exchange": {
                "SSE": len(sse),
                "SZSE": len(szse),
                "BSE": len(bse),
            },
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}")
    print(f"companies={len(all_companies)} aliases={alias_count}")
    print(f"by_exchange={snapshot['counts']['by_exchange']}")
    print(f"skipped: sse={sse_skipped} szse={szse_skipped} bse={bse_skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
