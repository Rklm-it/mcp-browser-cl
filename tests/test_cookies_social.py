import asyncio
import json

import httpx
import pytest

from mcp_browser import browser, cli, cookies, server, social


def test_cookie_editor_json_and_netscape(tmp_path, settings):
    ce = json.dumps([{"name": "remixsid", "value": "abc", "domain": ".vk.com", "path": "/",
                      "expirationDate": 1893456000.5, "secure": True, "httpOnly": True, "sameSite": "no_restriction"},
                     {"name": "s", "value": "1", "domain": "vk.com", "session": True, "sameSite": "unspecified"}])
    st = cookies.parse(ce)
    assert st["cookies"][0]["sameSite"] == "None" and st["cookies"][0]["secure"]
    assert st["cookies"][1]["expires"] == -1 and "sameSite" not in st["cookies"][1]
    ns = "# Netscape HTTP Cookie File\n#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tq\n"
    st2 = cookies.parse(ns)
    assert st2["cookies"][0] == {"name": "sessionid", "value": "q", "domain": ".instagram.com", "path": "/",
                                 "expires": 1893456000.0, "httpOnly": True, "secure": True}
    with pytest.raises(cookies.CookieError):
        cookies.parse("[]")
    f = tmp_path / "c.json"
    f.write_text(ce)
    assert cli.main(["profile-import", "vk", str(f)]) == 0
    f.write_text(json.dumps([{"name": "remixsid", "value": "NEW", "domain": ".vk.com", "path": "/"}]))
    assert cli.main(["profile-import", "vk", str(f)]) == 0
    saved = json.loads(browser.profile_path("vk").read_text())
    assert [c["value"] for c in saved["cookies"] if c["name"] == "remixsid"] == ["NEW"]
    assert (browser.profile_path("vk").stat().st_mode & 0o777) == 0o600
    assert browser.list_profiles()[0]["sites"] == ["vk.com"]
    assert cli.main(["profile-import", "../evil", str(f)]) == 1


def _mock(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("proxy", None)
        return real(*a, transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(social.httpx, "AsyncClient", factory)
    monkeypatch.setattr(server.social.httpx, "AsyncClient", factory)


def test_telegram_needs_preview_and_same_hash(monkeypatch, settings):
    settings.tg_bot_token = "123:abc"
    settings.tg_chats = ["@mychan"]
    sent = []

    def handler(req):
        sent.append((req.url.path, req.content.decode()))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7, "chat": {"username": "mychan"}}})

    _mock(monkeypatch, handler)
    prev = asyncio.run(server.social_post("telegram", "Привет, канал"))
    assert prev["preview"] and prev["target"] == "@mychan" and not sent
    bad = asyncio.run(server.social_post("telegram", "Привет, канал!", confirm=True, plan_hash=prev["plan_hash"]))
    assert not bad["ok"] and "plan_hash" in bad["error"] and not sent
    res = asyncio.run(server.social_post("telegram", "Привет, канал", confirm=True, plan_hash=prev["plan_hash"]))
    assert res == {"ok": True, "network": "telegram", "chat": "@mychan", "message_ids": [7],
                   "link": "https://t.me/mychan/7"}
    assert sent[0][0] == "/bot123:abc/sendMessage" and "chat_id=%40mychan" in sent[0][1]
    other = asyncio.run(server.social_post("telegram", "x", target="@чужой"))
    assert not other["ok"] and "разрешённых" in other["error"]


def test_vk_uploads_photo_then_posts(monkeypatch, settings, site):
    settings.vk_token = "vk1.a.tok"
    settings.vk_owners = ["-42"]
    settings.allow_private = True
    calls = []

    def handler(req):
        path = req.url.path
        if req.url.host == "127.0.0.1":  # картинку качает fetch — подмена задевает и его
            from conftest import PNG
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        calls.append(path)
        if path.endswith("photos.getWallUploadServer"):
            return httpx.Response(200, json={"response": {"upload_url": "https://pu.vk.com/up"}})
        if path == "/up":
            return httpx.Response(200, json={"server": 1, "photo": "[{...}]", "hash": "h"})
        if path.endswith("photos.saveWallPhoto"):
            return httpx.Response(200, json={"response": [{"id": 5, "owner_id": -42}]})
        if path.endswith("wall.post"):
            body = req.content.decode()
            assert "attachments=photo-42_5" in body and "from_group=1" in body
            return httpx.Response(200, json={"response": {"post_id": 99}})
        return httpx.Response(404)

    _mock(monkeypatch, handler)
    p = asyncio.run(server.social_post("vk", "Пост", images=[site + "/img.png"]))
    res = asyncio.run(server.social_post("vk", "Пост", images=[site + "/img.png"], confirm=True,
                                         plan_hash=p["plan_hash"]))
    assert res.get("link") == "https://vk.com/wall-42_99", res
    assert calls[-1].endswith("wall.post")


def test_not_configured_and_limits(settings):
    r = asyncio.run(server.social_post("vk", "x"))
    assert not r["ok"] and "не настроен" in r["error"]
    r = asyncio.run(server.social_post("instagram", "x"))
    assert "браузер" in r["error"]
    settings.tg_bot_token = "t"
    r = asyncio.run(server.social_post("telegram", "x", target="@a", publish_at=2000000000))
    assert "отложенные" in r["error"]
