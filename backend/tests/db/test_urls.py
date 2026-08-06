"""Tests for database URI conversion."""

from urllib.parse import quote

import pytest
from sqlalchemy import make_url

from app.db.urls import to_postgres_connection_uri


def test_converts_driver_and_preserves_parts() -> None:
    uri = to_postgres_connection_uri(
        "postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge?sslmode=disable"
    )
    parsed = make_url(uri)
    assert parsed.drivername == "postgresql"
    assert parsed.username == "user"
    assert parsed.password == "pass"
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 5433
    assert parsed.database == "insightforge"
    assert parsed.query.get("sslmode") == "disable"


def test_special_char_password() -> None:
    password = quote("p@ss:w/rd", safe="")
    uri = f"postgresql+psycopg://user:{password}@host:5432/db"
    parsed = make_url(to_postgres_connection_uri(uri))
    assert parsed.password == "p@ss:w/rd"


def test_wrong_driver_rejected() -> None:
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        to_postgres_connection_uri("postgresql://user:pass@host/db")


def test_error_does_not_include_password_or_url() -> None:
    with pytest.raises(ValueError) as exc_info:
        to_postgres_connection_uri("postgresql://user:supersecret@host/db")
    assert "supersecret" not in str(exc_info.value)
    assert "postgresql://" not in str(exc_info.value)


def test_invalid_url_rejected_without_echo() -> None:
    with pytest.raises(ValueError, match="invalid database_url format"):
        to_postgres_connection_uri("not a url:::")
