"""The app's two kinds of window, in one program and one process.

``AppWindows`` makes the RTPL window (marginal and forward graphs, ``.rtpl``
files) and the EQTA window (estimated taxes, ``.eqta`` files) when first
needed, sends each opened file to the window for its type, and gives both
windows a Window menu to open or switch to the other. Closing a window asks
about its own changes only; Qt ends the program when the last one closes.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from rtpl.app.documents import READY_WINDOWS, WINDOW_EQTA, WINDOW_RTPL, window_for

RTPL_SHORTCUT = "Ctrl+Alt+1"
EQTA_SHORTCUT = "Ctrl+Alt+2"


class AppWindows:
    def __init__(self, library=None, theme_mode: str = "system") -> None:
        self.library = library
        self.theme_mode = theme_mode
        self._rtpl = None
        self._eqta = None

    # -- the windows --------------------------------------------------------
    def rtpl_window(self, create: bool = True):
        if self._rtpl is None and create:
            from rtpl.app.main_window import MainWindow, apply_app_theme

            if self.library is None:
                win = MainWindow()
                self.library = win.library
            else:
                win = MainWindow(self.library)
            app = QApplication.instance()
            if app is not None:
                win.effective_mode = apply_app_theme(app, self.theme_mode)
            win.redraw()
            win.resize_to_screen()
            win.windows = self
            self.add_window_menu(win)
            self._rtpl = win
        return self._rtpl

    def eqta_window(self, create: bool = True):
        if self._eqta is None and create:
            from rtpl.app.eqta_window import EqtaWindow

            win = EqtaWindow(self.library)
            if self.library is None:
                self.library = win.library
            win.resize_to_screen()
            win.windows = self
            self.add_window_menu(win)
            self._eqta = win
        return self._eqta

    def show(self, win: QMainWindow) -> QMainWindow:
        if win.isMinimized():
            win.showNormal()
        win.show()
        win.raise_()
        win.activateWindow()
        return win

    def show_rtpl(self):
        return self.show(self.rtpl_window())

    def show_eqta(self):
        return self.show(self.eqta_window())

    def add_window_menu(self, win: QMainWindow) -> None:
        """A Window menu before Help (or last), with both windows."""
        bar = win.menuBar()
        menu = bar.addMenu("&Window")
        menu.setToolTipsVisible(True)
        rtpl = QAction("&RTPL: Marginal and Forward Graphs", win, shortcut=RTPL_SHORTCUT,
                       triggered=self.show_rtpl)
        rtpl.setToolTip("Open or switch to the window for profiles and plans (.rtpl)")
        eqta = QAction("&EQTA: Estimated Quarterly Taxes", win, shortcut=EQTA_SHORTCUT,
                       triggered=self.show_eqta)
        eqta.setToolTip("Open or switch to the window for estimated tax files (.eqta)")
        menu.addAction(rtpl)
        menu.addAction(eqta)
        actions = bar.actions()
        help_menu = next((a for a in actions if a.text().replace("&", "") == "Help"), None)
        if help_menu is not None:
            bar.removeAction(menu.menuAction())
            bar.insertAction(help_menu, menu.menuAction())
        win.window_menu = menu

    # -- files ------------------------------------------------------------------
    def open(self, path: Path, ask: bool = True) -> None:
        """Open ``path`` in the window for its type. ``ask``: ask first to
        save the window's own unsaved changes (a file sent while running)."""
        path = Path(path)
        kind = window_for(path)
        if kind not in READY_WINDOWS:
            parent = self._rtpl or self._eqta
            QMessageBox.information(parent, "Open", f"Could not open {path.name}.\n"
                                    "This app opens .rtpl, .json and .eqta files only.")
            return
        win = self.eqta_window() if kind == WINDOW_EQTA else self.rtpl_window()
        assert kind in (WINDOW_EQTA, WINDOW_RTPL)
        self.show(win)
        if ask:
            win.open_from_os(path)
        else:
            win.open_file(path)
