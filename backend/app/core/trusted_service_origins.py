"""Explicit administrator trust for container-to-host/local inference origins.

This exception belongs only to configured service adapters. Document imports and
arbitrary media URLs retain their independent public-address restrictions.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit, urlunsplit
import asyncio

import httpx

from app.core.errors import AppError

ENVIRONMENT_VARIABLE = 'TRUSTED_LOCAL_SERVICE_ORIGINS'
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7', '::1/128', '127.0.0.0/8'))


def _local_hostname(hostname: str) -> bool:
    if hostname in {'localhost', 'host.docker.internal', 'host.containers.internal'}:
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        # Explicit Compose service names and mDNS names are local-address trust,
        # never an implicit exemption for arbitrary Internet domains.
        return bool(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', hostname)
                    or (hostname.endswith('.local') and re.fullmatch(r'[a-z0-9.-]+', hostname)))
    if address==ipaddress.ip_address('fd00:ec2::254'):return False
    return any(address.version == network.version and address in network for network in _PRIVATE_NETWORKS)


def service_origin(value: str, *, origin_only: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except (ValueError, TypeError) as error:
        raise AppError(422, 'invalid_local_service_origin', '受信服务必须填写完整的HTTP(S) origin，例如 http://host.docker.internal:8189') from error
    if (parsed.scheme not in {'http', 'https'} or not hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or (origin_only and parsed.path not in {'', '/'})):
        raise AppError(422, 'invalid_local_service_origin', '受信服务 origin 不能包含路径、凭据、查询参数或片段')
    hostname = hostname.lower()
    host = '[' + hostname + ']' if ':' in hostname else hostname
    effective_port = port if port is not None else 443 if parsed.scheme == 'https' else 80
    return f'{parsed.scheme}://{host}:{effective_port}'


def configured_local_origins() -> frozenset[str]:
    result = set()
    for value in os.getenv(ENVIRONMENT_VARIABLE, '').split(','):
        value = value.strip()
        if not value:
            continue
        origin = service_origin(value, origin_only=True)
        if not _local_hostname(urlsplit(origin).hostname or ''):
            raise AppError(422, 'nonlocal_trusted_origin', '受信本地服务仅接受宿主网关、内网IP、Compose服务名或.local名称；不能将公共互联网API声明为本地')
        result.add(origin)
    return frozenset(result)


def confirmed_local_origin(value: str, profile_settings=None) -> str | None:
    """Validate the user's persisted opt-in for this service, never a global URL exemption."""
    confirmed = (profile_settings or {}).get('local_service_origin')
    if confirmed in (None, ''):
        return None
    if not isinstance(confirmed, str):
        raise AppError(422, 'invalid_local_service_origin', '本地服务确认必须保存为精确 HTTP(S) origin')
    origin = service_origin(confirmed, origin_only=True)
    if origin != service_origin(value):
        raise AppError(422, 'local_origin_confirmation_mismatch', '服务地址已变化，请重新确认当前本地服务地址')
    if not _local_hostname(urlsplit(origin).hostname or ''):
        raise AppError(422, 'nonlocal_trusted_origin', '本地服务仅接受宿主网关、内网IP、Compose服务名或.local名称；禁止公共API及云主机元数据地址')
    return origin


def is_trusted_local_service(value: str, profile_settings=None) -> bool:
    try:
        origin = service_origin(value)
        confirmed = confirmed_local_origin(value, profile_settings)
    except AppError:
        return False
    return bool(confirmed) or origin in configured_local_origins()


def pin_local_service_url(value: str) -> tuple[str, str, str]:
    """Pin every local adapter request to an allowed address; reject DNS rebinding/metadata."""
    origin = service_origin(value)
    parsed = urlsplit(value)
    hostname = parsed.hostname or ''
    if not _local_hostname(hostname):
        raise AppError(422, 'unsafe_local_service_host', '本地服务主机必须为宿主网关、私网或本机名称')
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)}
    except OSError as error:
        raise AppError(502, 'local_service_dns_failed', '无法解析本地模型服务地址；请检查服务名称、Docker网关或局域网连接') from error
    if not addresses or any(not _local_hostname(address) for address in addresses):
        raise AppError(422, 'unsafe_local_service_address', '本地模型服务解析到了非私网或元数据地址，已拒绝连接')
    address = sorted(addresses, key=lambda item: (':' in item, item))[0]
    pinned_host = '[' + address + ']' if ':' in address else address
    pinned = urlunsplit((parsed.scheme, f'{pinned_host}:{parsed.port or (443 if parsed.scheme == "https" else 80)}', parsed.path, parsed.query, ''))
    return pinned, hostname, urlsplit(origin).netloc


class _PinnedLocalTransport(httpx.AsyncBaseTransport):
    """Service-only transport: pin every request, including uploads and /view queries."""
    def __init__(self, origins, transport=None):
        self.origins = frozenset(service_origin(value) for value in origins)
        if not self.origins or any(not _local_hostname(urlsplit(value).hostname or '') for value in self.origins):
            raise AppError(422, 'unsafe_local_service_host', '本地请求只接受明确指定的本机或私网服务origin')
        self.transport = transport or httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request):
        original = urlsplit(str(request.url))
        without_query = urlunsplit((original.scheme, original.netloc, original.path, '', ''))
        if original.fragment or service_origin(without_query) not in self.origins:
            raise AppError(422, 'service_origin_changed', '本地服务请求不属于已明确确认的协议、主机和端口')
        pinned, sni, host = await asyncio.to_thread(pin_local_service_url, without_query)
        target = urlsplit(pinned)
        pinned = urlunsplit((target.scheme, target.netloc, target.path, original.query, ''))
        headers = httpx.Headers(request.headers)
        headers['Host'] = host
        extensions = {**request.extensions, 'sni_hostname': sni}
        forwarded = httpx.Request(request.method, pinned, headers=headers, stream=request.stream, extensions=extensions)
        response = await self.transport.handle_async_request(forwarded)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise AppError(422, 'service_redirect_refused', '本地模型服务返回重定向；请配置最终地址，系统不会向其他地址转发请求')
        return response

    async def aclose(self):
        await self.transport.aclose()


def local_service_client(origins: list[str], **kwargs) -> httpx.AsyncClient:
    """Build a pinned client after the caller validates its configured service profile."""
    # A supplied transport/proxy/mount could bypass address pinning.
    if any(key in kwargs for key in ('transport', 'proxy', 'proxies', 'mounts')):
        raise ValueError('local service transport/proxy cannot be overridden')
    kwargs['follow_redirects'] = False
    kwargs['trust_env'] = False
    return httpx.AsyncClient(transport=_PinnedLocalTransport(origins), **kwargs)


def is_loopback_service(value: str) -> bool:
    try:
        return urlsplit(value).hostname in LOOPBACK_HOSTS
    except ValueError:
        return False


def is_local_provider(provider: str, value: str, profile_settings=None) -> bool:
    if provider not in {'ffmpeg', 'comfyui', 'local_runtime', 'local_llm'}:
        return False
    if not value:
        return provider in {'ffmpeg', 'local_runtime'}
    return is_loopback_service(value) or is_trusted_local_service(value, profile_settings)
