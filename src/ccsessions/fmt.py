"""Small formatting helpers shared by the TUI and the HTML exporter."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

HOME = str(Path.home())


def tilde(path: str) -> str:
    return "~" + path[len(HOME):] if path.startswith(HOME) else path


def fmt_time(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M") if dt.date() == datetime.now().date() else dt.strftime("%y-%m-%d")


def parse_iso(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()


def fmt_iso(iso: str) -> str:
    if not iso:
        return "?"
    try:
        dt = parse_iso(iso)
    except ValueError:
        return iso
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"
