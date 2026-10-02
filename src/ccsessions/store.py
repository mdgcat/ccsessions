"""Discovery, indexing, parsing and deletion of Claude Code session files.

Layout under the projects root (default ``~/.claude/projects``)::

    <encoded-project-dir>/<session-uuid>.jsonl   the transcript
    <encoded-project-dir>/<session-uuid>/        sidecar (subagents, tool results, titles)

Claude Code also keeps per-session data in ``~/.claude/file-history/<uuid>`` and
``~/.claude/session-env/<uuid>``; deleting a session removes those as well.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SYSTEM_TAG_RE = re.compile(
    r"<(system-reminder|local-command-stdout|local-command-caveat|command-message)>.*?</\1>",
    re.DOTALL,
)
COMMAND_RE = re.compile(r"<command-name>(.*?)</command-name>.*?(?:<command-args>(.*?)</command-args>)?", re.DOTALL)

# Sessions written to within this window may belong to a running Claude Code process.
ACTIVE_WINDOW_SECONDS = 10 * 60


def default_root() -> Path:
    return Path.home() / ".claude" / "projects"


@dataclass
class Session:
    id: str
    path: Path
    project_dir: str
    mtime: float
    size: int
    title: str = ""
    cwd: str = ""
    git_branch: str = ""
    first_ts: str = ""
    last_ts: str = ""

    @property
    def sidecar(self) -> Path:
        return self.path.with_suffix("")

    @property
    def is_empty(self) -> bool:
        return self.size == 0

    @property
    def recently_active(self) -> bool:
        return time.time() - self.mtime < ACTIVE_WINDOW_SECONDS

    def resume_command(self) -> str:
        cwd = self.cwd or decode_project_dir(self.project_dir)
        return f"cd {shell_quote(cwd)} && claude --resume {self.id}"


@dataclass
class Project:
    dir_name: str
    path: Path
    display: str
    sessions: List[Session] = field(default_factory=list)

    @property
    def mtime(self) -> float:
        return max((s.mtime for s in self.sessions), default=0.0)


@dataclass
class Message:
    role: str  # "user" | "assistant"
    kind: str  # "text" | "thinking" | "tool_use" | "tool_result"
    text: str
    timestamp: str = ""
    name: str = ""  # tool name for tool_use
    is_error: bool = False


def shell_quote(s: str) -> str:
    if s and re.fullmatch(r"[A-Za-z0-9_./~@%+=:,-]+", s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"


def decode_project_dir(name: str) -> str:
    """Best-effort reverse of Claude's path encoding ('/' and '.' become '-'). Lossy."""
    return name.replace("-", "/") if name.startswith("-") else name


def _iter_json(path: Path) -> Iterator[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _first_user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return ""


def clean_user_text(text: str) -> str:
    """Strip injected harness tags; render slash commands compactly."""
    m = COMMAND_RE.search(text)
    if m and "<command-name>" in text:
        args = (m.group(2) or "").strip()
        return f"{m.group(1).strip()} {args}".strip()
    return SYSTEM_TAG_RE.sub("", text).strip()


def _one_line(s: str, limit: int = 100) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def scan_session(path: Path, project_dir: str) -> Session:
    st = path.stat()
    sess = Session(id=path.stem, path=path, project_dir=project_dir, mtime=st.st_mtime, size=st.st_size)
    if st.st_size == 0:
        sess.title = "(empty)"
        return sess

    custom = ai = last_prompt = first_prompt = first_command = ""
    for rec in _iter_json(path):
        t = rec.get("type")
        if not sess.cwd and rec.get("cwd"):
            sess.cwd = rec["cwd"]
        if rec.get("gitBranch"):
            sess.git_branch = rec["gitBranch"]
        ts = rec.get("timestamp")
        if ts and t in ("user", "assistant"):
            sess.first_ts = sess.first_ts or ts
            sess.last_ts = ts
        if t == "custom-title" and rec.get("customTitle"):
            custom = rec["customTitle"]
        elif t == "ai-title" and rec.get("aiTitle"):
            ai = rec["aiTitle"]
        elif t == "last-prompt" and rec.get("lastPrompt"):
            last_prompt = rec["lastPrompt"]
        elif t == "summary" and rec.get("summary") and not ai:
            ai = rec["summary"]
        elif t == "user" and not first_prompt and not rec.get("isMeta") and not rec.get("isSidechain"):
            text = clean_user_text(_first_user_text((rec.get("message") or {}).get("content")))
            if text.startswith("/"):
                first_command = first_command or text
            elif text:
                first_prompt = text

    sidecar_title = path.with_suffix("") / "custom-title.json"
    if not custom and sidecar_title.is_file():
        try:
            custom = json.loads(sidecar_title.read_text()).get("customTitle", "")
        except (OSError, ValueError):
            pass

    sess.title = _one_line(custom or ai or first_prompt or last_prompt or first_command or "(no messages)")
    return sess


class Store:
    def __init__(self, root: Optional[Path] = None, claude_home: Optional[Path] = None):
        self.root = Path(root) if root else default_root()
        # Per-session dirs outside projects/ live alongside it in ~/.claude.
        self.claude_home = Path(claude_home) if claude_home else self.root.parent
        self.projects: List[Project] = []
        self._cache: Dict[Path, Tuple[float, int, Session]] = {}

    def refresh(self) -> List[Project]:
        projects: List[Project] = []
        seen = set()
        if self.root.is_dir():
            for pdir in sorted(self.root.iterdir()):
                if not pdir.is_dir():
                    continue
                sessions = []
                for f in pdir.glob("*.jsonl"):
                    if not UUID_RE.match(f.stem):
                        continue
                    seen.add(f)
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    cached = self._cache.get(f)
                    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                        sess = cached[2]
                    else:
                        try:
                            sess = scan_session(f, pdir.name)
                        except OSError:
                            continue
                        self._cache[f] = (st.st_mtime, st.st_size, sess)
                    sessions.append(sess)
                if not sessions:
                    continue
                sessions.sort(key=lambda s: s.mtime, reverse=True)
                cwd = next((s.cwd for s in sessions if s.cwd), "")
                display = cwd or decode_project_dir(pdir.name)
                projects.append(Project(pdir.name, pdir, display, sessions))
        for stale in set(self._cache) - seen:
            del self._cache[stale]
        projects.sort(key=lambda p: p.mtime, reverse=True)
        self.projects = projects
        return projects

    def running_ids(self) -> set:
        """Session IDs owned by a live Claude Code process (from ~/.claude/sessions/<pid>.json)."""
        ids = set()
        reg = self.claude_home / "sessions"
        if not reg.is_dir():
            return ids
        for f in reg.glob("*.json"):
            try:
                info = json.loads(f.read_text())
                pid, sid = int(info.get("pid", 0)), info.get("sessionId")
            except (OSError, ValueError, TypeError):
                continue
            if sid and pid > 0 and _pid_alive(pid):
                ids.add(sid)
        return ids

    def all_sessions(self) -> List[Session]:
        out = [s for p in self.projects for s in p.sessions]
        out.sort(key=lambda s: s.mtime, reverse=True)
        return out

    def project_for_dir(self, dir_name: str) -> Optional[Project]:
        return next((p for p in self.projects if p.dir_name == dir_name), None)

    def artifacts(self, sess: Session) -> List[Path]:
        """Every path that belongs to this session and would be removed on delete."""
        paths = [sess.path, sess.sidecar]
        for sub in ("file-history", "session-env"):
            paths.append(self.claude_home / sub / sess.id)
        return [p for p in paths if p.exists() or p.is_symlink()]

    def delete(self, sess: Session) -> List[Path]:
        removed = []
        for p in self.artifacts(sess):
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(p)
        self._cache.pop(sess.path, None)
        return removed


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def load_messages(path: Path) -> List[Message]:
    msgs: List[Message] = []
    for rec in _iter_json(path):
        t = rec.get("type")
        if t not in ("user", "assistant") or rec.get("isMeta"):
            continue
        ts = rec.get("timestamp", "")
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            text = clean_user_text(content) if t == "user" else content.strip()
            if text:
                msgs.append(Message(t, "text", text, ts))
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "text":
                text = block.get("text", "")
                text = clean_user_text(text) if t == "user" else text.strip()
                if text:
                    msgs.append(Message(t, "text", text, ts))
            elif bt == "thinking":
                text = (block.get("thinking") or "").strip()
                if text:
                    msgs.append(Message(t, "thinking", text, ts))
            elif bt in ("tool_use", "server_tool_use"):
                msgs.append(Message(t, "tool_use", _tool_summary(block.get("input")), ts, name=block.get("name", "tool")))
            elif bt == "tool_result" or bt.endswith("_tool_result"):
                msgs.append(
                    Message(t, "tool_result", _result_text(block.get("content")), ts, is_error=bool(block.get("is_error")))
                )
    return msgs


def _tool_summary(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "pattern", "url", "query", "description", "prompt", "skill"):
        if isinstance(inp.get(key), str) and inp[key]:
            return inp[key]
    return json.dumps(inp, ensure_ascii=False)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    parts.append("[image]")
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return ""
