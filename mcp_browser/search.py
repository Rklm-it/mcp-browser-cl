"""Поиск в интернете. Движки по порядку, первый ответивший — итог:

1. SearXNG (MCP_BROWSER_SEARXNG_URL) — свой метапоиск, без ключей;
2. Brave Search API (MCP_BROWSER_BRAVE_KEY);
3. ddgs — DuckDuckGo, Bing, Brave, Яндекс, Mojeek… без ключей (по умолчанию).
"""

from __future__ import annotations

import asyncio

import httpx

from mcp_browser import config

TIMES = {"": "", "day": "d", "week": "w", "month": "m", "year": "y"}


class SearchError(Exception):
    pass


def _item(title: str, url: str, snippet: str, **extra) -> dict:
    d = {"title": (title or "").strip(), "url": (url or "").strip(), "snippet": (snippet or "").strip()}
    d.update({k: v for k, v in extra.items() if v})
    return d


async def _searxng(q: str, n: int, region: str, time: str, kind: str) -> list[dict]:
    s = config.settings
    params = {"q": q, "format": "json", "categories": "news" if kind == "news" else "general"}
    if region:
        params["language"] = region
    if time:
        params["time_range"] = time
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{s.searxng_url}/search", params=params)
        r.raise_for_status()
        data = r.json()
    return [_item(x.get("title"), x.get("url"), x.get("content"), date=x.get("publishedDate"),
                  engine=",".join(x.get("engines") or []))
            for x in data.get("results", [])[:n]]


async def _brave(q: str, n: int, region: str, time: str, kind: str) -> list[dict]:
    s = config.settings
    params = {"q": q, "count": min(n, 20)}
    if time:
        params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[time]
    if region:
        params["country"] = region.split("-")[-1].upper()[:2]
    path = "news" if kind == "news" else "web"
    async with httpx.AsyncClient(timeout=20, proxy=s.proxy or None) as c:
        r = await c.get(f"https://api.search.brave.com/res/v1/{path}/search", params=params,
                        headers={"X-Subscription-Token": s.brave_key, "Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    rows = data.get("results", []) if kind == "news" else (data.get("web") or {}).get("results", [])
    return [_item(x.get("title"), x.get("url"), x.get("description"), date=x.get("age"))
            for x in rows[:n]]


async def _ddgs(q: str, n: int, region: str, time: str, kind: str) -> list[dict]:
    from ddgs import DDGS

    s = config.settings

    def run() -> list[dict]:
        d = DDGS(proxy=s.proxy or None, timeout=15)
        kw = {"region": region or "wt-wt", "safesearch": "off", "max_results": n}
        if time:
            kw["timelimit"] = TIMES[time]
        if kind == "news":
            return [_item(x.get("title"), x.get("url"), x.get("body"), date=x.get("date"),
                          source=x.get("source")) for x in d.news(q, **kw)]
        return [_item(x.get("title"), x.get("href"), x.get("body")) for x in d.text(q, **kw)]

    return await asyncio.to_thread(run)


async def search(q: str, n: int = 10, region: str = "", time: str = "", kind: str = "web") -> tuple[str, list[dict]]:
    """(движок, результаты). Все движки отказали — SearchError со всеми причинами."""
    if time not in TIMES:
        raise SearchError(f"time: одно из {', '.join(k for k in TIMES if k)} или пусто")
    s = config.settings
    engines = []
    if s.searxng_url:
        engines.append(("searxng", _searxng))
    if s.brave_key:
        engines.append(("brave", _brave))
    engines.append(("ddgs", _ddgs))
    errors = []
    for name, fn in engines:
        try:
            rows = await asyncio.wait_for(fn(q, n, region, time, kind), 40)
        except Exception as e:  # движок чужой: падает как угодно
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
            continue
        rows = [r for r in rows if r["url"]]
        if rows:
            return name, rows
        errors.append(f"{name}: пусто")
    raise SearchError("поиск не ответил — " + "; ".join(errors))


def as_text(q: str, engine: str, rows: list[dict]) -> str:
    out = [f"Поиск «{q}» ({engine}), {len(rows)} результатов:"]
    for i, r in enumerate(rows, 1):
        meta = " · ".join(x for x in (r.get("source"), r.get("date")) if x)
        out.append(f"\n{i}. {r['title'] or r['url']}\n   {r['url']}" + (f"\n   {meta}" if meta else ""))
        if r["snippet"]:
            out.append(f"   {r['snippet'][:400]}")
    return "\n".join(out)
