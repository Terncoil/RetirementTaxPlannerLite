"""The EQTA year tab: the read-only Worksheet and the Calculation detail.

``worksheet_lines`` turns an engine ``Worksheet`` into rows of text, so the
layout can be tested without Qt and reused by a later export.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from rtpl.app import TIP_WIDTH
from rtpl.engine.estimated import COLUMN_COUNT, PERIODS, Worksheet, detail_rows, worksheet_groups
from rtpl.model.eqta import payment_label

COLUMN_TITLES = ("YTD Mar 31", "YTD May 31", "YTD Aug 31", "YTD Dec 31", "Plan")


@dataclass
class Line:
    label: str
    cells: list[str]  # one per column
    style: str = "item"  # heading, item, subtotal, total, guidance, info
    tip: str = ""


def money(v: float | None) -> str:
    if v is None:
        return ""
    v = round(v)
    return f"-${-v:,.0f}" if v < 0 else f"${v:,.0f}"


def day(d: dt.date | None) -> str:
    return "" if d is None else f"{d:%b} {d.day}, {d.year}"


def column_titles(ws: Worksheet) -> list[str]:
    return [f"YTD {d:%b} {d.day}" for d in ws.period_ends] + ["Plan"]


def worksheet_lines(ws: Worksheet) -> tuple[list[Line], list[str]]:
    """The worksheet's rows and its footnotes."""
    ty = ws.ty
    lines: list[Line] = []
    notes: list[str] = []
    marks = iter("*†‡§¶")

    def mark(text: str) -> str:
        m = next(marks, "#")
        notes.append(f"{m} {text}")
        return m

    def row(label, values, style="item", tip=""):
        lines.append(Line(label, [money(v) if not isinstance(v, str) else v for v in values], style, tip))

    # -- income -----------------------------------------------------------
    for label, members in worksheet_groups(ty):
        lines.append(Line(label, [""] * COLUMN_COUNT, "heading"))
        sub = [0.0] * COLUMN_COUNT
        for i, share in members:
            s = ty.sources[i]
            values = [ws.amounts[c][i] * (1 if share is None else share) for c in range(COLUMN_COUNT)]
            sub = [a + b for a, b in zip(sub, values)]
            name = s.name or "(no name)"
            if share is not None:
                name = f"{name} (estimated, {share:.0%})"
            who = ""
            idx = s.person_index(len(ty.profile.people))
            if idx is not None and len(ty.profile.people) > 1:
                who = f" · {ty.profile.people[idx].name}"
            row(f"  {name}{who}", values, tip=textwrap.fill(s.notes, TIP_WIDTH) if s.notes else "")
        if len(members) > 1:
            row(f"Total {label.lower()}", sub, "subtotal")
    if not ty.sources:
        lines.append(Line("No income sources yet: Add one in the Inputs panel.", [""] * COLUMN_COUNT, "info"))

    # -- federal ------------------------------------------------------------
    lines.append(Line("Federal", [""] * COLUMN_COUNT, "heading"))
    row("Estimated federal tax", ws.federal_tax, "total",
        "Tax on the income so far as if the year ended on that day\n(pay-as-you-go).")
    row("Due date", [day(d) for d in ws.due_dates] + [""], "info")
    for key in ("federal_withholding", "federal_estimated"):
        row(f"  {payment_label(key)}", ws.paid[key])
    row("Federal paid", ws.paid_total(("federal_withholding", "federal_estimated")), "subtotal",
        "Payments dated on or before each due date. The Plan column\nadds up the plan amounts.")
    row("Federal owed", ws.federal_owed, "total", "Estimated tax less paid. Negative: paid ahead.")

    # -- state --------------------------------------------------------------
    st, ss = ws.state, ws.state_schedule
    if st is not None and ss is not None:
        lines.append(Line(st.name, [""] * COLUMN_COUNT, "heading"))
        through = [f"{d:%b} {d.day}" for d in ss.period_ends] + ["Plan"]
        if any(ss.period_differs):
            m = mark(f"{st.name}'s periods end on different days from the federal ones, so its "
                     f"columns count income through {', '.join(through[:4])}.")
            through = [t + (f" {m}" if ss.period_differs[i] else "") if i < PERIODS else t
                       for i, t in enumerate(through)]
            row("Income through", through, "info")
        row(f"Estimated {st.name} tax", ws.state_tax, "total")
        dues = []
        for i in range(PERIODS):
            d = ss.due_dates[i]
            if d is None:
                dues.append("none due" if not ss.annual_only else "")
            elif ss.differs[i]:
                dues.append(f"{day(d)} {mark(f'{st.name} due {day(d)}; federal {day(ws.due_dates[i])}.')}")
            else:
                dues.append(day(d))
        row("Due date", dues + [""], "info")
        for key in ("state_withholding", "state_estimated"):
            row(f"  {payment_label(key, st.name)}", ws.paid[key])
        row(f"{st.name} paid", ws.paid_total(("state_withholding", "state_estimated")), "subtotal")
        row(f"{st.name} owed", ws.state_owed, "total")
        if ss.annual_only:
            notes.append(f"{st.name}: one payment, due {day(ss.due_dates[3])}.")
        if not ss.required:
            notes.append(f"{st.name} doesn't require estimated payments.")

    # -- guidance -----------------------------------------------------------
    lines.append(Line("Guidance (for information)", [""] * COLUMN_COUNT, "heading"))
    rap = ws.required_annual_payment
    row("Federal required by due date, annualized method", [*ws.annualized_required, None], "guidance",
        "Form 2210 Schedule AI: income so far annualized, taxed, times\n"
        "22.5%, 45%, 67.5% and 90%, but never more than the regular\n"
        "installments plus what earlier periods left unused.")
    row("Federal required by due date, regular installments", [*ws.regular_required, rap], "guidance",
        "The required annual payment in equal shares by each due date.\n"
        "The Plan column shows the whole required annual payment.")
    row("Federal counted as paid by due date", [*ws.credited, None], "guidance",
        "Estimated payments by their date, plus withholding spread evenly\n"
        "over the due dates, as the IRS counts it for the penalty.")
    row("Federal short by due date", [*ws.short, None], "guidance")
    basis = f"90% of this year's tax (${ws.current_tax:,.0f}, from {ws.current_tax_basis})"
    if ws.prior_tax is not None and ws.prior_share is not None:
        basis += (f" or {ws.prior_share:.0%} of last year's (${ws.prior_tax:,.0f}, from {ws.prior_basis})"
                  f", whichever is smaller")
    else:
        basis += "; type last year's tax in the Profile, or add last year's tab, for the prior-year safe harbor"
    notes.append(f"Required annual payment ${rap:,.0f}: {basis}. "
                 f"Four equal payments would be ${rap / 4:,.0f} each.")
    if st is not None and ss is not None and ws.state_annualized_required:
        pcts = "/".join(f"{p:.1%}".replace(".0%", "%") for p in ss.annualized_percentages)
        row(f"{st.name} required by due date, annualized method", [*ws.state_annualized_required, None],
            "guidance", textwrap.fill(f"The state's own percentages ({pcts}) of the annualized tax. "
                                      "State safe-harbor rules aren't modeled yet.", TIP_WIDTH))
        row(f"{st.name} counted as paid by due date", [*ws.state_credited, None], "guidance")
        notes.extend(st.estimated_payments.notes if st.estimated_payments else ())
    notes.extend(ws.notes)
    return lines, notes


class Grid(QTableWidget):
    """A read-only grid with styled rows."""

    def __init__(self, name: str) -> None:
        super().__init__(0, COLUMN_COUNT + 1)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAccessibleName(name)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, COLUMN_COUNT + 1):
            self.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.setWordWrap(False)
        self.setAlternatingRowColors(False)
        self.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 6)

    def show_lines(self, titles: list[str], lines: list[Line]) -> None:
        self.setHorizontalHeaderLabels(["", *titles])
        self.setRowCount(len(lines))
        dim = self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        for r, line in enumerate(lines):
            for c, text in enumerate([line.label, *line.cells]):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                font = QFont(self.font())
                if line.style in ("heading", "total"):
                    font.setBold(True)
                if line.style in ("guidance", "info"):
                    item.setForeground(dim)
                if line.style == "guidance":
                    font.setItalic(True)
                item.setFont(font)
                if line.tip:
                    item.setToolTip(line.tip)
                self.setItem(r, c, item)


class YearPage(QTabWidget):
    """One year's subtabs."""

    def __init__(self) -> None:
        super().__init__()
        self.worksheet = Grid("Worksheet")
        self.notes = QLabel()
        self.notes.setWordWrap(True)
        self.notes.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notes.setAccessibleName("Worksheet notes")
        notes_area = QScrollArea()
        notes_area.setWidget(self.notes)
        notes_area.setWidgetResizable(True)
        notes_area.setFrameShape(QFrame.Shape.NoFrame)
        notes_area.setMaximumHeight(7 * self.fontMetrics().height())
        sheet = QWidget()
        lay = QVBoxLayout(sheet)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.worksheet, 1)
        lay.addWidget(notes_area)
        self.detail = Grid("Calculation detail")
        self.addTab(sheet, "&Worksheet")
        self.addTab(self.detail, "Calculation &detail")
        self.worksheet_widget = sheet

    def show_worksheet(self, ws: Worksheet) -> None:
        lines, notes = worksheet_lines(ws)
        self.worksheet.show_lines(column_titles(ws), lines)
        self.notes.setText("\n".join(notes))
        self.detail.show_lines(column_titles(ws), detail_lines(ws))

    def show_problem(self, text: str) -> None:
        self.worksheet.show_lines(list(COLUMN_TITLES), [Line(text, [""] * COLUMN_COUNT, "info")])
        self.notes.setText("")
        self.detail.setRowCount(0)


def detail_lines(ws: Worksheet) -> list[Line]:
    out = []
    for label, values in detail_rows(ws):
        if label.endswith(":") and not values:
            out.append(Line(label[:-1], [""] * COLUMN_COUNT, "heading"))
            if ws.state is not None and label == f"{ws.state.name}:":
                ends = ws.state_schedule.period_ends
                out.append(Line("Income through", [f"{d:%b} {d.day}" for d in ends] + ["Plan"], "info"))
            continue
        cells = []
        for v in values:
            if v is None:
                cells.append("")
            elif "factor" in label:
                cells.append(f"{v:g}")
            else:
                cells.append(money(v))
        out.append(Line(label, cells))
    return out
