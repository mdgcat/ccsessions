"""Render a session transcript as a self-contained HTML file."""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import List

from markdown_it import MarkdownIt

from .fmt import fmt_iso, fmt_size, parse_iso, tilde
from .store import Message, Session, load_messages

EXPORT_DIR = Path.home() / "Downloads"

_md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")


def slugify(s: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return slug[:limit].rstrip("-") or "session"


def session_date(s: Session) -> datetime:
    if s.first_ts:
        try:
            return parse_iso(s.first_ts)
        except ValueError:
            pass
    return datetime.fromtimestamp(s.mtime)


def default_filename(s: Session, project: str) -> str:
    """<project-name>-<yy-mm-dd>-<title-slug>.html"""
    name = slugify(Path(project).name or "project", 40)
    return f"{name}-{session_date(s):%y-%m-%d}-{slugify(s.title)}.html"


def default_export_path(s: Session, project: str) -> Path:
    return EXPORT_DIR / default_filename(s, project)


CSS = """
:root { --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --panel: #f6f8fa; --border: #d0d7de;
        --user: #1a7f37; --claude: #c4613f; --tool: #9a6700; --err: #cf222e; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --panel: #161b22; --border: #30363d;
          --user: #3fb950; --claude: #d97757; --tool: #d29922; --err: #f85149; }
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); margin: 0; padding: 24px 16px 64px;
       font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width: 900px; margin: 0 auto; }
header { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; margin-bottom: 24px; }
header h1 { font-size: 1.3em; margin: 0 0 10px; }
header dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; margin: 0; font-size: 0.9em; }
header dt { color: var(--muted); }
header dd { margin: 0; overflow-wrap: anywhere; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.88em; }
pre { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px;
      overflow-x: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
:not(pre) > code { background: var(--panel); padding: 1px 4px; border-radius: 4px; }
.turn { margin-top: 22px; font-weight: 600; }
.turn.user { color: var(--user); }
.turn.assistant { color: var(--claude); }
.turn time { color: var(--muted); font-weight: normal; font-size: 0.85em; margin-left: 8px; }
.user-text { white-space: pre-wrap; overflow-wrap: anywhere; border-left: 3px solid var(--user); padding-left: 12px; }
.md { overflow-wrap: anywhere; }
.md table { border-collapse: collapse; }
.md th, .md td { border: 1px solid var(--border); padding: 4px 8px; }
.tool { color: var(--fg); margin: 6px 0 2px; font-size: 0.9em; overflow-wrap: anywhere; }
.tool b { color: var(--tool); }
details { margin: 4px 0 4px 18px; font-size: 0.9em; }
details > summary { cursor: pointer; color: var(--muted); overflow-wrap: anywhere; }
details.error > summary, details.error pre { color: var(--err); }
details.thinking { margin-left: 0; }
details.thinking div { white-space: pre-wrap; font-style: italic; color: var(--muted); padding: 4px 0 4px 12px;
                       border-left: 2px solid var(--border); }
"""


def _first_line(text: str, limit: int = 120) -> str:
    line = next((l.strip() for l in text.splitlines() if l.strip()), "(no output)")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def render_html(s: Session, msgs: List[Message], project: str) -> str:
    meta = [
        ("UUID", f"<code>{escape(s.id)}</code>"),
        ("Project", escape(tilde(s.cwd or project)) + (f" ({escape(s.git_branch)})" if s.git_branch else "")),
        ("When", escape(f"{fmt_iso(s.first_ts)} → {fmt_iso(s.last_ts)}")),
        ("Size", escape(fmt_size(s.size))),
        ("Resume", f"<code>{escape(s.resume_command())}</code>"),
        ("Exported", escape(f"{datetime.now():%Y-%m-%d %H:%M}")),
    ]
    out = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{escape(s.title)}</title>",
        f"<style>{CSS}</style></head><body><main>",
        f"<header><h1>{escape(s.title)}</h1><dl>",
        *(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in meta),
        "</dl></header>",
    ]

    last_role = None
    for m in msgs:
        if m.kind == "text" and (m.role == "user" or m.role != last_role):
            who = "You" if m.role == "user" else "Claude"
            out.append(f'<div class="turn {m.role}">{who}<time>{escape(fmt_iso(m.timestamp))}</time></div>')
            last_role = m.role
        if m.kind == "text":
            if m.role == "assistant":
                out.append(f'<div class="md">{_md.render(m.text)}</div>')
            else:
                out.append(f'<div class="user-text">{escape(m.text)}</div>')
        elif m.kind == "thinking":
            out.append(f'<details class="thinking"><summary>Thinking</summary><div>{escape(m.text)}</div></details>')
        elif m.kind == "tool_use":
            out.append(f'<div class="tool">⏺ <b>{escape(m.name)}</b> <code>{escape(m.text)}</code></div>')
        elif m.kind == "tool_result":
            text = m.text.rstrip()
            n = len(text.splitlines())
            cls = ' class="error"' if m.is_error else ""
            summary = f"{escape(_first_line(text))} <small>({n} line{'s' if n != 1 else ''})</small>"
            out.append(f"<details{cls}><summary>{summary}</summary><pre>{escape(text or '(no output)')}</pre></details>")
    if not msgs:
        out.append("<p><em>(no conversation messages)</em></p>")
    out.append("</main></body></html>\n")
    return "\n".join(out)


def export_session(s: Session, project: str, dest: Path) -> Path:
    """Render the session and write it to dest. Returns the resolved path."""
    dest = Path(dest).expanduser()
    msgs = load_messages(s.path) if not s.is_empty else []
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_html(s, msgs, project), encoding="utf-8")
    return dest
