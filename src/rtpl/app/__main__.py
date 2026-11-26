"""Start the desktop app: ``python -m rtpl.app`` or ``rtpl``.

Files named on the command line open at start-up; that is how a
double-clicked ``.rtpl`` file reaches the app on Windows and Linux. macOS
sends a file-open event instead, which the application passes on too.
"""

from __future__ import annotations

import sys
from pathlib import Path


class DocumentRouter:
    """Sends each file to the window for its type."""

    def __init__(self, rtpl_window) -> None:
        self.rtpl_window = rtpl_window

    def open(self, path: Path, ask: bool = True) -> None:
        from PySide6.QtWidgets import QMessageBox

        from rtpl.app.documents import READY_WINDOWS, WINDOW_EQTA, window_for

        kind = window_for(path)
        if kind not in READY_WINDOWS:
            # The EQTA window is still to come (GitHub issue #45).
            what = ("Estimated tax files (.eqta) open in a later version of the app."
                    if kind == WINDOW_EQTA else "This app opens .rtpl and .json files only.")
            QMessageBox.information(self.rtpl_window, "Open", f"Could not open {path.name}.\n{what}")
            return
        if ask:
            self.rtpl_window.open_from_os(path)
        else:
            self.rtpl_window.open_file(path)


def application_class():
    """QApplication that hands macOS file-open events to the router."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    class Application(QApplication):
        def __init__(self, argv: list[str]) -> None:
            super().__init__(argv)
            self.router: DocumentRouter | None = None
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
    from rtpl.app.documents import files_from_args
    from rtpl.app.icon import install_app_icon, set_windows_app_id
    from rtpl.app.main_window import APP_NAME, MainWindow, apply_app_theme

    set_windows_app_id()
    app = application_class()(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    install_app_icon(app)
    window = MainWindow()
    window.effective_mode = apply_app_theme(app, "system")
    window.redraw()
    window.resize_to_screen()
    app.router = DocumentRouter(window)
    window.show()
    # Qt has taken its own options out of app.arguments(); the rest are files.
    # Only one profile or plan is open at a time, so the first one wins.
    files = files_from_args(app.arguments()[1:]) + app.pending
    if files:
        app.router.open(files[0], ask=False)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
