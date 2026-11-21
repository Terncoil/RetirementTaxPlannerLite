"""Forward plan page: income-by-source graph, taxes-by-source graph,
balances graph and a year table.

The plan's items are edited on the Plan items tab of the left panel; the
baseline (people, filing status, state) is the profile on its Baseline
tab. Edits on either recalculate the plan, and the summary line says what
the last change did to lifetime tax and final balances.
"""

from __future__ import annotations

import textwrap
from html import escape

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from rtpl.app import TIP_WIDTH
from rtpl.charts.forward_chart import line_short_label, render_balances, render_income, render_taxes
from rtpl.engine.forward import (
    DEFAULT_TABLE_COLUMNS,
    TABLE_COLUMNS,
    PlanResult,
    default_line_visibility,
    run_plan,
    table_data,
)
from rtpl.model.plan import Plan
from rtpl.model.profile import Profile
from rtpl.taxlaw.loader import TaxLawLibrary
from rtpl.theme import Palette


class PlanView(QWidget):
    status = Signal(str)
    columns_changed = Signal()  # the user picked year table columns (saved in the plan)

    def __init__(self, library: TaxLawLibrary) -> None:
        super().__init__()
        self.library = library
        self.plan: Plan | None = None
        self.result: PlanResult | None = None
        self.palette: Palette | None = None
        self.overrides: dict[str, bool] = {}
        self.last_totals: tuple[float, float] | None = None  # (lifetime tax, final balances)
        self.last_change = ""  # what the last change that moved the totals did
        self.line_checks: dict[str, QCheckBox] = {}

        lay = QVBoxLayout(self)
        self.summary = QLabel("No plan loaded. Use File > Open Plan, or File > New Plan from Profile.")
        self.summary.setWordWrap(True)
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.summary)

        # The plan's graph options live in the Graph options side panel (the
        # main window shows them while this page is active).
        self.options_widget = QWidget()
        olay = QVBoxLayout(self.options_widget)
        olay.setContentsMargins(6, 6, 6, 6)
        view = QGroupBox("View")
        vlay = QVBoxLayout(view)
        self.detail = QCheckBox("Show each &source separately")
        self.detail.setToolTip("Split the bars by person, account and income stream instead of by type")
        self.todays = QCheckBox("In &today's dollars")
        self.todays.setToolTip("Divide each year's amounts by inflation since the plan's first year")
        self.tax_free = QCheckBox("Show ta&x-free withdrawals")
        self.tax_free.setChecked(True)
        self.tax_free.setToolTip("Roth, HSA and brokerage principal: money spent but not taxed;\n"
                                 "and QCDs, given straight to charity")
        for cb in (self.detail, self.todays, self.tax_free):
            cb.toggled.connect(self.redraw)
            vlay.addWidget(cb)
        olay.addWidget(view)
        self.line_groups: dict[str, QVBoxLayout] = {}
        for key, title in (("bracket", "Bracket tops"), ("irmaa", "IRMAA tiers (premiums 2 years later)"),
                           ("other", "Other lines")):
            group = QGroupBox(title)
            self.line_groups[key] = QVBoxLayout(group)
            olay.addWidget(group)
        reset = QPushButton("Reset &lines to suggested")
        reset.setToolTip("Go back to the lines suggested by the plan")
        reset.clicked.connect(self._reset)
        olay.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)
        olay.addStretch(1)

        self.tabs = QTabWidget()
        self.income_fig = Figure(figsize=(9, 6), dpi=100)
        self.income_canvas = FigureCanvasQTAgg(self.income_fig)
        self.income_canvas.setAccessibleName("Income by source and tax thresholds chart")
        self.income_canvas.setAccessibleDescription("The Year table tab lists the same information as text.")
        self.tabs.addTab(self.income_canvas, "&Income and brackets")
        self.tax_fig = Figure(figsize=(9, 6), dpi=100)
        self.tax_canvas = FigureCanvasQTAgg(self.tax_fig)
        self.tax_canvas.setAccessibleName("Taxes by source chart")
        self.tax_canvas.setAccessibleDescription("The Year table tab lists the same taxes as text.")
        self.tabs.addTab(self.tax_canvas, "Tax&es by source")
        self.bal_fig = Figure(figsize=(9, 6), dpi=100)
        self.bal_canvas = FigureCanvasQTAgg(self.bal_fig)
        self.bal_canvas.setAccessibleName("Account balances chart")
        self.tabs.addTab(self.bal_canvas, "&Balances")
        table_page = QWidget()
        tlay = QVBoxLayout(table_page)
        tlay.setContentsMargins(0, 4, 0, 0)
        bar = QHBoxLayout()
        self.columns_button = QPushButton("C&olumns…")
        self.columns_button.setToolTip("Choose the year table's columns\n(also on the column headers' right-click menu)")
        self.columns_menu = QMenu(self)
        self.columns_menu.setToolTipsVisible(True)
        self.column_actions: dict[str, QAction] = {}
        for col in TABLE_COLUMNS:
            act = QAction(col.label, self.columns_menu)
            act.setCheckable(True)
            if col.tip:
                act.setToolTip(col.tip)
            act.toggled.connect(self._columns_picked)
            self.columns_menu.addAction(act)
            self.column_actions[col.key] = act
        self.columns_menu.addSeparator()
        self.columns_menu.addAction("&Reset to suggested columns", self._reset_columns)
        self.columns_button.setMenu(self.columns_menu)
        bar.addWidget(self.columns_button)
        bar.addStretch(1)
        tlay.addLayout(bar)
        self.table = QTableWidget(0, 0)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAccessibleName("Year by year plan table")
        header = self.table.horizontalHeader()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda pos: self.columns_menu.exec(header.mapToGlobal(pos)))
        # Qt leaves a tooltip up when the mouse moves on to a header or cell
        # without one; hide it instead.
        header.viewport().installEventFilter(self)
        self.table.viewport().installEventFilter(self)
        tlay.addWidget(self.table, 1)
        self.tabs.addTab(table_page, "&Year table")
        lay.addWidget(self.tabs, 1)

    # -- data ------------------------------------------------------------
    def set_plan(self, plan: Plan | None) -> None:
        self.plan = plan
        self._sync_column_actions()
        self.overrides.clear()
        self.last_totals = None
        self.last_change = ""
        if plan is None:
            self.result = None
            self.summary.setText("No plan loaded. Use File > Open Plan, or File > New Plan from Profile.")
            self.table.setRowCount(0)
            self._paint_empty()

    def _paint_empty(self) -> None:
        """Blank charts in the theme's background, so dark mode has no white page."""
        for fig, canvas in ((self.income_fig, self.income_canvas), (self.tax_fig, self.tax_canvas),
                            (self.bal_fig, self.bal_canvas)):
            fig.clear()
            if self.palette is not None:
                fig.set_facecolor(self.palette.surface)
            canvas.draw_idle()

    def recompute(self, profile: Profile | None = None) -> None:
        if self.plan is None:
            return
        if profile is not None:
            self.plan.profile = profile
            self.plan.__post_init__()
        problems = self.plan.validate()
        if problems:
            self.status.emit("Plan: " + "; ".join(problems))
            return
        try:
            self.result = run_plan(self.plan, self.library)
        except (KeyError, ValueError, IndexError) as exc:
            self.status.emit(f"Plan cannot be computed: {exc}")
            return
        self._rebuild_line_checks()
        self.redraw()
        r = self.result
        if r.years:
            first, last = r.years[0], r.years[-1]
            short = [y.year for y in r.years if y.shortfall > 0]
            tax = sum(y.tax.total_tax for y in r.years)
            final = sum(last.balances_end.values())
            text = (
                f"<b>{escape(self.plan.title)}</b>: {first.year}–{last.year}, "
                f"{len(self.plan.accounts)} accounts, {len(self.plan.incomes)} income streams, "
                f"{len(self.plan.rules)} strategies. Lifetime federal + state tax "
                f"${tax:,.0f}; final balances ${final:,.0f}."
            )
            if short:
                text += f" Spending not covered in {short[0]}–{short[-1]}."
            # A recalculation that changes nothing keeps the last change's note.
            self.last_change = self._change_text(tax, final) or self.last_change
            if self.last_change:
                text += "<br>" + self.last_change
            self.last_totals = (tax, final)
            self.summary.setText(text)

    def _change_text(self, tax: float, final: float) -> str:
        """What the last change did, e.g. "Last change: tax −$36,351, final balances +$73,068"."""
        if self.last_totals is None:
            return ""
        d_tax, d_final = tax - self.last_totals[0], final - self.last_totals[1]
        if abs(d_tax) < 1 and abs(d_final) < 1:
            return ""

        def part(label: str, d: float, good_when_lower: bool) -> str:
            if abs(d) < 1:
                return f"{label} no change"
            good = (d < 0) == good_when_lower
            dark = self.palette is not None and QColor(self.palette.surface).lightness() < 128
            color = ("#5fd39a" if dark else "#16794c") if good else ("#ff8a80" if dark else "#b42318")
            sign = "+" if d > 0 else "\u2212"
            return f"{label} <span style='color:{color}'><b>{sign}${abs(d):,.0f}</b></span>"

        return "Last change: " + ", ".join((part("tax", d_tax, True), part("final balances", d_final, False)))

    def _rebuild_line_checks(self) -> None:
        assert self.result is not None
        keys = list(self.result.line_labels)
        if list(self.line_checks) == keys:
            return
        for lay in self.line_groups.values():
            while lay.count():
                w = lay.takeAt(0).widget()
                if w:
                    w.deleteLater()
        self.line_checks.clear()
        for key in keys:
            label = self.result.line_labels[key]
            if key.startswith("bracket:"):
                group, text = "bracket", f"Top of {line_short_label(key)}"
            elif key.startswith("irmaa:"):
                group, text = "irmaa", f"Tier {key.split(':')[1]}"
            else:
                group, text = "other", label
            cb = QCheckBox(text)
            cb.setToolTip(label)
            cb.setAccessibleName(label)
            cb.clicked.connect(lambda checked, k=key: self._toggled(k, checked))
            self.line_checks[key] = cb
            self.line_groups[group].addWidget(cb)

    def visibility(self) -> dict[str, bool]:
        assert self.result is not None
        vis = default_line_visibility(self.result)
        vis.update(self.overrides)
        vis["tax_free"] = self.tax_free.isChecked()
        return vis

    def _toggled(self, key: str, checked: bool) -> None:
        self.overrides[key] = checked
        self.redraw()

    def _reset(self) -> None:
        self.overrides.clear()
        self.redraw()

    # -- year table columns --------------------------------------------
    def table_columns(self) -> list[str]:
        """The plan's chosen columns, or the suggested set."""
        known = [c.key for c in TABLE_COLUMNS]
        picked = [k for k in (self.plan.table_columns if self.plan else []) if k in known]
        return picked or list(DEFAULT_TABLE_COLUMNS)

    def _sync_column_actions(self) -> None:
        chosen = set(self.table_columns())
        for key, act in self.column_actions.items():
            act.blockSignals(True)
            act.setChecked(key in chosen)
            act.blockSignals(False)

    def set_table_columns(self, keys: list[str]) -> None:
        """Pick the year table's columns (an empty list means the suggested set)."""
        if self.plan is None:
            return
        order = [c.key for c in TABLE_COLUMNS]
        keys = [k for k in order if k in set(keys)]
        self.plan.table_columns = [] if keys == list(DEFAULT_TABLE_COLUMNS) else keys
        self._sync_column_actions()
        self.redraw()
        self.columns_changed.emit()

    def _columns_picked(self, *_args) -> None:
        keys = [k for k, act in self.column_actions.items() if act.isChecked()]
        self.set_table_columns(keys or list(DEFAULT_TABLE_COLUMNS))

    def _reset_columns(self) -> None:
        self.set_table_columns([])

    # -- drawing ---------------------------------------------------------
    def set_palette(self, palette: Palette) -> None:
        self.palette = palette
        if self.result is None:
            self._paint_empty()
        self.redraw()

    def redraw(self, *_args) -> None:
        if self.result is None or self.palette is None:
            return
        vis = self.visibility()
        for key, cb in self.line_checks.items():
            cb.blockSignals(True)
            cb.setChecked(vis.get(key, False))
            cb.blockSignals(False)
        kw = dict(detail=self.detail.isChecked(), todays_dollars=self.todays.isChecked())
        render_income(self.income_fig, self.result, vis, self.palette, **kw)
        self.income_canvas.draw_idle()
        render_taxes(self.tax_fig, self.result, self.palette, **kw)
        self.tax_canvas.draw_idle()
        render_balances(self.bal_fig, self.result, self.palette, **kw)
        self.bal_canvas.draw_idle()
        data = table_data(self.result, todays_dollars=self.todays.isChecked(), columns=self.table_columns(),
                          detail=self.detail.isChecked())
        self.table.clear()
        self.table.setColumnCount(len(data.header))
        for c, (label, tip) in enumerate(zip(data.header, data.tips)):
            head = QTableWidgetItem(label)
            # Every header gets a tooltip: its explanation, or else its full
            # name (which a narrow column may cut off).
            name = textwrap.fill(label, TIP_WIDTH)
            head.setToolTip(tip or name)
            self.table.setHorizontalHeaderItem(c, head)
        self.table.setRowCount(len(data.rows))
        for r, row in enumerate(data.rows):
            for c, text in enumerate(row):
                item = QTableWidgetItem(text)
                if data.numeric[c]:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if text.startswith("-") and data.header[c] in ("Net cash flow",):
                    item.setForeground(QColor("#ff8a80" if QColor(self.palette.surface).lightness() < 128
                                              else "#b42318"))
                self.table.setItem(r, c, item)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

    def table_export(self):
        """The year table as shown (columns, today's dollars, each source), for export."""
        assert self.result is not None
        return table_data(self.result, todays_dollars=self.todays.isChecked(), columns=self.table_columns(),
                          detail=self.detail.isChecked())

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            header = self.table.horizontalHeader()
            if obj is header.viewport():
                item = self.table.horizontalHeaderItem(header.logicalIndexAt(event.pos()))
            elif obj is self.table.viewport():
                item = self.table.itemAt(event.pos())
            else:
                return super().eventFilter(obj, event)
            if item is None or not item.toolTip():
                QToolTip.hideText()
                return True
        return super().eventFilter(obj, event)

    def current_figure(self) -> Figure:
        page = self.tabs.currentWidget()
        if page is self.bal_canvas:
            return self.bal_fig
        if page is self.tax_canvas:
            return self.tax_fig
        return self.income_fig
