"""Cross-platform clipboard: native tools first, OSC 52 terminal escape as fallback."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, Optional


def _candidates() -> List[List[str]]:
    if sys.platform == "darwin":
        return [["pbcopy"]]
    cmds = []
    if os.environ.get("WAYLAND_DISPLAY"):
        cmds.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        cmds += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    if os.environ.get("WSL_DISTRO_NAME"):
        cmds.append(["clip.exe"])
    return cmds


def copy_native(text: str) -> Optional[str]:
    """Copy via a system tool. Returns the tool name used, or None if none worked."""
    for cmd in _candidates():
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text.encode(), check=True, timeout=3,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return cmd[0]
        except (OSError, subprocess.SubprocessError):
            continue
    return None
