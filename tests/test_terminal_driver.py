import pytest


terminal_driver = pytest.importorskip("pure_text_annotator.terminal_driver")
ButtonEventLinuxDriver = terminal_driver.ButtonEventLinuxDriver


class RecordingDriver:
    def __init__(self, *, mouse: bool = True) -> None:
        self._mouse = mouse
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, value: str) -> None:
        self.writes.append(value)

    def flush(self) -> None:
        self.flushes += 1


def test_button_event_mouse_mode_preserves_dragging_without_passive_motion() -> None:
    driver = RecordingDriver()

    ButtonEventLinuxDriver._enable_mouse_support(driver)

    assert "\x1b[?1000h" in driver.writes
    assert "\x1b[?1002h" in driver.writes
    assert "\x1b[?1003h" not in driver.writes
    assert driver.flushes == 1

    driver.writes.clear()
    ButtonEventLinuxDriver._disable_mouse_support(driver)

    assert "\x1b[?1002l" in driver.writes
    assert "\x1b[?1003l" in driver.writes
    assert driver.flushes == 2


def test_button_event_mouse_mode_respects_disabled_mouse_support() -> None:
    driver = RecordingDriver(mouse=False)

    ButtonEventLinuxDriver._enable_mouse_support(driver)
    ButtonEventLinuxDriver._disable_mouse_support(driver)

    assert driver.writes == []
    assert driver.flushes == 0
