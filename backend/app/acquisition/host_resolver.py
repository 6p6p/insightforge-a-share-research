"""DNS / SSRF preflight host resolver (stage 2D.2A).

在发起 HTTP 请求前把 hostname 解析为 IP 并拒绝非全局地址，作为纵深防御
（defense-in-depth）：即使 DNS 被污染到内网地址，请求也已在传输前被拦截。

重要边界：本模块只做"请求前的防御性检查"，**不宣称**传输层 DNS pinning
（不强制连接已预检的 IP、不监控握手实际对端）。拒绝语义：任一解析出的
IP 落在非全局范围（loopback/private/link-local/multicast/reserved/
unspecified/shared）即拒绝，因为无法控制 OS 连接其中的哪一个。
"""

import asyncio
import ipaddress
import socket
from typing import Protocol

from app.core.errors import DomainError


class HostResolutionError(DomainError):
    """hostname 无法解析、解析为空，或解析出非全局地址。"""

    code = "host_resolution_failed"
    message = "host resolution failed"


class HostResolver(Protocol):
    async def resolve(self, hostname: str) -> list[str]:
        """返回 hostname 的 IP 字符串列表（IPv4 与 IPv6）。"""


def _is_non_global(ip_str: str) -> bool:
    try:
        address = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    # is_shared（100.64.0.0/10）只存在于 IPv4Address，IPv6 用 getattr 兜底
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or getattr(address, "is_shared", False)
    )


class SystemHostResolver:
    """系统 getaddrinfo 的 asyncio 友好封装（IPv4 + IPv6）。"""

    async def resolve(self, hostname: str) -> list[str]:
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                hostname,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise HostResolutionError() from exc
        ips: list[str] = []
        for family, _socktype, _proto, _canonname, sockaddr in infos:
            if family in (socket.AF_INET, socket.AF_INET6):
                ip = sockaddr[0]
                # IPv6 scope-id 形式（fe80::1%eth0）去掉 %scope
                ip = ip.split("%", 1)[0]
                ips.append(ip)
        return ips


async def validate_hostname_addresses(hostname: str, ips: list[str]) -> None:
    """拒绝空解析或任一非全局地址（不区分 IPv4/IPv6）。"""
    if not ips:
        raise HostResolutionError()
    if any(_is_non_global(ip) for ip in ips):
        raise HostResolutionError()
