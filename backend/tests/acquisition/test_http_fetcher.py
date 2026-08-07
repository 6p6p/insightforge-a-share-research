"""Tests for SafePdfFetcher using httpx MockTransport; never touches the network."""

import os

import httpx
import pytest

from app.acquisition.http_fetcher import SafePdfFetcher
from app.core.errors import (
    SourceDownloadFailed,
    SourceFileTooLarge,
    SourceRedirectNotAllowed,
    SourceUrlNotAllowed,
)

_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
_ALLOWED = ["static.example.org", "example.org"]


def _fetcher(handler) -> SafePdfFetcher:
    return SafePdfFetcher(transport=httpx.MockTransport(handler))


def _pdf_response(content: bytes = _PDF) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={"content-type": "application/pdf", "content-length": str(len(content))},
    )


@pytest.mark.asyncio
async def test_fetch_downloads_pdf_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        return _pdf_response()

    fetcher = _fetcher(handler)
    pdf = await fetcher.fetch("https://static.example.org/2024/000001.pdf", _ALLOWED, 1024 * 1024)
    try:
        assert pdf.final_url == "https://static.example.org/2024/000001.pdf"
        assert pdf.reported_content_type == "application/pdf"
        assert pdf.reported_content_length == len(_PDF)
        assert pdf.content_stream.read() == _PDF
    finally:
        pdf.close()
    assert not os.path.exists(pdf.tmp_path)


@pytest.mark.asyncio
async def test_fetch_requires_https() -> None:
    fetcher = _fetcher(lambda request: _pdf_response())
    with pytest.raises(SourceUrlNotAllowed):
        await fetcher.fetch("http://static.example.org/a.pdf", _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_fetch_rejects_fragment() -> None:
    fetcher = _fetcher(lambda request: _pdf_response())
    with pytest.raises(SourceUrlNotAllowed):
        await fetcher.fetch("https://static.example.org/a.pdf#section", _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_fetch_rejects_userinfo() -> None:
    fetcher = _fetcher(lambda request: _pdf_response())
    with pytest.raises(SourceUrlNotAllowed):
        await fetcher.fetch("https://user:pass@static.example.org/a.pdf", _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_fetch_rejects_explicit_port() -> None:
    fetcher = _fetcher(lambda request: _pdf_response())
    with pytest.raises(SourceUrlNotAllowed):
        await fetcher.fetch("https://static.example.org:8443/a.pdf", _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_fetch_rejects_ip_address_host() -> None:
    fetcher = _fetcher(lambda request: _pdf_response())
    for url in (
        "https://192.168.1.10/a.pdf",
        "https://[::1]/a.pdf",
        "https://[2001:db8::1]/a.pdf",
        # 无括号 IPv6 被 urlparse 误判为 host:port，同样必须拒绝
        "https://2001:db8::1/a.pdf",
    ):
        with pytest.raises(SourceUrlNotAllowed):
            await fetcher.fetch(url, _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_fetch_rejects_domain_outside_allowed() -> None:
    fetcher = _fetcher(lambda request: _pdf_response())
    allowed = ["static.example.org"]
    for url in (
        "https://evil.example.org/a.pdf",
        "https://example.org/a.pdf",
        "https://example.org.evil.com/a.pdf",
        "https://static.example.org.evil.com/a.pdf",
        "https://example.com/a.pdf",
    ):
        with pytest.raises(SourceUrlNotAllowed):
            await fetcher.fetch(url, allowed, 1024)


@pytest.mark.asyncio
async def test_fetch_empty_allowed_domains_rejects_all() -> None:
    fetcher = _fetcher(lambda request: _pdf_response())
    with pytest.raises(SourceUrlNotAllowed):
        await fetcher.fetch("https://static.example.org/a.pdf", [], 1024)


@pytest.mark.asyncio
async def test_fetch_content_length_over_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _pdf_response(b"x" * 2048)

    fetcher = _fetcher(handler)
    with pytest.raises(SourceFileTooLarge):
        await fetcher.fetch("https://static.example.org/big.pdf", _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_fetch_streamed_body_over_limit() -> None:
    # content-length 声明小值，实际 body 超限 → 流式读上限触发
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"y" * 2048,
            headers={"content-length": "1", "content-type": "application/pdf"},
        )

    fetcher = _fetcher(handler)
    with pytest.raises(SourceFileTooLarge):
        await fetcher.fetch("https://static.example.org/big.pdf", _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_fetch_non_2xx_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/missing.pdf":
            return httpx.Response(404, content=b"")
        return httpx.Response(500, content=b"oops")

    fetcher = _fetcher(handler)
    with pytest.raises(SourceDownloadFailed):
        await fetcher.fetch("https://static.example.org/missing.pdf", _ALLOWED, 1024)
    with pytest.raises(SourceDownloadFailed):
        await fetcher.fetch("https://static.example.org/boom.pdf", _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_redirect_within_allowed_domain_succeeds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/final/000001.pdf"}, content=b"")
        return _pdf_response()

    fetcher = _fetcher(handler)
    pdf = await fetcher.fetch("https://static.example.org/redirect", _ALLOWED, 1024)
    try:
        assert pdf.content_stream.read() == _PDF
        assert pdf.final_url == "https://static.example.org/final/000001.pdf"
    finally:
        pdf.close()


@pytest.mark.asyncio
async def test_redirect_to_disallowed_domain_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(
                302,
                headers={"location": "https://evil.example.org/a.pdf"},
                content=b"",
            )
        return _pdf_response()

    fetcher = _fetcher(handler)
    allowed = ["static.example.org"]
    with pytest.raises(SourceUrlNotAllowed):
        await fetcher.fetch("https://static.example.org/redirect", allowed, 1024)


@pytest.mark.asyncio
async def test_redirect_loop_exceeds_max() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": request.url.path}, content=b"")

    fetcher = _fetcher(handler)
    with pytest.raises(SourceRedirectNotAllowed):
        await fetcher.fetch("https://static.example.org/a.pdf", _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_redirect_without_location_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={}, content=b"")

    fetcher = _fetcher(handler)
    with pytest.raises(SourceDownloadFailed):
        await fetcher.fetch("https://static.example.org/a.pdf", _ALLOWED, 1024)


@pytest.mark.asyncio
async def test_redirect_chain_within_limit_succeeds() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/r1":
            return httpx.Response(302, headers={"location": "/r2"}, content=b"")
        if request.url.path == "/r2":
            return httpx.Response(301, headers={"location": "/r3"}, content=b"")
        return _pdf_response()

    fetcher = _fetcher(handler)
    pdf = await fetcher.fetch("https://static.example.org/r1", _ALLOWED, 1024)
    try:
        assert pdf.content_stream.read() == _PDF
        assert pdf.final_url == "https://static.example.org/r3"
    finally:
        pdf.close()
    assert calls == ["/r1", "/r2", "/r3"]


@pytest.mark.asyncio
async def test_fetch_failure_cleans_up_temp_file(monkeypatch) -> None:
    import tempfile

    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr("app.acquisition.http_fetcher.tempfile.mkstemp", spy_mkstemp)

    def handler(request: httpx.Request) -> httpx.Response:
        # content-length 声明 1 字节，实际 body 2048 字节 → 流式读超限
        return httpx.Response(
            200,
            content=b"%PDF-" + b"x" * 2048,
            headers={"content-length": "1", "content-type": "application/pdf"},
        )

    fetcher = _fetcher(handler)
    with pytest.raises(SourceFileTooLarge):
        await fetcher.fetch("https://static.example.org/a.pdf", _ALLOWED, 1024)
    # _download 创建的唯一临时文件必须在异常传播前被删除
    assert len(created) == 1
    assert not os.path.exists(created[0])
