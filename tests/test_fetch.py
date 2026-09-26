"""Поиск не трогаем (нужен интернет); загрузка, guard, извлечение текста — на локальном сайте."""

import asyncio

import pytest

from mcp_browser import extract, fetch, server
from mcp_browser.guard import Blocked, check_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/healthz", "http://localhost/", "http://10.0.0.5/", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]:8765/", "http://[::ffff:127.0.0.1]/",
    "http://0.0.0.0/", "http://100.64.1.1/", "file:///etc/passwd", "ftp://example.com/", "javascript:alert(1)",
    "http://metadata.google.internal/",
])
def test_guard_blocks_local_and_odd(url):
    with pytest.raises(Blocked):
        asyncio.run(check_url(url))


def test_guard_allows_public_ip():
    assert asyncio.run(check_url("https://1.1.1.1/")) == "https://1.1.1.1/"


def test_guard_can_be_opened(settings):
    settings.allow_private = True
    assert asyncio.run(check_url("http://127.0.0.1:1/"))


def test_local_site_is_closed_by_default(site):
    with pytest.raises(Blocked):
        asyncio.run(fetch.download(site + "/article"))


def test_article_to_markdown(site, settings):
    settings.allow_private = True
    settings.browser = False
    out = asyncio.run(server.web_fetch(site + "/article", max_chars=1000))
    assert "Заголовок: Тестовая статья" in out
    assert "Абзац про браузер" in out
    assert "подвал" not in out
    assert "дальше — start=" in out
    nxt = int(out.split("дальше — start=")[1].split()[0])
    out2 = asyncio.run(server.web_fetch(site + "/article", start=nxt, max_chars=1000))
    assert f"Показаны символы {nxt}–" in out2


def test_redirect_to_metadata_is_blocked(site, settings):
    # Первый адрес локальный — открываем только его, а переход на метаданные
    # облака guard обязан остановить.
    settings.allow_private = True
    page = asyncio.run(fetch.download(site + "/redirect"))
    assert page.url.endswith("/article")
    settings.allow_private = False
    with pytest.raises(Blocked):
        asyncio.run(check_url("http://169.254.169.254/latest/meta-data/"))


def test_image_json_links(site, settings):
    settings.allow_private = True
    img = asyncio.run(server.web_fetch(site + "/img.png"))
    assert isinstance(img, list) and img[1].to_image_content().mime_type == "image/png"
    js = asyncio.run(server.web_fetch(site + "/json"))
    assert '"б": "в"' in js
    links = asyncio.run(server.web_fetch(site + "/form", format="links"))
    assert f"[Статья]({site}/article)" in links


def test_bad_format_and_404(site, settings):
    settings.allow_private = True
    assert asyncio.run(server.web_fetch(site + "/article", format="pdf")).startswith("Ошибка")
    out = asyncio.run(server.web_fetch(site + "/nope"))
    assert out.splitlines()[1].startswith("404")


def test_plain_text_fallback():
    html = "<html><body><script>var x=1</script><ul><li>Товар 1</li><li>Товар 2</li></ul></body></html>"
    assert extract.plain_text(html) == "Товар 1\nТовар 2"
