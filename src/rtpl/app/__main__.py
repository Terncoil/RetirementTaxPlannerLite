"""Start the desktop app: ``python -m rtpl.app`` or ``rtpl``.

Files named on the command line open at start-up; that is how a
double-clicked ``.rtpl`` or ``.eqta`` file reaches the app on Windows and
Linux. macOS sends a file-open event instead, which the application passes
on too. ``rtpl.app.windows.AppWindows`` sends each file to its window.
"""

from __future__ import annotations

import sys
from pathlib import Path


def application_class():
    """QApplication that hands macOS file-open events to the router."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    class Application(QApplication):
        def __init__(self, argv: list[str]) -> None:
            super().__init__(argv)
            self.router = None  # rtpl.app.windows.AppWindows, once ready
            self.pending: list[Path] = []  # files sent before the window is ready

        def event(self, event) -> bool:
            if event.type() == QEvent.Type.FileOpen and event.file():
                path = Path(event.file())
                if self.router is None:
                    self.pending.append(path)
                else:
                    self.router.open(path)
                return True
            return super().event(event)

    return Application


def main() -> int:
    from rtpl.app.documents import WINDOW_EQTA, files_from_args, window_for
    from rtpl.app.icon import install_app_icon, set_windows_app_id
    from rtpl.app.main_window import APP_NAME
    from rtpl.app.windows import AppWindows

    set_windows_app_id()
    app = application_class()(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    install_app_icon(app)
    windows = AppWindows()
    # Qt has taken its own options out of app.arguments(); the rest are files.
    files = files_from_args(app.arguments()[1:]) + app.pending
    # An .eqta file opens the EQTA window alone; anything else starts with
    # the RTPL window. Each window holds one file, so the first of each kind wins.
    first = files[0] if files else None
    if first is None or window_for(first) != WINDOW_EQTA:
        windows.show_rtpl()
    app.router = windows
    seen = set()
    for path in files:
        kind = window_for(path)
        if kind not in seen:
            seen.add(kind)
            windows.open(path, ask=False)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
