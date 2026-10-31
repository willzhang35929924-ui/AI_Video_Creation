"""Explicit administrator trust for container-to-host/local inference origins.

This exception belongs only to configured service adapters. Document imports and
arbitrary media URLs retain their independent public-address restrictions.
"""
from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit

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


def is_trusted_local_service(value: str) -> bool:
    try:
        origin = service_origin(value)
    except AppError:
        return False
    return origin in configured_local_origins()


def is_loopback_service(value: str) -> bool:
    try:
        return urlsplit(value).hostname in LOOPBACK_HOSTS
    except ValueError:
        return False


def is_local_provider(provider: str, value: str) -> bool:
    if provider not in {'ffmpeg', 'comfyui', 'local_runtime', 'local_llm'}:
        return False
    if not value:
        return provider in {'ffmpeg', 'local_runtime'}
    return is_loopback_service(value) or is_trusted_local_service(value)
