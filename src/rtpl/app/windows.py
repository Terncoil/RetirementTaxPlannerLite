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
    def __init__(self, library=None, theme_mode: str | None = None) -> None:
        self.library = library
        self.theme_mode = theme_mode  # None: the one saved from the View menu
        self.effective_mode: str | None = None  # light or dark, once applied
        self._rtpl = None
        self._eqta = None

    # -- the theme ----------------------------------------------------------
    def _ensure_theme(self) -> str:
        """Apply the saved theme before the first window shows, whichever
        window that is (a double-clicked .eqta file opens only EQTA's)."""
        if self.effective_mode is None:
            from rtpl.app.main_window import apply_app_theme, saved_theme_mode

            if self.theme_mode is None:
                self.theme_mode = saved_theme_mode()
            app = QApplication.instance()
            self.effective_mode = apply_app_theme(app, self.theme_mode) if app is not None else "light"
        return self.effective_mode

    def set_theme(self, mode: str) -> None:
        """A theme chosen in either window's View menu: saved, and shown in both."""
        from rtpl.app.main_window import apply_app_theme, save_theme_mode

        save_theme_mode(mode)
        self.theme_mode = mode
        self.effective_mode = apply_app_theme(QApplication.instance(), mode)
        for win in (self._rtpl, self._eqta):
            if win is not None:
                win.theme_changed(mode, self.effective_mode)

    # -- the windows --------------------------------------------------------
    def rtpl_window(self, create: bool = True):
        if self._rtpl is None and create:
            from rtpl.app.main_window import MainWindow

            effective = self._ensure_theme()
            if self.library is None:
                win = MainWindow()
                self.library = win.library
            else:
                win = MainWindow(self.library)
            win.windows = self
            win.theme_changed(self.theme_mode, effective)
            win.resize_to_screen()
            self.add_window_menu(win)
            self._rtpl = win
        return self._rtpl

    def eqta_window(self, create: bool = True):
        if self._eqta is None and create:
            from rtpl.app.eqta_window import EqtaWindow

            effective = self._ensure_theme()
            win = EqtaWindow(self.library)
            if self.library is None:
                self.library = win.library
            win.resize_to_screen()
            win.windows = self
            win.theme_changed(self.theme_mode, effective)
            self.add_window_menu(win)
            self._eqta = win
        return self._eqta

    def show(self, win: QMainWindow) -> QMainWindow:
        if win.isMinimized():
            win.showNormal()
        from rtpl.app.main_window import sync_title_bar

        win.winId()  # make the native window now, so its title bar is right when shown
        sync_title_bar(win)  # dark title bar in dark mode, for both windows
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
