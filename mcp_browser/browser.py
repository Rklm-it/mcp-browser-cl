"""Браузер для Claude: Chromium через Playwright, сессии с вкладками.

Сессия — отдельный контекст браузера (свои cookies). С профилем (`profile`)
cookies и localStorage сохраняются в `<state>/profiles/<имя>.json` после
каждого действия: так вход в соцсеть переживает перезапуск. Профиль
заводится входом через сам браузер или импортом cookies (`mcp-browser
profile-import`).

Элементы страницы нумеруются в снимке ([12] button «Опубликовать»), и
действия идут по этим номерам (ref). Номер живёт до следующего снимка.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from mcp_browser import config, fetch
from mcp_browser.guard import Blocked, check_url

logger = logging.getLogger(__name__)

PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ACTIONS = ("click", "type", "press", "select", "hover", "scroll", "goto", "back", "forward",
           "reload", "wait", "upload", "tab", "close_tab")

_SNAPSHOT_JS = r"""
(max) => {
  document.querySelectorAll('[data-mcpb-ref]').forEach(e => e.removeAttribute('data-mcpb-ref'));
  const sel = 'a[href],button,input,textarea,select,summary,label[for],[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],' +
    '[role=textbox],[role=combobox],[role=searchbox],[contenteditable=""],[contenteditable=true],' +
    '[onclick],[tabindex]:not([tabindex="-1"])';
  const vh = innerHeight, all = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    if (n >= 800) break;
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const file = tag === 'input' && el.type === 'file';
    if (!file && (r.width < 2 || r.height < 2)) continue;
    const st = getComputedStyle(el);
    if (!file && (st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0')) continue;
    if (el.closest('[aria-hidden=true]') && !file) continue;
    n++;
    el.setAttribute('data-mcpb-ref', String(n));
    let label = el.getAttribute('aria-label') || (tag === 'input' || tag === 'select' ? '' : el.innerText) ||
      el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') ||
      (el.querySelector('img[alt]') || {}).alt || el.getAttribute('name') || '';
    label = String(label).replace(/\s+/g, ' ').trim().slice(0, 100);
    const secret = tag === 'input' && el.type === 'password';
    all.push({
      ref: n, tag, role: el.getAttribute('role') || '', type: tag === 'input' ? (el.type || 'text') : '',
      label, href: tag === 'a' ? el.href : '',
      value: (tag === 'input' || tag === 'textarea' || tag === 'select') && !secret ? String(el.value || '').slice(0, 80) : '',
      editable: el.isContentEditable, checked: el.checked === true, disabled: el.disabled === true,
      inview: r.bottom > 0 && r.top < vh,
    });
  }
  const shown = all.filter(e => e.inview).concat(all.filter(e => !e.inview)).slice(0, max);
  shown.sort((a, b) => a.ref - b.ref);
  return {items: shown, total: all.length, below: all.filter(e => !e.inview).length,
          scrollY: Math.round(scrollY), height: document.documentElement.scrollHeight, vh};
}
"""


class BrowserError(Exception):
    pass


@dataclass
class Session:
    id: str
    profile: str
    context: object
    pages: list = field(default_factory=list)
    active: int = 0
    last: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    notes: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    @property
    def page(self):
        self.pages = [p for p in self.pages if not p.is_closed()]
        if not self.pages:
            raise BrowserError("в сессии не осталось вкладок — откройте адрес заново (browser_open)")
        self.active = min(self.active, len(self.pages) - 1)
        return self.pages[self.active]


def profile_path(name: str) -> Path:
    if not PROFILE_RE.match(name or ""):
        raise BrowserError("имя профиля: латиница в нижнем регистре, цифры, _ и -, до 32 символов")
    return config.settings.profiles_dir / f"{name}.json"


def list_profiles() -> list[dict]:
    d = config.settings.profiles_dir
    out = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        domains = sorted({c.get("domain", "").lstrip(".") for c in st.get("cookies", [])} - {""})
        out.append({"profile": p.stem, "cookies": len(st.get("cookies", [])),
                    "sites": domains[:30],
                    "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))})
    return out


def save_state(name: str, state: dict) -> None:
    path = profile_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def _proxy() -> dict | None:
    p = config.settings.proxy
    if not p:
        return None
    u = urlsplit(p)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"}
    if u.username:
        out["username"] = u.username
        out["password"] = u.password or ""
    return out


class Manager:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._ua = ""
        self._start_lock = asyncio.Lock()
        self.sessions: dict[str, Session] = {}
        self._reaper: asyncio.Task | None = None

    # ── Запуск ───────────────────────────────────────────────────────────────
    async def _ensure(self):
        if self._browser and self._browser.is_connected():
            return self._browser
        async with self._start_lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            s = config.settings
            if not s.browser:
                raise BrowserError("браузер выключен (MCP_BROWSER_BROWSER=0) — есть web_fetch и web_search")
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise BrowserError("playwright не установлен: переустановите mcp-browser") from None
            if self._pw is None:
                self._pw = await async_playwright().start()
            kw = {"headless": True, "proxy": _proxy(),
                  "args": ["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled",
                           "--no-first-run", "--no-default-browser-check", "--lang=ru-RU",
                           # Без фоновых запросов Chromium в Google (обновления
                           # компонентов, телеметрия): ходит только туда, куда просили.
                           "--disable-background-networking", "--disable-component-update",
                           "--disable-sync", "--disable-domain-reliability", "--no-pings"]}
            if s.chromium_path:
                kw["executable_path"] = s.chromium_path
            else:
                # Полный Chromium в новом headless: соцсети реже принимают
                # его за робота, чем урезанный headless shell.
                kw["channel"] = "chromium"
            try:
                self._browser = await self._pw.chromium.launch(**kw)
            except Exception as e:
                raise BrowserError(f"Chromium не запустился: {str(e)[:400]}") from None
            ver = self._browser.version.split(".")[0]
            self._ua = (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                        f"Chrome/{ver}.0.0.0 Safari/537.36")
            if self._reaper is None or self._reaper.done():
                self._reaper = asyncio.create_task(self._reap())
            return self._browser

    async def _reap(self) -> None:
        """Простаивающие сессии закрываются, без сессий — и сам браузер (память)."""
        while True:
            await asyncio.sleep(60)
            idle = config.settings.idle_min * 60
            now = time.monotonic()
            for sid, sess in list(self.sessions.items()):
                if now - sess.last > idle and not sess.lock.locked():
                    await self.close(sid)
            if not self.sessions and self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None

    async def shutdown(self) -> None:
        for sid in list(self.sessions):
            await self.close(sid)
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ── Сессии ───────────────────────────────────────────────────────────────
    async def open(self, url: str, session: str = "", profile: str = "") -> Session:
        url = await check_url(url)
        if session and session in self.sessions:
            sess = self.sessions[session]
            await self._goto(sess, url)
            return sess
        if len(self.sessions) >= config.settings.max_sessions:
            oldest = min(self.sessions.values(), key=lambda x: x.last)
            await self.close(oldest.id)
        browser = await self._ensure()
        kw = {"user_agent": self._ua, "locale": "ru-RU", "timezone_id": "Europe/Moscow",
              "viewport": {"width": 1280, "height": 900}, "accept_downloads": False}
        if profile:
            p = profile_path(profile)
            if p.exists():
                kw["storage_state"] = str(p)
        ctx = await browser.new_context(**kw)
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        sid = session or f"s{secrets.token_hex(3)}"
        sess = Session(id=sid, profile=profile, context=ctx)
        if not config.settings.allow_private:
            await ctx.route("**/*", lambda route: self._guard(sess, route))
            try:
                await ctx.route_web_socket("**/*", lambda ws: self._guard_ws(sess, ws))
            except AttributeError:  # старый playwright без route_web_socket
                pass
        ctx.on("page", lambda p: self._on_page(sess, p))
        await ctx.new_page()
        self.sessions[sid] = sess
        await self._goto(sess, url)
        return sess

    def _on_page(self, sess: Session, page) -> None:
        page.on("dialog", lambda d: asyncio.ensure_future(self._dialog(sess, d)))
        if page not in sess.pages:
            sess.pages.append(page)
            if len(sess.pages) > 1:
                sess.active = len(sess.pages) - 1
                sess.notes.append(f"открылась новая вкладка №{sess.active + 1} — она теперь активна")

    async def _dialog(self, sess: Session, d) -> None:
        sess.notes.append(f"окно {d.type}: «{d.message[:200]}» — принято")
        try:
            await d.accept()
        except Exception:
            pass

    async def _guard(self, sess: Session, route) -> None:
        url = route.request.url
        if url.startswith(("data:", "blob:", "about:", "chrome-extension:")):
            return await route.continue_()
        try:
            await check_url(url)
        except Blocked as e:
            if len(sess.blocked) < 20:
                sess.blocked.append(str(e))
            return await route.abort("blockedbyclient")
        await route.continue_()

    async def _guard_ws(self, sess: Session, ws) -> None:
        try:
            await check_url(ws.url.replace("wss://", "https://", 1).replace("ws://", "http://", 1))
        except Blocked as e:
            sess.blocked.append(str(e))
            await ws.close()
            return
        ws.connect_to_server()

    def get(self, session: str) -> Session:
        sess = self.sessions.get(session)
        if not sess:
            open_ = ", ".join(self.sessions) or "нет"
            raise BrowserError(f"сессии «{session}» нет (открытые: {open_}); закрылась по простою? "
                               "Откройте заново: browser_open")
        sess.last = time.monotonic()
        return sess

    async def close(self, session: str) -> bool:
        sess = self.sessions.pop(session, None)
        if not sess:
            return False
        await self._save(sess)
        try:
            await sess.context.close()
        except Exception:
            pass
        return True

    async def _save(self, sess: Session) -> None:
        if not sess.profile:
            return
        try:
            save_state(sess.profile, await sess.context.storage_state())
        except Exception as e:
            logger.warning("профиль %s не сохранён: %s", sess.profile, e)

    # ── Навигация и действия ─────────────────────────────────────────────────
    async def _settle(self, page) -> None:
        for state, t in (("domcontentloaded", 15000), ("networkidle", 3000)):
            try:
                await page.wait_for_load_state(state, timeout=t)
            except Exception:
                pass

    async def _goto(self, sess: Session, url: str) -> None:
        page = sess.page if sess.pages else None
        if page is None:
            page = await sess.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            msg = str(e).split("\n")[0][:300]
            if sess.blocked:
                msg += f" ({sess.blocked[-1]})"
            raise BrowserError(f"не открылось {url[:150]}: {msg}") from None
        await self._settle(page)

    def _target(self, page, ref: int, selector: str):
        if ref:
            return page.locator(f'[data-mcpb-ref="{int(ref)}"]').first
        if selector:
            return page.locator(selector).first
        return None

    async def act(self, sess: Session, action: str, ref: int = 0, selector: str = "", text: str = "",
                  url: str = "", key: str = "", x: int = -1, y: int = -1, submit: bool = False,
                  amount: int = 0, files: list[str] | None = None) -> str:
        if action not in ACTIONS:
            raise BrowserError(f"action — одно из: {', '.join(ACTIONS)}")
        page = sess.page
        loc = self._target(page, ref, selector)
        if loc is not None and action in ("click", "type", "select", "hover", "upload"):
            if await loc.count() == 0:
                raise BrowserError(f"элемента {'ref=' + str(ref) if ref else selector!r} на странице нет — "
                                   "номера живут до следующего снимка; возьмите свежий (browser_snapshot)")
        done = action
        try:
            if action == "click":
                if loc is not None:
                    await loc.scroll_into_view_if_needed(timeout=5000)
                    await loc.click(timeout=10000)
                elif x >= 0 and y >= 0:
                    await page.mouse.click(x, y)
                    done = f"click ({x}, {y})"
                else:
                    raise BrowserError("click: нужен ref, selector или x/y (координаты со скриншота)")
            elif action == "type":
                if loc is None:
                    await page.keyboard.insert_text(text)
                else:
                    editable = await loc.evaluate("e => e.isContentEditable")
                    if editable:
                        # Редакторы постов (VK, X, Дзен) — contenteditable:
                        # fill их не будит, печатаем как человек.
                        await loc.click(timeout=10000)
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Delete")
                        await page.keyboard.insert_text(text)
                    else:
                        await loc.fill(text, timeout=10000)
                if submit:
                    await page.keyboard.press("Enter")
                done = f"type ({len(text)} симв.)" + (" + Enter" if submit else "")
            elif action == "press":
                if not key:
                    raise BrowserError("press: нужен key, например Enter, Escape, Control+Enter, PageDown")
                await page.keyboard.press(key)
                done = f"press {key}"
            elif action == "select":
                if loc is None or not text:
                    raise BrowserError("select: нужен ref списка и text — подпись пункта")
                await loc.select_option(label=text, timeout=10000)
            elif action == "hover":
                await loc.hover(timeout=10000)
            elif action == "scroll":
                dy = amount or 700
                if loc is not None:
                    await loc.scroll_into_view_if_needed(timeout=5000)
                else:
                    await page.mouse.wheel(0, -dy if key == "up" or text == "up" else dy)
            elif action == "goto":
                await self._goto(sess, await check_url(url))
            elif action == "back":
                await page.go_back(timeout=20000)
            elif action == "forward":
                await page.go_forward(timeout=20000)
            elif action == "reload":
                await page.reload(timeout=30000)
            elif action == "wait":
                if text:
                    await page.get_by_text(text).first.wait_for(timeout=min(amount or 15, 60) * 1000)
                    done = f"дождался «{text}»"
                else:
                    await asyncio.sleep(min(max(amount, 1), 30))
            elif action == "upload":
                paths = await self._download_files(files or ([url] if url else []))
                try:
                    is_file = loc is not None and await loc.evaluate(
                        "e => e.tagName === 'INPUT' && e.type === 'file'")
                    if is_file:
                        await loc.set_input_files(paths, timeout=10000)
                    else:
                        if loc is None:
                            raise BrowserError("upload: нужен ref поля файла или кнопки, открывающей выбор файла")
                        async with page.expect_file_chooser(timeout=10000) as fc:
                            await loc.click(timeout=10000)
                        await (await fc.value).set_files(paths)
                finally:
                    for p in paths:
                        shutil.rmtree(Path(p).parent, ignore_errors=True)
                done = f"upload {len(paths)} файл(ов)"
            elif action == "tab":
                n = amount or int(text or 0)
                pages = [p for p in sess.pages if not p.is_closed()]
                if not 1 <= n <= len(pages):
                    raise BrowserError(f"tab: номер вкладки 1…{len(pages)} в amount")
                sess.active = n - 1
                await pages[n - 1].bring_to_front()
                done = f"вкладка №{n}"
            elif action == "close_tab":
                await page.close()
                sess.pages = [p for p in sess.pages if not p.is_closed()]
                sess.active = max(0, len(sess.pages) - 1)
        except BrowserError:
            raise
        except Blocked as e:
            raise BrowserError(str(e)) from None
        except Exception as e:
            raise BrowserError(f"{action}: {str(e).splitlines()[0][:300]}") from None
        if action not in ("wait", "tab", "close_tab"):
            await asyncio.sleep(0.4)
            await self._settle(sess.page)
        await self._save(sess)
        return done

    async def _download_files(self, urls: list[str]) -> list[str]:
        if not urls:
            raise BrowserError("upload: нужны адреса файлов (files=[url, …])")
        paths = []
        for u in urls[:10]:
            try:
                pg = await fetch.download(u)
            except (fetch.FetchError, Blocked) as e:
                raise BrowserError(f"файл {u[:120]} не скачался: {e}") from None
            name = Path(urlsplit(pg.url).path).name or "file"
            suffix = Path(name).suffix or {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                                           "image/gif": ".gif", "video/mp4": ".mp4"}.get(pg.ctype, "")
            # Своё имя файла, а не временное: сайт его показывает и хранит.
            stem = re.sub(r"[^\w.-]", "_", Path(name).stem)[:60] or "file"
            path = Path(tempfile.mkdtemp(prefix="mcpb-")) / f"{stem}{suffix}"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pg.body)
            paths.append(str(path))
        return paths

    # ── Что видно ────────────────────────────────────────────────────────────
    async def snapshot(self, sess: Session, max_items: int = 150, text_chars: int = 3000) -> str:
        page = sess.page
        try:
            snap = await page.evaluate(_SNAPSHOT_JS, max_items)
        except Exception as e:
            snap = {"items": [], "total": 0, "below": 0, "scrollY": 0, "height": 0, "vh": 0}
            sess.notes.append(f"снимок элементов не снят: {str(e)[:150]}")
        try:
            title = await page.title()
        except Exception:
            title = ""
        who = f"Сессия: {sess.id}" + (f" · профиль {sess.profile}" if sess.profile else "")
        head = [who, f"URL: {page.url}", f"Заголовок: {title}"]
        pages = [p for p in sess.pages if not p.is_closed()]
        if len(pages) > 1:
            head.append("Вкладки: " + "; ".join(
                f"{'▶' if i == sess.active else ''}№{i + 1} {p.url[:80]}" for i, p in enumerate(pages)))
        if snap.get("height"):
            head.append(f"Прокрутка: {snap['scrollY']} из {snap['height']} px (экран {snap['vh']} px)")
        for n in sess.notes:
            head.append(f"! {n}")
        sess.notes.clear()
        if sess.blocked:
            head.append(f"! закрыто guard'ом подзапросов: {len(sess.blocked)} (первый: {sess.blocked[0][:150]})")
            sess.blocked.clear()
        lines = []
        for e in snap["items"]:
            kind = e["role"] or e["tag"]
            if e["type"] and e["tag"] == "input":
                kind = f"input[{e['type']}]"
            if e["editable"] and e["tag"] not in ("input", "textarea"):
                kind += "[editable]"
            s = f"[{e['ref']}] {kind}"
            if e["label"]:
                s += f" «{e['label']}»"
            if e["value"]:
                s += f" = «{e['value']}»"
            if e["checked"]:
                s += " ✓"
            if e["disabled"]:
                s += " (неактивен)"
            if e["href"] and e["href"] != e["label"]:
                s += f" → {e['href'][:120]}"
            if not e["inview"]:
                s += " ·вне экрана"
            lines.append(s)
        more = snap["total"] - len(snap["items"])
        body = "\n".join(head) + "\n\nЭлементы (ref → что):\n" + ("\n".join(lines) or "(нет)")
        if more > 0:
            body += f"\n… ещё {more} элементов не показано — прокрутите (scroll) или сузьте selector"
        try:
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            text = ""
        text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
        if text_chars and text:
            body += f"\n\nТекст страницы ({min(len(text), text_chars)} из {len(text)} символов; " \
                    "целиком — browser_read):\n" + text[:text_chars]
        return body

    async def screenshot(self, sess: Session, full_page: bool = False, ref: int = 0) -> bytes:
        page = sess.page
        try:
            if ref:
                loc = page.locator(f'[data-mcpb-ref="{int(ref)}"]').first
                return await loc.screenshot(type="jpeg", quality=75, timeout=10000)
            if full_page:
                h = await page.evaluate("() => document.documentElement.scrollHeight")
                if h > 8000:
                    return await page.screenshot(type="jpeg", quality=60, full_page=True,
                                                 clip={"x": 0, "y": 0, "width": 1280, "height": 8000})
                return await page.screenshot(type="jpeg", quality=60, full_page=True)
            return await page.screenshot(type="jpeg", quality=75)
        except Exception as e:
            raise BrowserError(f"скриншот не снят: {str(e).splitlines()[0][:300]}") from None

    async def html(self, sess: Session) -> tuple[str, str, str]:
        page = sess.page
        return page.url, await page.title(), await page.content()

    async def evaluate(self, sess: Session, script: str):
        try:
            return await sess.page.evaluate(script)
        except Exception as e:
            raise BrowserError(f"скрипт упал: {str(e).splitlines()[0][:400]}") from None


manager = Manager()
