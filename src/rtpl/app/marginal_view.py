"""The marginal graph's pieces, shared by the RTPL and EQTA windows.

``MarginalOptions`` is the Graph options panel's content: the source of
additional income, how far to draw, the IRMAA brackets and the layer check
boxes. ``MarginalChart`` holds a chart and its data table and draws a
``MarginalSeries`` into them. ``compute_series`` builds the series for a
profile with the options' settings.
"""

from __future__ import annotations

import textwrap

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rtpl.app import TIP_WIDTH
from rtpl.app.inputs import ComboBox
from rtpl.app.profile_form import money_box
from rtpl.charts.marginal_chart import render_marginal
from rtpl.engine.marginal import ANNOTATIONS, LAYERS, MarginalSeries, build_marginal_series, default_visibility
from rtpl.engine.tax import IncomeSource
from rtpl.model.profile import Profile
from rtpl.taxlaw.loader import TaxLawLibrary

ANNOTATION_LABELS = {
    "irmaa": "IRMAA brackets (Medicare)",
    "aca": "ACA subsidy cliffs",
    "state_cliff": "State tax cliffs",
}
LAYER_NAMES = {
    "ordinary": "Ordinary income tax",
    "ss": "Social Security phase-in",
    "senior": "Senior deduction phase-out",
    "cg": "Capital gains / QD tax",
    "niit": "NIIT",
    "state": "State tax",
    "state_cg": "State CG & QD tax",
    "edu": "Education credit phase-out",
    "ptc": "ACA premium tax credit phase-out",
    "nonref": "Nonrefundable credit absorbing tax",
}
LAYER_TIPS = {
    "edu": "Education credits lost as MAGI rises through their phase-out\n"
           "(American Opportunity and Lifetime Learning).",
    "ptc": "ACA premium subsidy lost on each extra dollar below the 400%\n"
           "cliff. A premium rather than a tax, but it costs the same.\n"
           "Needs a benchmark premium in the profile. The cliff itself is\n"
           "the ACA line, labelled with the subsidy lost there.",
    "nonref": "Drawn below 0%: tax that an unused nonrefundable credit\n"
              "cancels, so the total stays at 0% until the credit is used up.",
}


class MarginalOptions(QWidget):
    """The marginal graph's options.

    ``changed`` asks for a new series (the source, range or IRMAA basis
    changed, or the layers were reset); ``toggled`` only asks for a redraw.
    """

    changed = Signal()
    toggled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.overrides: dict[str, bool] = {}  # the user's explicit layer choices
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.form = left = QFormLayout()
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
        names.update(LAYER_NAMES)
        names.update(ANNOTATION_LABELS)
        for key in [*LAYERS, *ANNOTATIONS]:
            cb = QCheckBox(names[key])
            if key in LAYER_TIPS:
                cb.setToolTip(LAYER_TIPS[key])
            cb.clicked.connect(lambda checked, k=key: self._user_toggled(k, checked))
            self.checks[key] = cb
            grid.addWidget(cb)
        reset = QPushButton("&Reset to suggested")
        reset.setToolTip("Go back to the layers suggested by the profile")
        reset.clicked.connect(self.reset_visibility)
        grid.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(layers)
        lay.addStretch(1)

        self.source.currentIndexChanged.connect(self.changed)
        self.x_max.valueChanged.connect(self.changed)
        self.irmaa_basis.currentIndexChanged.connect(self.changed)

    def _user_toggled(self, key: str, checked: bool) -> None:
        self.overrides[key] = checked
        self.toggled.emit()

    def reset_visibility(self) -> None:
        self.overrides.clear()
        self.changed.emit()

    def visibility(self, series: MarginalSeries) -> dict[str, bool]:
        vis = default_visibility(series)
        vis.update(self.overrides)
        return vis

    def sync_checks(self, vis: dict[str, bool]) -> None:
        for key, cb in self.checks.items():
            cb.blockSignals(True)
            cb.setChecked(vis.get(key, False))
            cb.blockSignals(False)

    def show_state(self, series: MarginalSeries) -> None:
        """Name the state layers after the profile's state, or disable them."""
        st = series.state
        state_cb = self.checks["state"]
        state_cb.setText(st.label if st else "State tax")
        state_cb.setEnabled(st is not None and st.has_state_layer)
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


def compute_series(profile: Profile, library: TaxLawLibrary, options: MarginalOptions) -> MarginalSeries:
    """The marginal series for ``profile`` with the options' settings."""
    return build_marginal_series(
        profile,
        library,
        source=IncomeSource(options.source.currentData()),
        x_max=options.x_max.value(),
        step=max(100.0, options.x_max.value() / 2500),
        irmaa_basis=options.irmaa_basis.currentData(),
    )


class MarginalChart:
    """A marginal chart and its data table (two widgets, placed by the owner)."""

    def __init__(self, name: str = "Marginal tax rate chart") -> None:
        self.figure = Figure(figsize=(9, 6), dpi=100)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumSize(320, 240)
        self.canvas.setAccessibleName(name)
        self.canvas.setAccessibleDescription("The data table lists the same information as text.")
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Additional income", "Marginal rate", "What changes"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAccessibleName("Marginal rate data table")

    def show(self, series: MarginalSeries, vis: dict[str, bool], palette) -> None:
        render_marginal(self.figure, series, vis, palette)
        self.canvas.draw_idle()
        self.fill_table(series, vis)

    def clear(self, message: str = "") -> None:
        self.figure.clear()
        if message:
            self.figure.text(0.5, 0.5, message, ha="center", va="center", wrap=True)
        self.canvas.draw_idle()
        self.table.setRowCount(0)

    def fill_table(self, s: MarginalSeries, vis: dict[str, bool]) -> None:
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
