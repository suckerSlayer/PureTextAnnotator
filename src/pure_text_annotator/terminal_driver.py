"""Terminal driver choices tailored to PTA's keyboard-first interface."""

from textual.drivers.linux_driver import LinuxDriver


class ButtonEventLinuxDriver(LinuxDriver):
    """Keep mouse clicks, wheels, and drags without passive motion floods."""

    def _enable_mouse_support(self) -> None:
        if not self._mouse:
            return

        write = self.write
        write("\x1b[?1003l")  # Disable all-motion reporting.
        write("\x1b[?1000h")  # Report button presses and releases.
        write("\x1b[?1002h")  # Report motion only while a button is held.
        write("\x1b[?1015h")  # Enable urxvt-compatible coordinates.
        write("\x1b[?1006h")  # Enable SGR extended coordinates.
        self.flush()

    def _disable_mouse_support(self) -> None:
        if not self._mouse:
            return

        write = self.write
        write("\x1b[?1000l")
        write("\x1b[?1002l")
        write("\x1b[?1003l")
        write("\x1b[?1015l")
        write("\x1b[?1006l")
        self.flush()
