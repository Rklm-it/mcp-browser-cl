"""Консоль коннектора на сервере (обёртка — bin/mcp-browser).

  profile-list                     — сохранённые входы
  profile-import <имя> <файл>      — cookies из своего браузера в профиль
  profile-delete <имя>             — забыть вход
  audit [N]                        — последние вызовы
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp_browser import audit, browser, cookies


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "profile-list":
            rows = browser.list_profiles()
            for r in rows:
                sites = ", ".join(r["sites"][:8]) + (" …" if len(r["sites"]) > 8 else "")
                print(f"{r['profile']:<16} {r['cookies']:>4} cookies  {r['updated']}  {sites}")
            if not rows:
                print("профилей нет")
            return 0
        if cmd == "profile-import" and len(rest) == 2:
            name, src = rest
            path = browser.profile_path(name)
            text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
            new = cookies.parse(text)
            old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            browser.save_state(name, cookies.merge(old, new))
            sites = sorted({c["domain"].lstrip(".") for c in new["cookies"]})
            print(f"✓ профиль {name}: {len(new['cookies'])} cookies ({', '.join(sites[:10])})")
            print(f"  в Claude: browser_open(url, profile=\"{name}\")")
            if src != "-":
                print(f"  файл с cookies — это вход в аккаунт: удалите его: rm {src}")
            return 0
        if cmd == "profile-delete" and len(rest) == 1:
            path = browser.profile_path(rest[0])
            if not path.exists():
                print(f"профиля {rest[0]} нет")
                return 1
            path.unlink()
            print(f"✓ профиль {rest[0]} удалён")
            return 0
        if cmd == "audit":
            for e in audit.tail(int(rest[0]) if rest else 30):
                print(f"{e['ts']} {'✓' if e['ok'] else '✗'} {e['tool']} {json.dumps(e['args'], ensure_ascii=False)[:160]}"
                      + (f" — {e['note'][:120]}" if e.get("note") else ""))
            return 0
    except (browser.BrowserError, cookies.CookieError, OSError, ValueError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
