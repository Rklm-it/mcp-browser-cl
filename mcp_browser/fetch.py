"""Загрузка страницы по адресу: HTML → текст, PDF → текст, картинка → картинка.

Длинный текст отдаётся кусками (start/max_chars), а загруженная страница
10 минут лежит в памяти — следующий кусок не качает её заново.
"""

from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass

import httpx

from mcp_browser import config, extract
from mcp_browser.guard import check_url

MAX_BYTES = 15 * 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024
FORMATS = ("markdown", "text", "html", "links")
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg",
               "image/gif": "gif", "image/webp": "webp"}


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str
    status: int
    ctype: str
    body: bytes
    text: str = ""

    @property
    def is_html(self) -> bool:
        return "html" in self.ctype or (not self.ctype and self.body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")))


def headers() -> dict[str, str]:
    return {"User-Agent": config.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"}


def client(**kw) -> httpx.AsyncClient:
    s = config.settings
    return httpx.AsyncClient(headers=headers(), timeout=httpx.Timeout(30, connect=15),
                             proxy=s.proxy or None, follow_redirects=False, **kw)


async def download(url: str, max_bytes: int = MAX_BYTES) -> Page:
    """GET с ручными редиректами: каждый переход проверяется guard'ом."""
    url = await check_url(url)
    async with client() as c:
        for _ in range(10):
            try:
                async with c.stream("GET", url) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = await check_url(extract.absolute(str(r.url), r.headers["location"]))
                        continue
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    declared = int(r.headers.get("content-length") or 0)
                    if declared > max_bytes:
                        raise FetchError(f"файл {declared // 1024 // 1024} МБ — больше предела "
                                         f"{max_bytes // 1024 // 1024} МБ")
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise FetchError(f"ответ больше {max_bytes // 1024 // 1024} МБ — оборван")
                    page = Page(str(r.url), r.status_code, ctype, bytes(buf))
                    if page.is_html or ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
                        page.text = _decode(buf, r.charset_encoding)
                    return page
            except httpx.HTTPError as e:
                raise FetchError(f"{type(e).__name__}: {e or 'нет ответа'} ({url[:120]})") from None
        raise FetchError("больше 10 редиректов")


def _decode(buf: bytes, enc: str | None) -> str:
    for e in (enc, "utf-8", "cp1251"):
        if not e:
            continue
        try:
            return buf.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return buf.decode("utf-8", "replace")


def pdf_text(body: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(body))
    except Exception as e:  # pypdf бросает что угодно на битом файле
        raise FetchError(f"PDF не читается: {e}") from None
    parts = []
    for i, p in enumerate(reader.pages, 1):
        try:
            t = (p.extract_text() or "").strip()
        except Exception:
            t = ""
        parts.append(f"## Страница {i}\n\n{t or '(текста нет — скан?)'}")
    return "\n\n".join(parts)


def render_text(page: Page, fmt: str) -> tuple[str, str]:
    """(заголовок, текст) страницы в нужном формате."""
    if page.ctype == "application/pdf" or page.body[:5] == b"%PDF-":
        return "", pdf_text(page.body)
    if page.is_html:
        title = extract.title(page.text)
        if fmt == "html":
            return title, page.text
        if fmt == "links":
            return title, "\n".join(f"- [{t or '—'}]({u})" for t, u in extract.links(page.text, page.url))
        return title, extract.main_content(page.text, page.url, fmt)
    if "json" in page.ctype:
        try:
            return "", json.dumps(json.loads(page.text), ensure_ascii=False, indent=1)
        except ValueError:
            return "", page.text
    if page.text:
        return "", page.text
    return "", f"(двоичный файл {page.ctype or 'без типа'}, {len(page.body)} байт — текста нет)"


# ── Кусками ──────────────────────────────────────────────────────────────────

_docs: dict[tuple, tuple[float, dict]] = {}
_KEEP = 600


def remember(key: tuple, doc: dict) -> None:
    now = time.monotonic()
    for k in [k for k, (t, _) in _docs.items() if now - t > _KEEP]:
        _docs.pop(k, None)
    if len(_docs) > 64:
        _docs.pop(next(iter(_docs)))
    _docs[key] = (now, doc)


def recall(key: tuple) -> dict | None:
    hit = _docs.get(key)
    if hit and time.monotonic() - hit[0] < _KEEP:
        return hit[1]
    return None


def window(doc: dict, start: int, max_chars: int, how: str = "") -> str:
    text = doc["text"]
    total = len(text)
    start = max(0, min(start, total))
    end = min(total, start + max(500, max_chars))
    head = [f"URL: {doc['url']}"]
    if doc.get("title"):
        head.append(f"Заголовок: {doc['title']}")
    meta = [str(doc["status"])] if doc.get("status") else []
    if doc.get("ctype"):
        meta.append(doc["ctype"])
    meta.append(f"{total} символов")
    if how:
        meta.append(how)
    head.append(" · ".join(meta))
    if start or end < total:
        tail = f"; дальше — start={end}" if end < total else "; это конец"
        head.append(f"Показаны символы {start}–{end}{tail}")
    return "\n".join(head) + "\n\n" + text[start:end]
