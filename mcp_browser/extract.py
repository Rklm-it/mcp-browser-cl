"""HTML → читаемый текст: основное содержимое страницы, заголовок, ссылки."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import logging

import lxml.html

# trafilatura ругается в журнал на каждую страницу без «статьи» — это не ошибка.
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")
_BLOCKS = ("p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
           "header", "footer", "ul", "ol", "table", "blockquote", "pre", "dd", "dt", "form", "main")


def _doc(html: str, base_url: str):
    try:
        doc = lxml.html.document_fromstring(html)
    except (ValueError, lxml.etree.ParserError):
        return None
    try:
        doc.make_links_absolute(base_url, resolve_base_href=True)
    except ValueError:
        pass
    return doc


def title(html: str) -> str:
    doc = _doc(html, "http://x/")
    if doc is None:
        return ""
    t = doc.findtext(".//title") or ""
    if not t.strip():
        og = doc.xpath('//meta[@property="og:title"]/@content')
        t = og[0] if og else ""
    return _WS.sub(" ", t).strip()


def plain_text(html: str) -> str:
    """Весь видимый текст страницы — запасной путь, когда главного не нашлось."""
    doc = _doc(html, "http://x/")
    if doc is None:
        return ""
    for bad in doc.xpath("//script|//style|//noscript|//template|//svg"):
        bad.drop_tree()
    # text_content() склеивает блоки: «Товар 1Товар 2». Перенос после блока.
    for el in doc.iter(*_BLOCKS):
        el.tail = "\n" + (el.tail or "")
    body = doc.find("body")
    text = (body if body is not None else doc).text_content()
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    return _NL.sub("\n\n", "\n".join(ln for ln in lines if ln)).strip()


def main_content(html: str, url: str, fmt: str = "markdown") -> str:
    """Главное содержимое (статья, пост, товар) без меню и подвала.

    trafilatura находит основной блок; не нашла (витрина, SPA, список) —
    весь видимый текст, чтобы не вернуть пустоту.
    """
    import trafilatura

    out = trafilatura.extract(
        html, url=url, output_format="markdown" if fmt == "markdown" else "txt",
        include_links=fmt == "markdown", include_tables=True, include_images=False,
        include_comments=False, favor_recall=True, deduplicate=True)
    out = (out or "").strip()
    full = plain_text(html)
    # Для страниц без «статьи» trafilatura отдаёт крохи — тогда полный текст.
    if len(out) < 300 and len(full) > 3 * max(len(out), 100):
        return full
    return out or full


def links(html: str, url: str, limit: int = 400) -> list[tuple[str, str]]:
    doc = _doc(html, url)
    if doc is None:
        return []
    seen, out = set(), []
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").split("#")[0].strip()
        if not href or urlsplit(href).scheme not in ("http", "https") or href in seen:
            continue
        seen.add(href)
        out.append((_WS.sub(" ", a.text_content()).strip()[:120], href))
        if len(out) >= limit:
            break
    return out


def absolute(base: str, href: str) -> str:
    return urljoin(base, href)
