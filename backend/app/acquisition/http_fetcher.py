"""Safe PDF fetcher with strict scheme, domain and redirect policy."""

import ipaddress
import os
import tempfile
from dataclasses import dataclass
from typing import BinaryIO
from urllib.parse import urljoin, urlparse

import httpx

from app.core.errors import (
    SourceDownloadFailed,
    SourceFileTooLarge,
    SourceRedirectNotAllowed,
    SourceUrlNotAllowed,
)
from app.source_registry.url_policy import is_url_allowed

_MAX_REDIRECTS = 5
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_CHUNK_SIZE = 1024 * 1024

_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@dataclass
class FetchedPdf:
    final_url: str
    content_stream: BinaryIO
    tmp_path: str
    reported_content_type: str | None
    reported_content_length: int | None

    def close(self) -> None:
        try:
            self.content_stream.close()
        finally:
            try:
                os.unlink(self.tmp_path)
            except OSError:
                pass


class SafePdfFetcher:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = timeout or _DEFAULT_TIMEOUT

    async def fetch(
        self,
        url: str,
        allowed_domains: list[str],
        max_bytes: int,
    ) -> FetchedPdf:
        current = url
        redirects = 0
        while True:
            self._validate_url(current, allowed_domains)
            async with self._new_client() as client:
                async with client.stream(
                    "GET",
                    current,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in _REDIRECT_CODES:
                        if redirects >= _MAX_REDIRECTS:
                            raise SourceRedirectNotAllowed()
                        location = response.headers.get("location")
                        if not location:
                            raise SourceDownloadFailed()
                        next_url = urljoin(str(response.url), location)
                        self._validate_url(next_url, allowed_domains)
                        if next_url == current:
                            raise SourceRedirectNotAllowed()
                        current = next_url
                        redirects += 1
                        continue
                    if not 200 <= response.status_code < 300:
                        raise SourceDownloadFailed()
                    return await self._download(response, max_bytes, current)

    def _new_client(self) -> httpx.AsyncClient:
        kwargs: dict = {
            "follow_redirects": False,
            "trust_env": False,
            "timeout": self._timeout,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    async def _download(
        response: httpx.Response,
        max_bytes: int,
        final_url: str,
    ) -> FetchedPdf:
        reported_content_type = response.headers.get("content-type")
        content_length = response.headers.get("content-length")
        reported_content_length: int | None = None
        if content_length is not None:
            try:
                parsed_length = int(content_length)
                reported_content_length = parsed_length
                if parsed_length > max_bytes:
                    raise SourceFileTooLarge()
            except ValueError:
                pass

        fd, tmp_path = tempfile.mkstemp(prefix="fetch-", suffix=".pdf")
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise SourceFileTooLarge()
                    out.write(chunk)
            stream = open(tmp_path, "rb")
            return FetchedPdf(
                final_url=final_url,
                content_stream=stream,
                tmp_path=tmp_path,
                reported_content_type=reported_content_type,
                reported_content_length=reported_content_length,
            )
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _validate_url(url: str, allowed_domains: list[str]) -> None:
        try:
            parsed = urlparse(url)
        except ValueError:
            raise SourceUrlNotAllowed() from None
        if parsed.scheme != "https":
            raise SourceUrlNotAllowed()
        if parsed.fragment:
            raise SourceUrlNotAllowed()
        if parsed.username is not None or parsed.password is not None:
            raise SourceUrlNotAllowed()
        try:
            port = parsed.port
        except ValueError:
            # 无括号 IPv6 等 host 会被 urlparse 误判为 host:port，port 解析失败
            raise SourceUrlNotAllowed() from None
        if port is not None:
            raise SourceUrlNotAllowed()
        host = parsed.hostname
        if not host or _is_ip_address(host):
            raise SourceUrlNotAllowed()
        if not is_url_allowed(url, allowed_domains):
            raise SourceUrlNotAllowed()
