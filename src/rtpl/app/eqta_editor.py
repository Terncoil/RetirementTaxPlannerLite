"""The EQTA window's left panel: a tree of the year's items and an editor.

The tree has fixed Profile, File notes, Year notes and Payments nodes, then the income
sources grouped by tax type. Below it is the form for the selected item
and, for income sources and payment lines, its register of date and
amount rows. Every change is written straight into the year and announced
with ``changed`` so the worksheet can recalculate.
"""

from __future__ import annotations

import datetime as dt
import textwrap

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from rtpl.app import TIP_WIDTH
from rtpl.app.inputs import ComboBox, SpinBox
from rtpl.app.profile_form import MEDICARE_CHOICES, compact_form
from rtpl.model.eqta import (
    PAYMENT_LINES,
    TAX_TYPE,
    TAX_TYPES,
    EqtaFile,
    Entry,
    IncomeSource,
    PaymentLine,
    TaxYear,
    evaluate_amount,
    parse_date,
    payment_label,
)
from rtpl.model.profile import FILING_STATUS_LABELS, Person

ROLE = Qt.ItemDataRole.UserRole
AMOUNT_TIP = (
    "Type an amount or simple arithmetic, such as 3662.90*12 or\n"
    "1200+350.25. Only numbers, + - * / and parentheses."
)


# A field or cell with a typing mistake: dark text on light red, in the
# light and dark themes alike.
ERROR_BACKGROUND = "#f5b7b1"
ERROR_TEXT = "#000000"
ERROR_SHEET = f"background: {ERROR_BACKGROUND}; color: {ERROR_TEXT};"


def money(v: float) -> str:
    return f"-${-v:,.2f}" if v < 0 else f"${v:,.2f}"


def _calendar_icon() -> QIcon:
    """A small calendar drawn in the text color, for the date cells."""
    size = 16
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    color = QApplication.palette().color(QPalette.ColorRole.Text)
    p.setPen(QPen(color, 1))
    p.drawRect(2, 3, 11, 10)
    p.fillRect(2, 3, 12, 3, color)
    for x in (5, 8, 11):
        for y in (8, 11):
            p.drawPoint(x, y)
    p.end()
    return QIcon(pix)


class DateEdit(QLineEdit):
    """A register date cell: type MM/DD or MM/DD/YYYY, or pick from the
    calendar button at the right, which shows only the dates that count."""

    picked = Signal()

    def __init__(self, parent, first: dt.date, last: dt.date) -> None:
        super().__init__(parent)
        self.first, self.last = first, last
        act = self.addAction(_calendar_icon(), QLineEdit.ActionPosition.TrailingPosition)
        act.setToolTip("Pick the date from a calendar")
        act.triggered.connect(self.show_calendar)
        self.calendar_action = act

    def show_calendar(self) -> None:
        menu = QMenu(self)
        cal = QCalendarWidget()
        cal.setDateRange(QDate(self.first), QDate(self.last))
        cal.setGridVisible(True)
        try:
            current = parse_date(self.text(), self.first.year) if self.text().strip() else None
        except ValueError:
            current = None
        start = current or min(max(dt.date.today(), self.first), self.last)
        cal.setSelectedDate(QDate(start))

        def choose(qdate: QDate) -> None:
            self.setText(qdate.toPython().strftime("%m/%d/%Y"))
            menu.close()
            self.picked.emit()

        cal.clicked.connect(choose)
        cal.activated.connect(choose)
        holder = QWidgetAction(menu)
        holder.setDefaultWidget(cal)
        menu.addAction(holder)
        self.calendar = menu  # for tests
        menu.popup(self.mapToGlobal(self.rect().bottomLeft()))
        cal.setFocus()


class DateDelegate(QStyledItemDelegate):
    """Edits the register's date column with a ``DateEdit``."""

    def __init__(self, register: "Register") -> None:
        super().__init__(register.table)
        self.register = register

    def createEditor(self, parent, option, index):  # noqa: N802 (Qt name)
        first, last = self.register.calendar_range
        editor = DateEdit(parent, first, last)
        editor.picked.connect(lambda: (self.commitData.emit(editor), self.closeEditor.emit(editor)))
        return editor

    def setEditorData(self, editor, index) -> None:  # noqa: N802
        editor.setText(index.data() or "")

    def setModelData(self, editor, model, index) -> None:  # noqa: N802
        model.setData(index, editor.text())


def amount_edit(tip: str = AMOUNT_TIP) -> QLineEdit:
    edit = QLineEdit()
    edit.setPlaceholderText("0")
    edit.setToolTip(tip)
    edit.setMaximumWidth(180)
    return edit


def _mark_error(item: QTableWidgetItem, error: bool) -> None:
    """Light red with dark text for a mistake; the theme's colors otherwise."""
    item.setBackground(QBrush(QColor(ERROR_BACKGROUND)) if error else QBrush())
    item.setForeground(QBrush(QColor(ERROR_TEXT)) if error else QBrush())


class Register(QWidget):
    """Date and amount rows. The last row is always blank, for a new entry."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[Entry] | None = None
        self.year = dt.date.today().year
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Date", "Amount"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAccessibleName("Register")
        self.table.setToolTip(
            "One row per amount. Date: MM/DD/YYYY, or MM/DD in the tax year.\n"
            "Amount: a number or simple arithmetic, such as 3662.90*12.\n"
            "Type in the blank last row to add one.")
        row_height = self.fontMetrics().height() + 8
        self.table.verticalHeader().setDefaultSectionSize(row_height)
        # Room for the header and four rows; the table grows with the panel.
        self.table.setMinimumHeight(5 * row_height + 4)
        self.table.setMinimumWidth(240)
        self.calendar_range = (dt.date(self.year, 1, 1), dt.date(self.year, 12, 31))
        self.table.setItemDelegateForColumn(0, DateDelegate(self))
        self.table.itemChanged.connect(self._item_changed)
        self.delete_btn = QPushButton("&Remove row")
        self.delete_btn.setToolTip("Remove the selected register rows")
        self.delete_btn.clicked.connect(self._remove_rows)
        self.sort_btn = QPushButton("&Sort by date")
        self.sort_btn.clicked.connect(self._sort)
        self.total = QLabel()
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #c0392b;")
        buttons = QHBoxLayout()
        buttons.addWidget(self.delete_btn)
        buttons.addWidget(self.sort_btn)
        buttons.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(buttons)  # above the table, so they stay on short screens
        lay.addWidget(self.table, 1)
        lay.addWidget(self.total)
        lay.addWidget(self.warning)
        self.window_dates: tuple[dt.date, dt.date] | None = None
        self.window_text = ""

    def set_entries(self, entries: list[Entry], year: int, valid: tuple[dt.date, dt.date], what: str,
                    calendar: tuple[dt.date, dt.date] | None = None) -> None:
        """Show ``entries`` (edited in place). ``valid`` is the first and last
        date that counts on the worksheet; ``what`` says why, for the warning.
        ``calendar``: the dates the date picker offers (default: the year)."""
        self.entries, self.year = entries, year
        self.calendar_range = calendar or (dt.date(year, 1, 1), dt.date(year, 12, 31))
        self.window_dates, self.window_text = valid, what
        self._fill()

    def _fill(self) -> None:
        self.table.blockSignals(True)
        rows = self.entries or []
        self.table.setRowCount(len(rows) + 1)
        for r, e in enumerate(rows):
            self._set_row(r, e.date.strftime("%m/%d/%Y"), e.text)
        self._set_row(len(rows), "", "")
        self.table.blockSignals(False)
        self._update_total()

    def _set_row(self, r: int, date_text: str, amount_text: str) -> None:
        for c, text in enumerate((date_text, amount_text)):
            item = QTableWidgetItem(text)
            if c == 1:
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if text:
                    try:
                        item.setToolTip(f"{text} = {money(evaluate_amount(text))}")
                    except ValueError:
                        pass
            self.table.setItem(r, c, item)

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self.entries is None:
            return
        r = item.row()
        date_item, amount_item = self.table.item(r, 0), self.table.item(r, 1)
        date_text = date_item.text() if date_item else ""
        amount_text = amount_item.text() if amount_item else ""
        self.table.blockSignals(True)
        problem = None
        try:
            day = parse_date(date_text, self.year) if date_text.strip() else None
        except ValueError as exc:
            day, problem = None, str(exc)
            _mark_error(date_item, True)
        else:
            if date_item:
                _mark_error(date_item, False)
                if day is not None:
                    date_item.setText(day.strftime("%m/%d/%Y"))
        try:
            value = evaluate_amount(amount_text) if amount_text.strip() else None
        except ValueError as exc:
            value, problem = None, str(exc)
            _mark_error(amount_item, True)
        else:
            if amount_item:
                _mark_error(amount_item, False)
                amount_item.setToolTip(f"{amount_text} = {money(value)}" if value is not None else "")
        self.table.blockSignals(False)
        if problem:
            self.warning.setText(problem)
            return
        if r < len(self.entries):
            if day is None:
                return
            self.entries[r] = Entry(day, amount_text.strip() or "0")
        elif day is not None and amount_text.strip():
            self.entries.append(Entry(day, amount_text.strip()))
            self.table.blockSignals(True)
            self.table.insertRow(self.table.rowCount())
            self._set_row(self.table.rowCount() - 1, "", "")
            self.table.blockSignals(False)
        else:
            return
        self._update_total()
        self.changed.emit()

    def _remove_rows(self) -> None:
        if self.entries is None:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes() if i.row() < len(self.entries)}, reverse=True)
        for r in rows:
            del self.entries[r]
        if rows:
            self._fill()
            self.changed.emit()

    def _sort(self) -> None:
        if self.entries:
            self.entries.sort(key=lambda e: e.date)
            self._fill()
            self.changed.emit()

    def _update_total(self) -> None:
        rows = self.entries or []
        total = sum(e.amount for e in rows)
        self.total.setText(f"Total {money(total)} in {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")
        if self.window_dates:
            lo, hi = self.window_dates
            outside = [e for e in rows if not lo <= e.date <= hi]
            self.warning.setText(
                f"{len(outside)} entr{'y is' if len(outside) == 1 else 'ies are'} dated outside "
                f"{lo:%m/%d/%Y} to {hi:%m/%d/%Y} and count{'s' if len(outside) == 1 else ''} in no "
                f"worksheet column ({self.window_text})." if outside else "")


class ProfilePage(QWidget):
    """The year's filing status, state, people, dependents and last year's figures."""

    changed = Signal()

    def __init__(self, states: list[str]) -> None:
        super().__init__()
        self.ty: TaxYear | None = None
        form = compact_form(QFormLayout())
        self.status = ComboBox()
        for key, label in FILING_STATUS_LABELS.items():
            self.status.addItem(label, key)
        form.addRow("&Filing status", self.status)
        self.state = ComboBox()
        self.state.addItem("(none)", None)
        for code in states:
            self.state.addItem(code, code)
        self.state.setToolTip("The state you live in this year. Its withholding and\n"
                              "estimated payment lines follow its own schedule.")
        form.addRow("&State", self.state)
        self.people_boxes: list[tuple[QGroupBox, QLineEdit, SpinBox, QCheckBox, ComboBox]] = []
        self.ages: list[QLabel] = []
        self.tax_year = dt.date.today().year
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(form)
        for n in range(2):
            box = QGroupBox(f"Person {n + 1}")
            f = compact_form(QFormLayout(box))
            name = QLineEdit()
            birth = SpinBox()
            birth.setRange(1900, 2100)
            birth.setMaximumWidth(80)
            age = QLabel()
            age.setAccessibleName("Calculated age")
            blind = QCheckBox()
            medicare = ComboBox()
            for text, _ in MEDICARE_CHOICES:
                medicare.addItem(text)
            medicare.setToolTip("Choose Yes for Medicare before 65 (for example on SSDI).")
            f.addRow("Name", name)
            f.addRow("Birth year", birth)
            f.addRow("", age)
            f.addRow("Blind", blind)
            f.addRow("Medicare Part B", medicare)
            for w, sig in ((name, name.textEdited), (birth, birth.valueChanged), (blind, blind.toggled),
                           (medicare, medicare.currentIndexChanged)):
                sig.connect(self._store)
            birth.valueChanged.connect(lambda _v, b=birth, a=age: self._show_age(b, a))
            self.ages.append(age)
            self.people_boxes.append((box, name, birth, blind, medicare))
            lay.addWidget(box)
        more = compact_form(QFormLayout())
        self.dependents = QLineEdit()
        self.dependents.setToolTip("Names of dependents, separated by commas. A tag only\n"
                                   "for now: they don't change the tax.")
        more.addRow("&Dependents", self.dependents)
        self.prior_tax = amount_edit(
            "Last year's total federal tax (Form 1040 line 22 plus other\n"
            "taxes, less credits), for the safe harbor. Leave blank to use\n"
            "last year's tab in this file, if there is one.")
        self.prior_agi = amount_edit(
            "Last year's AGI, which decides 100% or 110% for the safe\n"
            "harbor. Leave blank to use last year's tab.")
        for edit in (self.prior_tax, self.prior_agi):
            edit.setPlaceholderText("from last year's tab")
        more.addRow("Last year's federal &tax", self.prior_tax)
        more.addRow("Last year's &AGI", self.prior_agi)
        lay.addLayout(more)
        lay.addStretch(1)
        self.status.currentIndexChanged.connect(self._store)
        self.state.currentIndexChanged.connect(self._store)
        self.dependents.textEdited.connect(self._store)
        self.prior_tax.textEdited.connect(self._store)
        self.prior_agi.textEdited.connect(self._store)

    def _show_age(self, birth: SpinBox, label: QLabel) -> None:
        """As in RTPL's profile: the age the IRS uses for the year's tests."""
        label.setText(f"Age {self.tax_year - birth.value()} at end of {self.tax_year}")

    def set_year(self, ty: TaxYear) -> None:
        self.ty = None
        self.tax_year = ty.year
        p = ty.profile
        self.status.setCurrentIndex(max(0, self.status.findData(p.filing_status)))
        self.state.setCurrentIndex(max(0, self.state.findData(p.state)))
        for n, (box, name, birth, blind, medicare) in enumerate(self.people_boxes):
            person = p.people[n] if n < len(p.people) else Person(name=f"Person {n + 1}", birth_year=ty.year - 65)
            name.setText(person.name)
            birth.setValue(person.birth_year)
            blind.setChecked(person.blind)
            medicare.setCurrentIndex([v for _, v in MEDICARE_CHOICES].index(person.medicare_part_b))
            self._show_age(birth, self.ages[n])
        self.dependents.setText(", ".join(ty.dependents))
        self.prior_tax.setText("" if ty.prior_year_tax is None else f"{ty.prior_year_tax:g}")
        self.prior_agi.setText("" if ty.prior_year_agi is None else f"{ty.prior_year_agi:g}")
        self.ty = ty
        self._sync_people()

    def _people_count(self) -> int:
        return 2 if self.status.currentData() in ("mfj", "mfs") else 1

    def _sync_people(self) -> None:
        self.people_boxes[1][0].setVisible(self._people_count() == 2)

    def _store(self, *_args) -> None:
        ty = self.ty
        if ty is None:
            return
        self._sync_people()
        p = ty.profile
        p.filing_status = self.status.currentData()
        p.state = self.state.currentData()
        people = []
        for n in range(self._people_count()):
            _, name, birth, blind, medicare = self.people_boxes[n]
            old = p.people[n] if n < len(p.people) else Person()
            old.name = name.text() or f"Person {n + 1}"
            old.birth_year = birth.value()
            old.blind = blind.isChecked()
            old.medicare_part_b = MEDICARE_CHOICES[medicare.currentIndex()][1]
            people.append(old)
        p.people = people
        ty.dependents = [d.strip() for d in self.dependents.text().split(",") if d.strip()]
        for edit, attr in ((self.prior_tax, "prior_year_tax"), (self.prior_agi, "prior_year_agi")):
            text = edit.text().strip()
            try:
                setattr(ty, attr, evaluate_amount(text) if text else None)
                edit.setStyleSheet("")
            except ValueError:
                edit.setStyleSheet(ERROR_SHEET)
        self.changed.emit()


class NotesEdit(QWidget):
    """A notes box with simple formatting (bold and italic), kept as Markdown.

    Qt reads and writes the Markdown itself (``QTextDocument.setMarkdown``
    and ``toMarkdown``), so this needs nothing beyond PySide6. Pasted text
    arrives plain; Ctrl+B and Ctrl+I, or the buttons, format the selection.
    """

    changed = Signal()

    def __init__(self, name: str, placeholder: str) -> None:
        super().__init__()
        self.text = QTextEdit()
        self.text.setAccessibleName(name)
        self.text.setPlaceholderText(placeholder)
        self.text.setAcceptRichText(False)  # paste as plain text
        self.text.setTabChangesFocus(True)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        self.bold = self._button("B", "Bold (Ctrl+B)", "Ctrl+B", self._set_bold)
        self.italic = self._button("I", "Italic (Ctrl+I)", "Ctrl+I", self._set_italic)
        f = self.bold.font()
        f.setBold(True)
        self.bold.setFont(f)
        f = self.italic.font()
        f.setItalic(True)
        self.italic.setFont(f)
        bar.addWidget(self.bold)
        bar.addWidget(self.italic)
        bar.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.text, 1)
        self._loading = False
        self.text.textChanged.connect(self._changed)
        self.text.currentCharFormatChanged.connect(self._format_changed)

    def _button(self, text: str, tip: str, keys: str, slot) -> QToolButton:
        action = QAction(text, self.text, checkable=True, shortcut=keys, toolTip=tip)
        action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        action.triggered.connect(slot)
        self.text.addAction(action)
        button = QToolButton()
        button.setDefaultAction(action)
        button.setAccessibleName(tip.split(" (")[0])
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep the selection in the text
        return button

    def _set_bold(self, on: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
        self.text.mergeCurrentCharFormat(fmt)

    def _set_italic(self, on: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontItalic(on)
        self.text.mergeCurrentCharFormat(fmt)

    def _format_changed(self, fmt: QTextCharFormat) -> None:
        self.bold.defaultAction().setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
        self.italic.defaultAction().setChecked(fmt.fontItalic())

    def set_markdown(self, text: str) -> None:
        self._loading = True
        self.text.setMarkdown(text)
        self._loading = False

    def markdown(self) -> str:
        return self.text.toMarkdown().strip()

    def _changed(self) -> None:
        if not self._loading:
            self.changed.emit()


class NotesPage(QWidget):
    """File notes (for the whole file) or Year notes (for the year's tab)."""

    changed = Signal()

    def __init__(self, scope: str) -> None:
        super().__init__()
        self.scope = scope  # "file" or "year"
        self.target: EqtaFile | TaxYear | None = None
        if scope == "file":
            name, heading = "File notes", "Notes for the whole file, shown on every year's tab"
            hint = "Notes for every year, such as account numbers or a long-term plan."
        else:
            name, heading = "Year notes", "Notes for this year"
            hint = "Notes for this year, such as the inherited IRA withdrawal plan."
        self.edit = NotesEdit(name, hint)
        self.text = self.edit.text
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        title = QLabel(heading)
        title.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(self.edit, 1)
        self.edit.changed.connect(self._store)

    def set_target(self, target: EqtaFile | TaxYear) -> None:
        self.target = None
        self.edit.set_markdown(target.notes)
        self.target = target

    def _store(self) -> None:
        if self.target is not None:
            self.target.notes = self.edit.markdown()
            self.changed.emit()


class ItemPage(QWidget):
    """An income source or a payment line: its fields, notes and register."""

    changed = Signal()
    renamed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.item: IncomeSource | PaymentLine | None = None
        self.ty: TaxYear | None = None
        self.form = compact_form(QFormLayout())
        self.heading = QLabel()
        self.name = QLineEdit()
        self.name.setAccessibleName("Source name")
        self.name.setToolTip("Leave blank to show the tax type instead. The person's\n"
                             "name is added in the tree and on the worksheet.")
        self.tax_type = ComboBox()
        for t in TAX_TYPES:
            self.tax_type.addItem(t.label, t.key)
        # Size to a short width; the popup list shows the long names in full,
        # so the panel doesn't widen for "Deductible traditional IRA contribution".
        for combo in (self.tax_type,):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(22)
        self.tax_type.setToolTip("How the income or deduction is taxed. Ordinary dividends\n"
                                 "to split use the qualified share below.")
        self.person = ComboBox()
        self.person.setToolTip("Whose income this is. Social Security, wages, pensions\n"
                               "and IRA distributions belong to a person, for their\n"
                               "state exclusions and Social Security.")
        self.plan = amount_edit(
            "The annual plan amount, shown in the worksheet's Plan column.\n" + AMOUNT_TIP)
        self.plan.setAccessibleName("Annual plan amount")
        self.share = QDoubleSpinBox()
        self.share.setRange(0, 100)
        self.share.setDecimals(1)
        self.share.setSuffix("%")
        self.share.setToolTip("Share of this fund's ordinary dividends that are qualified\n"
                              "(from last year's 1099-DIV or the fund's estimate). The\n"
                              "worksheet shows both parts, marked as estimated.")
        self.notes = QPlainTextEdit()
        self.notes.setAccessibleName("Notes")
        self.notes.setMaximumHeight(70)
        self.form.addRow(self.heading)
        self.form.addRow("&Name", self.name)
        self.form.addRow("Tax &type", self.tax_type)
        self.form.addRow("&Person", self.person)
        self.form.addRow("Annual p&lan", self.plan)
        self.form.addRow("&Qualified share", self.share)
        self.form.addRow("N&otes", self.notes)
        self.register = Register()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.form)
        lay.addWidget(self.register, 1)
        self.name.textEdited.connect(self._store)
        self.tax_type.currentIndexChanged.connect(self._type_changed)
        self.person.currentIndexChanged.connect(self._store)
        self.plan.textEdited.connect(self._store)
        self.share.valueChanged.connect(self._store)
        self.notes.textChanged.connect(self._store)
        self.register.changed.connect(self.changed)

    def _row_visible(self, widget: QWidget, visible: bool) -> None:
        self.form.setRowVisible(widget, visible)

    def set_item(self, ty: TaxYear, item: IncomeSource | PaymentLine, state_name: str | None) -> None:
        self.item = None
        self.ty = ty
        is_source = isinstance(item, IncomeSource)
        self.person.clear()
        self.person.addItem("(household)", None)
        for n, p in enumerate(ty.profile.people):
            self.person.addItem(p.name, n)
        if is_source:
            self.heading.setText("<b>Income source</b>")
            self.name.setText(item.name)
            self.name.setPlaceholderText(item.type.label)
            self.tax_type.setCurrentIndex(self.tax_type.findData(item.tax_type))
            self.person.setCurrentIndex(max(0, self.person.findData(item.person_index(len(ty.profile.people)))))
            self.share.setValue(100 * (item.qualified_share if item.qualified_share is not None else 0.0))
            valid = (dt.date(ty.year, 1, 1), dt.date(ty.year, 12, 31))
            why = f"income counts in {ty.year} only"
            calendar = valid
        else:
            self.heading.setText(f"<b>{payment_label(item.key, state_name)}</b>")
            valid = (dt.date(ty.year, 1, 1), dt.date(ty.year + 1, 4, 30))
            why = "a payment counts by the due date it is on or before"
            # Estimated payments for the last period are made the next January.
            last = dt.date(ty.year + 1, 1, 31) if item.key.endswith("_estimated") else dt.date(ty.year, 12, 31)
            calendar = (dt.date(ty.year, 1, 1), last)
        for w in (self.name, self.tax_type, self.person):
            self._row_visible(w, is_source)
        self.plan.setText(item.plan_text)
        self.notes.setPlainText(item.notes)
        self.register.set_entries(item.entries, ty.year, valid, why, calendar)
        self.item = item
        self._sync_rows()

    def _sync_rows(self) -> None:
        item = self.item
        is_source = isinstance(item, IncomeSource)
        self._row_visible(self.share, is_source and item.tax_type == "dividends_to_split")
        if is_source:
            per_person = TAX_TYPE[item.tax_type].per_person
            # A per-person type always belongs to someone.
            self.person.model().item(0).setEnabled(not per_person)

    def _type_changed(self, *_args) -> None:
        item = self.item
        if isinstance(item, IncomeSource):
            item.tax_type = self.tax_type.currentData()
            self.name.setPlaceholderText(item.type.label)
            if item.tax_type == "dividends_to_split" and item.qualified_share is None:
                item.qualified_share = self.share.value() / 100 or 0.75
                self.share.setValue(item.qualified_share * 100)
            if item.type.per_person and item.person is None:
                item.person = 0
                self.person.setCurrentIndex(1)
            self._sync_rows()
            self.renamed.emit()
            self.changed.emit()

    def _store(self, *_args) -> None:
        item = self.item
        if item is None:
            return
        text = self.plan.text().strip()
        try:
            if text:
                evaluate_amount(text)
            item.plan_text = text
            self.plan.setStyleSheet("")
            self.plan.setToolTip(f"{text} = {money(evaluate_amount(text))}" if text else AMOUNT_TIP)
        except ValueError:
            self.plan.setStyleSheet(ERROR_SHEET)
        item.notes = self.notes.toPlainText()
        if isinstance(item, IncomeSource):
            if item.name != self.name.text():
                item.name = self.name.text()
                self.renamed.emit()
            if item.person != self.person.currentData():
                item.person = self.person.currentData()
                self.renamed.emit()
            if item.tax_type == "dividends_to_split":
                item.qualified_share = self.share.value() / 100
        self.changed.emit()


class EqtaEditor(QWidget):
    """Tree of the year's items, with Add, Clone and Delete, and the editor below."""

    changed = Signal()
    page_changed = Signal()  # another form shows: the panel re-measures its width

    def __init__(self, states: list[str], state_name=lambda code, year: code) -> None:
        super().__init__()
        self.eq: EqtaFile | None = None
        self.ty: TaxYear | None = None
        self.state_name = state_name
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName("Year items")
        self.tree.setMinimumHeight(150)
        self.tree.currentItemChanged.connect(self._selected)
        self.add_btn = QToolButton()
        self.add_btn.setText("&Add")
        self.add_btn.setToolTip("Add an income source of the type you choose")
        self.add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.add_btn)
        for t in TAX_TYPES:
            menu.addAction(t.label, lambda key=t.key: self.add_source(key))
        self.add_btn.setMenu(menu)
        self.clone_btn = QPushButton("C&lone")
        self.clone_btn.setToolTip("Copy the selected income source, without its register")
        self.clone_btn.clicked.connect(self.clone_source)
        self.delete_btn = QPushButton("&Delete")
        self.delete_btn.setToolTip("Delete the selected income source")
        self.delete_btn.clicked.connect(self.delete_source)
        buttons = QHBoxLayout()
        buttons.addWidget(self.add_btn)
        buttons.addWidget(self.clone_btn)
        buttons.addWidget(self.delete_btn)
        buttons.addStretch(1)
        self.profile_page = ProfilePage(states)
        self.file_notes_page = NotesPage("file")
        self.notes_page = NotesPage("year")
        self.item_page = ItemPage()
        self.blank = QLabel("Choose an item above to edit it.")
        self.blank.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.stack = QStackedWidget()
        for w in (self.blank, self.profile_page, self.file_notes_page, self.notes_page, self.item_page):
            self.stack.addWidget(w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addLayout(buttons)  # above the tree, as in RTPL's plan items
        lay.addWidget(self.tree)
        lay.addWidget(self.stack, 1)
        self.profile_page.changed.connect(self._profile_changed)
        self.notes_page.changed.connect(self.changed)
        self.file_notes_page.changed.connect(self.changed)
        self.item_page.changed.connect(self.changed)
        self.item_page.renamed.connect(self._refresh_labels)

    # -- tree -----------------------------------------------------------
    def set_year(self, eq: EqtaFile, ty: TaxYear | None) -> None:
        self.eq, self.ty = eq, ty
        self._rebuild()

    def _state_label(self) -> str | None:
        ty = self.ty
        return self.state_name(ty.profile.state, ty.year) if ty and ty.profile.state else None

    def _rebuild(self, select=None) -> None:
        ty = self.ty
        self.tree.blockSignals(True)
        self.tree.clear()
        if ty is None:
            self.tree.blockSignals(False)
            self._show_page(self.blank)
            return
        fixed = []
        for text, kind in (("Profile", "profile"), ("File notes", "file_notes"), ("Year notes", "notes")):
            node = QTreeWidgetItem([text])
            node.setData(0, ROLE, (kind, None))
            fixed.append(node)
        pays = QTreeWidgetItem(["Payments"])
        pays.setData(0, ROLE, ("payments", None))
        for key in PAYMENT_LINES:
            child = QTreeWidgetItem([payment_label(key, self._state_label())])
            child.setData(0, ROLE, ("payment", key))
            pays.addChild(child)
        fixed.append(pays)
        self.tree.addTopLevelItems(fixed)
        income = QTreeWidgetItem(["Income and deductions"])
        income.setData(0, ROLE, ("income", None))
        for t in TAX_TYPES:
            members = [(i, s) for i, s in enumerate(ty.sources) if s.tax_type == t.key]
            if not members:
                continue
            group = QTreeWidgetItem([t.label])
            group.setData(0, ROLE, ("group", t.key))
            for i, s in members:
                child = QTreeWidgetItem([s.label(ty.profile.people)])
                child.setData(0, ROLE, ("source", i))
                group.addChild(child)
            income.addChild(group)
        self.tree.addTopLevelItem(income)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        target = self._find(select) if select else None
        self.tree.setCurrentItem(target or fixed[0])
        self._selected(self.tree.currentItem(), None)

    def _find(self, key) -> QTreeWidgetItem | None:
        it = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while it:
            node = it.pop(0)
            if node.data(0, ROLE) == key:
                return node
            it.extend(node.child(i) for i in range(node.childCount()))
        return None

    def _selected(self, current, _previous) -> None:
        ty = self.ty
        kind, key = current.data(0, ROLE) if current is not None else (None, None)
        self.clone_btn.setEnabled(kind == "source")
        self.delete_btn.setEnabled(kind == "source")
        if ty is None or kind is None:
            self._show_page(self.blank)
        elif kind == "profile":
            self.profile_page.set_year(ty)
            self._show_page(self.profile_page)
        elif kind == "notes":
            self.notes_page.set_target(ty)
            self._show_page(self.notes_page)
        elif kind == "file_notes" and self.eq is not None:
            self.file_notes_page.set_target(self.eq)
            self._show_page(self.file_notes_page)
        elif kind == "payment":
            self.item_page.set_item(ty, ty.payments[key], self._state_label())
            self._show_page(self.item_page)
        elif kind == "source":
            self.item_page.set_item(ty, ty.sources[key], self._state_label())
            self._show_page(self.item_page)
        else:
            self.blank.setText("Choose an item above to edit it, or Add an income source."
                               if kind in ("income", "group") else "Choose a payment line to edit its register.")
            self._show_page(self.blank)

    def current_source_index(self) -> int | None:
        node = self.tree.currentItem()
        if node is None:
            return None
        kind, key = node.data(0, ROLE)
        return key if kind == "source" else None

    def _refresh_labels(self) -> None:
        i = self.current_source_index()
        if i is None:
            return
        node = self.tree.currentItem()
        s = self.ty.sources[i]
        group = node.parent()
        if group is not None and group.data(0, ROLE) != ("group", s.tax_type):
            self._rebuild(select=("source", i))  # moved to another group
        else:
            node.setText(0, s.label(self.ty.profile.people))

    def _profile_changed(self) -> None:
        # Names and the state appear in the tree's labels.
        node = self._find(("payments", None))
        for n, key in enumerate(PAYMENT_LINES):
            node.child(n).setText(0, payment_label(key, self._state_label()))
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            kind, key = it.value().data(0, ROLE)
            if kind == "source":
                it.value().setText(0, self.ty.sources[key].label(self.ty.profile.people))
            it += 1
        self.changed.emit()

    def _show_page(self, page: QWidget) -> None:
        """Show ``page``; only it counts toward the panel's size."""
        for i in range(self.stack.count()):
            w = self.stack.widget(i)
            policy = QSizePolicy.Policy.Preferred if w is page else QSizePolicy.Policy.Ignored
            w.setSizePolicy(policy, policy)
        self.stack.setCurrentWidget(page)
        self.page_changed.emit()

    # -- buttons ----------------------------------------------------------
    def add_source(self, tax_type: str) -> None:
        if self.ty is None:
            return
        s = self.eq.new_source(tax_type) if self.eq else IncomeSource(tax_type=tax_type)
        if s.type.per_person:
            s.person = 0
        self.ty.sources.append(s)
        self._rebuild(select=("source", len(self.ty.sources) - 1))
        self.item_page.name.setFocus()
        self.item_page.name.selectAll()
        self.changed.emit()

    def clone_source(self) -> None:
        i = self.current_source_index()
        if i is None:
            return
        s = self.ty.sources[i]
        copy_ = IncomeSource(name=f"{s.name} (copy)" if s.name.strip() else "", tax_type=s.tax_type, person=s.person,
                             plan_text=s.plan_text, notes=s.notes, qualified_share=s.qualified_share)
        self.ty.sources.insert(i + 1, copy_)
        self._rebuild(select=("source", i + 1))
        self.changed.emit()

    def delete_source(self, confirm: bool = True) -> None:
        i = self.current_source_index()
        if i is None:
            return
        s = self.ty.sources[i]
        if confirm and s.entries:
            answer = QMessageBox.question(
                self, "Delete", textwrap.fill(
                    f"Delete {s.label(self.ty.profile.people)} and its {len(s.entries)} register entries?", TIP_WIDTH))
            if answer != QMessageBox.StandardButton.Yes:
                return
        del self.ty.sources[i]
        self._rebuild(select=("group", s.tax_type))
        self.changed.emit()
