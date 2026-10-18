"""Best-effort native clipboard integration.

Textual writes clipboard data with the OSC 52 terminal sequence. macOS
Terminal does not support that sequence, so the viewer also tries a native
clipboard command. Failure is non-fatal because Textual's clipboard remains
available to terminals that support it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import List, Optional


class ClipboardCopyError(RuntimeError):
    """Raised when an available native clipboard command fails."""


def _clipboard_commands() -> List[List[str]]:
    if sys.platform == "darwin":
        return [["pbcopy"]]
    if sys.platform == "win32":
        return [["clip.exe"]]
    return [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]


def copy_to_system_clipboard(text: str) -> Optional[str]:
    """Copy text with the first available platform-native command.

    Returns the command name on success or ``None`` when no native clipboard
    utility is installed. Textual's OSC 52 copy path is still used by the app.
    """

    if not text:
        return None

    for command in _clipboard_commands():
        executable = shutil.which(command[0])
        if executable is None:
            continue
        resolved = [executable, *command[1:]]
        try:
            subprocess.run(
                resolved,
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise ClipboardCopyError(
                f"clipboard command {command[0]} failed"
            ) from error
        return command[0]

    return None

