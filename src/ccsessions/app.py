"""ccsessions: split-pane TUI for browsing and pruning Claude Code sessions."""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .clipboard import copy_native
from .store import Message, Session, Store, decode_project_dir, load_messages

ALL = "__all__"
TOOL_RESULT_PREVIEW_LINES = 8
HOME = str(Path.home())


def tilde(path: str) -> str:
    return "~" + path[len(HOME):] if path.startswith(HOME) else path


def fmt_time(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%H:%M") if dt.date() == datetime.now().date() else dt.strftime("%y-%m-%d")


def fmt_iso(iso: str) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


class ConfirmDelete(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Delete"),
        Binding("n,escape", "cancel", "Cancel"),
    ]

    def __init__(self, session: Session, paths: List[Path], warn: str = ""):
        super().__init__()
        self.session = session
        self.paths = paths
        self.warn = warn

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Delete this session?", id="dialog-title")
            yield Static(Text(self.session.title, style="bold"))
            yield Static(Text(self.session.id, style="cyan"))
            if self.warn:
                yield Static(Text(self.warn, style="bold yellow"))
            listing = Text("\nWill remove:\n", style="dim")
            for p in self.paths:
                listing.append(f"  {tilde(str(p))}{'/' if p.is_dir() else ''}\n", style="dim")
            yield Static(listing)
            with Horizontal(id="dialog-buttons"):
                yield Button("Delete (y)", variant="error", id="yes")
                yield Button("Cancel (n)", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class SessionsApp(App):
    TITLE = "Claude Sessions"
    CSS = """
    #left { width: 42%; min-width: 30; border-right: solid $panel-lighten-2; }
    .pane-title { background: $panel; color: $text; padding: 0 1; text-style: bold; width: 100%; }
    #projects { height: auto; max-height: 35%; border: none; }
    #sessions { height: 1fr; border: none; }
    #filter { display: none; border: none; height: 1; padding: 0 1; }
    #filter.visible { display: block; }
    #header { background: $panel; padding: 0 1; height: auto; }
    #body { height: 1fr; padding: 0 1; }
    #body:focus { background: $surface; }
    OptionList:focus, DataTable:focus { background: $surface; }
    ConfirmDelete { align: center middle; }
    #dialog { width: 80; max-width: 95%; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    #dialog-title { text-style: bold; color: $error; margin-bottom: 1; }
    #dialog-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #dialog-buttons Button { margin-left: 2; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab", "focus_next", "Pane", show=False),
        Binding("shift+tab", "focus_previous", show=False),
        Binding("a", "toggle_all", "All/Project"),
        Binding("n,j", "next_session", "Next"),
        Binding("p,k", "prev_session", "Prev"),
        Binding("d,delete", "delete", "Delete"),
        Binding("c", "copy_id", "Copy UUID"),
        Binding("r", "copy_resume", "Copy resume cmd"),
        Binding("t", "toggle_thinking", "Thinking"),
        Binding("o", "toggle_output", "Tool output"),
        Binding("slash", "filter", "Filter"),
        Binding("f5,ctrl+r", "reload", "Reload"),
        Binding("escape", "clear_filter", show=False),
    ]

    def __init__(self, store: Store, project_dir: Optional[str] = None):
        super().__init__()
        self.store = store
        self.scope = ALL  # ALL or a Project.dir_name
        self.last_project: Optional[str] = project_dir
        self.initial_project = project_dir
        self.rows: List[Session] = []
        self.current: Optional[Session] = None
        self.running: set = set()
        self.show_thinking = False
        self.full_output = False
        self.filter_text = ""
        self._messages: List[Message] = []
        self._loaded_id: Optional[str] = None

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Vertical(id="left"):
                yield Label("Projects", classes="pane-title")
                yield OptionList(id="projects")
                yield Label("Sessions", id="sessions-title", classes="pane-title")
                yield Input(placeholder="filter title / uuid / project…", id="filter")
                yield DataTable(id="sessions", cursor_type="row", show_header=False)
            with Vertical(id="right"):
                yield Static("", id="header")
                with VerticalScroll(id="body"):
                    yield Static("", id="transcript")
        yield Footer()

    def on_mount(self) -> None:
        self.reload(keep_index=False)
        if self.initial_project:
            self.set_scope(self.initial_project)
        self.query_one("#sessions", DataTable).focus()

    # ---------- data -> widgets ----------

    def reload(self, keep_index: bool = True) -> None:
        self.store.refresh()
        self.running = self.store.running_ids()
        self.drop_vanished_scope()
        self.populate_projects()
        self.populate_sessions(keep_index=keep_index)

    def drop_vanished_scope(self) -> None:
        if self.last_project and not self.store.project_for_dir(self.last_project):
            self.last_project = None
        if self.scope != ALL and not self.store.project_for_dir(self.scope):
            self.scope = ALL

    def populate_projects(self) -> None:
        ol = self.query_one("#projects", OptionList)
        ol.clear_options()
        total = sum(len(p.sessions) for p in self.store.projects)
        opts = [Option(Text.assemble(("★ All projects", "bold"), (f"  {total}", "dim")), id=ALL)]
        for p in self.store.projects:
            opts.append(Option(Text.assemble(tilde(p.display), (f"  {len(p.sessions)}", "dim")), id=p.dir_name))
        ol.add_options(opts)
        ids = [o.id for o in opts]
        ol.highlighted = ids.index(self.scope) if self.scope in ids else 0

    def scope_sessions(self) -> List[Session]:
        if self.scope == ALL:
            sessions = self.store.all_sessions()
        else:
            proj = self.store.project_for_dir(self.scope)
            sessions = list(proj.sessions) if proj else []
        f = self.filter_text.lower()
        if f:
            sessions = [s for s in sessions if f in s.title.lower() or f in s.id or f in s.cwd.lower()
                        or f in (self.project_label(s) or "").lower()]
        return sessions

    def project_label(self, s: Session) -> str:
        p = self.store.project_for_dir(s.project_dir)
        return tilde(p.display) if p else s.project_dir

    def session_cells(self, s: Session) -> list:
        cells = [
            Text("●", style="bold green") if s.id in self.running else Text(" "),
            Text(fmt_time(s.mtime), style="dim", justify="right"),
        ]
        if self.scope == ALL:
            name = Path(self.project_label(s)).name
            cells.append(Text(name if len(name) <= 18 else "…" + name[-17:], style="cyan"))
        cells.append(Text(s.title, style="dim italic" if s.is_empty else ""))
        return cells

    def populate_sessions(self, keep_index: bool = True, select_index: Optional[int] = None) -> None:
        table = self.query_one("#sessions", DataTable)
        prev_id = self.current.id if self.current else None
        prev_idx = table.cursor_row
        self.rows = self.scope_sessions()
        table.clear(columns=True)
        table.add_columns(*(["", "when", "project", "title"] if self.scope == ALL else ["", "when", "title"]))
        for s in self.rows:
            table.add_row(*self.session_cells(s), key=s.id)

        scope_name = "all projects" if self.scope == ALL else Path(self.project_label(self.rows[0])).name \
            if self.rows else self.scope
        suffix = f" — filter: {self.filter_text}" if self.filter_text else ""
        self.query_one("#sessions-title", Label).update(f"Sessions ({len(self.rows)}) · {scope_name}{suffix}")

        if not self.rows:
            self.show_session(None)
            return
        idx = 0
        if select_index is not None:
            idx = select_index
        elif keep_index:
            ids = [s.id for s in self.rows]
            if prev_id in ids:
                idx = ids.index(prev_id)
            elif prev_idx is not None:
                idx = prev_idx
        idx = max(0, min(idx, len(self.rows) - 1))
        table.move_cursor(row=idx, animate=False)
        self.show_session(self.rows[idx])

    def set_scope(self, scope: str) -> None:
        if scope != ALL:
            self.last_project = scope
        if scope == self.scope:
            return
        self.scope = scope
        ol = self.query_one("#projects", OptionList)
        ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
        if scope in ids and ol.highlighted != ids.index(scope):
            ol.highlighted = ids.index(scope)
        self.populate_sessions(keep_index=False)

    # ---------- right pane ----------

    def show_session(self, s: Optional[Session]) -> None:
        self.current = s
        header = self.query_one("#header", Static)
        if s is None:
            header.update(Text("No sessions", style="dim"))
            self.query_one("#transcript", Static).update("")
            self._loaded_id = None
            return
        h = Text()
        h.append(s.title + "\n", style="bold")
        h.append("UUID     ", style="dim")
        h.append(s.id, style="bold cyan")
        if s.id in self.running:
            h.append("  ● running", style="bold green")
        h.append("\nProject  ", style="dim")
        h.append(tilde(s.cwd or self.project_label(s)))
        if s.git_branch:
            h.append(f"  ({s.git_branch})", style="magenta")
        h.append("\nWhen     ", style="dim")
        h.append(f"{fmt_iso(s.first_ts)} → {fmt_iso(s.last_ts)}   {fmt_size(s.size)}")
        header.update(h)
        if self._loaded_id != s.id:
            self._loaded_id = s.id
            self.query_one("#transcript", Static).update(Text("loading…", style="dim"))
            self.load_transcript(s)

    @work(thread=True, exclusive=True, group="transcript")
    def load_transcript(self, s: Session) -> None:
        msgs = load_messages(s.path) if not s.is_empty else []
        self.call_from_thread(self._transcript_loaded, s.id, msgs)

    def _transcript_loaded(self, sid: str, msgs: List[Message]) -> None:
        if not self.current or self.current.id != sid:
            return
        self._messages = msgs
        self.render_transcript(scroll_home=True)

    def render_transcript(self, scroll_home: bool = False) -> None:
        parts = []
        last_role = None
        for m in self._messages:
            if m.kind == "thinking" and not self.show_thinking:
                continue
            if m.kind == "text" and (m.role == "user" or m.role != last_role):
                label = Text()
                label.append("\n▌ You" if m.role == "user" else "\n▌ Claude",
                             style="bold green" if m.role == "user" else "bold #d97757")
                label.append(f"  {fmt_iso(m.timestamp)}", style="dim")
                parts.append(label)
                last_role = m.role
            if m.kind == "text":
                parts.append(Markdown(m.text) if m.role == "assistant" else Text(m.text))
            elif m.kind == "thinking":
                parts.append(Padding(Text(m.text, style="dim italic"), (0, 0, 0, 2)))
            elif m.kind == "tool_use":
                summary = " ".join(m.text.split())
                if len(summary) > 160:
                    summary = summary[:159] + "…"
                parts.append(Text.assemble(("  ⏺ ", "yellow"), (m.name, "bold yellow"), (f"  {summary}", "")))
            elif m.kind == "tool_result":
                lines = m.text.rstrip().splitlines() or ["(no output)"]
                more = ""
                if not self.full_output and len(lines) > TOOL_RESULT_PREVIEW_LINES:
                    more = f"… {len(lines) - TOOL_RESULT_PREVIEW_LINES} more lines (o to expand)"
                    lines = lines[:TOOL_RESULT_PREVIEW_LINES]
                body = Text("\n".join(l[:400] for l in lines), style="red" if m.is_error else "dim")
                if more:
                    body.append("\n" + more, style="italic dim")
                parts.append(Padding(body, (0, 0, 0, 4)))
        if not parts:
            parts.append(Text("(no conversation messages)", style="dim"))
        self.query_one("#transcript", Static).update(Group(*parts))
        if scroll_home:
            self.query_one("#body", VerticalScroll).scroll_home(animate=False)

    # ---------- events ----------

    @on(DataTable.RowHighlighted, "#sessions")
    def session_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Highlight messages are queued; drop ones made stale by a later rebuild or cursor move.
        if event.cursor_row != event.data_table.cursor_row:
            return
        if 0 <= event.cursor_row < len(self.rows):
            self.show_session(self.rows[event.cursor_row])

    @on(DataTable.RowSelected, "#sessions")
    def session_selected(self, event: DataTable.RowSelected) -> None:
        self.query_one("#body").focus()

    @on(OptionList.OptionHighlighted, "#projects")
    def project_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_index != event.option_list.highlighted:
            return
        if event.option.id:
            self.set_scope(event.option.id)

    @on(OptionList.OptionSelected, "#projects")
    def project_selected(self, event: OptionList.OptionSelected) -> None:
        self.query_one("#sessions").focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self.populate_sessions(keep_index=False)

    @on(Input.Submitted, "#filter")
    def filter_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#sessions").focus()

    # ---------- actions ----------

    def _move(self, delta: int) -> None:
        if not self.rows:
            return
        table = self.query_one("#sessions", DataTable)
        table.move_cursor(row=max(0, min(table.cursor_row + delta, len(self.rows) - 1)))

    def action_next_session(self) -> None:
        self._move(1)

    def action_prev_session(self) -> None:
        self._move(-1)

    def action_toggle_all(self) -> None:
        if self.scope == ALL:
            target = self.last_project or (self.current.project_dir if self.current else None)
            if not target and self.store.projects:
                target = self.store.projects[0].dir_name
            if target:
                self.set_scope(target)
        else:
            self.set_scope(ALL)

    def action_toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        self.render_transcript()
        self.notify(f"Thinking {'shown' if self.show_thinking else 'hidden'}", timeout=1.5)

    def action_toggle_output(self) -> None:
        self.full_output = not self.full_output
        self.render_transcript()
        self.notify(f"Tool output {'expanded' if self.full_output else 'truncated'}", timeout=1.5)

    def action_reload(self) -> None:
        self._loaded_id = None
        self.reload()
        self.notify("Reloaded", timeout=1.5)

    def action_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.add_class("visible")
        inp.focus()

    def action_clear_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        if inp.has_class("visible") or self.filter_text:
            inp.value = ""
            inp.remove_class("visible")
            self.filter_text = ""
            self.populate_sessions()
            self.query_one("#sessions").focus()

    def _copy(self, text: str, what: str) -> None:
        tool = copy_native(text)
        if tool is None:
            self.copy_to_clipboard(text)  # OSC 52: works in most modern terminals and over SSH
            tool = "terminal (OSC 52)"
        self.notify(f"Copied {what} via {tool}:\n{text}", timeout=4)

    def action_copy_id(self) -> None:
        if self.current:
            self._copy(self.current.id, "UUID")

    def action_copy_resume(self) -> None:
        if self.current:
            self._copy(self.current.resume_command(), "resume command")

    def action_delete(self) -> None:
        s = self.current
        if not s:
            return
        self.running = self.store.running_ids()
        if s.id in self.running:
            self.notify("This session belongs to a running Claude Code process — refusing to delete.",
                        severity="error", timeout=5)
            return
        warn = ""
        if s.recently_active:
            warn = "⚠ Modified in the last 10 minutes — it may still be in use."
        idx = self.query_one("#sessions", DataTable).cursor_row

        def done(confirmed: Optional[bool]) -> None:
            if not confirmed:
                return
            try:
                removed = self.store.delete(s)
            except OSError as e:
                self.notify(f"Delete failed: {e}", severity="error", timeout=6)
                return
            self.notify(f"Deleted {s.id} ({len(removed)} path{'s' if len(removed) != 1 else ''})", timeout=3)
            self.current = None
            self.store.refresh()
            self.drop_vanished_scope()
            self.populate_projects()
            # Same index now points at the next session; clamps to the previous one at the end.
            self.populate_sessions(keep_index=False, select_index=idx)

        self.push_screen(ConfirmDelete(s, self.store.artifacts(s), warn), done)


def main() -> None:
    parser = argparse.ArgumentParser(prog="ccsessions", description="Browse and prune Claude Code sessions.")
    parser.add_argument("--root", type=Path, default=os.environ.get("CCSESSIONS_ROOT"),
                        help="projects directory (default: ~/.claude/projects, or $CCSESSIONS_ROOT)")
    parser.add_argument("--here", action="store_true", help="start scoped to the project for the current directory")
    parser.add_argument("--list", action="store_true", help="print sessions as TSV and exit (no UI)")
    args = parser.parse_args()

    store = Store(args.root)
    store.refresh()
    if args.list:
        for s in store.all_sessions():
            print(f"{s.id}\t{datetime.fromtimestamp(s.mtime):%Y-%m-%d %H:%M}\t{s.cwd or decode_project_dir(s.project_dir)}\t{s.title}")
        return

    project = None
    if args.here:
        cwd = os.getcwd()
        project = next((p.dir_name for p in store.projects if p.display == cwd), None)
    SessionsApp(store, project).run()


if __name__ == "__main__":
    main()
