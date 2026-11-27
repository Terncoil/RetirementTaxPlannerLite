"""The EQTA worksheet: tax, payments and amount owed for each period.

Each column of the worksheet runs ``compute_tax`` on a profile built from
the year's profile plus the income sources totalled through that column's
period end (or their annual plan amounts, for the Plan column):

* **Income** counts in a column when it is dated from January 1 through the
  period end: March 31, May 31, August 31 and December 31 for the federal lines.
  The state lines use the state's own period ends from its law file
  (Virginia's first period runs to April 30).
* **Estimated tax** is the tax on that year-to-date income as if the year
  ended on that day: the pay-as-you-go view.
* **Paid** is each payment register's total through the period's due
  date (the state's own date for the state lines; where the state has no
  installment for a period, the federal due date).
* **Owed** is estimated tax less paid.

Guidance, for information only:

* Federal annualized income installment method (Form 2210 Schedule AI):
  income through each period end annualized (x 12 / months), taxed, then
  multiplied by the schedule's percentage (22.5%, 45%, 67.5%, 90%). Each
  required installment is the smaller of that, less earlier installments,
  and the regular installment plus what earlier periods left unused.
* Federal regular installments: the required annual payment (the smaller
  of 90% of this year's tax and 100% or 110% of last year's, from the
  federal law file's ``[estimated_payments.safe_harbor]``) times the
  share due by each date.
* What counts as paid by each due date for the penalty: estimated
  payments by their date, and withholding spread over the due dates in
  the schedule's shares (the IRS default, IRC 6654(g)).
* State annualized amounts use the state's own percentages (California's
  30/40/0/30 gives 27%, 63%, 63%, 90%). State safe-harbor rules are not
  modeled yet.
"""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass, field

from rtpl.engine.tax import TaxResult, compute_tax
from rtpl.model.eqta import PAYMENT_LINES, TAX_TYPES, EqtaFile, TaxYear
from rtpl.model.profile import Household, Profile
from rtpl.taxlaw.estimated import PERIOD_ENDS, EstimatedPayments
from rtpl.taxlaw.loader import FederalLaw, StateLaw, TaxLawLibrary, status_key

PERIODS = 4
PLAN = PERIODS  # index of the Plan column
COLUMN_COUNT = PERIODS + 1

FEDERAL_PAYMENTS = ("federal_withholding", "federal_estimated")
STATE_PAYMENTS = ("state_withholding", "state_estimated")


def months_in_period(end: dt.date) -> int:
    """Whole months from January 1 through ``end`` (a month end)."""
    return end.month


def build_profile(ty: TaxYear, amounts: list[float], factor: float = 1.0) -> Profile:
    """The year's profile with each source's amount (times ``factor``) in
    the profile field for its tax type."""
    p = copy.deepcopy(ty.profile)
    p.year = ty.year
    for person in p.people:
        for attr in ("social_security", "wages", "pension", "ira_distributions"):
            setattr(person, attr, 0.0)
    # Only the registers supply income and deductions; the rest of RTPL's
    # household inputs (ACA, education credits) stay at their defaults.
    h = Household()
    p.household = h
    for src, amount in zip(ty.sources, amounts):
        amount *= factor
        t = src.type
        if t.field == "split_dividends":
            share = src.qualified_share if src.qualified_share is not None else 0.0
            h.qualified_dividends += amount * share
            h.nonqualified_dividends += amount * (1 - share)
        elif t.per_person:
            person = p.people[src.person_index(len(p.people))]
            setattr(person, t.field, getattr(person, t.field) + amount)
        else:
            setattr(h, t.field, getattr(h, t.field) + amount)
    if h.long_term_capital_gains or h.short_term_capital_gains or h.qualified_dividends:
        h.has_taxable_brokerage = True
    return p


@dataclass
class StateSchedule:
    """The state's periods as the worksheet uses them."""

    period_ends: tuple[dt.date, ...]  # one per worksheet column (4)
    due_dates: tuple[dt.date | None, ...]  # None = nothing due for that period
    differs: tuple[bool, ...]  # due date differs from the federal one
    period_differs: tuple[bool, ...]  # period end differs from the federal one
    cumulative_shares: tuple[float, ...]
    annualized_percentages: tuple[float, ...]
    annual_only: bool
    required: bool
    notes: tuple[str, ...]


@dataclass
class Worksheet:
    ty: TaxYear
    law: FederalLaw
    state: StateLaw | None  # None when the year has no state or it taxes no income
    period_ends: tuple[dt.date, ...]  # federal
    due_dates: tuple[dt.date | None, ...]  # federal
    # [column][source] amounts for the federal columns (YTD at each period end, then plan)
    amounts: list[list[float]]
    federal: list[TaxResult]  # per column
    state_schedule: StateSchedule | None = None
    state_amounts: list[list[float]] = field(default_factory=list)
    state_results: list[TaxResult] = field(default_factory=list)
    # paid[line][column]
    paid: dict[str, list[float]] = field(default_factory=dict)
    paid_through: dict[str, list[dt.date | None]] = field(default_factory=dict)
    # guidance
    current_tax: float = 0.0  # this year's federal tax used for the safe harbor
    current_tax_basis: str = ""
    prior_tax: float | None = None
    prior_agi: float | None = None
    prior_basis: str = ""
    required_annual_payment: float = 0.0
    prior_share: float | None = None
    annualized_factors: tuple[float, ...] = ()
    annualized_tax: list[float] = field(default_factory=list)  # federal, per period
    annualized_installments: list[float] = field(default_factory=list)  # Sched. AI line 27
    annualized_required: list[float] = field(default_factory=list)  # cumulative
    regular_required: list[float] = field(default_factory=list)  # cumulative
    credited: list[float] = field(default_factory=list)  # paid for the penalty, cumulative
    short: list[float] = field(default_factory=list)
    penalty_floor: float = 0.0
    state_annualized_factors: tuple[float, ...] = ()
    state_annualized_tax: list[float] = field(default_factory=list)
    state_annualized_required: list[float] = field(default_factory=list)
    state_credited: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def federal_tax(self) -> list[float]:
        return [r.federal_tax for r in self.federal]

    @property
    def state_tax(self) -> list[float]:
        return [r.state_tax for r in self.state_results]

    def paid_total(self, lines) -> list[float]:
        return [round(sum(self.paid[k][c] for k in lines), 2) for c in range(COLUMN_COUNT)]

    @property
    def federal_owed(self) -> list[float]:
        paid = self.paid_total(FEDERAL_PAYMENTS)
        return [round(t - p, 2) for t, p in zip(self.federal_tax, paid)]

    @property
    def state_owed(self) -> list[float]:
        paid = self.paid_total(STATE_PAYMENTS)
        return [round(t - p, 2) for t, p in zip(self.state_tax, paid)]


def _amounts(ty: TaxYear, end: dt.date | None) -> list[float]:
    """Each source's plan amount (``end`` None), or its entries from January 1
    through ``end``."""
    if end is None:
        return [s.plan for s in ty.sources]
    start = dt.date(ty.year, 1, 1)
    return [s.total_through(end, start) for s in ty.sources]


def _tax(ty: TaxYear, amounts, law, state, factor: float = 1.0) -> TaxResult:
    return compute_tax(build_profile(ty, amounts, factor), law, state)


def _state_schedule(st: StateLaw, fed: EstimatedPayments, year: int) -> StateSchedule:
    sched = st.estimated_payments
    fed_ends = fed.period_ends(year)
    fed_due = fed.due_dates(year)
    if sched is None or not sched.installments:
        return StateSchedule(fed_ends, (None,) * 4, (False,) * 4, (False,) * 4, (0, 0, 0, 0),
                             (0, 0, 0, 0), False, bool(sched and sched.required), sched.notes if sched else ())
    if len(sched.installments) == 1:
        # Paid once with the return (Washington): YTD columns use the
        # federal period ends, and only the year-end column has a due date.
        due = sched.due_dates(year)[0]
        return StateSchedule(
            fed_ends, (None, None, None, due), (False, False, False, due != fed_due[3]), (False,) * 4,
            (0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0, 0.9), True, sched.required, sched.notes)
    ends = sched.period_ends(year)
    return StateSchedule(
        ends, sched.due_dates(year), sched.differs_from(fed, year),
        tuple(a != b for a, b in zip(ends, fed_ends)), sched.cumulative_shares,
        sched.annualized_percentages(), False, sched.required, sched.notes)


def prior_year_figures(eq: EqtaFile | None, ty: TaxYear, lib: TaxLawLibrary) -> tuple[float | None, float | None, str]:
    """Last year's federal tax and AGI: typed in the year's profile, or else
    the tax on last year's tab at December 31."""
    if ty.prior_year_tax is not None:
        return ty.prior_year_tax, ty.prior_year_agi, "typed in the year's profile"
    prior = eq.prior_year(ty) if eq is not None else None
    if prior is None:
        return None, ty.prior_year_agi, ""
    end = dt.date(prior.year, 12, 31)
    res = _tax(prior, _amounts(prior, end), lib.federal(prior.year), None)
    agi = ty.prior_year_agi if ty.prior_year_agi is not None else res.agi
    return max(0.0, res.federal_tax), agi, f"the {prior.title} tab at December 31"


def compute_worksheet(ty: TaxYear, lib: TaxLawLibrary, eq: EqtaFile | None = None) -> Worksheet:
    year = ty.year
    law = lib.federal(year)
    fed = law.estimated_payments
    if fed is None:
        raise ValueError(f"The {year} federal law file has no [estimated_payments] table")
    st = lib.state(ty.profile.state, year) if ty.profile.state else None
    if st is not None and not st.has_any_tax:
        st = None
    ends = fed.period_ends(year)
    due = fed.due_dates(year)
    cols: list[dt.date | None] = [*ends, None]
    amounts = [_amounts(ty, e) for e in cols]
    federal = [_tax(ty, a, law, None) for a in amounts]
    ws = Worksheet(ty=ty, law=law, state=st, period_ends=ends, due_dates=due, amounts=amounts, federal=federal)

    # -- state columns, on the state's own period ends --------------------
    if st is not None:
        ss = _state_schedule(st, fed, year)
        ws.state_schedule = ss
        ws.state_amounts = [_amounts(ty, e) for e in [*ss.period_ends, None]]
        ws.state_results = [_tax(ty, a, law, st) for a in ws.state_amounts]

    # -- paid ---------------------------------------------------------------
    for key in PAYMENT_LINES:
        line = ty.payments[key]
        if key in STATE_PAYMENTS:
            if st is None:
                continue
            through = [d or f for d, f in zip(ws.state_schedule.due_dates, due)]
        else:
            through = list(due)
        through = [d or dt.date(year + 1, 1, 15) for d in through]
        ws.paid_through[key] = [*through, None]
        ws.paid[key] = [line.total_through(d, dt.date(year, 1, 1)) for d in through] + [line.plan]

    _federal_guidance(ws, eq, lib)
    if st is not None:
        _state_guidance(ws)
    return ws


def _federal_guidance(ws: Worksheet, eq: EqtaFile | None, lib: TaxLawLibrary) -> None:
    ty, law = ws.ty, ws.law
    fed = law.estimated_payments
    sh = fed.safe_harbor
    current_share = sh.current_year_share if sh else 0.90
    fs = status_key(ty.profile.filing_status)
    has_plan = any(s.plan_text for s in ty.sources)
    if has_plan:
        ws.current_tax, ws.current_tax_basis = max(0.0, ws.federal[PLAN].federal_tax), "the Plan column"
    else:
        ws.current_tax, ws.current_tax_basis = max(0.0, ws.federal[3].federal_tax), "YTD December 31"
    ws.prior_tax, ws.prior_agi, ws.prior_basis = prior_year_figures(eq, ty, lib)
    rap = current_share * ws.current_tax
    if sh is not None and ws.prior_tax is not None:
        ws.prior_share = sh.prior_share(ws.prior_agi, fs)
        rap = min(rap, ws.prior_share * ws.prior_tax)
    ws.required_annual_payment = round(rap, 2)
    ws.penalty_floor = sh.penalty_floor if sh else 0.0

    # Schedule AI
    pcts = fed.annualized_percentages(current_share)
    shares = fed.shares
    ws.annualized_factors = tuple(12 / months_in_period(e) for e in ws.period_ends)
    prior_sum = carry = 0.0
    cumulative = 0.0
    for i in range(PERIODS):
        res = _tax(ty, ws.amounts[i], law, None, ws.annualized_factors[i])
        ai_tax = max(0.0, res.federal_tax)
        ws.annualized_tax.append(round(ai_tax, 2))
        line24 = max(0.0, ai_tax * pcts[i] - prior_sum)
        line25 = shares[i] * rap
        line27 = min(line24, line25 + carry)
        carry = line25 + carry - line27
        prior_sum += line27
        cumulative += line27
        ws.annualized_installments.append(round(line27, 2))
        ws.annualized_required.append(round(cumulative, 2))
    ws.regular_required = [round(c * rap, 2) for c in fed.cumulative_shares]

    withheld = ty.payments["federal_withholding"].total
    est = ty.payments["federal_estimated"]
    cum_shares = fed.cumulative_shares
    for i in range(PERIODS):
        through = ws.paid_through["federal_estimated"][i]
        ws.credited.append(round(est.total_through(through, dt.date(ty.year, 1, 1)) + withheld * cum_shares[i], 2))
    ws.short = [round(max(0.0, r - c), 2) for r, c in zip(ws.annualized_required, ws.credited)]
    if ws.penalty_floor and ws.current_tax - withheld < ws.penalty_floor:
        ws.notes.append(
            f"No federal underpayment penalty applies when the tax not covered by withholding "
            f"is under ${ws.penalty_floor:,.0f}.")


def _state_guidance(ws: Worksheet) -> None:
    ss = ws.state_schedule
    ty, law, st = ws.ty, ws.law, ws.state
    ws.state_annualized_factors = tuple(12 / months_in_period(e) for e in ss.period_ends)
    best = 0.0
    for i in range(PERIODS):
        res = _tax(ty, ws.state_amounts[i], law, st, ws.state_annualized_factors[i])
        ai = max(0.0, res.state_tax)
        ws.state_annualized_tax.append(round(ai, 2))
        best = max(best, ai * ss.annualized_percentages[i])
        ws.state_annualized_required.append(round(best, 2) if ss.cumulative_shares[i] or i == 3 else 0.0)
    withheld = ty.payments["state_withholding"].total
    est = ty.payments["state_estimated"]
    for i in range(PERIODS):
        through = ws.paid_through["state_estimated"][i]
        ws.state_credited.append(
            round(est.total_through(through, dt.date(ty.year, 1, 1)) + withheld * ss.cumulative_shares[i], 2))


def worksheet_groups(ty: TaxYear) -> list[tuple[str, list[tuple[int, float | None]]]]:
    """Worksheet income groups in tax-type order: (label, [(source index,
    share of its amount shown)]). A source of dividends to split is shown in
    both dividend groups with its estimated share; others have share None."""
    groups: list[tuple[str, list[tuple[int, float | None]]]] = []
    for t in TAX_TYPES:
        if t.key == "dividends_to_split":
            continue
        rows: list[tuple[int, float | None]] = [
            (i, None) for i, s in enumerate(ty.sources) if s.tax_type == t.key]
        if t.key in ("qualified_dividends", "nonqualified_dividends"):
            for i, s in enumerate(ty.sources):
                if s.tax_type == "dividends_to_split":
                    q = s.qualified_share if s.qualified_share is not None else 0.0
                    rows.append((i, q if t.key == "qualified_dividends" else 1 - q))
        if rows:
            groups.append((t.label, rows))
    return groups


# -- calculation detail -------------------------------------------------
def _bands(schedule, lo: float, hi: float) -> list[tuple[float, float]]:
    """(rate, amount) of the income from ``lo`` to ``hi`` in each bracket."""
    out = []
    b = schedule.brackets
    for i, (start, rate) in enumerate(b):
        end = b[i + 1][0] if i + 1 < len(b) else float("inf")
        amt = max(0.0, min(hi, end) - max(lo, start))
        out.append((rate, amt))
    return out


def detail_rows(ws: Worksheet) -> list[tuple[str, list[float | None]]]:
    """Rows for the Calculation detail tab: (label, value per column).
    A label ending in ":" with no values is a heading."""
    fs = status_key(ws.ty.profile.filing_status)
    law = ws.law
    R = ws.federal
    rows: list[tuple[str, list[float | None]]] = [("Federal:", [])]

    def add(label, fn):
        rows.append((label, [round(fn(r), 2) for r in R]))

    add("Ordinary income (before Social Security)", lambda r: r.ordinary_income)
    add("Qualified dividends", lambda r: r.qualified_dividends)
    add("Net long-term capital gains", lambda r: r.net_ltcg)
    add("Social Security benefits", lambda r: r.social_security)
    add("Provisional income", lambda r: r.provisional_income)
    add("Taxable Social Security", lambda r: r.taxable_social_security)
    add("Adjusted gross income (AGI)", lambda r: r.agi)
    add("Standard or itemized deduction", lambda r: r.deduction)
    add("Senior deduction", lambda r: r.senior_deduction)
    add("Charitable deduction (non-itemizer)", lambda r: r.charitable_deduction)
    add("Taxable income", lambda r: r.taxable_income)
    add("Taxed at capital gains rates", lambda r: r.preferential_income)
    ord_taxable = [r.taxable_income - r.preferential_income for r in R]
    for n, (_, rate) in enumerate(law.ordinary[fs].brackets):
        rows.append((f"  Ordinary income in the {rate * 100:g}% bracket",
                     [round(_bands(law.ordinary[fs], 0, o)[n][1], 2) for o in ord_taxable]))
    for n, (_, rate) in enumerate(law.capital_gains[fs].brackets):
        rows.append((f"  Gains and qualified dividends at {rate * 100:g}%",
                     [round(_bands(law.capital_gains[fs], o, o + r.preferential_income)[n][1], 2)
                      for o, r in zip(ord_taxable, R)]))
    add("Ordinary income tax", lambda r: r.ordinary_tax)
    add("Capital gains tax", lambda r: r.capital_gains_tax)
    add("Net investment income tax", lambda r: r.niit)
    add("Credits", lambda r: r.credits)
    add("Federal tax", lambda r: r.federal_tax)
    if ws.state is not None:
        rows.append((f"{ws.state.name}:", []))
        S = ws.state_results
        rows.append(("AGI", [round(r.agi, 2) for r in S]))
        rows.append(("State tax under its gains rules", [round(r.state_gains_tax, 2) for r in S]))
        rows.append(("State tax", [round(r.state_tax, 2) for r in S]))
    rows.append(("Annualized (Form 2210 Schedule AI):", []))
    rows.append(("Annualization factor", [*ws.annualized_factors, None]))
    rows.append(("Annualized federal tax", [*ws.annualized_tax, None]))
    rows.append(("Required installment", [*ws.annualized_installments, None]))
    if ws.state is not None and ws.state_annualized_tax:
        rows.append((f"{ws.state.name} annualization factor", [*ws.state_annualized_factors, None]))
        rows.append((f"Annualized {ws.state.name} tax", [*ws.state_annualized_tax, None]))
    return rows


__all__ = [
    "Worksheet", "StateSchedule", "compute_worksheet", "build_profile", "worksheet_groups",
    "detail_rows", "prior_year_figures", "PERIODS", "PLAN", "COLUMN_COUNT", "PERIOD_ENDS",
]
