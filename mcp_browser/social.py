"""Посты в соцсети через официальные API: Telegram-канал и стена VK.

Путь надёжнее браузера: без капчи и вёрстки, которая меняется. Остальные
сети (Instagram, X, Дзен, Threads…) — через браузер с профилем.

Публикация идёт планом, как правка ноды в хабе: вызов без confirm — что
именно уйдёт и куда, с plan_hash; публикация — confirm=true с тем же
plan_hash. Текст поменялся после предпросмотра — отказ.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlsplit

import httpx

from mcp_browser import config, fetch
from mcp_browser.guard import Blocked

NETWORKS = ("telegram", "vk")
VK_API = "https://api.vk.com/method"
VK_V = "5.199"


class SocialError(Exception):
    pass


def accounts() -> dict:
    s = config.settings
    return {
        "telegram": {"configured": bool(s.tg_bot_token), "chats": s.tg_chats,
                     "how": "MCP_BROWSER_TG_BOT_TOKEN + MCP_BROWSER_TG_CHATS; бот — админ канала с правом публикации"},
        "vk": {"configured": bool(s.vk_token), "walls": s.vk_owners,
               "how": "MCP_BROWSER_VK_TOKEN (ключ пользователя: wall, photos, offline) + MCP_BROWSER_VK_OWNERS "
                      "(-id сообщества или id страницы)"},
    }


def _target(network: str, target: str) -> str:
    s = config.settings
    if network == "telegram":
        if not s.tg_bot_token:
            raise SocialError("Telegram не настроен: " + accounts()["telegram"]["how"])
        allowed = s.tg_chats
    else:
        if not s.vk_token:
            raise SocialError("VK не настроен: " + accounts()["vk"]["how"])
        allowed = s.vk_owners
    target = (target or "").strip() or (allowed[0] if allowed else "")
    if not target:
        raise SocialError(f"{network}: укажите target — куда публиковать")
    # Список задан — публикуем только туда: секрет коннектора не должен
    # открывать постинг в любой чат, куда добавлен бот.
    if allowed and target not in allowed:
        raise SocialError(f"{network}: «{target}» нет в списке разрешённых ({', '.join(allowed)})")
    if network == "vk" and not re.fullmatch(r"-?\d+", target):
        raise SocialError("vk: target — числовой id стены (-123 сообщество, 456 страница)")
    return target


def plan(network: str, text: str, images: list[str], target: str, fmt: str, publish_at: int) -> dict:
    if network not in NETWORKS:
        raise SocialError(f"network — одно из: {', '.join(NETWORKS)}; остальные сети — через браузер")
    if not text.strip() and not images:
        raise SocialError("пустой пост: нужен текст или картинки")
    if len(images) > 10:
        raise SocialError("не больше 10 картинок")
    for u in images:
        if urlsplit(u).scheme not in ("http", "https"):
            raise SocialError(f"картинка — адрес http(s), а не «{u[:80]}»")
    if fmt not in ("plain", "html"):
        raise SocialError("format: plain или html (html — только Telegram)")
    target = _target(network, target)
    if network == "telegram":
        if publish_at:
            raise SocialError("Telegram-бот не умеет отложенные посты — publish_at только для VK")
        limit = 1024 if images else 4096
        if len(text) > limit and images:
            note = "текст длиннее 1024 — уйдёт отдельным сообщением после картинок"
        elif len(text) > 4096:
            raise SocialError(f"текст {len(text)} символов — в Telegram до 4096")
        else:
            note = ""
    else:
        if publish_at and publish_at < time.time() + 60:
            raise SocialError("publish_at — unix-время в будущем")
        note = ""
    body = {"network": network, "target": target, "text": text, "images": images, "format": fmt,
            "publish_at": publish_at}
    h = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    return {**body, "chars": len(text), "note": note, "plan_hash": h}


async def _images(urls: list[str]) -> list[tuple[str, bytes, str]]:
    out = []
    for i, u in enumerate(urls, 1):
        try:
            pg = await fetch.download(u, max_bytes=20 * 1024 * 1024)
        except (fetch.FetchError, Blocked) as e:
            raise SocialError(f"картинка {i} не скачалась: {e}") from None
        if pg.status >= 400:
            raise SocialError(f"картинка {i}: HTTP {pg.status}")
        if not pg.ctype.startswith("image/"):
            raise SocialError(f"картинка {i}: это не картинка ({pg.ctype or 'без типа'})")
        ext = {"image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(pg.ctype, "jpg")
        out.append((f"img{i}.{ext}", pg.body, pg.ctype))
    return out


# ── Telegram ─────────────────────────────────────────────────────────────────

async def _tg(c: httpx.AsyncClient, method: str, data: dict, files=None) -> dict:
    tok = config.settings.tg_bot_token
    r = await c.post(f"https://api.telegram.org/bot{tok}/{method}", data=data, files=files)
    try:
        js = r.json()
    except ValueError:
        raise SocialError(f"Telegram {method}: HTTP {r.status_code}") from None
    if not js.get("ok"):
        raise SocialError(f"Telegram {method}: {js.get('description', 'отказ')}")
    return js["result"]


def _tg_link(msg: dict) -> str:
    chat = msg.get("chat") or {}
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{msg['message_id']}"
    cid = str(chat.get("id", ""))
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{msg['message_id']}"
    return ""


async def _post_tg(p: dict) -> dict:
    s = config.settings
    imgs = await _images(p["images"])
    text, chat = p["text"], p["target"]
    extra = {"parse_mode": "HTML"} if p["format"] == "html" else {}
    caption, tail = (text, "") if len(text) <= 1024 else ("", text)
    msgs = []
    async with httpx.AsyncClient(timeout=60, proxy=s.proxy or None) as c:
        if not imgs:
            msgs.append(await _tg(c, "sendMessage", {"chat_id": chat, "text": text, **extra}))
        elif len(imgs) == 1:
            name, data, ctype = imgs[0]
            d = {"chat_id": chat, **({"caption": caption, **extra} if caption else {})}
            msgs.append(await _tg(c, "sendPhoto", d, files={"photo": (name, data, ctype)}))
        else:
            media = []
            for i, (name, _, _) in enumerate(imgs):
                m = {"type": "photo", "media": f"attach://{name}"}
                if i == 0 and caption:
                    m.update({"caption": caption, **extra})
                media.append(m)
            files = {name: (name, data, ctype) for name, data, ctype in imgs}
            msgs.extend(await _tg(c, "sendMediaGroup",
                                  {"chat_id": chat, "media": json.dumps(media, ensure_ascii=False)}, files=files))
        if tail:
            msgs.append(await _tg(c, "sendMessage", {"chat_id": chat, "text": tail, **extra}))
    return {"ok": True, "network": "telegram", "chat": chat,
            "message_ids": [m["message_id"] for m in msgs], "link": _tg_link(msgs[0])}


# ── VK ───────────────────────────────────────────────────────────────────────

async def _vk(c: httpx.AsyncClient, method: str, params: dict) -> dict | list:
    r = await c.post(f"{VK_API}/{method}",
                     data={**params, "access_token": config.settings.vk_token, "v": VK_V})
    js = r.json()
    if "error" in js:
        e = js["error"]
        raise SocialError(f"VK {method}: {e.get('error_code')} {e.get('error_msg', '')}")
    return js["response"]


async def _post_vk(p: dict) -> dict:
    s = config.settings
    owner = int(p["target"])
    imgs = await _images(p["images"])
    group = {"group_id": -owner} if owner < 0 else {}
    atts = []
    async with httpx.AsyncClient(timeout=90, proxy=s.proxy or None) as c:
        for name, data, ctype in imgs:
            srv = await _vk(c, "photos.getWallUploadServer", group)
            up = (await c.post(srv["upload_url"], files={"photo": (name, data, ctype)})).json()
            if not up.get("photo") or up.get("photo") == "[]":
                raise SocialError(f"VK не принял картинку {name}: {str(up)[:200]}")
            saved = await _vk(c, "photos.saveWallPhoto",
                              {**group, "server": up["server"], "photo": up["photo"], "hash": up["hash"]})
            atts.append(f"photo{saved[0]['owner_id']}_{saved[0]['id']}")
        params = {"owner_id": owner, "message": p["text"]}
        if owner < 0:
            params["from_group"] = 1
        if atts:
            params["attachments"] = ",".join(atts)
        if p["publish_at"]:
            params["publish_date"] = p["publish_at"]
        res = await _vk(c, "wall.post", params)
    pid = res["post_id"]
    return {"ok": True, "network": "vk", "owner_id": owner, "post_id": pid,
            "link": f"https://vk.com/wall{owner}_{pid}",
            **({"scheduled": time.strftime("%Y-%m-%d %H:%M", time.localtime(p["publish_at"]))}
               if p["publish_at"] else {})}


async def publish(p: dict) -> dict:
    return await (_post_tg(p) if p["network"] == "telegram" else _post_vk(p))


async def delete(network: str, target: str, post_id: int) -> dict:
    s = config.settings
    target = _target(network, target)
    async with httpx.AsyncClient(timeout=30, proxy=s.proxy or None) as c:
        if network == "telegram":
            await _tg(c, "deleteMessage", {"chat_id": target, "message_id": post_id})
        else:
            await _vk(c, "wall.delete", {"owner_id": int(target), "post_id": post_id})
    return {"ok": True, "deleted": post_id, "network": network, "target": target}
