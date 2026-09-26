"""Импорт cookies в профиль браузера — вход в соцсеть без пароля в чате.

Понимает три формата:
- JSON-массив расширений Cookie-Editor / EditThisCookie / Get cookies.txt
  (поля name, value, domain, path, expirationDate, secure, httpOnly, sameSite);
- storage_state Playwright ({"cookies": [...], "origins": [...]});
- cookies.txt (Netscape): строки через табуляцию.
"""

from __future__ import annotations

import json

_SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}


class CookieError(Exception):
    pass


def _one(c: dict) -> dict | None:
    name, domain = c.get("name"), c.get("domain") or c.get("host")
    if not name or not domain:
        return None
    exp = c.get("expires", c.get("expirationDate", c.get("expiry", -1)))
    try:
        exp = float(exp) if exp not in (None, "", "session") else -1
    except (TypeError, ValueError):
        exp = -1
    if c.get("session") is True:
        exp = -1
    out = {"name": str(name), "value": str(c.get("value", "")), "domain": str(domain),
           "path": c.get("path") or "/", "expires": exp,
           "httpOnly": bool(c.get("httpOnly", False)), "secure": bool(c.get("secure", False))}
    ss = _SAMESITE.get(str(c.get("sameSite", "")).lower())
    if ss:
        out["sameSite"] = ss
        if ss == "None":
            out["secure"] = True
    return out


def _netscape(text: str) -> list[dict]:
    out = []
    for ln in text.splitlines():
        http_only = ln.startswith("#HttpOnly_")
        if http_only:
            ln = ln[len("#HttpOnly_"):]
        if not ln.strip() or ln.startswith("#"):
            continue
        parts = ln.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure, exp, name, value = parts[:7]
        out.append({"name": name, "value": value.rstrip("\r\n"), "domain": domain, "path": path,
                    "expires": float(exp) if exp.strip() not in ("", "0") else -1,
                    "httpOnly": http_only, "secure": secure.upper() == "TRUE"})
    return out


def parse(text: str) -> dict:
    """Текст файла → storage_state Playwright."""
    text = text.strip().lstrip("﻿")
    if not text:
        raise CookieError("файл пустой")
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise CookieError(f"JSON не читается: {e}") from None
        if isinstance(data, dict):
            origins = data.get("origins") or []
            raw = data.get("cookies") or []
        else:
            origins, raw = [], data
        cookies = [c for c in (_one(x) for x in raw if isinstance(x, dict)) if c]
    else:
        origins, cookies = [], _netscape(text)
    if not cookies:
        raise CookieError("cookies не нашлись: нужен экспорт Cookie-Editor (JSON) или cookies.txt")
    return {"cookies": cookies, "origins": origins}


def merge(old: dict | None, new: dict) -> dict:
    """Новые cookies поверх старых (ключ — имя, домен, путь)."""
    old = old or {"cookies": [], "origins": []}
    key = lambda c: (c["name"], c["domain"], c.get("path", "/"))  # noqa: E731
    fresh = {key(c) for c in new["cookies"]}
    cookies = [c for c in old.get("cookies", []) if key(c) not in fresh] + new["cookies"]
    by_origin = {o.get("origin"): o for o in old.get("origins", [])}
    for o in new.get("origins", []):
        by_origin[o.get("origin")] = o
    return {"cookies": cookies, "origins": [o for o in by_origin.values() if o]}
