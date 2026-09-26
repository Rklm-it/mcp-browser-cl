"""Куда веб-коннектору можно ходить.

Рядом на машине живут хаб (ключи ко всем нодам) и чат, в облаке — сервис
метаданных (169.254.169.254). Поэтому адреса машины, локальной сети и
служебные диапазоны закрыты: и для запросов коннектора, и для каждого
подзапроса браузера. Проверяется каждый переход редиректа, а не только
первый адрес.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit

from mcp_browser import config


class Blocked(Exception):
    """Адрес закрыт — причина в тексте."""


_cache: dict[str, tuple[float, list[str]]] = {}
_TTL = 60.0


def _bad_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return not a.is_global or a.is_multicast


async def _resolve(host: str) -> list[str]:
    now = time.monotonic()
    hit = _cache.get(host)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), 10)
    except (OSError, asyncio.TimeoutError) as e:
        raise Blocked(f"адрес {host} не находится в DNS: {e}") from None
    ips = sorted({i[4][0] for i in infos})
    _cache[host] = (now, ips)
    return ips


async def check_url(url: str) -> str:
    """Адрес годится — вернуть его (без фрагмента); нет — Blocked с причиной."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise Blocked(f"только http(s), а не «{parts.scheme or 'без схемы'}»: {url[:100]}")
    host = (parts.hostname or "").strip(".").lower()
    if not host:
        raise Blocked(f"в адресе нет хоста: {url[:100]}")
    if config.settings.allow_private:
        return url
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        raise Blocked(f"{host} — адрес самой машины, коннектору закрыт")
    try:
        ips = [str(ipaddress.ip_address(host))]
    except ValueError:
        if config.settings.proxy:
            # Имя разрешит прокси, не мы: локальный DNS тут не показатель.
            return url
        ips = await _resolve(host)
    for ip in ips:
        if _bad_ip(ip):
            raise Blocked(f"{host} → {ip}: локальный/служебный адрес, коннектору закрыт "
                          "(MCP_BROWSER_ALLOW_PRIVATE=1 открывает)")
    return url
