"""Tests for the bundled issuer-domain snapshot contract (V1.1 closure)."""

import pytest

from app.issuer_domains.snapshot import (
    BUNDLED_SNAPSHOT_PATH,
    IssuerDomainSnapshot,
    load_bundled_snapshot,
)


def test_bundled_snapshot_exists() -> None:
    assert BUNDLED_SNAPSHOT_PATH.exists()


def test_bundled_snapshot_loads_and_validates() -> None:
    loaded = load_bundled_snapshot()
    snapshot = loaded.snapshot
    assert snapshot.schema_version == 1
    assert snapshot.snapshot_version.startswith("issuer-domains-v1-")
    assert len(snapshot.domains) > 5000
    assert loaded.content_sha256
    assert loaded.byte_size > 0


def test_bundled_snapshot_exchange_coverage() -> None:
    snapshot = load_bundled_snapshot().snapshot
    by_exchange: dict[str, int] = {}
    for entry in snapshot.domains:
        by_exchange[entry.exchange] = by_exchange.get(entry.exchange, 0) + 1
    assert by_exchange.get("SSE", 0) > 1000
    assert by_exchange.get("SZSE", 0) > 1000
    assert by_exchange.get("BSE", 0) > 100


def test_bundled_snapshot_has_key_company() -> None:
    snapshot = load_bundled_snapshot().snapshot
    catl = [e for e in snapshot.domains if e.security_code == "300750"]
    assert catl, "宁德时代(300750) 应有官网域名"
    assert catl[0].domain.endswith("catl.com")


def test_snapshot_validation_rejects_bad_domain() -> None:
    with pytest.raises(Exception):
        IssuerDomainSnapshot.model_validate(
            {
                "schema_version": 1,
                "snapshot_version": "x",
                "domains": [
                    {
                        "security_code": "300750",
                        "exchange": "SZSE",
                        "domain": "bad domain!",
                        "source_url": "https://bad domain!",
                        "provider_key": "issuer_official",
                        "verified_at": "2026-08-15",
                    }
                ],
            }
        )


def test_snapshot_validation_rejects_url_domain_mismatch() -> None:
    snapshot = IssuerDomainSnapshot(
        snapshot_version="x",
        domains=[
            {
                "security_code": "300750",
                "exchange": "SZSE",
                "domain": "www.catl.com",
                "source_url": "https://www.vanke.com",
                "provider_key": "issuer_official",
                "verified_at": "2026-08-15",
            }
        ],
    )
    with pytest.raises(ValueError):
        snapshot.validate_consistency()
