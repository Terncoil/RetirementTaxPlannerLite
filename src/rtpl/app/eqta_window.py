"""The Estimated Quarterly Tax Assistant (EQTA) window.

One tab per tax year in the open ``.eqta`` file, each with a Worksheet and
a Calculation detail tab. The Inputs panel on the left edits the current
year. The window has its own File, Year and View menus; the Window menu
(added by ``rtpl.app.windows``) switches to the RTPL window.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QRadioButton,
    QSizePolicy,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from rtpl.app import TIP_WIDTH
from rtpl.app.documents import EQTA_FILTER, EQTA_SUFFIX, WINDOW_EQTA, window_for, with_eqta_suffix
from rtpl.app.eqta_editor import EqtaEditor
from rtpl.app.eqta_worksheet import YearPage, money
from rtpl.app.inputs import SpinBox
from rtpl.app.side_panel import PanelHost, SidePanel
from rtpl.engine.estimated import compute_worksheet
from rtpl.model.eqta import EqtaFile, NewYearDefaults, TaxYear
from rtpl.taxlaw.loader import TaxLawLibrary

EQTA_NAME = "Estimated Quarterly Tax Assistant"
DEFAULT_PANEL_WIDTH = 400

QUALIFIED_SHARE_NOTE = (
    "Why the qualified share matters: a fund's ordinary dividends are taxed in "
    "two ways. The qualified part is taxed at the capital gains rates (often 0% "
    "or 15%), the rest as ordinary income. Until the 1099-DIV arrives, the "
    "worksheet splits each fund's dividends by its qualified share and marks both "
    "parts as estimated. Last year's 1099-DIV, or the fund's own estimate, is a "
    "good guide."
)


class YearDialog(QDialog):
    """Tax year and description, for New, Clone and Rename/Describe Year."""

    def __init__(self, title: str, year: int, description: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        form = QFormLayout(self)
        self.year = SpinBox()
        self.year.setRange(2020, 2100)
        self.year.setValue(year)
        self.description = QLineEdit(description)
        self.description.setPlaceholderText("for example Projection or Actuals")
        form.addRow("Tax &year", self.year)
        form.addRow("&Description", self.description)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class NewYearDefaultsDialog(QDialog):
    def __init__(self, defaults: NewYearDefaults, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Year Defaults")
        lay = QVBoxLayout(self)
        note = QLabel(QUALIFIED_SHARE_NOTE)
        note.setWordWrap(True)
        lay.addWidget(note)
        form = QFormLayout()
        self.share = QDoubleSpinBox()
        self.share.setRange(0, 100)
        self.share.setDecimals(1)
        self.share.setSuffix("%")
        self.share.setValue(defaults.qualified_share * 100)
        form.addRow("&Qualified share for new ordinary-dividend sources", self.share)
        lay.addLayout(form)
        lay.addWidget(QLabel("When cloning a year, each ordinary-dividend source's qualified share:"))
        self.retain = QRadioButton("&Retain source year value")
        self.set_to = QRadioButton("&Set to")
        group = QButtonGroup(self)
        group.addButton(self.retain)
        group.addButton(self.set_to)
        self.clone_share = QDoubleSpinBox()
        self.clone_share.setRange(0, 100)
        self.clone_share.setDecimals(1)
        self.clone_share.setSuffix("%")
        self.clone_share.setValue(defaults.clone_share * 100)
        (self.retain if defaults.clone_mode == "retain" else self.set_to).setChecked(True)
        row = QHBoxLayout()
        row.addWidget(self.set_to)
        row.addWidget(self.clone_share)
        row.addStretch(1)
        lay.addWidget(self.retain)
        lay.addLayout(row)
        self.set_to.toggled.connect(self.clone_share.setEnabled)
        self.clone_share.setEnabled(self.set_to.isChecked())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def defaults(self) -> NewYearDefaults:
        return NewYearDefaults(
            qualified_share=self.share.value() / 100,
            clone_mode="retain" if self.retain.isChecked() else "set",
            clone_share=self.clone_share.value() / 100,
        )


class EqtaWindow(QMainWindow):
    def __init__(self, library: TaxLawLibrary | None = None, settings_path: Path | None = None) -> None:
        super().__init__()
        from rtpl.app.main_window import config_dir

        self.library = library or TaxLawLibrary(user_dirs=[config_dir() / "taxlaw"])
        self.windows = None  # the app's window manager, when there is one
        self.path: Path | None = None
        self.eq = EqtaFile()
        self.settings = QSettings(str(settings_path or config_dir() / "eqta_window.ini"),
                                  QSettings.Format.IniFormat)

        self.years = QTabWidget()
        self.years.setDocumentMode(True)
        self.years.setAccessibleName("Tax years")
        self.years.currentChanged.connect(self._year_changed)
        self.editor = EqtaEditor(self.library.available_states(), state_name=self._state_name)
        self.inputs_panel = SidePanel("Inputs", self.editor, Qt.Edge.LeftEdge)
        workspace = QWidget()
        self.panels = PanelHost(workspace, self.years)
        self.setCentralWidget(workspace)
        pinned = self.settings.value("panels/inputs_pinned", True) in (True, "true")
        # Wide enough for the register's amounts and the forms' labels.
        self.inputs_panel.saved_width = int(self.settings.value("panels/inputs_width", 0) or 0) or DEFAULT_PANEL_WIDTH
        self.panels.add(self.inputs_panel, pinned, pinned)
        self.panels.visibility_changed.connect(self._panel_state_changed)
        self.panels.pinned_changed.connect(self._panel_state_changed)
        self._timer = QTimer(self, singleShot=True, interval=200)
        self._timer.timeout.connect(self.recompute)
        self.editor.changed.connect(self._edited)
        self.editor.page_changed.connect(self._refit_inputs)
        self._build_toolbar()
        self._build_menus()
        self.statusBar().showMessage("Ready")
        self.new_file(ask=False)

    # -- building -------------------------------------------------------
    def _state_name(self, code: str | None, year: int) -> str | None:
        if not code:
            return None
        try:
            st = self.library.state(code, year)
        except (KeyError, ValueError):
            return code
        return st.name if st is not None else code

    def _build_toolbar(self) -> None:
        bar = QToolBar("Panels")
        bar.setObjectName("EqtaPanelsToolbar")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.inputs_action = QAction("&Inputs", self, checkable=True, shortcut="Ctrl+1")
        self.inputs_action.setToolTip("Show or hide the inputs panel (Ctrl+1)")
        self.inputs_action.triggered.connect(lambda: self.panels.toggle(self.inputs_panel))
        bar.addAction(self.inputs_action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self.inputs_action.setChecked(self.panels.is_open(self.inputs_panel))

    def _build_menus(self) -> None:
        m = self.menuBar().addMenu("&File")
        for text, key, slot in (
            ("&New", QKeySequence.StandardKey.New, self.new_file),
            ("&Open…", QKeySequence.StandardKey.Open, self.open_document),
            ("&Save", QKeySequence.StandardKey.Save, self.save),
            ("Save &As…", QKeySequence.StandardKey.SaveAs, self.save_as),
        ):
            m.addAction(QAction(text, self, shortcut=key, triggered=slot))
        m.addSeparator()
        m.addAction(QAction("&Close Window", self, shortcut=QKeySequence.StandardKey.Close, triggered=self.close))
        y = self.menuBar().addMenu("&Year")
        self.year_actions = {}
        for key, text, slot in (
            ("new", "&New Year…", self.new_year),
            ("clone", "&Clone Year…", self.clone_year),
            ("rename", "&Rename/Describe Year…", self.rename_year),
            ("delete", "&Delete Year", self.delete_year),
        ):
            act = QAction(text, self, triggered=slot)
            self.year_actions[key] = act
            y.addAction(act)
        self.year_actions["clone"].setToolTip("A copy of this year's profile and income sources with every\n"
                                              "amount zeroed: no register entries and no plan amounts.")
        y.setToolTipsVisible(True)
        v = self.menuBar().addMenu("&View")
        from rtpl.app.main_window import THEME_MODES, saved_theme_mode

        group = QActionGroup(self)
        self.theme_actions = {}
        current = saved_theme_mode()
        for text, mode in THEME_MODES:
            act = QAction(text, self, checkable=True, checked=mode == current)
            act.triggered.connect(lambda _=False, md=mode: self.set_theme_mode(md))
            group.addAction(act)
            v.addAction(act)
            self.theme_actions[mode] = act
        v.addSeparator()
        v.addAction(self.inputs_action)
        v.addAction(QAction("&Reset Window Layout", self, triggered=self.reset_layout))
        v.addSeparator()
        v.addAction(QAction("New Year &Defaults…", self, triggered=self.edit_defaults))
        self.view_menu = v

    def set_theme_mode(self, mode: str) -> None:
        """View > System theme, Light mode or Dark mode, shared with RTPL."""
        if self.windows is not None:
            self.windows.set_theme(mode)
            return
        from rtpl.app.main_window import apply_app_theme, save_theme_mode

        save_theme_mode(mode)
        self.theme_changed(mode, apply_app_theme(QApplication.instance(), mode))

    def theme_changed(self, mode: str, effective: str) -> None:
        self.theme_actions[mode].setChecked(True)
        self.recompute()  # the worksheet's dimmed lines take the new colors

    def resize_to_screen(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry is not None and self.restoreGeometry(geometry):
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1300, 850)
            return
        avail = screen.availableGeometry()
        w, h = min(1400, avail.width()), min(900, avail.height())
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2)

    def reset_layout(self) -> None:
        self.settings.remove("window")
        self.settings.remove("panels")
        self.inputs_panel.saved_width = DEFAULT_PANEL_WIDTH
        self.panels.set_pinned(self.inputs_panel, True)
        self.panels.set_open(self.inputs_panel, True)
        self.showNormal()
        self.resize_to_screen()

    def _refit_inputs(self) -> None:
        """Re-measure the panel for the form now shown: the scroll area takes
        its height at once and the panel widens if the form needs it, so the
        form is never cut off or scrolled sideways."""
        QApplication.sendEvent(self.inputs_panel.scroll, QEvent(QEvent.Type.LayoutRequest))
        self.panels.refit(self.inputs_panel)
        self.inputs_panel.scroll.horizontalScrollBar().setValue(0)

    def _panel_state_changed(self, *_args) -> None:
        self.inputs_action.setChecked(self.panels.is_open(self.inputs_panel))
        self.settings.setValue("panels/inputs_pinned", self.panels.is_pinned(self.inputs_panel))

    def save_window_state(self) -> None:
        self.settings.setValue("window/geometry", self.saveGeometry())
        if self.inputs_panel.saved_width:
            self.settings.setValue("panels/inputs_width", self.inputs_panel.saved_width)
        self.settings.sync()

    # -- years ------------------------------------------------------------
    def current_year(self) -> TaxYear | None:
        i = self.years.currentIndex()
        return self.eq.years[i] if 0 <= i < len(self.eq.years) else None

    def _rebuild_tabs(self, select: TaxYear | None = None) -> None:
        self.years.blockSignals(True)
        while self.years.count():
            w = self.years.widget(0)
            self.years.removeTab(0)
            w.deleteLater()
        for ty in self.eq.years:
            self.years.addTab(YearPage(), ty.title)
        if select is not None and select in self.eq.years:
            self.years.setCurrentIndex(self.eq.years.index(select))
        self.years.blockSignals(False)
        for key in ("clone", "rename", "delete"):
            self.year_actions[key].setEnabled(bool(self.eq.years))
        self._year_changed(self.years.currentIndex())

    def _year_changed(self, _index: int) -> None:
        self.editor.set_year(self.eq, self.current_year())
        self.recompute()

    def _ask_year(self, title: str, year: int, description: str = "") -> tuple[int, str] | None:
        dlg = YearDialog(title, year, description, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return dlg.year.value(), dlg.description.text().strip()

    def new_year(self) -> None:
        like = self.current_year()
        answer = self._ask_year("New Year", (like.year + 1) if like else dt.date.today().year)
        if answer:
            ty = self.eq.new_year(*answer, like=like)
            self._rebuild_tabs(ty)
            self._mark_modified()

    def clone_year(self) -> None:
        src = self.current_year()
        if src is None:
            return
        answer = self._ask_year("Clone Year", src.year + 1, src.description)
        if answer:
            ty = self.eq.clone_year(src, *answer)
            self._rebuild_tabs(ty)
            self._mark_modified()

    def rename_year(self) -> None:
        ty = self.current_year()
        if ty is None:
            return
        answer = self._ask_year("Rename/Describe Year", ty.year, ty.description)
        if answer:
            ty.year, ty.description = answer
            ty.profile.year = ty.year
            self.eq.years.sort(key=lambda y: y.year)
            self._rebuild_tabs(ty)
            self._mark_modified()

    def delete_year(self, confirm: bool = True) -> None:
        ty = self.current_year()
        if ty is None:
            return
        if confirm:
            answer = QMessageBox.question(self, "Delete Year", f"Delete the {ty.title} tab and everything in it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        i = self.eq.years.index(ty)
        del self.eq.years[i]
        rest = self.eq.years
        self._rebuild_tabs(rest[min(i, len(rest) - 1)] if rest else None)
        self._mark_modified()

    def edit_defaults(self) -> None:
        dlg = NewYearDefaultsDialog(self.eq.defaults, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.eq.defaults = dlg.defaults()
            self._mark_modified()

    # -- recalculation ------------------------------------------------------
    def _edited(self) -> None:
        self._mark_modified()
        self._timer.start()

    def recompute(self) -> None:
        ty = self.current_year()
        page = self.years.currentWidget()
        if ty is None or page is None:
            return
        problems = ty.profile.validate()
        if problems:
            page.show_problem("; ".join(problems))
            self.statusBar().showMessage("; ".join(problems))
            return
        try:
            ws = compute_worksheet(ty, self.library, self.eq)
        except (KeyError, ValueError) as exc:
            page.show_problem(f"Cannot compute: {exc}")
            self.statusBar().showMessage(f"Cannot compute: {exc}")
            return
        page.show_worksheet(ws)
        self.worksheet = ws
        self.statusBar().showMessage(
            f"{ty.title} through December 31: federal tax {money(ws.federal_tax[3])}, "
            f"owed {money(ws.federal_owed[3])}"
            + (f"; {ws.state.name} tax {money(ws.state_tax[3])}, owed {money(ws.state_owed[3])}"
               if ws.state else ""))

    # -- files ----------------------------------------------------------------
    def _mark_modified(self) -> None:
        self.setWindowModified(True)

    def _update_title(self) -> None:
        name = self.path.name if self.path else "Untitled"
        self.setWindowTitle(f"{name}[*] - {EQTA_NAME}")

    def load(self, eq: EqtaFile, path: Path | None) -> None:
        self.eq, self.path = eq, path
        self._rebuild_tabs(eq.years[-1] if eq.years else None)
        self.setWindowModified(False)
        self._update_title()

    def maybe_save(self) -> bool:
        if not self.isWindowModified():
            return True
        answer = QMessageBox.warning(
            self, EQTA_NAME, "The estimated tax file has unsaved changes. Save them?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if self.maybe_save():
            self.save_window_state()
            event.accept()
        else:
            event.ignore()

    def new_file(self, ask: bool = True) -> None:
        if ask and not self.maybe_save():
            return
        eq = EqtaFile()
        eq.new_year(dt.date.today().year)
        self.load(eq, None)

    def open_document(self) -> None:
        if not self.maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open", "", EQTA_FILTER)
        if path:
            self.open_file(Path(path))

    def open_from_os(self, path: Path) -> None:
        """A file double-clicked while the app is running."""
        self.show()
        self.raise_()
        self.activateWindow()
        if self.maybe_save():
            self.open_file(path)

    def open_file(self, path: Path) -> None:
        if window_for(path) != WINDOW_EQTA and self.windows is not None:
            self.windows.open(path)  # a profile or plan: the RTPL window opens it
            return
        try:
            eq = EqtaFile.load(path)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            QMessageBox.warning(self, "Open", textwrap.fill(f"Could not open {path.name}: {exc}", TIP_WIDTH))
            return
        self.load(eq, path)

    def save(self) -> bool:
        if self.path is None:
            return self.save_as()
        return self._write(self.path)

    def save_as(self) -> bool:
        start = str(self.path) if self.path else f"Estimated taxes{EQTA_SUFFIX}"
        path, _ = QFileDialog.getSaveFileName(self, "Save As", start, EQTA_FILTER)
        if not path:
            return False
        return self._write(with_eqta_suffix(Path(path)))

    def _write(self, path: Path) -> bool:
        try:
            self.eq.save(path)
        except OSError as exc:
            QMessageBox.warning(self, "Save", f"Could not save {path.name}:\n{exc}")
            return False
        self.path = path
        self.setWindowModified(False)
        self._update_title()
        self.statusBar().showMessage(f"Saved {path}")
        return True
