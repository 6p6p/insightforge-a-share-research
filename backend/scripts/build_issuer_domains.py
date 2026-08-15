# -*- coding: utf-8 -*-
"""Build the A-share issuer official-domain snapshot (V1.1 closure).

数据来源（只取官方权威来源，降级逐条记录）：
1. 深交所上市公司名录 xlsx（CATALOGID=1110）「公司网址」列（Tier-1 官方，
   SZSE 公司首选）；
2. 东方财富 F10 ORGINFO（datacenter API）ORG_WEB 列（Tier-3 数据供应商，
   SSE / BSE 公司 + SZSE 缺失回退）。

输出 `app/issuer_domains/issuer_domains_v1.json`（schema 见
`app/issuer_domains/snapshot.py`）。只收录 bundled company master 中存在的
公司（导入时 company_id FK 必须能解析）。

用法：
    conda run -n insightforge python scripts/build_issuer_domains.py [--out PATH]
"""

import argparse
import io
import json
import re
import ssl
import tempfile
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from zipfile import ZipFile

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = BACKEND_ROOT / "app" / "issuer_domains" / "issuer_domains_v1.json"
COMPANY_MASTER_PATH = BACKEND_ROOT / "app" / "companies" / "master" / "company_master_v1.json"

_SZSE_XLSX_URL = "http://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1110"
_EM_ORGINFO_URL = (
    "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
    "reportName=RPT_F10_BASIC_ORGINFO"
    "&columns=SECURITY_CODE,SECURITY_TYPE,ORG_WEB"
    "&pageSize=600&pageNumber={page}&sortColumns=SECURITY_CODE&sortTypes=1"
)

# EM SECURITY_TYPE → exchange（A 股类型白名单；排除新三板/CDR/B 股）。
_EM_TYPE_TO_EXCHANGE = {
    "上交所主板A股": "SSE",
    "上交所科创板A股": "SSE",
    "上交所风险警示板A股": "SSE",
    "深交所主板A股": "SZSE",
    "深交所创业板A股": "SZSE",
    "深交所风险警示板A股": "SZSE",
    "北京证券交易所A股": "BSE",
}

_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
)
_CODE_RE = re.compile(r"^\d{6}$")

_SSL_CTX = ssl.create_default_context()


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


def _xlsx_inline_strings(path: Path) -> list[list[str]]:
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
    return rows


def normalize_website(raw: str) -> str | None:
    """「公司网址」→ 规范 hostname domain（无 scheme / 无 path / 无端口）。

    值可能形如 `www.vanke.com`、`http://www.x.com/`、`www.x.com:8080`、
    `x.com/index.html`。只保留 hostname 部分；非法 → None。
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    text = re.sub(r"^[a-z]+://", "", text)
    # 去掉 path / query / fragment / port（端口域名无法做 URL 校验）。
    text = re.sub(r"[/?#].*$", "", text)
    text = re.sub(r":[0-9]+$", "", text)
    text = text.rstrip(".")
    if not _HOST_RE.fullmatch(text):
        return None
    return text


def load_master_codes() -> set[tuple[str, str]]:
    """bundled company master 的 (exchange, code) 集合（FK 可解析性校验用）。"""
    raw = COMPANY_MASTER_PATH.read_bytes()
    payload = json.loads(raw)
    return {
        (str(entry["exchange"]), str(entry["security_code"]))
        for entry in payload.get("companies", [])
    }


def fetch_szse_websites(tmp_dir: Path) -> tuple[dict[str, str], dict]:
    """SZSE 官方名录 xlsx「公司网址」列（列 18）。"""
    xlsx_path = tmp_dir / "szse_list.xlsx"
    data = _http_get(_SZSE_XLSX_URL, "http://www.szse.cn/market/product/stock/list/index.html")
    xlsx_path.write_bytes(data)
    rows = _xlsx_inline_strings(xlsx_path)
    websites: dict[str, str] = {}
    skipped: dict[str, int] = {}
    for values in rows[1:]:
        if len(values) < 19:
            continue
        code = values[4].strip()
        if not _CODE_RE.fullmatch(code):
            skipped["bad_code"] = skipped.get("bad_code", 0) + 1
            continue
        domain = normalize_website(values[18])
        if domain is None:
            skipped["no_website"] = skipped.get("no_website", 0) + 1
            continue
        websites[code] = domain
    return websites, skipped


def fetch_em_websites() -> tuple[dict[str, tuple[str, str]], dict]:
    """EM F10 ORGINFO ORG_WEB（全 A 股分页，白名单 SECURITY_TYPE）。"""
    websites: dict[str, tuple[str, str]] = {}  # code -> (exchange, domain)
    skipped: dict[str, int] = {}
    page = 1
    while page <= 50:
        url = _EM_ORGINFO_URL.format(page=page)
        payload = json.loads(_http_get(url, "https://data.eastmoney.com/"))
        result = payload.get("result") or {}
        rows = result.get("data") or []
        for row in rows:
            code = str(row.get("SECURITY_CODE") or "")
            sec_type = str(row.get("SECURITY_TYPE") or "")
            exchange = _EM_TYPE_TO_EXCHANGE.get(sec_type)
            if not _CODE_RE.fullmatch(code):
                skipped["bad_code"] = skipped.get("bad_code", 0) + 1
                continue
            if exchange is None:
                skipped["non_a_share"] = skipped.get("non_a_share", 0) + 1
                continue
            domain = normalize_website(str(row.get("ORG_WEB") or ""))
            if domain is None:
                skipped["no_website"] = skipped.get("no_website", 0) + 1
                continue
            websites[code] = (exchange, domain)
        if len(rows) < 600:
            break
        page += 1
    return websites, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the issuer official-domain snapshot")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    master_codes = load_master_codes()
    print(f"company master codes: {len(master_codes)}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="issuer_domains_build_"))
    szse_websites, szse_skipped = fetch_szse_websites(tmp_dir)
    print(f"SZSE xlsx websites: {len(szse_websites)} (skipped: {szse_skipped})")
    em_websites, em_skipped = fetch_em_websites()
    print(f"EM ORGINFO websites: {len(em_websites)} (skipped: {em_skipped})")

    # SZSE 公司优先官方名录；其余用 EM；均缺失 → 跳过。
    entries: list[dict] = []
    skipped_total: dict[str, int] = {}
    for code, domain in sorted(szse_websites.items()):
        exchange = "SZSE"
        if (exchange, code) not in master_codes:
            skipped_total["not_in_master"] = skipped_total.get("not_in_master", 0) + 1
            continue
        entries.append(
            {
                "security_code": code,
                "exchange": exchange,
                "domain": domain,
                "source_url": f"https://{domain}",
            }
        )
    for code, (exchange, domain) in sorted(em_websites.items()):
        if exchange == "SZSE" and code in szse_websites:
            continue
        if (exchange, code) not in master_codes:
            skipped_total["not_in_master"] = skipped_total.get("not_in_master", 0) + 1
            continue
        entries.append(
            {
                "security_code": code,
                "exchange": exchange,
                "domain": domain,
                "source_url": f"https://{domain}",
            }
        )

    print(f"total entries: {len(entries)} (skipped: {skipped_total})")
    by_exchange: dict[str, int] = {}
    for entry in entries:
        by_exchange[entry["exchange"]] = by_exchange.get(entry["exchange"], 0) + 1
    print("by exchange:", by_exchange)

    if len(entries) <= 1000:
        raise RuntimeError(f"issuer domain snapshot too small: {len(entries)}")

    today = date.today().isoformat()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    snapshot = {
        "schema_version": 1,
        "snapshot_version": f"issuer-domains-v1-{today}",
        "as_of": today,
        "sources": [
            {
                "source_name": "深交所上市公司名录（公司网址列）",
                "url": _SZSE_XLSX_URL,
                "fetched_at": now,
                "row_count": len(szse_websites),
                "domain_count": sum(1 for c in szse_websites),
                "authority_tier": 1,
                "note": "SZSE 公司首选官方来源",
            },
            {
                "source_name": "东方财富 F10 ORGINFO（ORG_WEB）",
                "url": "https://datacenter.eastmoney.com/securities/api/data/v1/get",
                "fetched_at": now,
                "row_count": len(em_websites),
                "domain_count": len(em_websites),
                "authority_tier": 3,
                "note": "SSE / BSE 公司 + SZSE 缺失回退；Tier-3 供应商",
            },
        ],
        "domains": [
            {
                **entry,
                "provider_key": "issuer_official",
                "verified_at": today,
            }
            for entry in entries
        ],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
