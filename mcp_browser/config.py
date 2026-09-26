"""Настройки коннектора — только из окружения (`/etc/mcp-browser.env` у systemd).

Коннектор — отдельный сервис (mcp-browser, 127.0.0.1:8767). Рядом с хабом
nexus-mcp он встаёт на тот же домен (путь /browser/…), но со своим секретом
и под своим пользователем: ключи к нодам и токены панели ему не видны, а
секрет хаба не открывает браузер, и наоборот.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def _list(name: str) -> list[str]:
    return [x.strip() for x in _env(name).split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # Секрет коннектора: https://<домен>/browser/mcp/<secret> или Bearer на /browser/mcp.
    secret: str = field(default_factory=lambda: _env("MCP_BROWSER_SECRET"))
    host: str = field(default_factory=lambda: _env("MCP_BROWSER_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("MCP_BROWSER_PORT", 8767))
    public_hosts: list[str] = field(default_factory=lambda: _list("MCP_BROWSER_PUBLIC_HOSTS"))
    state_dir: Path = field(default_factory=lambda: Path(_env("MCP_BROWSER_STATE_DIR", "/var/lib/mcp-browser")))

    # Адреса машины и локальной сети (127.0.0.1, 10.x, 169.254.169.254 —
    # метаданные облака) закрыты: на хабе рядом живут хаб и чат. Открыть —
    # только осознанно.
    allow_private: bool = field(default_factory=lambda: _flag("MCP_BROWSER_ALLOW_PRIVATE"))
    # Выход в интернет через прокси (http://, socks5://) — например, через
    # ноду, если сайт режет IP хаба. Пусто — напрямую.
    proxy: str = field(default_factory=lambda: _env("MCP_BROWSER_PROXY"))

    # Поиск: SearXNG (свой, без ключей) → Brave Search API → ddgs (по
    # умолчанию: DuckDuckGo, Bing, Brave, Яндекс… без ключей).
    searxng_url: str = field(default_factory=lambda: _env("MCP_BROWSER_SEARXNG_URL").rstrip("/"))
    brave_key: str = field(default_factory=lambda: _env("MCP_BROWSER_BRAVE_KEY"))

    # Браузер (Playwright + Chromium).
    browser: bool = field(default_factory=lambda: _env("MCP_BROWSER_BROWSER", "1") not in ("0", "false", "no", "off"))
    chromium_path: str = field(default_factory=lambda: _env("MCP_BROWSER_CHROMIUM"))
    max_sessions: int = field(default_factory=lambda: _int("MCP_BROWSER_MAX_SESSIONS", 4))
    idle_min: int = field(default_factory=lambda: _int("MCP_BROWSER_IDLE_MIN", 15))

    # Соцсети через официальные API. Telegram: бот — админ канала.
    tg_bot_token: str = field(default_factory=lambda: _env("MCP_BROWSER_TG_BOT_TOKEN"))
    tg_chats: list[str] = field(default_factory=lambda: _list("MCP_BROWSER_TG_CHATS"))
    # VK: ключ пользователя (права wall, photos, offline) и стены через
    # запятую: -123 — сообщество, 456 — страница.
    vk_token: str = field(default_factory=lambda: _env("MCP_BROWSER_VK_TOKEN"))
    vk_owners: list[str] = field(default_factory=lambda: _list("MCP_BROWSER_VK_OWNERS"))

    user_agent: str = field(default_factory=lambda: _env(
        "MCP_BROWSER_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"))

    @property
    def profiles_dir(self) -> Path:
        return self.state_dir / "profiles"

    @property
    def audit_log(self) -> Path:
        return self.state_dir / "audit.jsonl"


settings = Settings()
