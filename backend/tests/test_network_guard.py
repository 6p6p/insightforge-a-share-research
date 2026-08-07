"""Shared network-isolation guard tests.

验证顶层 conftest 的 autouse `_forbid_real_http` fixture：
- 真实外部 httpx transport 请求（异步与同步）都被阻止；
- 本地回环地址（127.0.0.1）放行，不影响 PostgreSQL / Docker Chroma；
- 0.0.0.0 不放行；
- MockTransport 不受影响；
- FastAPI TestClient（ASGI transport）不受影响。
"""

import asyncio

import httpx
import pytest


def _request(url: str) -> httpx.Request:
    return httpx.Request("GET", url)


def test_real_transport_external_host_is_blocked() -> None:
    async def probe() -> None:
        transport = httpx.AsyncHTTPTransport()
        try:
            with pytest.raises(AssertionError, match="real external HTTP is forbidden in tests"):
                await transport.handle_async_request(_request("https://evil.example.com/x.pdf"))
        finally:
            await transport.aclose()

    asyncio.run(probe())


def test_real_transport_sync_external_host_is_blocked() -> None:
    transport = httpx.HTTPTransport()
    try:
        with pytest.raises(AssertionError, match="real external HTTP is forbidden in tests"):
            transport.handle_request(_request("https://evil.example.com/x.pdf"))
    finally:
        transport.close()


def test_real_transport_loopback_is_not_blocked() -> None:
    """回环地址放行给真实 transport：结果不是 AssertionError（连接失败或成功）。"""

    async def probe() -> None:
        transport = httpx.AsyncHTTPTransport()
        try:
            try:
                await transport.handle_async_request(_request("http://127.0.0.1:1/never-listening"))
            except AssertionError as exc:
                pytest.fail(f"loopback request must not be blocked: {exc}")
            except httpx.ConnectError:
                pass  # 端口未监听 → 连接失败，属预期，说明 guard 放行了
        finally:
            await transport.aclose()

    asyncio.run(probe())


def test_real_transport_zero_zero_zero_zero_is_blocked() -> None:
    """0.0.0.0 不是回环放行地址：guard 把它当外部地址拦截。"""

    async def probe() -> None:
        transport = httpx.AsyncHTTPTransport()
        try:
            with pytest.raises(AssertionError, match="real external HTTP is forbidden in tests"):
                await transport.handle_async_request(_request("http://0.0.0.0:1/x"))
        finally:
            await transport.aclose()

    asyncio.run(probe())


@pytest.mark.asyncio
async def test_mock_transport_not_affected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.7\n%%EOF\n")

    transport = httpx.MockTransport(handler)
    request = _request("https://www.sse.com.cn/2024/000001.pdf")
    response = await transport.handle_async_request(request)
    assert response.status_code == 200
    assert await response.aread() == b"%PDF-1.7\n%%EOF\n"


def test_asgi_testclient_not_affected(client) -> None:
    """FastAPI TestClient 走 ASGI transport，真实请求外部 host 前由路由处理。"""
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
