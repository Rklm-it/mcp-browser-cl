import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ARTICLE = ("<html><head><title>Тестовая статья</title></head><body><nav>Меню Меню Меню</nav>"
           "<article><h1>Заголовок статьи</h1>" + "<p>Абзац про браузер для Claude, достаточно длинный текст. " * 40
           + "</p></article><footer>подвал</footer></body></html>")

FORM = """<html><head><title>Форма</title></head><body>
<input id="q" placeholder="Что искать">
<button onclick="document.getElementById('out').innerText='нашлось: '+document.getElementById('q').value;
 document.cookie='seen=1; max-age=3600'">Найти</button>
<div id="out"></div>
<div contenteditable="true" id="ed" style="min-height:20px;border:1px solid">.</div>
<input type="file" id="f" onchange="document.getElementById('out').innerText='файл: '+this.files[0].name">
<a href="/article">Статья</a>
</body></html>"""

SPA = """<html><head><title>SPA</title></head><body><div id="root"></div><noscript>Включите JavaScript</noscript>
<script>document.getElementById('root').innerText = 'Содержимое, собранное скриптом. '.repeat(30)</script></body></html>"""

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8cf"
                    "c0f01f0005000201a5a3b7520000000049454e44ae426082")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p == "/article":
            return self._send(200, ARTICLE)
        if p == "/form":
            return self._send(200, FORM)
        if p == "/spa":
            return self._send(200, SPA)
        if p == "/img.png":
            return self._send(200, PNG, "image/png")
        if p == "/json":
            return self._send(200, '{"a": [1, 2], "б": "в"}', "application/json")
        if p.startswith("/redirect-meta"):
            return self._send(302, "", extra={"Location": "http://169.254.169.254/latest/meta-data/"})
        if p == "/redirect":
            return self._send(302, "", extra={"Location": "/article"})
        return self._send(404, "нет")


@pytest.fixture(scope="session")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    from mcp_browser import config, fetch, guard

    s = config.Settings()
    s.secret = "x" * 32
    s.state_dir = tmp_path / "state"
    s.allow_private = False
    s.proxy = ""
    s.tg_bot_token = ""
    s.vk_token = ""
    s.tg_chats = []
    s.vk_owners = []
    s.searxng_url = ""
    s.brave_key = ""
    s.chromium_path = os.environ.get("MCP_BROWSER_CHROMIUM", "")
    monkeypatch.setattr(config, "settings", s)
    fetch._docs.clear()
    guard._cache.clear()
    return s

