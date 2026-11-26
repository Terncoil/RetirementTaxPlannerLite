"""Main window: the graph in the middle, inputs and graph options in side panels."""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import date
from pathlib import Path

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QEvent, QSettings, QSignalBlocker, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QIcon, QPixmap, QGuiApplication, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProxyStyle,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOptionMenuItem,
    QTableWidget,
    QTableWidgetItem,
    QStackedWidget,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from rtpl import __version__
from rtpl.app import TIP_WIDTH
from rtpl.export import write_csv, write_ods
from rtpl.app.documents import (
    OPEN_FILTER,
    RTPL_SUFFIX,
    SAVE_FILTER,
    save_as_start,
    with_rtpl_suffix,
)
from rtpl.app.icon import about_pixmap
from rtpl.app.shortcut import (
    can_register_file_types,
    file_types_registered,
    register_file_types,
    unregister_file_types,
)
from rtpl.app.inputs import ComboBox
from rtpl.app.plan_editor import PlanEditor
from rtpl.app.plan_view import PlanView
from rtpl.app.profile_form import ProfileForm, money_box
from rtpl.app.side_panel import PanelHost, PanelTabs, SidePanel
from rtpl.charts.forward_chart import render_balances, render_income, render_taxes
from rtpl.charts.marginal_chart import render_marginal
from rtpl.engine.marginal import (
    ANNOTATIONS,
    LAYERS,
    MarginalSeries,
    build_marginal_series,
    default_visibility,
)
from rtpl.engine.tax import IncomeSource
from rtpl.model.plan import Plan
from rtpl.model.profile import Person, Profile
from rtpl.report import ReportChart, Section, profile_sections, write_report
from rtpl.taxlaw.loader import TaxLawLibrary
from rtpl.theme import Theme

APP_NAME = "RetirementTaxPlannerLite"
ANNOTATION_LABELS = {
    "irmaa": "IRMAA brackets (Medicare)",
    "aca": "ACA subsidy cliffs",
    "state_cliff": "State tax cliffs",
}


PROJECT_URL = "https://github.com/Terncoil/RetirementTaxPlannerLite"


def config_dir() -> Path:
    path = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
    path.mkdir(parents=True, exist_ok=True)
    return path


class AppStyle(QProxyStyle):
    """Fusion, with menu separators drawn in the palette's Mid color so the
    groups in a menu show in dark mode too (Fusion's own line nearly matches
    a dark menu's background)."""

    def __init__(self) -> None:
        super().__init__("Fusion")

    def drawControl(self, element, option, painter, widget=None) -> None:
        if (element == QStyle.ControlElement.CE_MenuItem and isinstance(option, QStyleOptionMenuItem)
                and option.menuItemType == QStyleOptionMenuItem.MenuItemType.Separator):
            r = option.rect
            painter.save()
            painter.setPen(option.palette.color(QPalette.ColorRole.Mid))
            y = r.center().y()
            painter.drawLine(r.left() + 6, y, r.right() - 6, y)
            painter.restore()
            return
        super().drawControl(element, option, painter, widget)


def apply_app_theme(app: QApplication, mode: str) -> str:
    """Apply light/dark to the Qt widgets. Returns the effective mode."""
    hints = QGuiApplication.styleHints()
    can_set = hasattr(hints, "setColorScheme")  # Qt 6.8 and later
    if mode == "system":
        if can_set:
            hints.unsetColorScheme()  # forget an earlier Light or Dark choice
        mode = "dark" if hints.colorScheme() == Qt.ColorScheme.Dark else "light"
    elif can_set:
        # Tell Qt too, so nothing follows the system's scheme behind our back
        # (with Windows in dark mode, Fusion's own palette is dark).
        hints.setColorScheme(Qt.ColorScheme.Dark if mode == "dark" else Qt.ColorScheme.Light)
    pal = QPalette()
    if mode == "dark":
        c = {
            QPalette.ColorRole.Window: "#242423",
            QPalette.ColorRole.WindowText: "#f0efec",
            QPalette.ColorRole.Base: "#151514",
            QPalette.ColorRole.Light: "#77766f",
            QPalette.ColorRole.Midlight: "#5c5b56",
            QPalette.ColorRole.Mid: "#6a6964",
            QPalette.ColorRole.Dark: "#4a4945",
            QPalette.ColorRole.Shadow: "#000000",
            QPalette.ColorRole.AlternateBase: "#262625",
            QPalette.ColorRole.Text: "#f0efec",
            QPalette.ColorRole.Button: "#2b2b2a",
            QPalette.ColorRole.ButtonText: "#f0efec",
            QPalette.ColorRole.Highlight: "#3987e5",
            QPalette.ColorRole.HighlightedText: "#ffffff",
            QPalette.ColorRole.ToolTipBase: "#2b2b2a",
            QPalette.ColorRole.ToolTipText: "#f0efec",
            QPalette.ColorRole.PlaceholderText: "#9a998f",
            QPalette.ColorRole.Link: "#86b6ef",
        }
        for role, color in c.items():
            pal.setColor(role, QColor(color))
        pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#7a7970"))
        pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#7a7970"))
    else:
        # Fusion's classic light colors, set out in full rather than taken
        # from standardPalette(), which follows the system's dark mode.
        c = {
            QPalette.ColorRole.Window: "#efefef",
            QPalette.ColorRole.WindowText: "#000000",
            QPalette.ColorRole.Base: "#ffffff",
            QPalette.ColorRole.Light: "#ffffff",
            QPalette.ColorRole.Midlight: "#cacaca",
            QPalette.ColorRole.Mid: "#b8b8b8",
            QPalette.ColorRole.Dark: "#9f9f9f",
            QPalette.ColorRole.Shadow: "#767676",
            QPalette.ColorRole.AlternateBase: "#f7f7f7",
            QPalette.ColorRole.Text: "#000000",
            QPalette.ColorRole.Button: "#efefef",
            QPalette.ColorRole.ButtonText: "#000000",
            QPalette.ColorRole.BrightText: "#ffffff",
            QPalette.ColorRole.Highlight: "#308cc6",
            QPalette.ColorRole.HighlightedText: "#ffffff",
            QPalette.ColorRole.ToolTipBase: "#ffffdc",
            QPalette.ColorRole.ToolTipText: "#000000",
            QPalette.ColorRole.PlaceholderText: "#6e6e6e",
            QPalette.ColorRole.Link: "#0000ff",
            QPalette.ColorRole.LinkVisited: "#ff00ff",
        }
        for role, color in c.items():
            pal.setColor(role, QColor(color))
        for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
            pal.setColor(QPalette.ColorGroup.Disabled, role, QColor("#bebebe"))
    app.setPalette(pal)
    # Set the style after the palette: changing the style re-polishes every
    # widget, and widgets with a style sheet (the side panels) take their
    # colors at that moment. Style sheets are re-applied too, since a
    # "palette(...)" color in one is read when the sheet is set.
    app.setStyle(AppStyle())
    for widget in app.allWidgets():
        sheet = widget.styleSheet()
        if sheet:
            widget.setStyleSheet("")
            widget.setStyleSheet(sheet)
    return mode


class ColorDialog(QDialog):
    """Pick the chart color for each layer, for light and dark mode."""

    def __init__(self, theme: Theme, labels: dict[str, str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Chart Colors")
        self.theme = theme
        self.labels = labels
        self.buttons: dict[tuple[str, str], QPushButton] = {}
        outer = QVBoxLayout(self)
        self.same = QCheckBox("&Same colors for light and dark mode")
        self.same.setChecked(theme.linked)
        self.same.toggled.connect(self._link_toggled)
        outer.addWidget(self.same)

        grid = QGridLayout()
        grid.addWidget(QLabel("<b>Layer</b>"), 0, 0)
        self.light_header = QLabel()
        self.dark_header = QLabel("<b>Dark mode</b>")
        grid.addWidget(self.light_header, 0, 1)
        grid.addWidget(self.dark_header, 0, 2)
        for row, key in enumerate(LAYERS, start=1):
            grid.addWidget(QLabel(labels[key]), row, 0)
            for col, mode in enumerate(("light", "dark"), start=1):
                btn = QPushButton()
                btn.clicked.connect(lambda _=False, m=mode, k=key: self._pick(m, k))
                self.buttons[(mode, key)] = btn
                grid.addWidget(btn, row, col)
        outer.addLayout(grid)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        restore = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        restore.setText("&Reset to Defaults")
        restore.clicked.connect(self._reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._refresh()

    def _refresh(self) -> None:
        linked = self.theme.linked
        self.light_header.setText("<b>Both modes</b>" if linked else "<b>Light mode</b>")
        self.dark_header.setVisible(not linked)
        for (mode, key), btn in self.buttons.items():
            color = self.theme.raw[mode]["layers"][key]
            btn.setText(color)
            swatch = QPixmap(28, 16)
            swatch.fill(QColor(color))
            btn.setIcon(QIcon(swatch))
            btn.setIconSize(swatch.size())
            which = "light and dark mode" if linked else f"{mode} mode"
            btn.setAccessibleName(f"{self.labels[key]} color, {which}")
            if mode == "dark":
                btn.setVisible(not linked)
        self.adjustSize()

    def _link_toggled(self, checked: bool) -> None:
        self.theme.linked = checked
        if checked:  # start the shared set from the light colors
            for key in LAYERS:
                self.theme.set_layer_color("dark", key, self.theme.raw["light"]["layers"][key])
        self._refresh()

    def _pick(self, mode: str, key: str) -> None:
        current = QColor(self.theme.raw[mode]["layers"][key])
        color = QColorDialog.getColor(current, self, f"Choose color: {self.labels[key]}")
        if color.isValid():
            self.theme.set_layer_color(mode, key, color.name())
            if self.theme.linked:
                self.theme.set_layer_color("dark", key, color.name())
            self._refresh()

    def _reset(self) -> None:
        self.theme.reset_layers()
        if self.theme.linked:
            for key in LAYERS:
                self.theme.set_layer_color("dark", key, self.theme.raw["light"]["layers"][key])
        self._refresh()


class NewProfileDefaultsDialog(QDialog):
    """Edit the profile that File > New starts from.

    It is kept in the app's settings folder, not in any profile or plan
    file, so a single filer in New York (say) can stop changing the same
    fields on every new profile.
    """

    def __init__(self, defaults: Profile, current: Profile, builtin: Profile,
                 states: list[str], parent=None, law_for=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Profile Defaults")
        self._current = current
        self._builtin = builtin
        outer = QVBoxLayout(self)
        intro = QLabel("File > New starts from these values. They are kept with the "
                       "app's settings, not saved in any profile or plan file.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        self.form = ProfileForm(states, law_for=law_for)
        self.form.set_profile(defaults)
        # New profiles always start in the current calendar year.
        self.form.year.setEnabled(False)
        self.form.year.setToolTip("New profiles always start in the current year")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form)
        outer.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        restore = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        restore.setText("&Reset to Built-in")
        restore.setToolTip("Go back to the example profile the app comes with")
        restore.clicked.connect(lambda: self.form.set_profile(self._builtin))
        use_current = buttons.addButton("Copy &Current Profile", QDialogButtonBox.ButtonRole.ActionRole)
        use_current.setToolTip("Fill the form from the profile that is open now")
        use_current.clicked.connect(lambda: self.form.set_profile(self._current))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.resize(460, 640)

    def profile(self) -> Profile:
        return self.form.profile()


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_file_stem(name: str, fallback: str = "profile") -> str:
    """Turn a plan name into a file name that is valid on Windows, macOS and Linux."""
    cleaned = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:120]


class MainWindow(QMainWindow):
    def __init__(self, library: TaxLawLibrary | None = None) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.library = library or TaxLawLibrary(user_dirs=[config_dir() / "taxlaw"])
        self.theme_path = config_dir() / "theme.toml"
        self.theme = Theme.load(self.theme_path)
        self.new_defaults_path = config_dir() / "new_profile.json"
        self.theme_mode = "system"
        self.effective_mode = "light"
        self.current_path: Path | None = None
        self.series: MarginalSeries | None = None
        self.overrides: dict[str, bool] = {}  # user's explicit layer choices

        # Center: chart and data table
        self.tabs = QTabWidget()
        self.figure = Figure(figsize=(9, 6), dpi=100)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumSize(320, 240)
        self.canvas.setAccessibleName("Marginal tax rate chart")
        self.canvas.setAccessibleDescription("The Data table tab lists the same information as text.")
        self.tabs.addTab(self.canvas, "&Chart")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Additional income", "Marginal rate", "What changes"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAccessibleName("Marginal rate data table")
        self.tabs.addTab(self.table, "&Data table")

        # Side panels: inputs on the left, graph options on the right. Each
        # can be pinned beside the graph or pop open over it.
        self.form = ProfileForm(self.library.available_states(), law_for=self._law_for_tips)
        # On the Forward plan page the panel also lists the plan's items; the
        # profile becomes its Baseline tab. Elsewhere only the profile shows.
        self.plan_editor = PlanEditor()
        self.input_tabs = PanelTabs()
        self.input_tabs.addTab(self.form, "&Baseline")
        self.input_tabs.addTab(self.plan_editor, "&Plan items")
        self.inputs_panel = SidePanel("Inputs", self.input_tabs, Qt.Edge.LeftEdge)
        self.input_tabs.currentChanged.connect(self._size_input_tabs)
        # Center pages: the marginal graph, and the forward-looking plan
        self.plan_path: Path | None = None
        self.plan_view = PlanView(self.library)
        self.plan_view.status.connect(lambda msg: self.statusBar().showMessage(msg))
        self.pages = QTabWidget()
        self.pages.addTab(self.tabs, "&Marginal graph")
        self.pages.addTab(self.plan_view, "&Forward plan")
        # The Graph options panel shows the options of whichever page is active.
        self.options_stack = QStackedWidget()
        self.options_stack.addWidget(self._build_options())
        self.options_stack.addWidget(self.plan_view.options_widget)
        self.options_panel = SidePanel("Graph options", self.options_stack, Qt.Edge.RightEdge)
        self.pages.currentChanged.connect(self._page_changed)
        self._page_changed(self.pages.currentIndex())
        workspace = QWidget()
        self.panels = PanelHost(workspace, self.pages)
        self.setCentralWidget(workspace)
        self.settings = QSettings(str(config_dir() / "window.ini"), QSettings.Format.IniFormat)
        wide = self._screen_width() >= 1300
        for panel, key, default_pinned in (
            (self.inputs_panel, "inputs", True),
            (self.options_panel, "options", wide),
        ):
            pinned = self._setting_bool(f"panels/{key}_pinned", default_pinned)
            open_ = self._setting_bool(f"panels/{key}_open", pinned)
            panel.saved_width = int(self.settings.value(f"panels/{key}_width", 0) or 0)
            self.panels.add(panel, pinned, open_)
        self.panels.visibility_changed.connect(self._panel_state_changed)
        self.panels.pinned_changed.connect(self._panel_state_changed)
        self._build_toolbar()
        self._build_menus()

        self._timer = QTimer(self, singleShot=True, interval=200)
        self._timer.timeout.connect(self.recompute)
        self.form.changed.connect(self._schedule)
        self.form.changed.connect(self._mark_modified)
        # Plan edits recalculate only the plan, a moment after the last change.
        self._plan_timer = QTimer(self, singleShot=True, interval=250)
        self._plan_timer.timeout.connect(self._recompute_plan)
        self.plan_editor.changed.connect(self._plan_timer.start)
        self.plan_editor.changed.connect(self._mark_modified)
        self.plan_view.columns_changed.connect(self._mark_modified)
        self._sync_input_tabs()

        self._load_profile(self._new_profile_defaults(), None)
        self.statusBar().showMessage("Ready")

    # -- building ------------------------------------------------------
    def _new_profile_defaults(self) -> Profile:
        """The profile File > New starts from: the user's saved defaults
        (View > New Profile Defaults), or the built-in example."""
        if self.new_defaults_path.exists():
            try:
                profile = Profile.load(self.new_defaults_path)
                profile.year = date.today().year  # always the current year
                return profile
            except (OSError, ValueError, TypeError) as exc:
                print(f"Ignoring {self.new_defaults_path}: {exc}")
        return self._starter_profile()

    def edit_new_profile_defaults(self) -> None:
        dlg = NewProfileDefaultsDialog(
            self._new_profile_defaults(), self.form.profile(), self._starter_profile(),
            self.library.available_states(), self, law_for=self._law_for_tips,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        profile = dlg.profile()
        profile.year = date.today().year
        if profile.to_dict() == self._starter_profile().to_dict():
            self.new_defaults_path.unlink(missing_ok=True)
        else:
            profile.save(self.new_defaults_path)
        self.statusBar().showMessage("File > New will use these defaults")

    def _law_for_tips(self, year: int):
        """Federal law for ``year``, for tooltips that quote its amounts."""
        return self.library.federal(year)

    @staticmethod
    def _starter_profile() -> Profile:
        return Profile(
            name="Example",
            year=date.today().year,
            filing_status="mfj",
            people=[
                Person(name="Person 1", birth_year=1959, social_security=30000),
                Person(name="Person 2", birth_year=1961, social_security=18000),
            ],
        )

    def _build_options(self) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 6, 6, 6)
        left = QFormLayout()
        left.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.source = ComboBox()
        for s in IncomeSource:
            self.source.addItem(s.label[0].upper() + s.label[1:], s.value)
        self.source.setAccessibleName("Source of additional income")
        src_label = QLabel("&Additional income source")
        src_label.setBuddy(self.source)
        left.addRow(src_label, self.source)
        self.x_max = money_box()
        self.x_max.setRange(10_000, 5_000_000)
        self.x_max.setSingleStep(25_000)
        self.x_max.setValue(250_000)
        self.x_max.setAccessibleName("Show additional income up to")
        xlabel = QLabel("Show up &to")
        xlabel.setBuddy(self.x_max)
        left.addRow(xlabel, self.x_max)
        self.irmaa_basis = ComboBox()
        self.irmaa_basis.addItem("Premiums 2 years out (projected)", "premium_year")
        self.irmaa_basis.addItem("This year's published brackets", "current_year")
        self.irmaa_basis.setAccessibleName("IRMAA brackets to show")
        self.irmaa_basis.setToolTip(
            "Income this year sets Medicare premiums two years from now, so the\n"
            "default shows those brackets, projected with the profile's\n"
            "inflation rate."
        )
        ilabel = QLabel("&IRMAA brackets")
        ilabel.setBuddy(self.irmaa_basis)
        left.addRow(ilabel, self.irmaa_basis)
        for combo in (self.source, self.irmaa_basis):
            # Size to a short width and let the popup list show full items.
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(20)
        lay.addLayout(left)

        layers = QGroupBox("Layers and lines")
        grid = QVBoxLayout(layers)
        self.checks: dict[str, QCheckBox] = {}
        names = {k: k for k in LAYERS}
        names.update(
            ordinary="Ordinary income tax",
            ss="Social Security phase-in",
            senior="Senior deduction phase-out",
            cg="Capital gains / QD tax",
            niit="NIIT",
            state="State tax",
            state_cg="State CG & QD tax",
            edu="Education credit phase-out",
            ptc="ACA premium tax credit phase-out",
            nonref="Nonrefundable credit absorbing tax",
        )
        names.update(ANNOTATION_LABELS)
        tips = {
            "edu": "Education credits lost as MAGI rises through their phase-out\n"
                   "(American Opportunity and Lifetime Learning).",
            "ptc": "ACA premium subsidy lost on each extra dollar below the 400%\n"
                   "cliff. A premium rather than a tax, but it costs the same.\n"
                   "Needs a benchmark premium in the profile. The cliff itself is\n"
                   "the ACA line, labelled with the subsidy lost there.",
            "nonref": "Drawn below 0%: tax that an unused nonrefundable credit\n"
                      "cancels, so the total stays at 0% until the credit is used up.",
        }
        for key in [*LAYERS, *ANNOTATIONS]:
            cb = QCheckBox(names[key])
            if key in tips:
                cb.setToolTip(tips[key])
            cb.clicked.connect(lambda checked, k=key: self._user_toggled(k, checked))
            self.checks[key] = cb
            grid.addWidget(cb)
        reset = QPushButton("&Reset to suggested")
        reset.setToolTip("Go back to the layers suggested by the profile")
        reset.clicked.connect(self._reset_visibility)
        grid.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(layers)
        lay.addStretch(1)

        self.source.currentIndexChanged.connect(self._schedule)
        self.x_max.valueChanged.connect(self._schedule)
        self.irmaa_basis.currentIndexChanged.connect(self._schedule)
        return box

    def _page_changed(self, index: int) -> None:
        self.options_stack.setCurrentIndex(index)
        # Only the shown page's options count toward the panel's size.
        for i in range(self.options_stack.count()):
            policy = QSizePolicy.Policy.Preferred if i == index else QSizePolicy.Policy.Ignored
            self.options_stack.widget(i).setSizePolicy(policy, policy)
        name = "Forward plan options" if self.pages.widget(index) is self.plan_view else "Marginal graph options"
        self.options_panel.title.setText(f"<b>{name}</b>")
        self.options_panel.setAccessibleName(name)
        if hasattr(self, "plan_editor"):
            self._sync_input_tabs()

    def _sync_input_tabs(self) -> None:
        """Show the Plan items tab only on the Forward plan page with a plan open."""
        show = self.pages.currentWidget() is self.plan_view and self.plan_view.plan is not None
        items = self.input_tabs.indexOf(self.plan_editor)
        was = self.input_tabs.isTabVisible(items)
        self.input_tabs.setTabVisible(items, show)
        if show and not was:
            self.input_tabs.setCurrentIndex(items)
        elif not show:
            self.input_tabs.setCurrentIndex(self.input_tabs.indexOf(self.form))
        title = "Plan" if show else "Inputs"
        self.inputs_panel.title.setText(f"<b>{title}</b>")
        self.inputs_panel.setAccessibleName(title)
        self._size_input_tabs()

    def _size_input_tabs(self, *_args) -> None:
        """Re-measure the panel for the page now shown: its scroll area takes
        the new page's height at once, and the panel widens if needed."""
        QApplication.sendEvent(self.inputs_panel.scroll, QEvent(QEvent.Type.LayoutRequest))
        if hasattr(self, "panels"):
            self.panels.refit(self.inputs_panel)

    def _set_plan(self, plan: Plan | None) -> None:
        self.plan_view.set_plan(plan)
        self.plan_editor.set_plan(plan)
        if hasattr(self, "export_table_action"):
            self.export_table_action.setEnabled(plan is not None)
        self._sync_input_tabs()

    def _recompute_plan(self) -> None:
        if self.plan_view.plan is not None:
            self.plan_view.recompute()

    def _build_toolbar(self) -> None:
        bar = QToolBar("Panels")
        bar.setObjectName("PanelsToolbar")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.inputs_action = QAction("&Inputs", self, checkable=True, shortcut="Ctrl+1")
        self.inputs_action.setToolTip("Show or hide the inputs panel (Ctrl+1)")
        self.inputs_action.triggered.connect(lambda: self.panels.toggle(self.inputs_panel))
        self.options_action = QAction("&Graph options", self, checkable=True, shortcut="Ctrl+2")
        self.options_action.setToolTip("Show or hide the graph options panel (Ctrl+2)")
        self.options_action.triggered.connect(lambda: self.panels.toggle(self.options_panel))
        bar.addAction(self.inputs_action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        bar.addAction(self.options_action)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self._sync_panel_actions()

    def resize_to_screen(self) -> None:
        """Restore the last size and position, or open at 1400 x 900
        (smaller on a smaller screen), centered."""
        geometry = self.settings.value("window/geometry")
        if geometry is not None and self.restoreGeometry(geometry):
            return
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1400, 900)
            return
        avail = screen.availableGeometry()
        w, h = min(1400, avail.width()), min(900, avail.height())
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2)

    def save_window_state(self) -> None:
        self.settings.setValue("window/geometry", self.saveGeometry())
        for panel, key in ((self.inputs_panel, "inputs"), (self.options_panel, "options")):
            if panel.saved_width:
                self.settings.setValue(f"panels/{key}_width", panel.saved_width)
        self.settings.sync()

    def reset_layout(self) -> None:
        """Forget saved window size, position and panel settings."""
        self.settings.remove("window")
        self.settings.remove("panels")
        wide = self._screen_width() >= 1300
        for panel, pinned in ((self.inputs_panel, True), (self.options_panel, wide)):
            panel.saved_width = 0
            self.panels.set_pinned(panel, pinned)
            self.panels.set_open(panel, pinned)
        self.showNormal()
        self.resize_to_screen()

    def _screen_width(self) -> int:
        screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry().width() if screen else 1920

    def _setting_bool(self, key: str, default: bool) -> bool:
        value = self.settings.value(key, default)
        return value in (True, "true", "1", 1) if not isinstance(value, bool) else value

    def _sync_panel_actions(self) -> None:
        if hasattr(self, "inputs_action"):
            self.inputs_action.setChecked(self.panels.is_open(self.inputs_panel))
            self.options_action.setChecked(self.panels.is_open(self.options_panel))

    def _panel_state_changed(self, *_args) -> None:
        self._sync_panel_actions()
        for panel, key in ((self.inputs_panel, "inputs"), (self.options_panel, "options")):
            pinned = self.panels.is_pinned(panel)
            self.settings.setValue(f"panels/{key}_pinned", pinned)
            # A floating panel always starts closed; a pinned one remembers.
            self.settings.setValue(f"panels/{key}_open", self.panels.is_open(panel) and pinned)

    def _build_menus(self) -> None:
        m = self.menuBar().addMenu("&File")
        m.setToolTipsVisible(True)
        for text, key, slot in (
            ("&New", QKeySequence.StandardKey.New, self.new_profile),
            ("&Open…", QKeySequence.StandardKey.Open, self.open_document),
            ("&Save", QKeySequence.StandardKey.Save, self.save),
            ("Save &As…", QKeySequence.StandardKey.SaveAs, self.save_as),
        ):
            act = QAction(text, self, shortcut=key, triggered=slot)
            m.addAction(act)
        save_profile = QAction("Save &Profile As…", self, shortcut="Ctrl+Alt+S", triggered=self.save_profile_as)
        save_profile.setToolTip("Save only the profile (no plan items), even while a plan is open")
        m.addAction(save_profile)
        m.addSeparator()
        new_plan = QAction("New P&lan from Profile", self, triggered=self.new_plan)
        new_plan.setToolTip("Start a forward plan from the profile. It replaces the open plan,\n"
                            "if there is one; you are asked first about unsaved changes")
        m.addAction(new_plan)
        m.addSeparator()
        m.addAction(QAction("&Export Chart…", self, shortcut="Ctrl+E", triggered=self.export_chart))
        self.export_table_action = QAction("Export &Year Table…", self, shortcut="Ctrl+Shift+E",
                                           triggered=self.export_year_table)
        self.export_table_action.setToolTip("Save the forward plan's year table as a spreadsheet (.ods)\nor CSV")
        self.export_table_action.setEnabled(False)
        m.addAction(self.export_table_action)
        report = QAction("Export &Report…", self, shortcut="Ctrl+Shift+R", triggered=self.export_report)
        report.setToolTip("Save one PDF with the profile and every chart (the forward plan's\n"
                          "charts too when a plan is open), ready to print or share")
        m.addAction(report)
        m.addSeparator()
        m.addAction(QAction("E&xit", self, shortcut=QKeySequence.StandardKey.Quit, triggered=self.close))

        v = self.menuBar().addMenu("&View")
        v.setToolTipsVisible(True)
        group = QActionGroup(self)
        for text, mode in (("&System theme", "system"), ("&Light mode", "light"), ("&Dark mode", "dark")):
            act = QAction(text, self, checkable=True, checked=mode == "system")
            act.triggered.connect(lambda _=False, md=mode: self.set_theme_mode(md))
            group.addAction(act)
            v.addAction(act)
        v.addSeparator()
        v.addAction(self.inputs_action)
        v.addAction(self.options_action)
        v.addAction(QAction("&Reset Window Layout", self, triggered=self.reset_layout))
        v.addSeparator()
        v.addAction(QAction("Chart &Colors…", self, triggered=self.edit_colors))
        defaults = QAction("New Profile &Defaults…", self, triggered=self.edit_new_profile_defaults)
        defaults.setToolTip("Choose the values File > New starts from, such as filing status,\n"
                            "state, inflation and Social Security")
        v.addAction(defaults)
        # Ticked while the OS opens .rtpl files with this copy of the app; it
        # reads the OS each time the menu opens. macOS: the bundle does it.
        self.file_types_action = None
        if can_register_file_types():
            v.addSeparator()
            self.file_types_action = QAction("Open .rtpl Files With &This App", self, checkable=True)
            self.file_types_action.setToolTip(
                "Ticked: double-clicking a .rtpl file opens it in this copy of the app.\n"
                "Untick to stop. If you move the app to another folder, the tick\n"
                "clears; tick it again")
            self.file_types_action.toggled.connect(self._file_types_toggled)
            v.addAction(self.file_types_action)
            v.aboutToShow.connect(self.refresh_file_types_action)
            self.refresh_file_types_action()

        h = self.menuBar().addMenu("&Help")
        h.setToolTipsVisible(True)
        home = QAction("&Project Home…", self, triggered=self.open_project_home)
        home.setToolTip("Open the project's GitHub page in your web browser")
        h.addAction(home)
        h.addSeparator()
        h.addAction(QAction("&About", self, triggered=self.about))

    # -- behaviour -----------------------------------------------------
    def _schedule(self, *_args) -> None:
        self._timer.start()

    def _user_toggled(self, key: str, checked: bool) -> None:
        self.overrides[key] = checked
        self.redraw()

    def _reset_visibility(self) -> None:
        self.overrides.clear()
        self.recompute()

    def visibility(self) -> dict[str, bool]:
        assert self.series is not None
        vis = default_visibility(self.series)
        vis.update(self.overrides)
        return vis

    def recompute(self) -> None:
        profile = self.form.profile()
        problems = profile.validate()
        if problems:
            self.statusBar().showMessage("; ".join(problems))
            return
        try:
            self.series = build_marginal_series(
                profile,
                self.library,
                source=IncomeSource(self.source.currentData()),
                x_max=self.x_max.value(),
                step=max(100.0, self.x_max.value() / 2500),
                irmaa_basis=self.irmaa_basis.currentData(),
            )
        except (KeyError, ValueError) as exc:
            self.statusBar().showMessage(f"Cannot compute: {exc}")
            return
        state_cb = self.checks["state"]
        state_cb.setText(self.series.state.label if self.series.state else "State tax")
        state_cb.setEnabled(self.series.state is not None)
        st = self.series.state
        state_cb.setEnabled(st is not None and st.has_income_tax)
        cg_cb = self.checks["state_cg"]
        cg_cb.setText(st.gains_label if st else "State CG & QD tax")
        cg_cb.setEnabled(st is not None and st.has_separate_gains_tax)
        cg_cb.setToolTip(
            "Tax under the state's own capital gains rules (Washington's gains\n"
            "tax, Montana's gains rates, Hawaii's gains rate cap). Where a state\n"
            "taxes gains as regular income, that tax is in the State Tax layer."
        )
        state_cb.setToolTip(
            # State notes and sources come from the data files: wrap them to the tooltip width.
            "\n".join(textwrap.fill(line, TIP_WIDTH, subsequent_indent="  ") for line in
                      [f"{st.name} ({st.year or ''})", *st.notes, *(f"Source: {x}" for x in st.sources)])
            if st else "Choose a state in the profile to add this layer."
        )
        self.redraw()
        if self.plan_view.plan is not None:
            self.plan_view.recompute(profile)
            self.plan_editor.refresh()  # names and ages come from the baseline
        b = self.series.baseline
        self.statusBar().showMessage(
            f"Baseline: AGI ${b.agi:,.0f}, taxable income ${b.taxable_income:,.0f}, "
            f"federal tax {'-' if b.federal_tax < 0 else ''}${abs(b.federal_tax):,.0f}, "
            f"state tax ${b.state_tax:,.0f}"
        )

    def redraw(self) -> None:
        if self.series is None:
            return
        vis = self.visibility()
        for key, cb in self.checks.items():
            cb.blockSignals(True)
            cb.setChecked(vis.get(key, False))
            cb.blockSignals(False)
        render_marginal(self.figure, self.series, vis, self.theme.palette(self.effective_mode))
        self.canvas.draw_idle()
        self.plan_view.set_palette(self.theme.palette(self.effective_mode))
        self._fill_table(vis)

    def _fill_table(self, vis: dict[str, bool]) -> None:
        s = self.series
        assert s is not None
        shown = [k for k in LAYERS if vis.get(k)]
        rows: list[tuple[float, str, str]] = []
        prev = None
        # Exact thresholds rather than chart steps, so no row shows a blend
        # of the rates on either side of a threshold.
        for bp in s.breakpoints():
            total = round(sum(bp.rates[k] for k in shown), 4)
            if prev is None or abs(total - prev) > 1e-4:
                parts = [
                    f"{s.layer_label(k)} {bp.rates[k]:.1%}" for k in shown if abs(bp.rates[k]) > 1e-6
                ]
                rows.append((bp.x, f"{total:.1%}", ", ".join(parts)))
                prev = total
        for a in s.annotations:
            if vis.get(a.kind):
                rows.append((a.x, "", a.detail))
        rows.sort(key=lambda r: r[0])
        self.table.setRowCount(len(rows))
        for r, (x, rate, what) in enumerate(rows):
            for c, text in enumerate((f"${x:,.0f}", rate, what)):
                item = QTableWidgetItem(text)
                if c < 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, item)

    def set_theme_mode(self, mode: str) -> None:
        self.theme_mode = mode
        self.effective_mode = apply_app_theme(QApplication.instance(), mode)
        self.redraw()

    def edit_colors(self) -> None:
        labels = {k: (self.series.layer_label(k) if self.series else k) for k in LAYERS}
        working = Theme(raw=__import__("copy").deepcopy(self.theme.raw))
        dlg = ColorDialog(working, labels, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.theme = working
            self.theme_path.write_text(self.theme.to_toml(), encoding="utf-8")
            self.redraw()

    # -- files -----------------------------------------------------------
    def _mark_modified(self) -> None:
        self.setWindowModified(True)

    def _update_title(self) -> None:
        path = self.plan_path if getattr(self, "plan_view", None) and self.plan_view.plan is not None else self.current_path
        name = path.name if path else "Untitled"
        self.setWindowTitle(f"{name}[*] - {APP_NAME}")

    def _load_profile(self, profile: Profile, path: Path | None) -> None:
        self.current_path = path
        self.overrides.clear()
        self.form.set_profile(profile)
        self.setWindowModified(False)
        self._update_title()

    def maybe_save(self) -> bool:
        """Ask about unsaved changes. Returns False if the user cancels."""
        if not self.isWindowModified():
            return True
        answer = QMessageBox.warning(
            self,
            APP_NAME,
            f"The {'plan' if self.plan_view.plan is not None else 'profile'} has unsaved changes. Save them?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if self.maybe_save():
            self.save_window_state()
            event.accept()
        else:
            event.ignore()

    def new_profile(self) -> None:
        if self.maybe_save():
            self._set_plan(None)
            self._load_profile(self._new_profile_defaults(), None)

    def open_document(self) -> None:
        """File > Open: a profile or a plan, whichever the file holds."""
        if not self.maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open", "", OPEN_FILTER)
        if path:
            self.open_file(Path(path))

    def open_from_os(self, path: Path) -> None:
        """A file double-clicked while the app is running (macOS sends these)."""
        self.raise_()
        self.activateWindow()
        if self.maybe_save():
            self.open_file(path)

    def refresh_file_types_action(self) -> None:
        """Tick View > Open .rtpl Files With This App to match the OS."""
        if self.file_types_action is None:
            return
        try:
            registered = file_types_registered()
        except OSError:
            registered = False
        with QSignalBlocker(self.file_types_action):
            self.file_types_action.setChecked(registered)

    def _file_types_toggled(self, checked: bool) -> None:
        """View > Open .rtpl Files With This App (Windows and Linux)."""
        try:
            if checked:
                register_file_types()
            else:
                unregister_file_types()
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, APP_NAME, f"Could not change the .rtpl file type:\n{exc}")
        self.refresh_file_types_action()
        if checked and not self.file_types_action.isChecked() and sys.platform == "win32":
            QMessageBox.information(
                self, APP_NAME,
                "Another app was chosen for .rtpl files with \"Open with\", and only you can\n"
                "change that: right-click a .rtpl file, choose Open with, Choose another\n"
                f"app, pick {APP_NAME} and tick Always use this app.")

    def open_file(self, path: Path) -> None:
        """Open a profile or a plan; a plan file is recognized by its "profile" section."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "profile" in data:
                self.load_plan(Plan.from_dict(data), path)
                return
            profile = Profile.from_dict(data)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            QMessageBox.warning(self, "Open", f"Could not open {path.name}:\n{exc}")
            return
        self._set_plan(None)
        self._load_profile(profile, path)

    def save(self) -> bool:
        """File > Save: the plan when one is open, otherwise the profile."""
        if self.plan_view.plan is not None:
            return self._save_plan()
        if self.current_path is None:
            return self.save_as()
        if not self._write_profile(self.current_path):
            return False
        self.setWindowModified(False)
        self._update_title()
        return True

    def save_as(self) -> bool:
        """File > Save As: what is open (the plan, or else the profile) under a new name."""
        if self.plan_view.plan is not None:
            start = save_as_start(self.plan_path, self._suggested_plan_path(RTPL_SUFFIX))
            path, _ = QFileDialog.getSaveFileName(self, "Save Plan As", start, SAVE_FILTER)
            if not path:
                return False
            self.plan_path = with_rtpl_suffix(Path(path))
            return self._save_plan()
        start = save_as_start(self.current_path, self._suggested_path(RTPL_SUFFIX))
        path, _ = QFileDialog.getSaveFileName(self, "Save Profile As", start, SAVE_FILTER)
        if not path:
            return False
        self.current_path = with_rtpl_suffix(Path(path))
        return self.save()

    def save_profile_as(self) -> bool:
        """File > Save Profile As: only the profile, even while a plan is open.

        With no plan open this is the same as Save As. With a plan open the
        plan stays the open file, so its own unsaved changes stay unsaved."""
        if self.plan_view.plan is None:
            return self.save_as()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Profile As", self._suggested_path(RTPL_SUFFIX), SAVE_FILTER)
        if not path:
            return False
        return self._write_profile(with_rtpl_suffix(Path(path)))

    def _write_profile(self, path: Path) -> bool:
        try:
            self.form.profile().save(path)
        except OSError as exc:
            QMessageBox.warning(self, "Save Profile", f"Could not save the profile:\n{exc}")
            return False
        self.current_path = path
        self.statusBar().showMessage(f"Saved {path}", 4000)
        return True

    def _suggested_path(self, suffix: str) -> str:
        folder = self.current_path.parent if self.current_path else Path.home()
        return str(folder / (safe_file_stem(self.form.plan_name.text()) + suffix))

    def _suggested_plan_path(self, suffix: str) -> str:
        """Baseline name plus the plan's own name, e.g. "Smith - Roth until SS.rtpl"."""
        plan = self.plan_view.plan
        assert plan is not None
        plan.profile = self.form.profile()
        folder = (self.plan_path or self.current_path or Path.home() / "x").parent
        return str(folder / (safe_file_stem(plan.title, "plan") + suffix))

    # -- plans -----------------------------------------------------------
    def load_plan(self, plan: Plan, path: Path | None) -> None:
        """Show a plan; its profile becomes the profile on the left."""
        self.plan_path = path
        self._set_plan(plan)
        self._load_profile(plan.profile, self.current_path if path is None else None)
        self.pages.setCurrentWidget(self.plan_view)
        self._sync_input_tabs()
        self.recompute()

    def new_plan(self) -> None:
        """File > New Plan from Profile. Only one plan is open at a time, so
        this asks first about unsaved changes, as File > New and Exit do."""
        if not self.maybe_save():
            return
        self.load_plan(Plan.from_profile(self.form.profile()), None)
        # The new plan is not on disk yet: Exit or New asks before losing it.
        self.setWindowModified(True)
        self.statusBar().showMessage(
            "New plan from the profile: wages, pensions and Social Security carry forward. "
            "Add accounts and strategies on the Plan items tab.", 8000)

    def _save_plan(self) -> bool:
        if self.plan_path is None:
            return self.save_as()
        plan = self.plan_view.plan
        plan.profile = self.form.profile()
        try:
            plan.save(self.plan_path)
        except OSError as exc:
            QMessageBox.warning(self, "Save Plan", f"Could not save the plan:\n{exc}")
            return False
        self.setWindowModified(False)
        self._update_title()
        self.statusBar().showMessage(f"Saved {self.plan_path}", 4000)
        return True

    def export_chart(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Chart",
            self._suggested_plan_path(".png") if self.pages.currentWidget() is self.plan_view and self.plan_view.plan
            else self._suggested_path(".png"), "PNG image (*.png);;SVG image (*.svg);;PDF (*.pdf)"
        )
        if path:
            fig = self.plan_view.current_figure() if self.pages.currentWidget() is self.plan_view else self.figure
            fig.savefig(path, dpi=200, facecolor=fig.get_facecolor())
            self.statusBar().showMessage(f"Exported {path}", 4000)

    def export_year_table(self) -> None:
        """Save the year table as shown (columns, dollars, each source) as .ods or .csv."""
        if self.plan_view.result is None:
            return
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export Year Table", self._suggested_plan_path(".ods"),
            "OpenDocument spreadsheet (*.ods);;CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith((".ods", ".csv")):
            path += ".csv" if "csv" in chosen.lower() else ".ods"
        data = self.plan_view.table_export()
        try:
            if path.lower().endswith(".csv"):
                write_csv(path, data.header, data.rows)
            else:
                write_ods(path, data.header, data.rows, data.numeric)
        except OSError as exc:
            QMessageBox.warning(self, "Export Year Table", f"Could not save {path}:\n{exc}")
            return
        self.statusBar().showMessage(f"Exported {path}", 4000)

    def export_report(self) -> None:
        """One PDF: the profile, the marginal graph and, with a plan open, the
        forward plan's charts. Drawn in light mode, which prints well."""
        if self.series is None:
            QMessageBox.information(self, "Export Report", "Fix the profile so the marginal graph draws, then try again.")
            return
        start = self._suggested_plan_path(" report.pdf") if self.plan_view.plan is not None else self._suggested_path(" report.pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Export Report", start, "PDF (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        try:
            pages = write_report(path, *self.report_contents())
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Export Report", f"Could not save {path}:\n{exc}")
            return
        self.statusBar().showMessage(f"Exported {path} ({pages} pages)", 4000)

    def report_contents(self) -> tuple[str, list[Section], list[ReportChart]]:
        """The report's title, profile sections and charts, as the window shows them now."""
        assert self.series is not None
        palette = self.theme.palette("light")
        profile = self.form.profile()
        series = self.series
        state = series.state
        sections = profile_sections(profile, state_name=state.name if state else None)
        b = series.baseline
        sections.append(Section(f"Baseline tax for {profile.year}", [
            ("AGI", f"${b.agi:,.0f}"),
            ("Taxable income", f"${b.taxable_income:,.0f}"),
            ("Federal tax", f"{'-' if b.federal_tax < 0 else ''}${abs(b.federal_tax):,.0f}"),
            ("State tax", f"${b.state_tax:,.0f}"),
        ]))
        vis = self.visibility()
        charts = [ReportChart("Marginal tax graph", lambda fig: render_marginal(fig, series, vis, palette))]
        title = profile.name
        plan, result = self.plan_view.plan, self.plan_view.result
        if plan is not None and result is not None and result.years:
            title = plan.title
            view = self.plan_view
            kw = dict(detail=view.detail.isChecked(), todays_dollars=view.todays.isChecked())
            plan_vis = view.visibility()
            rows = [("Plan", plan.name)] if plan.name.strip() else []
            rows += [
                ("Years", f"{result.years[0].year} to {result.years[-1].year}"),
                ("Amounts", "Today's dollars" if kw["todays_dollars"] else "Future (nominal) dollars"),
            ]
            sections.append(Section("Forward plan", rows))
            charts.append(ReportChart("Income and brackets",
                                      lambda fig: render_income(fig, result, plan_vis, palette, **kw)))
            charts.append(ReportChart("Taxes by source", lambda fig: render_taxes(fig, result, palette, **kw)))
            charts.append(ReportChart("Balances", lambda fig: render_balances(fig, result, palette, **kw)))
        return title, sections, charts

    def open_project_home(self) -> None:
        if not QDesktopServices.openUrl(QUrl(PROJECT_URL)):
            QMessageBox.information(self, APP_NAME, f"Couldn't open a web browser. The project is at\n{PROJECT_URL}")

    def about(self) -> None:
        years = ", ".join(str(y) for y in self.library.enacted_years)
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            f"<b>{APP_NAME}</b> {__version__}<br>Free, open-source tax graphs.<br><br>"
            f"Tax law data files: {years} (later years are projected).<br>"
            "This is a planning aid, not tax advice."
        )
        box.setIconPixmap(about_pixmap(64, self.devicePixelRatioF()))
        box.exec()
