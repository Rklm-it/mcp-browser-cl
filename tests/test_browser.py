"""Браузер — прогоном на настоящем Chromium (локальный сайт из conftest).

Chromium в CI ставит `playwright install chromium`; локально можно указать
свой: MCP_BROWSER_CHROMIUM=/путь/к/chrome."""

import asyncio
import json

import pytest

from mcp_browser import browser, server


@pytest.fixture
def fresh(monkeypatch, settings):
    m = browser.Manager()
    monkeypatch.setattr(browser, "manager", m)
    monkeypatch.setattr(server, "manager", m)
    return m


def test_open_type_click_upload_screenshot(fresh, site, settings):
    settings.allow_private = True

    async def flow():
        try:
            snap = await server.browser_open(site + "/form", profile="test")
            assert "Заголовок: Форма" in snap, snap
            sid = snap.split("Сессия: ")[1].split()[0]
            lines = snap.splitlines()
            ref_q = next(ln for ln in lines if "Что искать" in ln).split("]")[0][1:]
            ref_btn = next(ln for ln in lines if "button «Найти»" in ln).split("]")[0][1:]
            ref_ed = next(ln for ln in lines if "[editable]" in ln).split("]")[0][1:]
            ref_file = next(ln for ln in lines if "input[file]" in ln).split("]")[0][1:]
            out = await server.browser_act(sid, "type", ref=int(ref_q), text="котики")
            assert "✓ type (6 симв.)" in out and "= «котики»" in out
            out = await server.browser_act(sid, "click", ref=int(ref_btn))
            assert "нашлось: котики" in out, out
            out = await server.browser_act(sid, "type", ref=int(ref_ed), text="Текст поста")
            assert "Текст поста" in out
            out = await server.browser_act(sid, "upload", ref=int(ref_file), files=[site + "/img.png"])
            assert "файл: img.png" in out, out
            shot = await server.browser_screenshot(sid)
            assert shot[1].to_image_content().mime_type == "image/jpeg"
            read = await server.browser_read(sid, format="text")
            assert "файл: img.png" in read and "Текст поста" in read
            js = await server.browser_js(sid, "() => document.title")
            assert js == '"Форма"'
            gone = await server.browser_act(sid, "click", ref=999)
            assert gone.startswith("Ошибка") and "свежий" in gone
            out = await server.browser_act(sid, "click", selector="text=Статья")
            assert "Заголовок: Тестовая статья" in out
            back = await server.browser_act(sid, "back")
            assert "Заголовок: Форма" in back
            assert (await server.browser_close("all")).startswith("Закрыто: " + sid)
        finally:
            await fresh.shutdown()

    asyncio.run(flow())
    st = json.loads(browser.profile_path("test").read_text())
    assert any(c["name"] == "seen" for c in st["cookies"]), "вход профиля не сохранился"


def test_guard_in_browser(fresh, site):
    async def flow():
        try:
            out = await server.browser_open(site + "/form")
            assert out.startswith("Ошибка") and "закрыт" in out
        finally:
            await fresh.shutdown()

    asyncio.run(flow())


def test_fetch_renders_js_page(fresh, site, settings):
    settings.allow_private = True

    async def flow():
        try:
            return await server.web_fetch(site + "/spa")
        finally:
            await fresh.shutdown()

    out = asyncio.run(flow())
    assert "Содержимое, собранное скриптом" in out and "через браузер" in out, out
