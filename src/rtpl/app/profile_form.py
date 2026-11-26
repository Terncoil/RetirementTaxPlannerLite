"""Profile editor: the baseline financial starting point.

Built from standard Qt form widgets with real labels (``setBuddy``) and
accessible names, so screen readers and keyboard users get the same form
as everyone else.
"""

from __future__ import annotations

import textwrap
from typing import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from rtpl.app import TIP_WIDTH
from rtpl.app.inputs import ComboBox, DoubleSpinBox, SpinBox
from rtpl.model.profile import ACA_PLAN_TIERS, FILING_STATUS_LABELS, Household, Person, Profile
from rtpl.taxlaw.loader import FederalLaw, StateLaw, status_key

PERSON_MONEY = [
    ("social_security", "Social Security"),
    ("wages", "Wages"),
    ("pension", "Pension"),
    ("ira_distributions", "IRA / 401(k) withdrawals"),
]

HOUSEHOLD_MONEY = [
    ("taxable_interest", "Taxable interest"),
    ("tax_exempt_interest", "Tax-exempt interest"),
    ("qualified_dividends", "Qualified dividends"),
    ("nonqualified_dividends", "Non-qualified dividends"),
    ("long_term_capital_gains", "Net long-term capital gains"),
    ("short_term_capital_gains", "Net short-term capital gains"),
    ("other_ordinary_income", "Other ordinary income"),
    ("other_investment_income", "Rental / passive income"),
    ("state_investment_income_adjustment", "State interest & dividends adjustment"),
    ("adjustments", "Above-the-line adjustments"),
    ("itemized_deductions", "Itemized deductions"),
    ("charitable_cash_gifts", "Cash gifts to charity"),
]

# Monthly ACA marketplace premiums, per person.
PERSON_ACA = [
    ("aca_benchmark_premium", "ACA benchmark Silver (monthly)",
     "This person's monthly premium for the benchmark plan: the\n"
     "second-lowest-cost Silver plan, before any subsidy. Healthcare.gov's\n"
     "window shopping shows it. The premium tax credit is worked out from\n"
     "it whatever plan you buy. Leave at $0 if not on the marketplace;\n"
     "it stops at Medicare (65)."),
    ("aca_plan_premium", "ACA plan you'd buy (monthly)",
     "Monthly premium, before any subsidy, of the plan this person would\n"
     "buy (for example a Bronze plan). The credit can't be more than it.\n"
     "Leave at $0 to use the benchmark premium."),
]

def _fill(*paragraphs: str) -> str:
    return "\n".join(textwrap.fill(p, TIP_WIDTH) for p in paragraphs)


def charity_tip(law: FederalLaw, filing_status: str) -> str:
    """Tooltip for cash gifts, with the year's cap for this filing status."""
    cap = law.charitable_nonitemizer_cap(filing_status)
    label = FILING_STATUS_LABELS.get(filing_status, filing_status).lower()
    if cap <= 0:
        return _fill(
            f"Cash gifts to charity this year. In {law.year} there is no deduction "
            "for them with the standard deduction; include them in itemized "
            "deductions if you itemize.")
    return _fill(
        f"Cash gifts to charity this year. If you take the standard deduction "
        f"you can also deduct up to ${cap:,.0f} of them in {law.year} ({label}); "
        "anything over that has no effect.",
        "It lowers taxable income but not AGI, so Social Security, IRMAA and "
        "ACA amounts don't change. Gifts to donor-advised funds don't count. "
        "State taxes ignore it. Ignored if you itemize, so include them in "
        "itemized deductions.")


STATE_ADJUSTMENT_KEY = "state_investment_income_adjustment"


def state_adjustment_tip(state: StateLaw | None, year: int) -> str:
    """Tooltip for the state interest and dividends adjustment, naming the
    year's tax from the state's law file."""
    tax = state.investment_income_tax if state is not None else None
    if tax is None:
        where = f"{state.name} in {year}" if state is not None else "no state"
        return _fill(
            "Used only where a state taxes interest and dividends on their own, "
            f"with no tax on wages. Not used for {where}.")
    kinds = {"interest": "taxable interest", "tax_exempt_interest": "tax-exempt interest",
             "dividends": "dividends"}
    taxed = ", ".join(kinds[k] for k in tax.income)
    return _fill(
        f"Changes the income {state.name}'s {tax.name} ({year}, {tax.rate:.0%}) "
        f"is figured on: this year's {taxed}, plus this amount.",
        "Enter a negative amount for interest the state exempts but the profile "
        "includes (U.S. Treasury interest, the state's own municipal bonds), and "
        "a positive amount for interest it taxes that the profile leaves out "
        "(for example corporate bond interest, where bank interest is exempt). "
        "Federal tax, other states and the forward plan ignore it.")


def stcg_tip(law: FederalLaw, filing_status: str) -> str:
    """Tooltip for short-term gains, with the year's capital loss limit."""
    limit = law.capital_loss_limit[status_key(filing_status)]
    label = FILING_STATUS_LABELS.get(filing_status, filing_status).lower()
    return _fill(
        "Taxed as ordinary income. Enter a net loss as a negative amount.",
        f"A loss first offsets the other term's gains; at most ${limit:,.0f} "
        f"of a net loss reduces other income in {law.year} ({label}). "
        "Carrying the rest to later years is not modeled.")


MONEY_TIPS = {
    "social_security": "Yearly Social Security benefit, before Medicare premiums.",
    "itemized_deductions": (
        "Leave at $0 to use the standard deduction. If you itemize,\n"
        "include your charitable gifts here."),
    "charitable_cash_gifts": (
        "Cash gifts to charity this year. From 2026, if you take the\n"
        "standard deduction you can also deduct some of them, up to a\n"
        "yearly cap. Ignored if you itemize, so include them in itemized\n"
        "deductions."),
    "long_term_capital_gains": "Enter a net loss as a negative amount.",
    # The amounts in these two tips come from the year's tax law; see
    # ProfileForm.update_law_tips. These are the wording without a law.
    "short_term_capital_gains": (
        "Taxed as ordinary income. Enter a net loss as a negative amount.\n"
        "A loss first offsets the other term's gains; a net loss reduces\n"
        "other income by at most the yearly limit. Carrying the rest to\n"
        "later years is not modeled."),
}

# Amounts that may be entered as negative: gains (a loss) and adjustments.
GAIN_KEYS = ("long_term_capital_gains", "short_term_capital_gains")
SIGNED_KEYS = (*GAIN_KEYS, "state_investment_income_adjustment")

# Widest a number field needs to be: "$10,000,000" plus the arrows.
FIELD_WIDTH = 124

MEDICARE_CHOICES = [("Automatic (age 65+)", None), ("Yes", True), ("No", False)]


def money_box(allow_negative: bool = False) -> DoubleSpinBox:
    box = DoubleSpinBox()
    box.setPrefix("$")
    box.setDecimals(0)
    box.setRange(-10_000_000 if allow_negative else 0, 10_000_000)
    box.setSingleStep(1000)
    box.setGroupSeparatorShown(True)
    box.setAccelerated(True)
    box.setMaximumWidth(FIELD_WIDTH)
    return box


def compact_form(form: QFormLayout) -> QFormLayout:
    """Labels left, fields only as wide as they need, long labels wrap above."""
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    form.setHorizontalSpacing(8)
    return form


def _set_tip(widget: QWidget, tip: str) -> None:
    widget.setToolTip(tip)
    widget.setAccessibleDescription(tip)


def _add_row(form: QFormLayout, text: str, widget: QWidget, tip: str | None = None) -> None:
    label = QLabel(text)
    label.setBuddy(widget)
    widget.setAccessibleName(text.replace("&", ""))
    if tip:
        _set_tip(widget, tip)
    form.addRow(label, widget)


class PersonBox(QGroupBox):
    changed = Signal()

    def __init__(self, title: str) -> None:
        super().__init__(title)
        form = compact_form(QFormLayout(self))
        self.name = QLineEdit()
        self.name.setMinimumWidth(FIELD_WIDTH)
        self.birth_year = SpinBox()
        self.birth_year.setRange(1900, 2100)
        self.birth_year.setMaximumWidth(80)
        self.age = QLabel()
        self.age.setAccessibleName("Calculated age")
        self.life = SpinBox()
        self.life.setRange(50, 120)
        self.life.setMaximumWidth(80)
        self._tax_year = 2026
        self.medicare = ComboBox()
        for text, _ in MEDICARE_CHOICES:
            self.medicare.addItem(text)
        self.blind = QCheckBox()
        _add_row(form, "Name", self.name)
        birth_label = QLabel("Birth year")
        birth_label.setBuddy(self.birth_year)
        self.birth_year.setAccessibleName("Birth year")
        form.addRow(birth_label, self.birth_year)
        form.addRow("", self.age)
        _add_row(form, "Life expectancy", self.life,
                 "Age at death in the forward plan; the survivor files single after,\n"
                 "and the plan ends when the last person dies. Each plan keeps its\n"
                 "own copy. Not used by the marginal graph.")
        _add_row(
            form,
            "Medicare Part B",
            self.medicare,
            "Choose Yes for Medicare before 65 (for example on SSDI),\nso IRMAA applies.",
        )
        _add_row(form, "Legally blind", self.blind)
        self.money: dict[str, DoubleSpinBox] = {}
        for key, text in PERSON_MONEY:
            box = money_box()
            self.money[key] = box
            _add_row(form, text, box, MONEY_TIPS.get(key))
            box.valueChanged.connect(self.changed)
        for key, text, tip in PERSON_ACA:
            box = money_box()
            box.setSingleStep(50)
            self.money[key] = box
            _add_row(form, text, box, tip)
            box.valueChanged.connect(self.changed)
        self.name.textChanged.connect(self.changed)
        self.birth_year.valueChanged.connect(self.changed)
        self.birth_year.valueChanged.connect(self._update_age)
        self.life.valueChanged.connect(self.changed)
        self.medicare.currentIndexChanged.connect(self.changed)
        self.blind.toggled.connect(self.changed)

    def set_tax_year(self, year: int) -> None:
        self._tax_year = year
        self._update_age()

    def _update_age(self, *_args) -> None:
        age = self._tax_year - self.birth_year.value()
        self.age.setText(f"Age {age} at end of {self._tax_year}")

    def set_person(self, p: Person) -> None:
        self.name.setText(p.name)
        self.birth_year.setValue(p.birth_year)
        self.life.setValue(p.life_expectancy)
        self.medicare.setCurrentIndex([v for _, v in MEDICARE_CHOICES].index(p.medicare_part_b))
        self.blind.setChecked(p.blind)
        for key, box in self.money.items():
            box.setValue(getattr(p, key))

    def person(self) -> Person:
        return Person(
            name=self.name.text() or self.title(),
            birth_year=self.birth_year.value(),
            life_expectancy=self.life.value(),
            medicare_part_b=MEDICARE_CHOICES[self.medicare.currentIndex()][1],
            blind=self.blind.isChecked(),
            **{k: b.value() for k, b in self.money.items()},
        )


class ProfileForm(QWidget):
    """Edits a :class:`Profile`; emits ``changed`` on every edit.

    ``law_for`` gives the federal law for a tax year, so tooltips can show
    that year's amounts (such as the charitable deduction cap). Without it
    they keep their general wording.
    """

    changed = Signal()

    def __init__(
        self,
        states: list[str],
        law_for: Callable[[int], FederalLaw] | None = None,
        state_for: Callable[[str, int], StateLaw | None] | None = None,
    ) -> None:
        super().__init__()
        self._loading = False
        self._law_for = law_for
        self._state_for = state_for
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        basics = QGroupBox("Baseline")
        form = compact_form(QFormLayout(basics))
        self.plan_name = QLineEdit()
        self.plan_name.setMinimumWidth(FIELD_WIDTH)
        self.year = SpinBox()
        self.year.setRange(2020, 2100)  # the earliest federal data file
        self.year.setMaximumWidth(80)
        self.filing = ComboBox()
        for key, text in FILING_STATUS_LABELS.items():
            self.filing.addItem(text, key)
        self.state = ComboBox()
        self.state.addItem("(none)", None)
        for code in states:
            self.state.addItem(code, code)
        _add_row(form, "Plan name", self.plan_name)
        _add_row(form, "Tax year", self.year)
        _add_row(form, "Filing status", self.filing)
        _add_row(form, "State of residence", self.state, "Optional. Adds a state tax layer.")
        self.inflation = DoubleSpinBox()
        self.inflation.setSuffix("%")
        self.inflation.setDecimals(2)
        self.inflation.setRange(0.0, 15.0)
        self.inflation.setSingleStep(0.1)
        self.inflation.setMaximumWidth(90)
        _add_row(
            form,
            "Inflation rate",
            self.inflation,
            "Yearly inflation used to project tax law for years with no data\n"
            "yet, such as the IRMAA brackets two years ahead. The forward plan\n"
            "uses it too.",
        )
        outer.addWidget(basics)

        self.person_boxes = [PersonBox("Person 1"), PersonBox("Person 2 (spouse)")]
        for box in self.person_boxes:
            outer.addWidget(box)
            box.changed.connect(self._emit)

        hh = QGroupBox("Household income and settings")
        hform = compact_form(QFormLayout(hh))
        self.household_money: dict[str, DoubleSpinBox] = {}
        for key, text in HOUSEHOLD_MONEY:
            box = money_box(allow_negative=key in SIGNED_KEYS)
            self.household_money[key] = box
            _add_row(hform, text, box, MONEY_TIPS.get(key))
            box.valueChanged.connect(self._emit)
        # Check boxes go in the field column with their label on the left,
        # like every other row.
        self.brokerage = QCheckBox()
        self.aca = QCheckBox()
        self.household_size = SpinBox()
        self.household_size.setRange(0, 20)
        self.household_size.setSpecialValueText("Automatic")
        self.household_size.setMaximumWidth(FIELD_WIDTH)
        self.aca_tier = ComboBox()
        for key, text in ACA_PLAN_TIERS.items():
            self.aca_tier.addItem(text, key)
        _add_row(hform, "Taxable brokerage account", self.brokerage,
                 "The household has a taxable brokerage account.")
        _add_row(hform, "ACA marketplace coverage", self.aca,
                 "The household buys health coverage on an ACA marketplace.")
        _add_row(hform, "ACA plan tier", self.aca_tier,
                 "Metal tier of the marketplace plan. Cost-sharing reductions (the\n"
                 "250% FPL line) only come with a Silver plan.")
        _add_row(hform, "Household size (for FPL)", self.household_size,
                 "People in the tax household, dependents included, for the\n"
                 "poverty line the ACA lines and premium tax credit use.")
        outer.addWidget(hh)

        edu = QGroupBox("Education credits")
        eform = compact_form(QFormLayout(edu))
        self.aotc_students = SpinBox()
        self.aotc_students.setRange(0, 10)
        self.aotc_students.setMaximumWidth(80)
        self.aotc_expenses = money_box()
        self.aotc_expenses.setSingleStep(500)
        self.llc_expenses = money_box()
        self.education_last = SpinBox()
        self.education_last.setRange(2019, 2100)
        self.education_last.setSpecialValueText("This year only")
        self.education_last.setMaximumWidth(FIELD_WIDTH)
        _add_row(eform, "College students (AOTC)", self.aotc_students,
                 "Students in their first four years of college that you claim\n"
                 "the American Opportunity credit for: up to $2,500 each, 40% of\n"
                 "it refundable. Phases out from MAGI $80,000 ($160,000 joint).")
        _add_row(eform, "Expenses per student", self.aotc_expenses,
                 "Qualified tuition, fees and books for each student, after\n"
                 "grants and scholarships. $4,000 or more gets the full credit.")
        _add_row(eform, "Lifetime Learning expenses", self.llc_expenses,
                 "Qualified expenses for other students (graduate school, or\n"
                 "beyond four years): 20% of up to $10,000 a return, not refundable.")
        _add_row(eform, "Claimed through", self.education_last,
                 "Last year the forward plan claims these credits.")
        outer.addWidget(edu)
        outer.addStretch(1)

        self.plan_name.textChanged.connect(self._emit)
        self.year.valueChanged.connect(self._year_changed)
        self.filing.currentIndexChanged.connect(self._filing_changed)
        self.state.currentIndexChanged.connect(self._state_changed)
        self.inflation.valueChanged.connect(self._emit)
        self.brokerage.toggled.connect(self._emit)
        self.aca.toggled.connect(self._emit)
        self.aca_tier.currentIndexChanged.connect(self._emit)
        self.household_size.valueChanged.connect(self._emit)
        self.aotc_students.valueChanged.connect(self._emit)
        self.aotc_expenses.valueChanged.connect(self._emit)
        self.llc_expenses.valueChanged.connect(self._emit)
        self.education_last.valueChanged.connect(self._emit)

    def _emit(self, *_args) -> None:
        if not self._loading:
            self.changed.emit()

    def _state_changed(self, *_args) -> None:
        self.update_law_tips()
        self._emit()

    def update_state_tip(self) -> None:
        """Name the year's state tax on interest and dividends, if any, and
        enable the adjustment only where one applies."""
        box = self.household_money[STATE_ADJUSTMENT_KEY]
        code, year = self.state.currentData(), self.year.value()
        state = None
        if code and self._state_for is not None:
            try:
                state = self._state_for(code, year)
            except (KeyError, ValueError):
                state = None
        _set_tip(box, state_adjustment_tip(state, year))
        # Keep a value the user entered visible even where it does nothing.
        box.setEnabled(self._state_for is None or (state is not None and state.taxes_investment_income)
                       or box.value() != 0)

    def update_law_tips(self) -> None:
        """Show the tax year's own amounts in the tooltips that quote them."""
        self.update_state_tip()
        if self._law_for is None:
            return
        try:
            law = self._law_for(self.year.value())
        except (KeyError, ValueError):
            return
        fs = self.filing.currentData()
        _set_tip(self.household_money["charitable_cash_gifts"], charity_tip(law, fs))
        _set_tip(self.household_money["short_term_capital_gains"], stcg_tip(law, fs))

    def _year_changed(self, year: int) -> None:
        for box in self.person_boxes:
            box.set_tax_year(year)
        # The lowest value of "Claimed through" reads "This year only".
        last = self.education_last
        keep = last.value() if last.value() > last.minimum() else None
        last.blockSignals(True)
        last.setMinimum(year - 1)
        last.setValue(keep if keep is not None and keep >= year else year - 1)
        last.blockSignals(False)
        self.update_law_tips()
        self._emit()

    def _filing_changed(self, *_args) -> None:
        married = self.filing.currentData() in ("mfj", "mfs")
        self.person_boxes[1].setVisible(married)
        self.update_law_tips()
        self._emit()

    def set_profile(self, profile: Profile) -> None:
        self._loading = True
        try:
            self.plan_name.setText(profile.name)
            self.year.setValue(profile.year)
            self.filing.setCurrentIndex(max(0, self.filing.findData(profile.filing_status)))
            self.state.setCurrentIndex(max(0, self.state.findData(profile.state)))
            self.inflation.setValue(profile.inflation * 100)
            people = list(profile.people) + [Person(name="Person 2")] * (2 - len(profile.people))
            for box, person in zip(self.person_boxes, people):
                box.set_person(person)
                box.set_tax_year(profile.year)
            for key, box in self.household_money.items():
                box.setValue(getattr(profile.household, key))
            self.brokerage.setChecked(profile.household.has_taxable_brokerage)
            self.aca.setChecked(profile.household.aca_marketplace)
            h = profile.household
            self.household_size.setValue(h.household_size or 0)
            self.aca_tier.setCurrentIndex(max(0, self.aca_tier.findData(h.aca_plan_tier)))
            self.aotc_students.setValue(h.aotc_students)
            self.aotc_expenses.setValue(h.aotc_expenses_per_student)
            self.llc_expenses.setValue(h.llc_expenses)
            self.education_last.setMinimum(profile.year - 1)
            self.education_last.setValue(h.education_last_year or profile.year - 1)
            self.person_boxes[1].setVisible(profile.filing_status in ("mfj", "mfs"))
        finally:
            self._loading = False
        self.update_law_tips()
        self.changed.emit()

    def profile(self) -> Profile:
        fs = self.filing.currentData()
        n = 2 if fs in ("mfj", "mfs") else 1
        household = Household(
            **{k: b.value() for k, b in self.household_money.items()},
            has_taxable_brokerage=self.brokerage.isChecked(),
            aca_marketplace=self.aca.isChecked(),
            household_size=self.household_size.value() or None,
            aca_plan_tier=self.aca_tier.currentData(),
            aotc_students=self.aotc_students.value(),
            aotc_expenses_per_student=self.aotc_expenses.value(),
            llc_expenses=self.llc_expenses.value(),
            education_last_year=(None if self.education_last.value() <= self.education_last.minimum()
                                 else self.education_last.value()),
        )
        return Profile(
            name=self.plan_name.text() or "My plan",
            year=self.year.value(),
            filing_status=fs,
            state=self.state.currentData(),
            people=[b.person() for b in self.person_boxes[:n]],
            household=household,
            inflation=round(self.inflation.value() / 100, 4),
        )
