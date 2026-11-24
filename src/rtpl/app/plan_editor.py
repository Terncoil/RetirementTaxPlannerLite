"""Plan items: the forward plan's accounts, income, Social Security,
strategies (rules) and assumptions, as a list with a bar for the years each
item covers, and a form for the selected item.

Every change is written straight into the plan and announced with
``changed``, so the graph can recalculate while the user types. A tick box
on accounts, income streams and strategies switches the item off without
deleting it, for quick what-ifs.
"""

from __future__ import annotations

import copy
import textwrap
from typing import Callable

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygon
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from rtpl.app import TIP_WIDTH
from rtpl.app.inputs import ComboBox, DoubleSpinBox, SpinBox
from rtpl.app.profile_form import compact_form, money_box
from rtpl.engine.forward import spousal_topup
from rtpl.engine.rmd import rmd_start_age
from rtpl.engine.social_security import full_retirement_age
from rtpl.taxlaw.loader import TaxLawLibrary
from rtpl.model.plan import (
    ACCOUNT_KINDS,
    BENEFICIARY_KINDS,
    COLA_INCOME_KINDS,
    COLA_KINDS,
    CONTRIBUTION_KINDS,
    HSA_CATCH_UP_AGE,
    HSA_COVERAGE,
    INCOME_KINDS,
    QCD_KINDS,
    QCD_MIN_AGE,
    RULE_GROWTH_KINDS,
    RULE_MODES,
    SPENDING_SOURCES,
    RULE_KINDS,
    ROTH_KINDS,
    TRADITIONAL_KINDS,
    Account,
    IncomeStream,
    PersonPlan,
    Plan,
    Rule,
    When,
    event_label,
    events_for,
)

ROLE = Qt.ItemDataRole.UserRole  # (kind, object) of a row
BAR_ROLE = Qt.ItemDataRole.UserRole + 1  # (first, last, color, milestone 0 or 1) for the bar column

GROUPS = (
    ("assumptions", "Assumptions"),
    ("accounts", "Accounts"),
    ("incomes", "Income"),
    ("people", "Social Security"),
    ("rules", "Strategies"),
)

GROUP_COLORS = {
    "accounts": "#1c5cab",
    "incomes": "#eb6834",
    "people": "#2a78d6",
    "rules": "#e34948",
    "assumptions": "#6b6a66",
}
INCOME_COLORS = {
    "wages": "#eb6834",
    "pension": "#1baf7a",
    "foreign_pension": "#1baf7a",
    "annuity": "#1baf7a",
    "ltcg": "#6da7ec",
    "qualified_dividends": "#1c5cab",
    "interest": "#eda100",
    "other_ordinary": "#eda100",
    "tax_exempt": "#aeaca5",
}
JOINT_KINDS = ("taxable", "cash")
SURVIVOR_KINDS = ("pension", "foreign_pension", "annuity")

# Fill targets offered for "fill" rules, as (label, target).
FILL_TARGETS = [
    ("Top of the 10% bracket", "bracket:0.10"),
    ("Top of the 12% bracket", "bracket:0.12"),
    ("Top of the 22% bracket", "bracket:0.22"),
    ("Top of the 24% bracket", "bracket:0.24"),
    ("Top of the 32% bracket", "bracket:0.32"),
    ("Top of the 35% bracket", "bracket:0.35"),
    ("Top of the 0% capital gains band", "cg0"),
    ("Just under IRMAA tier 1", "irmaa:1"),
    ("Just under IRMAA tier 2", "irmaa:2"),
    ("Just under IRMAA tier 3", "irmaa:3"),
    ("Just under IRMAA tier 4", "irmaa:4"),
    ("Just under IRMAA tier 5", "irmaa:5"),
    ("ACA: 400% of the poverty line", "aca:4.0"),
    ("A MAGI amount", "magi"),
]
MODE_LABELS = {
    "fixed": "A fixed amount each year",
    "fill": "Fill up to a target",
    "spread": "Spread the balance evenly (1/N)",
    "all": "The whole balance",
    "rmd": "The year's RMD from this account",
    "max": "The most allowed each year",
}

HSA_TIP = (
    "HSA contributions need high-deductible health plan coverage and no\n"
    "Medicare, but no earned income. The plan takes the coverage as given\n"
    "for the years of the rule. The money comes from the year's cash flow\n"
    "and is deducted from income (California and New Jersey add it back).\n"
    "Limits: self-only or family coverage (spouses with family coverage\n"
    "share one family limit), plus $1,000 from 55, which only goes into\n"
    "the HSA the person owns. Employer contributions count toward the\n"
    "limit, so lower a fixed amount by them.\n"
    "Medicare stops contributions. With only birth years the plan stops in\n"
    "the year a person turns 65. If the baseline says they are not on\n"
    "Medicare at 65, they can go on until Social Security starts, but\n"
    "Part A is then backdated up to 6 months: stop contributing 6 months\n"
    "before claiming Social Security or applying for Medicare."
)

QCD_TIP = (
    "A qualified charitable distribution goes straight from an IRA to a\n"
    "charity. It is left out of income (so it can't also be deducted) and\n"
    "counts toward the account's RMD; only the rest of the RMD is taxed.\n"
    "Traditional and inherited IRAs only (SEP and SIMPLE IRAs once the\n"
    "employer has stopped contributing), not 401(k)s or 403(b)s.\n"
    "The owner must be 70\u00bd on the day of the gift. With only birth\n"
    "years, the plan starts QCDs in the year they turn 71; someone born\n"
    "January to June can start late in the year they turn 70."
)


def short_money(v: float) -> str:
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.2f}M"
    if abs(v) >= 1e4:
        return f"${v / 1e3:,.0f}K"
    return f"${v:,.0f}"


def target_label(target: str) -> str:
    if target.startswith("magi:"):
        try:
            return f"MAGI {short_money(float(target.split(':', 1)[1]))}"
        except ValueError:
            return target
    label = FILL_TARGETS[target_index(target)][0]
    if FILL_TARGETS[target_index(target)][1] != target and not target.startswith("bracket:"):
        return target
    return label[0].lower() + label[1:]


def target_index(target: str) -> int:
    """Position of a fill target in FILL_TARGETS (bracket:0.1 matches bracket:0.10)."""
    for i, (_, t) in enumerate(FILL_TARGETS):
        if t == target:
            return i
        if t.startswith("bracket:") and target.startswith("bracket:") and abs(float(t[8:]) - _float(target[8:])) < 1e-9:
            return i
    if target.startswith("magi:"):
        return len(FILL_TARGETS) - 1
    return 2


def _float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return -1.0


class TrimmedSpinBox(DoubleSpinBox):
    """Shows only the decimals a value needs (at least two): 0.0175%, 1.50%."""

    def textFromValue(self, value: float) -> str:  # noqa: N802 (Qt name)
        text = super().textFromValue(value)
        point = self.locale().decimalPoint()
        if point in text:
            head, tail = text.split(point, 1)
            tail = tail.rstrip("0").ljust(2, "0")
            text = f"{head}{point}{tail}"
        return text


def percent_box(lo: float = 0.0, hi: float = 100.0, step: float = 0.1, decimals: int = 2) -> DoubleSpinBox:
    box = DoubleSpinBox() if decimals <= 2 else TrimmedSpinBox()
    box.setSuffix("%")
    box.setDecimals(decimals)
    box.setRange(lo, hi)
    box.setSingleStep(step)
    box.setMaximumWidth(110 if decimals <= 2 else 130)
    return box


NOMINAL_TIP = ("Nominal, before inflation: for example about 10% a year for US\n"
               "stocks over the long run, not the 7% left after inflation")
YIELD_TIP = ("A percentage of the whole account balance, paid each year and taxed\n"
             "then. Not the sweep or money market rate: $350 of interest on a\n"
             "$2,000,000 account is 0.0175%. Up to four decimals.")


def age_text(age: float) -> str:
    """An age in years and months: "67", "67 and 5 months"."""
    years, months = split_age(age)
    if not months:
        return str(years)
    return f"{years} and {months} month{'s' if months != 1 else ''}"


def split_age(age: float) -> tuple[int, int]:
    total = round(age * 12)
    return total // 12, total % 12


class AgeEdit(QWidget):
    """An age as whole years plus months, for Social Security ages."""

    changed = Signal()

    def __init__(self, lo: int, hi: int) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.years = SpinBox()
        self.years.setRange(lo, hi)
        self.years.setSuffix(" y")
        self.years.setMaximumWidth(80)
        self.months = SpinBox()
        self.months.setRange(0, 11)
        self.months.setSuffix(" m")
        self.months.setMaximumWidth(70)
        lay.addWidget(self.years)
        lay.addWidget(self.months)
        lay.addStretch(1)
        self._loading = False
        self.years.valueChanged.connect(self._edited)
        self.months.valueChanged.connect(self._edited)
        self._limit_months()

    def setAccessibleName(self, name: str) -> None:  # noqa: N802 (Qt name)
        super().setAccessibleName(name)
        self.years.setAccessibleName(f"{name}: years")
        self.months.setAccessibleName(f"{name}: months")

    def setToolTip(self, tip: str) -> None:  # noqa: N802 (Qt name)
        super().setToolTip(tip)
        self.years.setToolTip(tip)
        self.months.setToolTip(tip)

    def _limit_months(self) -> None:
        # The top age takes no extra months (70 and 0 months at most).
        self.months.setMaximum(0 if self.years.value() >= self.years.maximum() else 11)

    def value(self) -> float:
        return self.years.value() + self.months.value() / 12

    def setValue(self, age: float) -> None:  # noqa: N802 (matches the spin boxes)
        years, months = split_age(age)
        self._loading = True
        self.years.setValue(years)
        self._limit_months()
        self.months.setValue(months)
        self._loading = False
        self.changed.emit()

    def _edited(self, *_args) -> None:
        if self._loading:
            return
        self._loading = True
        self._limit_months()
        self._loading = False
        self.changed.emit()


class OptionalField(QWidget):
    """A value with a "use the default" check box in front of it.

    Ticked, the field is greyed out and shows the default it stands for;
    ``value()`` is then None.
    """

    changed = Signal()

    def __init__(self, text: str, field: QWidget) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.check = QCheckBox(text)
        self.field = field
        lay.addWidget(self.check)
        lay.addWidget(field)
        lay.addStretch(1)
        self.check.toggled.connect(self._toggled)
        signal = getattr(field, "valueChanged", None) or field.changed
        signal.connect(self._field_changed)

    def setAccessibleName(self, name: str) -> None:  # noqa: N802 (Qt name)
        super().setAccessibleName(name)
        self.check.setAccessibleName(f"{name}: {self.check.text()}")
        self.field.setAccessibleName(name)

    def setToolTip(self, tip: str) -> None:  # noqa: N802 (Qt name)
        super().setToolTip(tip)
        self.check.setToolTip(tip)
        self.field.setToolTip(tip)

    def is_default(self) -> bool:
        return self.check.isChecked()

    def value(self):
        return None if self.check.isChecked() else self.field.value()

    def set_value(self, value, default) -> None:
        """Show ``value``, or tick the box and show ``default`` when it is None."""
        self.blockSignals(True)
        self.check.setChecked(value is None)
        self.field.setValue(default if value is None else value)
        self.field.setEnabled(value is not None)
        self.blockSignals(False)

    def show_default(self, default) -> None:
        """While ticked, keep the greyed-out field showing the current default."""
        if self.check.isChecked():
            self.blockSignals(True)
            self.field.setValue(default)
            self.blockSignals(False)

    def _toggled(self, checked: bool) -> None:
        self.field.setEnabled(not checked)
        self.changed.emit()

    def _field_changed(self, *_args) -> None:
        if not self.check.isChecked():
            self.changed.emit()


class WhenEdit(QWidget):
    """A timing: one person's age, a calendar year, a death or the end of
    the plan, or (if allowed) none. As a start, a death means the year after
    it (the first year without that person)."""

    changed = Signal()

    def __init__(self, none_label: str | None = None, *, start: bool = False) -> None:
        super().__init__()
        self.none_label = none_label
        self.is_start = start
        self.plan: Plan | None = None
        self.max_year: int | None = None
        self.min_year: int | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.mode = ComboBox()
        self.spin = SpinBox()
        self.spin.setMaximumWidth(80)
        self.result = QLabel()
        self.result.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(self.mode)
        lay.addWidget(self.spin)
        lay.addWidget(self.result, 1)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.spin.valueChanged.connect(self._value_changed)
        self._loading = False

    def set_plan(self, plan: Plan) -> None:
        self.plan = plan
        self.min_year = plan.start_year  # nothing in a plan starts before its first year
        self._loading = True
        self.mode.clear()
        if self.none_label:
            self.mode.addItem(self.none_label, "none")
        for i, p in enumerate(plan.profile.people):
            self.mode.addItem(f"{p.name}'s age", i)
        self.mode.addItem("Year", "year")
        for event in events_for(plan.profile, start=self.is_start):
            self.mode.addItem(event_label(event, plan.profile, start=self.is_start), f"event:{event}")
        self._loading = False

    def setAccessibleName(self, name: str) -> None:  # noqa: N802 (Qt name)
        super().setAccessibleName(name)
        self.mode.setAccessibleName(f"{name}: measured by")
        self.spin.setAccessibleName(f"{name}: age or year")

    def set_value(self, when: When | None) -> None:
        self._loading = True
        if when is None:
            self.mode.setCurrentIndex(max(0, self.mode.findData("none")))
        elif when.event is not None:
            idx = self.mode.findData(f"event:{when.event}")
            if idx < 0 and when.event.startswith("death:"):  # a person the baseline no longer has
                idx = self.mode.findData(f"event:death:{len(self.plan.profile.people) - 1}")
            self.mode.setCurrentIndex(max(0, idx))
        elif when.year is not None:
            self.mode.setCurrentIndex(self.mode.findData("year"))
        else:
            idx = self.mode.findData(min(when.person, len(self.plan.profile.people) - 1))
            self.mode.setCurrentIndex(max(0, idx))
        self._apply_range()
        if when is not None and when.event is None:
            self.spin.setValue(int(when.year if when.year is not None else when.age))
        self._loading = False
        self._show_result()

    def value(self) -> When | None:
        data = self.mode.currentData()
        if data == "none":
            return None
        if data == "year":
            return When(year=self.spin.value())
        if isinstance(data, str) and data.startswith("event:"):
            return When(event=data[6:])
        return When(age=self.spin.value(), person=int(data))

    def set_max_year(self, year: int | None) -> None:
        """Limit the timing to ``year`` at the latest (None: no limit)."""
        if year == self.max_year:
            return
        self.max_year = year
        self._apply_range()

    def set_min_year(self, year: int | None) -> None:
        """Limit the timing to ``year`` at the earliest (None: the plan's first year)."""
        if year is None and self.plan is not None:
            year = self.plan.start_year
        if year == self.min_year:
            return
        self.min_year = year
        self._apply_range()

    def _apply_range(self) -> None:
        data = self.mode.currentData()
        event = isinstance(data, str) and data.startswith("event:")
        self.spin.setVisible(data != "none" and not event)
        if event:
            return
        lo, hi = self.min_year, self.max_year
        if lo is not None and hi is not None:
            hi = max(hi, lo)
        if data == "year":
            self.spin.setRange(1900 if lo is None else lo, 2200 if hi is None else hi)
        else:
            if self.plan is not None and data != "none":
                born = self.plan.profile.people[int(data)].birth_year
                lo = None if lo is None else lo - born
                hi = None if hi is None else hi - born
            self.spin.setRange(0 if lo is None else max(0, lo), 120 if hi is None else max(0, hi))

    def _mode_changed(self, *_args) -> None:
        if self._loading or self.plan is None:
            return
        # Keep the same calendar year when switching between age and year.
        old_year = self._year_of(self._previous) if getattr(self, "_previous", None) else self.plan.start_year
        self._loading = True
        self._apply_range()
        data = self.mode.currentData()
        if data == "year":
            self.spin.setValue(old_year)
        elif isinstance(data, int):
            self.spin.setValue(old_year - self.plan.profile.people[data].birth_year)
        self._loading = False
        self._show_result()
        self.changed.emit()

    def _value_changed(self, *_args) -> None:
        if self._loading:
            return
        self._show_result()
        self.changed.emit()

    def _year_of(self, when: When | None) -> int:
        assert self.plan is not None
        return self.plan.start_year if when is None else when.resolve(self.plan.profile, start=self.is_start)

    def _show_result(self) -> None:
        when = self.value()
        self._previous = when
        if self.plan is None or when is None:
            self.result.setText("")
            return
        year = when.resolve(self.plan.profile, start=self.is_start)
        if when.event is not None:
            self.result.setText(f"= {year}")
        elif when.year is not None:
            ages = [f"{p.name} {year - p.birth_year}" for p in self.plan.profile.people]
            self.result.setText(", ".join(ages))
        else:
            self.result.setText(f"= {year}")


class SpendingOrderEdit(QWidget):
    """Which account types pay for spending when income falls short, and in
    what order: a list of tick boxes with Up and Down buttons."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setMinimumWidth(200)
        buttons = QVBoxLayout()
        self.up = QPushButton("Up")
        self.down = QPushButton("Down")
        self.up.setAccessibleName("Move up in the spending order")
        self.down.setAccessibleName("Move down in the spending order")
        buttons.addWidget(self.up)
        buttons.addWidget(self.down)
        buttons.addStretch(1)
        lay.addWidget(self.list, 1)
        lay.addLayout(buttons)
        self.up.clicked.connect(lambda: self.move(-1))
        self.down.clicked.connect(lambda: self.move(1))
        self.list.itemChanged.connect(lambda _item: self.changed.emit())
        self.list.currentRowChanged.connect(self._update_buttons)

    def setAccessibleName(self, name: str) -> None:  # noqa: N802 (Qt name)
        super().setAccessibleName(name)
        self.list.setAccessibleName(name)

    def setToolTip(self, tip: str) -> None:  # noqa: N802 (Qt name)
        super().setToolTip(tip)
        self.list.setToolTip(tip)

    def set_value(self, order: list[str]) -> None:
        """Ticked types first, in ``order``; the others after, unticked."""
        self.list.blockSignals(True)
        self.list.clear()
        keys = [k for k in order if k in SPENDING_SOURCES] + [k for k in SPENDING_SOURCES if k not in order]
        for k in keys:
            item = QListWidgetItem(SPENDING_SOURCES[k])
            item.setData(ROLE, k)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if k in order else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        rows = self.list.count()
        self.list.setFixedHeight(rows * max(1, self.list.sizeHintForRow(0)) + 2 * self.list.frameWidth() + 2)
        self._update_buttons()

    def value(self) -> list[str]:
        return [self.list.item(i).data(ROLE) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def move(self, step: int) -> None:
        row = self.list.currentRow()
        new = row + step
        if row < 0 or not 0 <= new < self.list.count():
            return
        self.list.blockSignals(True)
        item = self.list.takeItem(row)
        self.list.insertItem(new, item)
        self.list.setCurrentRow(new)
        self.list.blockSignals(False)
        self._update_buttons()
        self.changed.emit()

    def _update_buttons(self, *_args) -> None:
        row = self.list.currentRow()
        self.up.setEnabled(row > 0)
        self.down.setEnabled(0 <= row < self.list.count() - 1)


# -- forms -------------------------------------------------------------------------


class ItemForm(QWidget):
    """Base for the per-item forms: load an object, write edits back into it."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.form = compact_form(QFormLayout(self))
        self.form.setContentsMargins(0, 0, 0, 0)
        self.plan: Plan | None = None
        self.obj = None
        self._loading = False

    def row(self, text: str, widget: QWidget, tip: str | None = None) -> QWidget:
        label = QLabel(text)
        label.setBuddy(widget.check if isinstance(widget, OptionalField) else widget)
        widget.setAccessibleName(text.replace("&", ""))
        if tip:
            widget.setToolTip(tip)
        self.form.addRow(label, widget)
        return widget

    def note_row(self, text: str) -> QLabel:
        """A read-only line of information in the form."""
        label = QLabel()
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.row(text, label)
        return label

    def hint_row(self, text: str = "") -> QLabel:
        """A visible, greyed line of explanation under the field above it."""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setMinimumWidth(220)
        label.setStyleSheet("color: palette(placeholder-text);")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.form.addRow("", label)
        return label

    def info_row(self, text: str) -> QLabel:
        """A greyed, read-only value set somewhere else (the label's text says where)."""
        label = self.note_row(text)
        label.setStyleSheet("color: palette(placeholder-text);")
        return label

    def show_row(self, widget: QWidget, visible: bool) -> None:
        self.form.setRowVisible(widget, visible)

    def watch(self, *widgets: QWidget) -> None:
        for w in widgets:
            if isinstance(w, (DoubleSpinBox, SpinBox)):
                w.valueChanged.connect(self._edited)
            elif isinstance(w, ComboBox):
                w.currentIndexChanged.connect(self._edited)
            elif isinstance(w, QLineEdit):
                w.textEdited.connect(self._edited)
            elif isinstance(w, QCheckBox):
                w.toggled.connect(self._edited)
            elif isinstance(w, (WhenEdit, AgeEdit, OptionalField, SpendingOrderEdit)):
                w.changed.connect(self._edited)

    def load(self, plan: Plan, obj) -> None:
        self.plan, self.obj = plan, obj
        self._loading = True
        try:
            self.fill()
        finally:
            self._loading = False
        self.update_rows()

    def _edited(self, *_args) -> None:
        if self._loading or self.obj is None:
            return
        self.store()
        self.update_rows()
        self.changed.emit()

    # Subclasses fill these in.
    def fill(self) -> None: ...
    def store(self) -> None: ...
    def update_rows(self) -> None: ...


def owner_combo(plan: Plan, joint: bool) -> list[tuple[str, int | None]]:
    out: list[tuple[str, int | None]] = [(p.name, i) for i, p in enumerate(plan.profile.people)]
    if joint:
        out.append(("Joint", None))
    return out


def set_items(combo: ComboBox, items: list[tuple[str, object]], current) -> None:
    combo.blockSignals(True)
    combo.clear()
    for text, data in items:
        combo.addItem(text, data)
    idx = combo.findData(current)
    combo.setCurrentIndex(idx if idx >= 0 else 0)
    combo.blockSignals(False)


class AccountForm(ItemForm):
    def __init__(self) -> None:
        super().__init__()
        self.name = self.row("Name", QLineEdit())
        self.kind = self.row("Type", ComboBox())
        for k, label in ACCOUNT_KINDS.items():
            self.kind.addItem(label, k)
        self.owner = self.row("Owner", ComboBox())
        self.balance = self.row("Balance on Jan 1", money_box(),
                                "Enter the balance at the start of the plan's first year\n"
                                "(last year's Dec 31 statement). The plan applies this year's\n"
                                "withdrawals and a full year's return to it.")
        self.own_return = self.row("Return", QCheckBox("Own return (otherwise the plan's default)"))
        self.return_rate = self.row("Yearly return (nominal)", percent_box(-20, 30), NOMINAL_TIP)
        self.cash_note = self.note_row("Return")
        self.basis = self.row("Cost basis", OptionalField("Same as balance", money_box()),
                              "What you paid; gains above it are taxed when sold")
        self.interest = self.row("Interest yield", percent_box(0, 20, 0.01, 4), YIELD_TIP)
        self.qdiv = self.row("Qualified dividend yield", percent_box(0, 20, 0.01, 4), YIELD_TIP)
        self.nqdiv = self.row("Non-qualified dividend yield", percent_box(0, 20, 0.01, 4), YIELD_TIP)
        self.capdist = self.row("Capital gain distributions", percent_box(0, 20, 0.01, 4), YIELD_TIP)
        self.spouse_bene = self.row("Beneficiary", QCheckBox("Spouse is the sole beneficiary"))
        self.inherited_year = self.row("Year inherited", SpinBox())
        self.inherited_year.setRange(1950, 2200)
        self.orig_birth = self.row("Original owner born", SpinBox())
        self.orig_birth.setRange(1880, 2100)
        self.started = self.row("RMDs", QCheckBox("The original owner had started RMDs"))
        self.beneficiary = self.row("Who inherited it", ComboBox(),
                                    "The owner of this account in the plan,\nas the original owner's beneficiary")
        for k, label in BENEFICIARY_KINDS.items():
            self.beneficiary.addItem(label, k)
        self.rmd_taken = self.row("This year's RMD", QCheckBox("Already taken (not in the balance above)"),
                                  "Check this if the balance above is after this year's RMD came\n"
                                  "out of it, rather than the Jan 1 balance. The plan still counts\n"
                                  "the RMD as this year's income, but doesn't take it from the\n"
                                  "balance again. Applies only while this year is the plan's\n"
                                  "first year.")
        self.deadline = self.note_row("Withdrawals")
        self.deadline.setWordWrap(True)
        self.deadline.setMinimumWidth(220)
        self.watch(self.name, self.kind, self.owner, self.balance, self.own_return, self.return_rate, self.basis,
                   self.interest, self.qdiv, self.nqdiv, self.capdist, self.spouse_bene, self.inherited_year,
                   self.orig_birth, self.started, self.beneficiary, self.rmd_taken)

    def fill(self) -> None:
        a: Account = self.obj
        self.name.setText(a.name)
        self.name.setPlaceholderText(ACCOUNT_KINDS.get(a.kind, a.kind))
        self.kind.setCurrentIndex(max(0, self.kind.findData(a.kind)))
        set_items(self.owner, owner_combo(self.plan, a.kind in JOINT_KINDS), a.owner)
        self.balance.setValue(a.balance)
        balance_label = f"Balance on Jan 1, {self.plan.start_year}"
        self.form.labelForField(self.balance).setText(balance_label)
        self.balance.setAccessibleName(balance_label)
        self.own_return.setChecked(a.return_rate is not None)
        self.return_rate.setValue(100 * (a.return_rate if a.return_rate is not None else self.plan.assumptions.return_rate))
        self.basis.set_value(a.cost_basis, a.balance)
        self.interest.setValue(100 * a.interest_yield)
        self.qdiv.setValue(100 * a.qualified_dividend_yield)
        self.nqdiv.setValue(100 * a.nonqualified_dividend_yield)
        self.capdist.setValue(100 * a.cap_gain_distribution_yield)
        self.spouse_bene.setChecked(a.spouse_sole_beneficiary)
        self.inherited_year.setValue(a.inherited_year or self.plan.start_year - 1)
        self.orig_birth.setValue(a.original_owner_birth_year or 1945)
        self.started.setChecked(a.owner_started_rmds)
        self.beneficiary.setCurrentIndex(max(0, self.beneficiary.findData(a.beneficiary)))
        self.rmd_taken.setChecked(self.plan.rmd_taken_now(a))
        self.rmd_taken.setText(f"Already taken for {self.plan.start_year} (not in the balance above)")

    def store(self) -> None:
        a: Account = self.obj
        a.name = self.name.text().strip()
        kind = self.kind.currentData()
        if kind != a.kind:
            a.kind = kind
            # Joint is only offered for brokerage and cash accounts.
            self._loading = True
            set_items(self.owner, owner_combo(self.plan, kind in JOINT_KINDS), a.owner)
            self.name.setPlaceholderText(ACCOUNT_KINDS.get(kind, kind))
            self._loading = False
        a.owner = self.owner.currentData()
        a.balance = self.balance.value()
        a.return_rate = self.return_rate.value() / 100 if self.own_return.isChecked() else None
        a.cost_basis = self.basis.value()
        a.interest_yield = self.interest.value() / 100
        a.qualified_dividend_yield = self.qdiv.value() / 100
        a.nonqualified_dividend_yield = self.nqdiv.value() / 100
        a.cap_gain_distribution_yield = self.capdist.value() / 100
        a.spouse_sole_beneficiary = self.spouse_bene.isChecked()
        if a.kind == "inherited_ira":
            a.inherited_year = self.inherited_year.value()
            a.original_owner_birth_year = self.orig_birth.value()
        a.owner_started_rmds = self.started.isChecked()
        a.beneficiary = self.beneficiary.currentData()
        a.rmd_taken_year = self.plan.start_year if self.rmd_taken.isChecked() else None

    def update_rows(self) -> None:
        kind = self.kind.currentData()
        self.return_rate.setEnabled(self.own_return.isChecked())
        taxable = kind == "taxable"
        for w in (self.basis, self.interest, self.qdiv, self.nqdiv, self.capdist):
            self.show_row(w, taxable)
        self.basis.show_default(self.balance.value())
        # A cash account always earns the plan's Cash return, taxed as interest.
        cash = kind == "cash"
        self.show_row(self.own_return, not cash)
        self.show_row(self.return_rate, not cash)
        self.show_row(self.cash_note, cash)
        if cash and self.plan is not None:
            self.cash_note.setText(f"The plan's Cash return: {100 * self.plan.assumptions.cash_return:.2f}%")
            self.cash_note.setToolTip("Every Cash / savings account earns the plan's Cash return,\n"
                                      "taxed as interest. Change it in Assumptions.")
        self.show_row(self.spouse_bene, kind in TRADITIONAL_KINDS)
        self.show_row(self.rmd_taken, self._rmd_due())
        inherited = kind == "inherited_ira"
        for w in (self.inherited_year, self.orig_birth, self.started, self.beneficiary, self.deadline):
            self.show_row(w, inherited)
        if inherited and self.plan is not None and self.obj is not None:
            rule = self.obj.inherited_rule(self.plan.profile)
            self.deadline.setText(rule.text if rule else "")


    def _rmd_due(self) -> bool:
        """Whether this account has an RMD in the plan's first year (so it may already be taken)."""
        a = self.obj
        if self.plan is None or a is None:
            return False
        if a.rmd_taken_year == self.plan.start_year:
            return True  # keep the box visible while it is ticked
        y0 = self.plan.start_year
        if a.kind in TRADITIONAL_KINDS:
            if a.owner is None or a.owner >= len(self.plan.profile.people):
                return False
            owner = self.plan.profile.people[a.owner]
            return owner.age_in(y0) >= rmd_start_age(owner.birth_year)
        if a.kind == "inherited_ira" and a.inherited_year is not None:
            rule = a.inherited_rule(self.plan.profile)
            return bool(rule and rule.annual_rmds and a.inherited_year < y0)
        return False


class IncomeForm(ItemForm):
    def __init__(self) -> None:
        super().__init__()
        self.name = self.row("Name", QLineEdit())
        self.kind = self.row("Type", ComboBox(),
                             "Wages (gross pay, before 401(k) deferrals) count toward\n"
                             "the Social Security earnings test before full retirement age")
        for k, label in INCOME_KINDS.items():
            self.kind.addItem(label, k)
        self.owner = self.row("Owner", ComboBox())
        self.amount = self.row("Yearly amount", money_box(), "In the first year it is paid")
        self.start = self.row("Starts", WhenEdit("Plan start", start=True))
        self.end = self.row("Ends (last year paid)", WhenEdit("For life"))
        self.cola = self.row("COLA", ComboBox(), "How the amount rises after the first year it is paid")
        for k, label in COLA_KINDS.items():
            self.cola.addItem(label, k)
        self.growth = self.row("COLA rate", percent_box(-10, 20), "The yearly increase after the first year")
        self.cola_note = self.hint_row(
            "For a pension with a floor or cap, enter the rate it works out to at your inflation rate.")
        self.survivor = self.row("To the survivor", percent_box(0, 100, 5), "Share that continues after the owner dies")
        self.taxable = self.row("Federally taxable", percent_box(0, 100, 5))
        self.watch(self.name, self.kind, self.owner, self.amount, self.start, self.end, self.cola, self.growth,
                   self.survivor, self.taxable)

    def fill(self) -> None:
        s: IncomeStream = self.obj
        self.name.setText(s.name)
        self.name.setPlaceholderText(INCOME_KINDS.get(s.kind, s.kind))
        self.kind.setCurrentIndex(max(0, self.kind.findData(s.kind)))
        set_items(self.owner, owner_combo(self.plan, True), s.owner)
        self.amount.setValue(s.amount)
        for w, when in ((self.start, s.start), (self.end, s.end)):
            w.set_plan(self.plan)
            w.set_value(when)
        self.cola.setCurrentIndex(max(0, self.cola.findData(s.cola)))
        self.growth.setValue(100 * s.growth)
        self.survivor.setValue(100 * s.survivor_pct)
        self.taxable.setValue(100 * s.federal_taxable_pct)

    def store(self) -> None:
        s: IncomeStream = self.obj
        s.name = self.name.text().strip()
        s.kind = self.kind.currentData()
        self.name.setPlaceholderText(INCOME_KINDS.get(s.kind, s.kind))
        s.owner = self.owner.currentData()
        s.amount = self.amount.value()
        s.start = self.start.value()
        s.end = self.end.value()
        s.cola = self.cola.currentData()
        s.growth = self.growth.value() / 100
        s.survivor_pct = self.survivor.value() / 100
        s.federal_taxable_pct = self.taxable.value() / 100

    def update_rows(self) -> None:
        kind = self.kind.currentData()
        self.show_row(self.survivor, kind in SURVIVOR_KINDS)
        # Wages, pensions and annuities have a COLA; other income a yearly increase.
        word = "COLA" if kind in COLA_INCOME_KINDS else "Yearly increase"
        self.form.labelForField(self.cola).setText(word)
        self.cola.setAccessibleName(word)
        rate = "COLA rate" if kind in COLA_INCOME_KINDS else "Yearly increase rate"
        self.form.labelForField(self.growth).setText(rate)
        self.growth.setAccessibleName(rate)
        self.show_row(self.growth, self.cola.currentData() == "fixed")
        self.show_row(self.cola_note, kind in SURVIVOR_KINDS)
        if self.plan is not None:
            start = self.start.value()
            self.end.set_min_year(start.resolve(self.plan.profile, start=True) if start else None)


SPOUSAL_TIP = (
    "The plan adds a spousal top-up on its own: half the other person's\n"
    "benefit at full retirement age, less this person's own. It starts\n"
    "once both have filed, is reduced for starting before full retirement\n"
    "age, and gets no delayed credits after it. Either person can get it.")


class PersonForm(ItemForm):
    def __init__(self) -> None:
        super().__init__()
        self.fra = self.row("Benefit at full retirement age", money_box(),
                            "Yearly benefit at full retirement age, in today's dollars\n(from your SSA statement)")
        # Clergy and some government workers have no benefit of their own.
        self.fra.setSpecialValueText("No Social Security")
        self.fra.setMaximumWidth(170)
        self.claim = self.row("Claiming age", AgeEdit(62, 70),
                              "Years and months; the benefit starts that many months into the year")
        self.claimed = self.row("Already claimed", QCheckBox("Benefit is already being paid"))
        self.current = self.row("Yearly benefit now", money_box())
        self.ssdi = self.row("Disability", QCheckBox("Receiving SSDI"))
        self.ssdi_end = self.row("SSDI ends at age", OptionalField("At full retirement age", AgeEdit(17, 67)))
        self.spousal = self.note_row("Spousal top-up")
        self.spousal.setWordWrap(True)
        self.spousal.setMinimumWidth(220)
        self.spousal.setToolTip(SPOUSAL_TIP)
        self.spousal_hint = self.hint_row()
        self.cola_note = self.info_row("Social Security COLA")
        self.life_note = self.info_row("Life expectancy")
        self.watch(self.fra, self.claim, self.claimed, self.current, self.ssdi, self.ssdi_end)

    def fill(self) -> None:
        pp: PersonPlan = self.obj
        ss = pp.social_security
        self.fra.setValue(ss.fra_benefit)
        self.claim.setValue(ss.claim_age)
        self.claimed.setChecked(ss.current_benefit is not None)
        self.current.setValue(ss.current_benefit or 0)
        self.ssdi.setChecked(ss.ssdi)
        self.ssdi_end.set_value(ss.ssdi_end_age, self._fra())

    def store(self) -> None:
        pp: PersonPlan = self.obj
        ss = pp.social_security
        ss.fra_benefit = self.fra.value()
        ss.claim_age = self.claim.value()
        ss.current_benefit = self.current.value() if self.claimed.isChecked() else None
        ss.ssdi = self.ssdi.isChecked()
        ss.ssdi_end_age = self.ssdi_end.value()

    def _fra(self) -> float:
        assert self.plan is not None
        return full_retirement_age(self.plan.profile.people[self.plan.people.index(self.obj)].birth_year)

    def update_rows(self) -> None:
        if self.plan is not None and self.obj is not None:
            self.ssdi_end.show_default(self._fra())
            same = " (same as inflation)" if self.plan.assumptions.ss_cola is None else ""
            self.cola_note.setText(f"{100 * self.plan.ss_cola:.2f}%{same}\n"
                                   "Change it in Assumptions ▸ Social Security COLA")
            i = self.plan.people.index(self.obj)
            p = self.plan.profile.people[i]
            self.life_note.setText(f"{p.life_expectancy} ({self.plan.death_year(i)})\nChange it on the Baseline tab")
        claimed = self.claimed.isChecked()
        self.show_row(self.current, claimed)
        self.show_row(self.ssdi_end, self.ssdi.isChecked())
        self._show_spousal()

    def _show_spousal(self) -> None:
        """The spousal top-up the plan adds, so it is visible, not hidden in the engine."""
        top = None
        if self.plan is not None and self.obj is not None:
            top = spousal_topup(self.plan, self.plan.people.index(self.obj))
        self.show_row(self.spousal, top is not None)
        self.show_row(self.spousal_hint, top is not None)
        if top is None:
            return
        i = self.plan.people.index(self.obj)
        name = self.plan.profile.people[i].name
        own = self.obj.social_security.fra_benefit
        if top.amount <= 0:
            self.spousal.setText(f"None ({top.reason})")
        else:
            self.spousal.setText(f"${top.amount:,.0f} a year from {top.start_year} (age {top.age}), "
                                 "in today's dollars")
        hints = []
        if top.amount > 0 and own <= 0:
            hints.append(f"With no benefit of their own, {name} gets the spousal benefit only.")
        if top.amount > 0 and top.waits_for_spouse:
            hints.append(f"{name} files before {top.spouse}, so the top-up waits until {top.spouse} files "
                         f"in {top.spouse_claim_year}.")
        hints.append("Either of you can get a top-up: half the other's full benefit, less your own.")
        self.spousal_hint.setText(" ".join(hints))


_LIBRARY: list[TaxLawLibrary] = []


def _shipped_law(year: int, inflation: float):
    """The shipped federal law for ``year`` (for hints in the forms), or None."""
    try:
        if not _LIBRARY:
            _LIBRARY.append(TaxLawLibrary())
        return _LIBRARY[0].federal(year, inflation)
    except (KeyError, OSError, ValueError):
        return None


class RuleForm(ItemForm):
    def __init__(self) -> None:
        super().__init__()
        self.kind = self.row("Strategy", ComboBox())
        for k, label in RULE_KINDS.items():
            self.kind.addItem(label, k)
        self.account = self.row("From account", ComboBox())
        self.coverage = self.row("Health plan coverage", ComboBox(),
                                 "Self-only or family high-deductible health plan coverage;\n"
                                 "it sets the yearly limit")
        for k, label in HSA_COVERAGE.items():
            self.coverage.addItem(label, k)
        self.to_account = self.row("Into", ComboBox())
        self.start = self.row("Starts", WhenEdit(start=True))
        self.end = self.row("Ends (last year)", WhenEdit("Same year (one year only)"))
        self.mode = self.row("Amount", ComboBox())
        for k, label in MODE_LABELS.items():
            self.mode.addItem(label, k)
        self.amount = self.row("Each year", money_box())
        self.growth = self.row("Yearly increase", ComboBox(),
                               "Inflation: the amount is in today's dollars and grows with inflation.\n"
                               "Fixed %: the amount is the first year's and grows by this rate\n"
                               "each year after.")
        for k, label in RULE_GROWTH_KINDS.items():
            self.growth.addItem(label, k)
        self.growth_rate = self.row("Increase rate", percent_box(-20, 20), "Negative for a falling amount")
        self.target = self.row("Fill up to", ComboBox())
        for label, t in FILL_TARGETS:
            self.target.addItem(label, t)
        self.magi = self.row("MAGI amount", money_box(), "In that year's dollars")
        self.deadline = self.note_row("Deadline")
        self.qcd_hint = self.hint_row()
        self.qcd_hint.setToolTip(QCD_TIP)
        self.hsa_hint = self.hint_row()
        self.hsa_hint.setToolTip(HSA_TIP)
        self.note = self.row("Note", QLineEdit())
        self.watch(self.kind, self.account, self.coverage, self.to_account, self.start, self.end, self.mode, self.amount,
                   self.growth, self.growth_rate, self.target, self.magi, self.note)

    def _accounts(self, kind: str) -> list[tuple[str, str]]:
        assert self.plan is not None
        if kind == "roth_conversion":
            ok = lambda a: a.kind in TRADITIONAL_KINDS or a.kind == "inherited_ira"  # noqa: E731
        elif kind == "qcd":
            ok = lambda a: a.kind in QCD_KINDS  # noqa: E731
        elif kind == "hsa_contribution":
            ok = lambda a: a.kind == "hsa" and a.owner is not None  # noqa: E731
        else:
            ok = lambda a: True  # noqa: E731
        return [(a.label(self.plan.profile), a.id) for a in self.plan.accounts if ok(a)]

    def _fill_modes(self, kind: str, current: str) -> None:
        modes = RULE_MODES.get(kind, tuple(MODE_LABELS))
        set_items(self.mode, [(MODE_LABELS[m], m) for m in modes], current)

    def _fill_accounts(self) -> None:
        r: Rule = self.obj
        set_items(self.account, self._accounts(r.kind), r.account)
        self._fill_modes(r.kind, r.mode)
        roths = [("Owner's Roth IRA", None)] + [
            (a.label(self.plan.profile), a.id) for a in self.plan.accounts if a.kind in ROTH_KINDS]
        set_items(self.to_account, roths, r.to_account)

    def fill(self) -> None:
        r: Rule = self.obj
        self.kind.setCurrentIndex(max(0, self.kind.findData(r.kind)))
        self._fill_accounts()
        for w, when in ((self.start, r.start), (self.end, r.end)):
            w.set_plan(self.plan)
            w.set_value(when)
        self.amount.setValue(r.amount)
        self.coverage.setCurrentIndex(max(0, self.coverage.findData(r.coverage)))
        self.growth.setCurrentIndex(max(0, self.growth.findData(r.growth)))
        self.growth_rate.setValue(100 * r.growth_rate)
        if r.target.startswith("magi:"):
            self.target.setCurrentIndex(self.target.findData("magi"))
            self.magi.setValue(_float(r.target[5:]))
        else:
            self.target.setCurrentIndex(target_index(r.target))
        self.note.setText(r.note)

    def store(self) -> None:
        r: Rule = self.obj
        kind = self.kind.currentData()
        if kind != r.kind:
            r.kind = kind
            self._loading = True
            self._fill_accounts()
            self._loading = False
        if self.account.currentData() is not None:
            r.account = self.account.currentData()
        r.to_account = self.to_account.currentData() if r.kind == "roth_conversion" else None
        start = self.start.value()
        if start is not None:
            r.start = start
        r.end = self.end.value()
        r.mode = self.mode.currentData() or r.mode
        r.amount = self.amount.value()
        r.coverage = self.coverage.currentData() or r.coverage
        r.growth = self.growth.currentData()
        r.growth_rate = self.growth_rate.value() / 100
        t = self.target.currentData()
        r.target = f"magi:{self.magi.value():.0f}" if t == "magi" else t
        r.note = self.note.text()

    def update_rows(self) -> None:
        mode = self.mode.currentData()
        acct = self.plan.account(self.account.currentData()) if self.plan is not None else None
        deadline = acct.deadline_year(self.plan.profile) if acct is not None else None
        # A rule on a 10-year-rule inherited IRA can't run past the deadline.
        self.start.set_max_year(deadline)
        self.end.set_max_year(deadline)
        if self.plan is not None:
            start = self.start.value()
            self.end.set_min_year(max(start.resolve(self.plan.profile, start=True), self.plan.start_year)
                                  if start else None)
        self.show_row(self.deadline, deadline is not None)
        if deadline is not None:
            self.deadline.setText(f"10-year rule: ends by {deadline} at the latest")
            self.deadline.setToolTip(f"The inherited IRA must be empty by the end of {deadline},\n"
                                     "so the rule can't run past it and 1/N counts the years to then.")
        self.show_row(self.to_account, self.kind.currentData() == "roth_conversion")
        self.show_row(self.amount, mode == "fixed")
        self.show_row(self.growth, mode == "fixed")
        self.show_row(self.growth_rate, mode == "fixed" and self.growth.currentData() == "fixed")
        self.show_row(self.target, mode == "fill")
        self.show_row(self.magi, mode == "fill" and self.target.currentData() == "magi")
        is_qcd = self.kind.currentData() == "qcd"
        self.show_row(self.qcd_hint, is_qcd)
        if is_qcd and self.plan is not None:
            self.qcd_hint.setText(self._qcd_text(acct))
        is_hsa = self.kind.currentData() == "hsa_contribution"
        word = "Into account" if self.kind.currentData() in CONTRIBUTION_KINDS else "From account"
        self.form.labelForField(self.account).setText(word)
        self.account.setAccessibleName(word)
        self.show_row(self.coverage, is_hsa)
        self.show_row(self.hsa_hint, is_hsa)
        if is_hsa and self.plan is not None:
            self.hsa_hint.setText(self._hsa_text(acct))

    def _hsa_text(self, acct: Account | None) -> str:
        """The limits, catch-up and last year for an HSA contribution, shown under the rule."""
        plan = self.plan
        y0 = plan.start_year
        parts = []
        law = _shipped_law(y0, plan.inflation)
        if law is not None and law.hsa_self_only is not None:
            parts.append(f"{y0} limit ${law.hsa_self_only:,.0f} self-only or ${law.hsa_family:,.0f} family "
                         "(shared by spouses), rising with inflation; no earned income needed.")
        if acct is not None and acct.owner is not None and acct.owner < len(plan.profile.people):
            i = acct.owner
            p = plan.profile.people[i]
            catch = max(y0, p.birth_year + HSA_CATCH_UP_AGE)
            parts.append(f"{p.name} adds $1,000 from {catch} (age {catch - p.birth_year}), into this HSA only.")
            last = plan.hsa_last_year(i)
            if last is None:
                parts.append(f"{p.name} is on Medicare, so can't contribute.")
            elif p.medicare_part_b is False:
                parts.append(f"Last year: {last}, before Medicare Part A starts with Social Security "
                             "(backdated up to 6 months).")
            else:
                parts.append(f"Last year: {last}, before Medicare at 65.")
        return " ".join(parts)

    def _qcd_text(self, acct: Account | None) -> str:
        """The limit and first year for a QCD, shown under the rule."""
        plan = self.plan
        y0 = plan.start_year
        parts = []
        law = _shipped_law(y0, plan.inflation)
        if law is not None:
            parts.append(f"Limit ${law.qcd_limit:,.0f} a person in {y0}, rising with inflation.")
        if acct is not None and acct.owner is not None and acct.owner < len(plan.profile.people):
            p = plan.profile.people[acct.owner]
            parts.append(f"{p.name} can give from {p.birth_year + QCD_MIN_AGE} (age {QCD_MIN_AGE}).")
        parts.append("Not taxed, and counts toward this account's RMD; the money goes to charity, "
                     "not to spending.")
        return " ".join(parts)


class AssumptionsForm(ItemForm):
    def __init__(self) -> None:
        super().__init__()
        self.inflation = self.info_row("Inflation")
        self.cola = self.row("Social Security COLA", OptionalField("Same as inflation", percent_box(-1, 15)))
        self.ret = self.row("Default return (nominal)", percent_box(-20, 30),
                            "For accounts without a return of their own.\n" + NOMINAL_TIP)
        self.cash = self.row("Cash return (nominal)", percent_box(-5, 20),
                             "What every Cash / savings account earns, taxed as interest.\nNominal, before inflation")
        self.spending = self.row("Yearly spending", money_box(), "In today's dollars; grows with inflation")
        self.survivor = self.row("Spending after the first death", percent_box(0, 200, 5),
                                 "The survivor's spending, as a share of the couple's")
        self.health = self.row("ACA premium growth", percent_box(-5, 20),
                               "Yearly growth of the ACA marketplace premiums on the Baseline\n"
                               "tab (usually faster than inflation). The plan pays the net\n"
                               "premium (premium less the premium tax credit) until Medicare,\n"
                               "so leave those premiums out of spending.")
        self.order = self.row("Pay for spending from", SpendingOrderEdit(),
                              "When income falls short, money comes from the ticked account\n"
                              "types, top first (several accounts of one type: in list order).\n"
                              "Unticked types are never used for spending; withdrawals from\n"
                              "them need a strategy.")
        magi_tip = ("Form 1040 line 11 (AGI) plus line 2a (tax-exempt interest).\n"
                    "Sets Medicare premiums (IRMAA) two years later. Not entered: no\n"
                    "IRMAA is charged in the plan's first year, and the second year\n"
                    "uses the plan's own first-year MAGI.")
        self.magi1 = self.row("IRMAA MAGI two years before", money_box(), magi_tip)
        self.magi2 = self.row("IRMAA MAGI last year", money_box(), magi_tip)
        for box in (self.magi1, self.magi2):
            box.setSpecialValueText("Not entered")
        self.watch(self.cola, self.ret, self.cash, self.spending, self.survivor, self.health, self.order, self.magi1,
                   self.magi2)

    def fill(self) -> None:
        a = self.obj
        y = self.plan.start_year
        self.cola.set_value(None if a.ss_cola is None else 100 * a.ss_cola, 100 * self.plan.inflation)
        self.ret.setValue(100 * a.return_rate)
        self.cash.setValue(100 * a.cash_return)
        self.spending.setValue(a.spending)
        self.survivor.setValue(100 * a.survivor_spending_pct)
        self.health.setValue(100 * a.health_premium_growth)
        self.order.set_value(a.spending_order)
        self.magi1.setValue(a.prior_magi.get(str(y - 2), 0))
        self.magi2.setValue(a.prior_magi.get(str(y - 1), 0))
        for box, year in ((self.magi1, y - 2), (self.magi2, y - 1)):
            self.form.labelForField(box).setText(f"IRMAA MAGI for {year}")
            box.setAccessibleName(f"IRMAA MAGI for {year}")

    def store(self) -> None:
        a = self.obj
        y = self.plan.start_year
        a.ss_cola = None if self.cola.value() is None else self.cola.value() / 100
        a.return_rate = self.ret.value() / 100
        a.cash_return = self.cash.value() / 100
        a.spending = self.spending.value()
        a.survivor_spending_pct = self.survivor.value() / 100
        a.health_premium_growth = self.health.value() / 100
        a.spending_order = self.order.value()
        for year, box in ((y - 2, self.magi1), (y - 1, self.magi2)):
            if box.value():
                a.prior_magi[str(year)] = box.value()
            else:
                a.prior_magi.pop(str(year), None)

    def update_rows(self) -> None:
        if self.plan is not None:
            self.inflation.setText(f"{100 * self.plan.inflation:.2f}%\nChange it on the Baseline tab")
            self.cola.show_default(100 * self.plan.inflation)
            self.show_row(self.survivor, len(self.plan.profile.people) > 1)


# -- the list ------------------------------------------------------------------------


class BarDelegate(QStyledItemDelegate):
    """Paints the years an item covers as a bar on the plan's year axis."""

    def __init__(self, editor: "PlanEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(150, super().sizeHint(option, index).height())

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index) -> None:
        super().paint(p, option, index)
        bar = index.data(BAR_ROLE)
        plan = self.editor.plan
        if plan is None:
            return
        r = option.rect.adjusted(4, 0, -6, 0)
        y0, y1 = plan.start_year, plan.end_year
        span = max(1, y1 - y0 + 1)

        def x(year: float) -> float:
            return r.left() + (year - y0) / span * r.width()

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        guide = QColor(option.palette.mid().color())
        guide.setAlpha(60)
        p.setPen(QPen(guide, 1))
        for yr in range(y0 - y0 % 10 + 10, y1 + 1, 10):
            p.drawLine(int(x(yr)), r.top(), int(x(yr)), r.bottom())
        if bar is not None:
            first, last, color, milestone = bar
            c = QColor(color)
            cy = r.center().y()
            if milestone:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                cx = int(x(first + 0.5))
                p.drawPolygon(QPolygon([QPoint(cx, cy - 5), QPoint(cx + 5, cy), QPoint(cx, cy + 5),
                                        QPoint(cx - 5, cy)]))
            else:
                first = max(first, y0)
                last = min(last, y1)
                if last >= first:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(c)
                    left, right = x(first), x(last + 1)
                    p.drawRoundedRect(QRect(int(left), cy - 4, max(4, int(right - left)), 9), 3, 3)
        p.restore()


class PlanEditor(QWidget):
    """The Plan items tab: the list, its buttons and the selected item's form."""

    changed = Signal()  # the plan was edited (recalculate)
    structure_changed = Signal()  # items were added or removed

    def __init__(self) -> None:
        super().__init__()
        self.plan: Plan | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        row = QHBoxLayout()
        self.add_btn = QToolButton()
        self.add_btn.setText("&Add item")
        self.add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.add_menu = QMenu(self)
        self.add_actions: dict[str, object] = {}
        for key, text in (("account", "Account"), ("income", "Income stream"),
                          ("one_time", "One-time income (sale, gift, bonus)"), (None, None),
                          ("roth_conversion", "Roth conversion"), ("withdrawal", "Withdrawal"),
                          ("qcd", "Qualified charitable distribution (QCD)"),
                          ("hsa_contribution", "HSA contribution")):
            if key is None:
                self.add_menu.addSeparator()
                continue
            self.add_actions[key] = self.add_menu.addAction(text, lambda k=key: self.add_item(k))
        self.add_btn.setMenu(self.add_menu)
        self.dup_btn = QPushButton("D&uplicate")
        self.del_btn = QPushButton("&Delete")
        self.dup_btn.clicked.connect(lambda: self.duplicate_item())
        self.del_btn.clicked.connect(lambda: self.delete_item())
        row.addWidget(self.add_btn)
        row.addWidget(self.dup_btn)
        row.addWidget(self.del_btn)
        row.addStretch(1)
        name_row = QHBoxLayout()
        name_label = QLabel("Plan &name")
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Roth conversions until Social Security")
        self.name.setToolTip("A name for this version of the plan;\nit is part of the suggested file name")
        self.name.setAccessibleName("Plan name")
        name_label.setBuddy(self.name)
        self.name.textEdited.connect(self._name_edited)
        name_row.addWidget(name_label)
        name_row.addWidget(self.name, 1)
        lay.addLayout(name_row)
        lay.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Item", "Years"])
        self.tree.setAccessibleName("Plan items")
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setItemDelegateForColumn(1, BarDelegate(self))
        head = self.tree.header()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        head.resizeSection(1, 140)
        self.tree.setMinimumHeight(260)
        self.tree.setMinimumWidth(400)
        self.tree.currentItemChanged.connect(self._selected)
        self.tree.itemChanged.connect(self._item_changed)
        lay.addWidget(self.tree, 1)

        self.form_title = QLabel()
        lay.addWidget(self.form_title)
        self.forms: dict[str, ItemForm] = {
            "accounts": AccountForm(),
            "incomes": IncomeForm(),
            "people": PersonForm(),
            "rules": RuleForm(),
            "assumptions": AssumptionsForm(),
        }
        self.stack = QStackedWidget()
        self.hint = QLabel("Select an item to change it. Untick an account, income or strategy to see the plan "
                           "without it.")
        self.hint.setWordWrap(True)
        # Near the top, a couple of lines down: the stack is as tall as the
        # tallest form, so a centred hint can end up below a short screen.
        self.hint.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.hint.setContentsMargins(0, 2 * self.hint.fontMetrics().lineSpacing(), 0, 0)
        self.stack.addWidget(self.hint)
        for form in self.forms.values():
            self.stack.addWidget(form)
            form.changed.connect(self._form_changed)
        lay.addWidget(self.stack, 0)
        self._update_buttons()

    # -- building the list -------------------------------------------------
    def set_plan(self, plan: Plan | None) -> None:
        self.plan = plan
        self.name.setText(plan.name if plan else "")
        self.name.setEnabled(plan is not None)
        self.rebuild()

    def _name_edited(self, text: str) -> None:
        if self.plan is not None:
            self.plan.name = text.strip()
            self.changed.emit()

    def rebuild(self, select=None) -> None:
        """Fill the list from the plan, keeping (or moving to) a selection."""
        current = select if select is not None else self.current_object()
        self.tree.blockSignals(True)
        self.tree.clear()
        self.groups: dict[str, QTreeWidgetItem] = {}
        found = None
        if self.plan is not None:
            for key, title in GROUPS:
                g = QTreeWidgetItem(self.tree, [title])
                g.setFlags(Qt.ItemFlag.ItemIsEnabled)
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                g.setFirstColumnSpanned(False)
                self.groups[key] = g
                for obj in self._objects(key):
                    item = QTreeWidgetItem(g)
                    item.setData(0, ROLE, (key, obj))
                    self._describe(item)
                    if obj is current:
                        found = item
                g.setExpanded(True)
        self.tree.blockSignals(False)
        if found is not None:
            self.tree.setCurrentItem(found)
        else:
            self._selected(None, None)
        self._update_buttons()

    def _objects(self, key: str) -> list:
        assert self.plan is not None
        if key == "assumptions":
            return [self.plan.assumptions]
        return list(getattr(self.plan, key))

    def refresh(self) -> None:
        """Re-describe every row (names or the baseline may have changed)."""
        self.tree.blockSignals(True)
        for g in getattr(self, "groups", {}).values():
            for i in range(g.childCount()):
                self._describe(g.child(i))
        self.tree.blockSignals(False)
        self.tree.viewport().update()

    def _describe(self, item: QTreeWidgetItem) -> None:
        key, obj = item.data(0, ROLE)
        plan = self.plan
        assert plan is not None
        profile = plan.profile
        y0, y1 = plan.start_year, plan.end_year
        color = GROUP_COLORS[key]
        bar = None
        tip = ""
        if key == "accounts":
            text = f"{obj.label(profile)}  {short_money(obj.balance)}"
            deadline = obj.deadline_year(profile)
            last = deadline or y1
            if obj.kind == "inherited_ira":
                color = "#008300"
                tip = f"Must be empty by {deadline}" if deadline else ""
            bar = (y0, last, color, 0)
        elif key == "incomes":
            text = f"{obj.label(profile)}  {short_money(obj.amount)}"
            first = obj.start.resolve(profile, start=True) if obj.start else y0
            last = obj.end.resolve(profile) if obj.end else y1
            bar = (first, last, INCOME_COLORS.get(obj.kind, color), 0)
            tip = f"{first}–{last}" if first != last else str(first)
        elif key == "people":
            i = plan.people.index(obj)
            p = profile.people[i]
            ss = obj.social_security
            spouse = plan.people[1 - i].social_security if len(plan.people) == 2 else None
            if ss.current_benefit is not None:
                claim_text = f"Social Security {short_money(ss.current_benefit)} now"
                first = y0
            elif ss.fra_benefit <= 0 and not ss.ssdi:
                spousal = spouse is not None and spouse.fra_benefit > 0
                claim_text = f"spousal only, claims at {age_text(ss.claim_age)}" if spousal else "No Social Security"
                first = p.birth_year + int(ss.claim_age)
            else:
                claim_text = f"claims at {age_text(ss.claim_age)}"
                first = p.birth_year + int(ss.claim_age)
            text = f"{p.name}: {claim_text}, to age {p.life_expectancy}"
            last = plan.death_year(i)
            if claim_text == "No Social Security":
                tip = f"No Social Security; lives to {last}"
            else:
                bar = (first, last, color, 0)
                tip = f"Social Security {first}–{last}"
        elif key == "rules":
            acct = plan.account(obj.account)
            name = acct.label(profile) if acct else obj.account
            how = {"fixed": short_money(obj.amount), "fill": f"fill to {target_label(obj.target)}",
                   "spread": "spread evenly (1/N)", "all": "whole balance", "rmd": "the RMD",
                   "max": "the most allowed"}.get(obj.mode, obj.mode)
            verb = {"roth_conversion": "Convert", "qcd": "QCD from",
                    "hsa_contribution": "Contribute to"}.get(obj.kind, "Withdraw from")
            text = f"{verb} {name}: {how}"
            first, last = obj.years(profile)
            bar_color = {"roth_conversion": color, "qcd": "#1f9e8f",
                         "hsa_contribution": "#b8559a"}.get(obj.kind, "#6250d6")
            bar = (first, last, bar_color, 0)
            tip = obj.describe(profile, {a.id: a for a in plan.accounts}) + (f"\n{obj.note}" if obj.note else "")
        else:
            a = obj
            infl = plan.inflation
            text = (f"Inflation {100 * infl:.1f}%, return {100 * a.return_rate:.1f}%, "
                    f"spending {short_money(a.spending)}")
            tip = text
        item.setText(0, text)
        # Names and notes are the user's, so wrap them to the tooltip width.
        def wrap(t: str) -> str:
            return "\n".join(textwrap.fill(line, TIP_WIDTH) for line in t.split("\n"))

        tip = wrap(tip) if tip else ""
        item.setToolTip(0, tip or wrap(text))
        item.setData(1, BAR_ROLE, bar)
        if bar is not None:
            item.setToolTip(1, tip or f"{bar[0]}–{bar[1]}")
            item.setData(1, Qt.ItemDataRole.AccessibleTextRole, tip or f"{bar[0]} to {bar[1]}")
        if hasattr(obj, "enabled"):
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked if obj.enabled else Qt.CheckState.Unchecked)
            if not obj.enabled:
                item.setForeground(0, self.palette().placeholderText())
                item.setData(1, BAR_ROLE, (bar[0], bar[1], "#c9c7c0", 0) if bar else None)
            else:
                item.setData(0, Qt.ItemDataRole.ForegroundRole, None)

    # -- selection and editing ----------------------------------------------
    def current_object(self):
        item = self.tree.currentItem()
        data = item.data(0, ROLE) if item is not None else None
        return data[1] if data else None

    def current_key(self) -> str | None:
        item = self.tree.currentItem()
        data = item.data(0, ROLE) if item is not None else None
        return data[0] if data else None

    def _selected(self, current, _previous) -> None:
        data = current.data(0, ROLE) if current is not None else None
        if not data or self.plan is None:
            self.stack.setCurrentWidget(self.hint)
            self.form_title.setText("")
        else:
            key, obj = data
            form = self.forms[key]
            form.load(self.plan, obj)
            self.stack.setCurrentWidget(form)
            title = dict(GROUPS)[key]
            if key == "people":
                title = self.plan.profile.people[self.plan.people.index(obj)].name
            elif key == "rules":
                title = RULE_KINDS.get(obj.kind, obj.kind)
            elif key == "accounts":
                title = "Account"
            elif key == "incomes":
                title = "Income"
            self.form_title.setText(f"<b>{title}</b>")
        self._update_buttons()

    def _form_changed(self) -> None:
        item = self.tree.currentItem()
        if item is not None and item.data(0, ROLE):
            self.tree.blockSignals(True)
            self._describe(item)
            self.tree.blockSignals(False)
        if self.current_key() == "accounts":
            self.refresh()  # rules show account names
        self.changed.emit()

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        data = item.data(0, ROLE)
        if column != 0 or not data:
            return
        obj = data[1]
        if hasattr(obj, "enabled"):
            enabled = item.checkState(0) == Qt.CheckState.Checked
            if enabled != obj.enabled:
                obj.enabled = enabled
                self.tree.blockSignals(True)
                self._describe(item)
                self.tree.blockSignals(False)
                self.changed.emit()

    def _update_buttons(self) -> None:
        key = self.current_key()
        has_plan = self.plan is not None
        self.add_btn.setEnabled(has_plan)
        removable = key in ("accounts", "incomes", "rules")
        self.dup_btn.setEnabled(removable)
        self.del_btn.setEnabled(removable)

    # -- adding and removing ------------------------------------------------
    def add_item(self, what: str) -> None:
        plan = self.plan
        if plan is None:
            return
        y0 = plan.start_year
        if what == "account":
            obj = Account(id=plan.new_account_id("traditional_ira", 0), kind="traditional_ira", owner=0)
            plan.accounts.append(obj)
        elif what in ("income", "one_time"):
            if what == "income":
                obj = IncomeStream(kind="pension", amount=0.0, owner=0, cola="none")
            else:
                obj = IncomeStream(kind="ltcg", amount=0.0, owner=None, name="One-time income", cola="none",
                                   start=When(year=y0 + 1), end=When(year=y0 + 1))
            plan.incomes.append(obj)
        else:
            if what == "qcd":
                sources = [a for a in plan.accounts if a.kind in QCD_KINDS]
            elif what == "hsa_contribution":
                sources = [a for a in plan.accounts if a.kind == "hsa" and a.owner is not None]
            else:
                sources = [a for a in plan.accounts
                           if what != "roth_conversion" or a.kind in TRADITIONAL_KINDS or a.kind == "inherited_ira"]
            if not sources:
                need = {"roth_conversion": "a traditional IRA or 401(k)",
                        "qcd": "a traditional or inherited IRA",
                        "hsa_contribution": "an HSA"}.get(what, "an account")
                QMessageBox.information(self, "Add strategy", f"Add {need} first.")
                return
            if what == "qcd":
                owner = sources[0].owner if sources[0].owner is not None else 0
                first = max(y0, plan.profile.people[min(owner, len(plan.profile.people) - 1)].birth_year
                            + QCD_MIN_AGE)
                obj = Rule(kind="qcd", account=sources[0].id, start=When(year=first),
                           end=When(event="last_death"), mode="rmd")
            elif what == "hsa_contribution":
                owner = min(sources[0].owner, len(plan.profile.people) - 1)
                last = plan.hsa_last_year(owner)
                end = When(age=last - plan.profile.people[owner].birth_year, person=owner) if last else None
                obj = Rule(kind="hsa_contribution", account=sources[0].id, start=When(year=y0), end=end,
                           mode="max", coverage="family" if len(plan.profile.people) > 1 else "self")
            elif what == "roth_conversion":
                obj = Rule(kind="roth_conversion", account=sources[0].id, start=When(year=y0),
                           end=When(year=y0 + 4), mode="fill", target="bracket:0.12")
            else:
                obj = Rule(kind="withdrawal", account=sources[0].id, start=When(year=y0), mode="fixed")
            plan.rules.append(obj)
        self.rebuild(select=obj)
        self.structure_changed.emit()
        self.changed.emit()

    def duplicate_item(self) -> None:
        key, obj = self.current_key(), self.current_object()
        if self.plan is None or key not in ("accounts", "incomes", "rules"):
            return
        new = copy.deepcopy(obj)
        items = getattr(self.plan, key)
        if key == "accounts":
            new.id = self.plan.new_account_id(new.kind, new.owner)
            if new.name:
                new.name += " (copy)"
        items.insert(items.index(obj) + 1, new)
        self.rebuild(select=new)
        self.structure_changed.emit()
        self.changed.emit()

    def delete_item(self, *, confirm: Callable[[str], bool] | None = None) -> None:
        key, obj = self.current_key(), self.current_object()
        if self.plan is None or key not in ("accounts", "incomes", "rules"):
            return
        items = getattr(self.plan, key)
        idx = items.index(obj)
        if key == "accounts":
            users = [r for r in self.plan.rules if r.account == obj.id or r.to_account == obj.id]
            if users:
                ask = confirm or self._confirm
                if not ask(f"{len(users)} strateg{'y uses' if len(users) == 1 else 'ies use'} this account "
                           f"and will be deleted with it."):
                    return
                self.plan.rules = [r for r in self.plan.rules if r not in users]
        items.pop(idx)
        neighbours = getattr(self.plan, key)
        self.rebuild(select=neighbours[min(idx, len(neighbours) - 1)] if neighbours else None)
        self.structure_changed.emit()
        self.changed.emit()

    def _confirm(self, text: str) -> bool:
        answer = QMessageBox.question(self, "Delete account", text + " Delete them?")
        return answer == QMessageBox.StandardButton.Yes
