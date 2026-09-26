"""Журнал вызовов: что коннектор открывал и публиковал. JSON-строка на вызов."""

from __future__ import annotations

import json
import logging
import time

from mcp_browser import config

logger = logging.getLogger(__name__)


def record(tool: str, args: dict, ok: bool, note: str = "") -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": tool, "args": args, "ok": ok}
    if note:
        entry["note"] = note[:500]
    try:
        path = config.settings.audit_log
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("audit не записан: %s", e)


def tail(n: int = 50) -> list[dict]:
    path = config.settings.audit_log
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines()[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out
