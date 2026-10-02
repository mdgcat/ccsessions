# ccsessions

A terminal UI for browsing and cleaning up Claude Code sessions stored in `~/.claude/projects`. It runs on macOS and Linux.

```
┌ Projects ───────────────────┬ Session title ────────────────────────────┐
│ ★ All projects          42  │ UUID     bd0d72d7-…   ● running           │
│ ~/Projects/foo          38  │ Project  ~/Projects/foo (main)            │
│ ~/Projects/bar           4  │ When     2026-10-01 13:10 → 16:25  501K   │
├ Sessions (42) ──────────────┤                                           │
│ ● 16:39  foo  Fix login bug │ ▌ You                                     │
│   15:47  foo  Add CI        │ ...transcript...                          │
└─────────────────────────────┴───────────────────────────────────────────┘
```

## Install

You need [uv](https://docs.astral.sh/uv/). Install it on macOS with `brew install uv`, or on either OS with `curl -LsSf https://astral.sh/uv/install.sh | sh`.

```sh
uv tool install /path/to/claude-sessions-tool    # puts `ccsessions` on your PATH
# or run it without installing:
uv run --project /path/to/claude-sessions-tool ccsessions
```

If you pull changes later, upgrade with `uv tool install --reinstall /path/to/claude-sessions-tool`.

### Clipboard on Linux

The tool tries these in order: `wl-copy` (Wayland), then `xclip` or `xsel` (X11). If none of them is available, it falls back to the OSC 52 terminal escape, which works in most modern terminals and over SSH. On macOS it uses `pbcopy`. If you use tmux, the OSC 52 fallback also needs `set -g set-clipboard on` in `~/.tmux.conf`.

## Usage

```sh
ccsessions            # all projects
ccsessions --here     # start scoped to the project for the current directory
ccsessions --list     # print sessions as TSV (uuid, time, cwd, title) and exit
ccsessions --root DIR # use a different projects dir (or set $CCSESSIONS_ROOT)
```

| Key | Action |
| --- | --- |
| `tab` / `shift+tab` | Move focus between the projects, sessions and transcript panes |
| `↑` `↓` | Move within the focused pane. In the projects list this switches the scope. |
| `a` | Switch between all projects and a single project |
| `n` / `j`, `p` / `k` | Next or previous session (works from any pane) |
| `enter` | On a session, jump into the transcript |
| `d` / `delete` | Delete the session (asks for confirmation), then move to the next one |
| `c` | Copy the session UUID |
| `r` | Copy `cd '<project>' && claude --resume <uuid>` |
| `t` | Show or hide thinking blocks |
| `o` | Expand or truncate tool output |
| `/` | Filter by title, UUID or project (`esc` clears it) |
| `f5` / `ctrl+r` | Reload from disk |
| `q` | Quit |

## What delete removes

- `~/.claude/projects/<project>/<uuid>.jsonl`, the transcript
- `~/.claude/projects/<project>/<uuid>/`, which holds subagent transcripts, tool results and the custom title
- `~/.claude/file-history/<uuid>/` and `~/.claude/session-env/<uuid>/`, if they exist

The confirmation dialog lists every path before anything is removed.

Two safety checks:
- Sessions that belong to a running Claude Code process are marked `●`, and the tool refuses to delete them. It finds them through `~/.claude/sessions/<pid>.json` and checks that the process is alive.
- Sessions modified in the last 10 minutes show an extra warning.

## Development

```sh
uv run ccsessions
# Headless smoke test. It DELETES sessions, so always point it at a copy:
mkdir -p /tmp/fx/claude && cp -Rp ~/.claude/projects /tmp/fx/claude/
uv run python tests/smoke.py /tmp/fx/claude
```
