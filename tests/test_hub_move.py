"""Контракт с переездом хаба nexus-mcp (nexus_mcp/backup.py, migrate.py).

Хаб везёт /etc/mcp-browser.env и /var/lib/mcp-browser в бэкапе переезда и на
новом сервере запускает этот установщик: секрет коннектора, токены и ключи
обязаны сохраниться из лежащего .env (`keep`), а адрес — взяться у хаба.
Перестанет установщик хранить ключ — переезд молча сменит коннектор или
потеряет токен Telegram.
"""

import re
from pathlib import Path

INSTALL = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")

KEPT = ("MCP_BROWSER_SECRET", "MCP_BROWSER_PROXY", "MCP_BROWSER_SEARXNG_URL", "MCP_BROWSER_BRAVE_KEY",
        "MCP_BROWSER_TG_BOT_TOKEN", "MCP_BROWSER_TG_CHATS", "MCP_BROWSER_VK_TOKEN", "MCP_BROWSER_VK_OWNERS")


def test_installer_keeps_secret_and_tokens_from_existing_env():
    for key in KEPT:
        assert re.search(rf"keep {key}\b", INSTALL), f"{key} не берётся из лежащего .env — переезд его потеряет"


def test_paths_the_hub_backs_up():
    assert "ENVF=/etc/mcp-browser.env" in INSTALL
    assert "STATE=/var/lib/mcp-browser" in INSTALL
    assert 'chown -R "$SVC_USER:$SVC_USER" "$STATE"' in INSTALL
    # Адрес — у хаба (новый сервер), а не из старого .env.
    assert 'DOMAIN="$(envget NEXUS_MCP_PUBLIC_HOSTS "$HUB_ENV")"' in INSTALL
