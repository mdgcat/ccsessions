"""Headless smoke test. Usage: uv run python tests/smoke.py <fixture ~/.claude copy>

Never point this at your real ~/.claude — it deletes sessions.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from ccsessions.app import ALL, SessionsApp
from ccsessions.store import Store

home = Path(sys.argv[1]).resolve()
assert home != (Path.home() / ".claude").resolve(), "refusing to run against real ~/.claude"
root = home / "projects"


async def main() -> None:
    store = Store(root)
    store.refresh()
    sessions = store.all_sessions()
    assert sessions, "fixture has no sessions"
    live = sessions[1]
    (home / "sessions").mkdir(exist_ok=True)
    (home / "sessions" / "test.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": live.id}))

    app = SessionsApp(store)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        assert app.scope == ALL
        n_all = len(app.rows)
        print("all sessions:", n_all, "projects:", len(store.projects))

        # Toggle to a single project and back.
        await pilot.press("a")
        await pilot.pause()
        assert app.scope != ALL and all(s.project_dir == app.scope for s in app.rows)
        print("project scope:", app.scope, len(app.rows))
        await pilot.press("a")
        await pilot.pause()
        assert app.scope == ALL and len(app.rows) == n_all

        # Next/prev navigation updates the right pane.
        first = app.current.id
        await pilot.press("n")
        await pilot.pause()
        assert app.current.id != first and app.current.id == live.id
        assert live.id in app.running

        # Deleting a running session is refused.
        await pilot.press("d")
        await pilot.pause()
        assert not isinstance(app.screen, type(None)) and app.screen is app.screen_stack[0]
        assert live.path.exists()

        # Delete the next one (pick one with a sidecar if possible), confirm, and check we advance.
        await pilot.press("n")
        await pilot.pause(0.3)
        victim = app.current
        idx = app.rows.index(victim)
        expected_next = app.rows[idx + 1].id if idx + 1 < len(app.rows) else app.rows[idx - 1].id
        await pilot.press("d")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConfirmDelete"
        await pilot.press("y")
        await pilot.pause(0.3)
        assert not victim.path.exists() and not victim.sidecar.exists()
        assert len(app.rows) == n_all - 1
        assert app.current.id == expected_next, (app.current.id, expected_next)
        print("deleted", victim.id, "-> now on", app.current.id)

        # Delete from the transcript pane: focus stays there and the next session loads.
        await pilot.press("enter")
        await pilot.pause()
        assert app.focused.id == "body"
        victim = app.current
        expected_next = app.rows[app.rows.index(victim) + 1].id
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause(0.3)
        assert not victim.path.exists() and app.current.id == expected_next and app.focused.id == "body"
        n_all -= 1
        print("deleted from right pane", victim.id, "-> now on", app.current.id)

        # Cancel path keeps the file.
        keep = app.current
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("n")  # 'n' cancels inside the modal
        await pilot.pause()
        assert keep.path.exists() and app.current.id == keep.id

        # Filter.
        await pilot.press("slash")
        for ch in keep.id[:8]:
            await pilot.press(ch)
        await pilot.pause()
        assert [s.id for s in app.rows] == [keep.id]
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.rows) == n_all - 1

        # Transcript renders and thinking toggle works.
        await pilot.pause(0.5)
        print("messages in current transcript:", len(app._messages))
        await pilot.press("t")
        await pilot.press("o")
        await pilot.pause()
        app.save_screenshot(str(home.parent / "screen.svg"))

    # Delete a session that has file-history/session-env dirs.
    target = next((s for s in store.all_sessions() if (home / "file-history" / s.id).exists()), None)
    if target:
        paths = store.artifacts(target)
        store.delete(target)
        assert not any(p.exists() for p in paths)
        print("removed artifacts:", [str(p.relative_to(home)) for p in paths])
    print("OK")


asyncio.run(main())
