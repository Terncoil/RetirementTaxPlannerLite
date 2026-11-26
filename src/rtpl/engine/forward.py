"""The forward-looking plan: year-by-year income, tax and balances.

Each year:

1. Accounts owned by someone who died last year pass to the survivor.
2. Qualified charitable distributions (QCDs) go from IRAs straight to
   charity, within each owner's yearly limit. Then required distributions:
   RMDs from traditional accounts (Uniform Lifetime, or Joint and Last
   Survivor for a much younger spouse), and inherited IRA RMDs (Single
   Life) plus emptying in the 10-year deadline year. A QCD counts toward
   the RMD of the account it comes from, so only the rest is taxed.
3. Income streams (wages, pensions, one-off gains), Social Security (own,
   spousal and survivor benefits) and taxable-account yields.
4. HSA contributions (deducted from income), within each person's limit
   for their coverage and the months before Medicare. Then rules: Roth
   conversions and withdrawals, fixed amounts first, then "fill to a
   target" rules, each solved against the full tax return.
5. Tax with ``compute_tax()`` and that year's (possibly projected) law.
6. Cash flow: income minus spending, tax, IRMAA surcharges and HSA
   contributions. A surplus
   goes into the taxable account; a deficit is taken from the account
   types in the plan's spending order (by default cash, then the taxable
   account). Gains realized on sales and withdrawals from traditional
   accounts feed back into the tax, so this is solved by iteration.
   After the first death a couple's spending is the survivor's share.
7. Balances grow by their return.

Threshold lines for the chart (bracket tops, IRMAA tiers, ACA) are given in
the same units as the bars: the total income at which that year's return
would reach the threshold, found by adding or removing ordinary income and
recomputing the return. Where the income that can't be removed (Social
Security, RMDs, dividends, gains) is already over a threshold, the line is
drawn where the threshold sits, inside the bar, by carrying the last
dollar-for-dollar step on below the removable income.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from rtpl.model.plan import (
    CONTRIBUTION_KINDS,
    GAIN_INCOME_KINDS,
    HSA_CATCH_UP_AGE,
    HSA_COVERAGE,
    QCD_KINDS,
    QCD_MIN_AGE,
    ROTH_KINDS,
    TRADITIONAL_KINDS,
    Account,
    Plan,
    Rule,
)
from rtpl.model.profile import Household, Person, Profile
from rtpl.taxlaw.loader import FederalLaw, StateLaw, TaxLawLibrary, status_key

from . import social_security as ssa
from .rmd import RmdTables, default_tables, inherited_first_period, rmd_start_age
from .tax import IncomeSource, TaxResult, compute_tax

# Income categories, in stack order from the bottom. Ordinary income comes
# first, then gains (which stack on top of ordinary income for tax), then
# money that is not taxed at all.
CATEGORIES = (
    "ss",
    "wages",
    "pension",
    "other_ordinary",
    "rmd",
    "inherited",
    "ira_withdrawal",
    "roth_conversion",
    "dividends",
    "realized_gains",
    "tax_exempt",
    "roth_withdrawal",
    "principal",
    "qcd",
)
TAX_FREE = ("tax_exempt", "roth_withdrawal", "principal", "qcd")
# Money that leaves the household (QCDs go to charity): drawn on the graph
# but not part of the household's income.
NOT_INCOME = ("qcd",)
# Taxed at capital gains rates, stacked above ordinary income. Dividends and
# fund distributions come whether you want them or not; realized gains are
# from sales you choose (or that pay for a shortfall) and one-off gains.
PREFERENTIAL = ("dividends", "realized_gains")

CATEGORY_LABELS = {
    "ss": "Social Security",
    "wages": "Wages",
    "pension": "Pensions and annuities",
    "other_ordinary": "Interest and other ordinary income",
    "rmd": "RMDs (traditional accounts)",
    "inherited": "Inherited IRA withdrawals",
    "ira_withdrawal": "Other IRA / 401(k) withdrawals",
    "roth_conversion": "Roth conversions",
    "dividends": "Qualified dividends and fund distributions",
    "realized_gains": "Realized capital gains (sales)",
    "tax_exempt": "Tax-exempt interest",
    "roth_withdrawal": "Roth and HSA withdrawals (tax-free)",
    "principal": "Brokerage and cash principal (tax-free)",
    "qcd": "Qualified charitable distributions (to charity, not taxed)",
}

# Taxes paid each year, in stack order from the bottom, for the taxes by
# source graph. State tax is split into the state's separate capital gains
# tax (such as Washington's) and the rest. IRMAA surcharges are Medicare
# premiums rather than tax, so they go on top. FICA and local income taxes
# are not modeled.
TAX_SOURCES = ("ordinary", "ltcg", "niit", "state", "state_gains", "irmaa")
TAX_SOURCE_LABELS = {
    "ordinary": "Federal ordinary income tax",
    "ltcg": "Federal capital gains tax",
    "niit": "Net investment income tax (NIIT)",
    "state": "State income tax",
    "state_gains": "State capital gains tax",
    "irmaa": "Medicare IRMAA surcharges",
}


@dataclass(frozen=True)
class Source:
    category: str
    key: str  # unique per source, for the "show each source" view
    label: str
    amount: float


@dataclass
class YearResult:
    year: int
    ages: dict[int, int]  # person index -> age at year end, living people only
    filing_status: str
    sources: list[Source]
    tax: TaxResult
    law: FederalLaw
    irmaa_surcharge: float
    irmaa_note: str
    balances_start: dict[str, float]
    balances_end: dict[str, float]
    spending: float
    shortfall: float
    lines: dict[str, float | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Money out of each account (account id -> amount): RMDs and required
    # inherited withdrawals, Roth conversions, and everything else taken
    # (withdrawal rules and sales or withdrawals that pay for spending).
    rmds: dict[str, float] = field(default_factory=dict)
    conversions: dict[str, float] = field(default_factory=dict)
    withdrawals: dict[str, float] = field(default_factory=dict)
    # Qualified charitable distributions (account id -> amount). They count
    # toward the account's RMD, so ``rmds`` holds only the taxable rest.
    qcds: dict[str, float] = field(default_factory=dict)
    # HSA contributions (account id -> amount), paid from the year's cash
    # flow and deducted from income.
    contributions: dict[str, float] = field(default_factory=dict)
    # Social Security withheld under the earnings test (person index -> amount).
    ss_withheld: dict[int, float] = field(default_factory=dict)
    # Income and Social Security received, less spending, tax and IRMAA
    # surcharges: a surplus is saved; a deficit is taken from the spending
    # order, and what that can't cover is the shortfall.
    net_cash_flow: float = 0.0
    # Federal + state tax on the next $1,000 of ordinary income, as a rate.
    marginal_rate: float = 0.0
    # Qualified dividends and long-term gains by the federal rate they are
    # taxed at (rate -> amount); any sheltered by deductions count as 0%.
    gains_by_rate: dict[float, float] = field(default_factory=dict)

    def category_totals(self) -> dict[str, float]:
        out = {c: 0.0 for c in CATEGORIES}
        for s in self.sources:
            out[s.category] += s.amount
        return out

    def tax_by_source(self) -> dict[str, float]:
        """The year's taxes and IRMAA surcharges by source (see TAX_SOURCES).

        Federal tax is after credits: they come off the ordinary income tax
        first, then capital gains tax, then NIIT (a refundable credit larger
        than all three is left out, so no source goes below zero).
        """
        t = self.tax
        left = t.credits
        fed = {}
        for key, amount in (("ordinary", t.ordinary_tax), ("ltcg", t.capital_gains_tax), ("niit", t.niit)):
            used = min(left, max(0.0, amount))
            fed[key] = amount - used
            left -= used
        return {
            **fed,
            "state": t.state_tax - t.state_gains_tax,
            "state_gains": t.state_gains_tax,
            "irmaa": self.irmaa_surcharge,
        }

    @property
    def taxable_bar(self) -> float:
        """Height of the bar below the tax-free segments."""
        return sum(s.amount for s in self.sources if s.category not in TAX_FREE)

    @property
    def total_income(self) -> float:
        """The household's income (QCDs go to charity, so they are left out)."""
        return sum(s.amount for s in self.sources if s.category not in NOT_INCOME)


@dataclass
class PlanResult:
    plan: Plan
    years: list[YearResult]
    account_labels: dict[str, str]
    account_kinds: dict[str, str]
    line_labels: dict[str, str]
    notes: list[str] = field(default_factory=list)

    def deflator(self, year: int) -> float:
        return (1 + self.plan.inflation) ** (year - self.plan.start_year)


# -- helpers -------------------------------------------------------------------


def _active_rules(rules: list[Rule], profile: Profile, year: int) -> list[Rule]:
    """For each (kind, account), the rule with the narrowest span wins; ties go to the later rule."""
    best: dict[tuple[str, str], tuple[int, int, Rule]] = {}
    for idx, r in enumerate(rules):
        if not r.enabled:
            continue
        first, last = r.years(profile)
        if not first <= year <= last:
            continue
        key = (r.kind, r.account)
        span = last - first
        cur = best.get(key)
        if cur is None or span < cur[0] or (span == cur[0] and idx > cur[1]):
            best[key] = (span, idx, r)
    chosen = [v for v in best.values()]
    chosen.sort(key=lambda t: t[1])
    return [t[2] for t in chosen]


@dataclass
class _Year:
    """Everything that feeds one year's return. Amounts keyed by person index."""

    ss: dict[int, float] = field(default_factory=dict)
    wages: dict[int, float] = field(default_factory=dict)
    pension: dict[int, float] = field(default_factory=dict)  # domestic pensions and annuities (taxable part)
    ira: dict[int, float] = field(default_factory=dict)  # all traditional/inherited distributions + conversions
    interest: float = 0.0
    nonqualified_div: float = 0.0
    qualified_div: float = 0.0
    ltcg: float = 0.0
    stcg: float = 0.0
    other_ordinary: float = 0.0  # includes the taxable part of foreign pensions
    tax_exempt: float = 0.0
    hsa_deduction: float = 0.0  # HSA contributions, an above-the-line deduction

    def add(self, attr: str, owner: int | None, amount: float) -> None:
        d = getattr(self, attr)
        if isinstance(d, dict):
            d[owner if owner is not None else 0] = d.get(owner if owner is not None else 0, 0.0) + amount
        else:
            setattr(self, attr, d + amount)


def ss_claim_year(plan: Plan, i: int) -> int:
    """The year person ``i`` files for Social Security (the year before the
    plan for a benefit already being paid or SSDI)."""
    ss = plan.people[i].social_security
    if ss.current_benefit is not None or ss.ssdi:
        return plan.start_year - 1
    return plan.profile.people[i].birth_year + int(ss.claim_age)


@dataclass(frozen=True)
class SpousalTopUp:
    """The spousal top-up person ``i`` gets on top of their own benefit."""

    amount: float  # yearly, plan-start-year dollars, once it is paid for a full year (0 = none)
    start_year: int | None  # first year paid; None if it never starts
    age: int | None  # their age that year
    spouse: str  # the other person's name
    waits_for_spouse: bool  # they file before the spouse, so it waits for the spouse's claim
    spouse_claim_year: int
    reason: str = ""  # why there is none


def spousal_topup(plan: Plan, i: int) -> SpousalTopUp | None:
    """Half the other person's full benefit, less person ``i``'s own, reduced
    for starting before full retirement age (no delayed credits). It starts
    once both have filed. None for a single person."""
    people = plan.profile.people
    if len(people) != 2 or len(plan.people) < 2:
        return None
    j = 1 - i
    own = plan.people[i].social_security.fra_benefit
    other = plan.people[j].social_security.fra_benefit
    claim_i, claim_j = ss_claim_year(plan, i), ss_claim_year(plan, j)
    waits = claim_j > claim_i
    excess = max(0.0, 0.5 * other - own)
    name = people[j].name
    if excess <= 0:
        why = f"No Social Security for {name}" if other <= 0 else \
            f"own benefit is at least half of {name}'s"
        return SpousalTopUp(0.0, None, None, name, False, claim_j, why)
    start = max(claim_i, claim_j)
    if start > min(plan.death_year(i), plan.death_year(j)):
        return SpousalTopUp(0.0, None, None, name, waits, claim_j,
                            "one of you dies before both have filed")
    age = start - people[i].birth_year
    amount = excess * ssa.spousal_factor(people[i].birth_year, age)
    shown = max(start, plan.start_year)
    return SpousalTopUp(amount, shown, shown - people[i].birth_year, name, waits, claim_j)


class PlanEngine:
    def __init__(self, plan: Plan, library: TaxLawLibrary, tables: RmdTables | None = None) -> None:
        problems = plan.validate()
        if problems:
            raise ValueError("; ".join(problems))
        self.plan = plan
        self.library = library
        self.tables = tables or default_tables()
        self.profile = plan.profile
        self._state_laws: dict[int, StateLaw | None] = {}
        # Inherited IRA deadlines; a successor beneficiary of a stretching
        # account gets a new 10-year deadline when the first one dies.
        self.deadlines: dict[str, int | None] = {a.id: a.deadline_year(plan.profile) for a in plan.accounts}
        self.inherited_rules = {a.id: a.inherited_rule(plan.profile) for a in plan.accounts}
        self._taken_before: dict[str, float] = {}
        self._qcd: dict[str, float] = {}
        # Earnings test: months withheld so far (credited at full retirement
        # age), and this year's withheld amounts and notes.
        self._credit_own: dict[int, int] = {}
        self._credit_spousal: dict[int, int] = {}
        self.ss_withheld: dict[int, float] = {}
        self.ss_notes: list[str] = []

    # -- people ----------------------------------------------------------
    def alive(self, i: int, year: int) -> bool:
        return year <= self.plan.death_year(i)

    def living(self, year: int) -> list[int]:
        return [i for i in range(len(self.profile.people)) if self.alive(i, year)]

    def filing_status(self, year: int) -> str:
        fs = self.profile.filing_status
        if len(self.profile.people) == 2 and len(self.living(year)) < 2:
            return "single"  # qualifying surviving spouse is not modelled yet (issue #2)
        return fs

    def survivor_of(self, i: int, year: int) -> int | None:
        others = [j for j in self.living(year) if j != i]
        return others[0] if others else None

    # -- Social Security -------------------------------------------------
    def _ss_own(self, i: int, year: int) -> tuple[float, float]:
        """(yearly own benefit if fully paid this year, share of year paid)."""
        p = self.profile.people[i]
        ss = self.plan.people[i].social_security
        cola = (1 + self.plan.ss_cola) ** (year - self.plan.start_year)
        if ss.ssdi:
            amount = self._ssdi_year(p.birth_year, ss, year) * cola
            return amount, (1.0 if amount > 0 else 0.0)
        credit = self._own_credit(i, year)
        if ss.current_benefit is not None:
            return ss.current_benefit * cola * credit, 1.0
        if ss.fra_benefit <= 0:
            # No benefit of their own, but filing still starts any spousal benefit.
            return 0.0, ssa.claim_year_fraction(year, p.birth_year, ss.claim_age)
        factor = ssa.own_benefit_factor(p.birth_year, ss.claim_age) * credit
        return ss.fra_benefit * factor * cola, ssa.claim_year_fraction(year, p.birth_year, ss.claim_age)

    def _credits_apply(self, i: int, year: int) -> bool:
        """True once the benefit is recalculated for months withheld under
        the earnings test: from the year after full retirement age is
        reached (that year itself if it is reached in January)."""
        fy, months = ssa.fra_year_and_months(self.profile.people[i].birth_year)
        return year > fy or (year == fy and months == 0)

    def _own_credit(self, i: int, year: int) -> float:
        """How much the recalculation at full retirement age raises the own benefit."""
        months = self._credit_own.get(i, 0)
        if not months or not self._credits_apply(i, year):
            return 1.0
        birth = self.profile.people[i].birth_year
        claim = self.plan.people[i].social_security.claim_age
        fra = ssa.full_retirement_age(birth)
        if claim >= fra:
            return 1.0
        later = min(fra, claim + months / 12)
        return ssa.own_benefit_factor(birth, later) / ssa.own_benefit_factor(birth, claim)

    @staticmethod
    def _ssdi_year(birth_year: int, ss, year: int) -> float:
        """Benefit paid in ``year`` (today's dollars) for someone on SSDI.

        SSDI pays the full benefit and becomes a retirement benefit at full
        retirement age. With a claiming age past FRA the benefit is
        suspended from FRA and restarts at that age with delayed credits.
        If SSDI ends early (a return to work), nothing is paid until the
        claiming age, which then works as an ordinary claim.
        """
        pia = ss.fra_benefit or (ss.current_benefit or 0.0)
        fra = ssa.full_retirement_age(birth_year)
        end = ss.ssdi_end_age if ss.ssdi_end_age is not None and ss.ssdi_end_age < fra else fra
        # Share of the year on SSDI: whole years before ``end``, then the months before it.
        on_ssdi = min(1.0, max(0.0, end - (year - birth_year)))
        paid = pia * on_ssdi
        if end < fra:
            claim = max(ss.claim_age, end)
            paid += pia * ssa.own_benefit_factor(birth_year, claim) * ssa.claim_year_fraction(year, birth_year, claim)
        elif ss.claim_age > fra:
            factor = ssa.own_benefit_factor(birth_year, ss.claim_age)
            paid += pia * factor * ssa.claim_year_fraction(year, birth_year, ss.claim_age)
        else:
            paid += pia * (1.0 - on_ssdi) * (1.0 if year - birth_year >= int(fra) else 0.0)
        return paid

    def _ss_claim_year(self, i: int) -> int:
        return ss_claim_year(self.plan, i)

    def social_security(self, year: int, law: FederalLaw | None = None) -> dict[int, float]:
        """Benefits paid in ``year``, by person. Call it once a year, in
        order, with that year's ``law`` to apply the earnings test: what it
        withholds goes to ``ss_withheld`` and the months withheld raise the
        benefit from full retirement age."""
        people = self.profile.people
        cola = (1 + self.plan.ss_cola) ** (year - self.plan.start_year)
        paid: dict[int, float] = {}
        spousal_paid: dict[int, float] = {}
        start_month: dict[int, int] = {}  # first month paid this year (0 = January)
        survivor: set[int] = set()
        for i in self.living(year):
            own, frac = self._ss_own(i, year)
            amount = own * frac
            j = 1 - i if len(people) == 2 else None
            if j is not None and self.alive(j, year):
                # Spousal top-up once both have filed (deemed filing means
                # filing for one is filing for both).
                other_own, other_frac = self._ss_own(j, year)
                if frac > 0 and other_frac > 0:
                    pia_self = self.plan.people[i].social_security.fra_benefit * cola
                    pia_other = self.plan.people[j].social_security.fra_benefit * cola
                    excess = max(0.0, 0.5 * pia_other - pia_self)
                    if excess > 0:
                        start = max(self._ss_claim_year(i), self._ss_claim_year(j))
                        age_at_start = start - people[i].birth_year
                        if self._credit_spousal.get(i) and self._credits_apply(i, year):
                            age_at_start += self._credit_spousal[i] / 12
                        spousal_paid[i] = excess * ssa.spousal_factor(people[i].birth_year, age_at_start) \
                            * min(frac, other_frac)
                        amount += spousal_paid[i]
            elif j is not None and not self.alive(j, year):
                # Survivor: the larger of their own benefit and the deceased's.
                dy = self.plan.death_year(j)
                dj_own, _ = self._ss_own(j, year)
                claimed = self._ss_claim_year(j) <= dy
                pia_dead = self.plan.people[j].social_security.fra_benefit * cola
                surv = ssa.survivor_amount(pia_dead, dj_own if claimed else None)
                if surv > (own if frac > 0 else 0.0):
                    survivor.add(i)
                    frac = 1.0
                amount = max(own if frac > 0 else 0.0, surv)
            if amount > 0:
                paid[i] = amount
                start_month[i] = min(11, round(12 * (1 - frac)))
        self.ss_withheld = {}
        self.ss_notes = []
        if law is not None and law.earnings_test_under_fra is not None:
            self._earnings_test(year, law, paid, spousal_paid, start_month, survivor)
        for i in self.living(year):
            months = self._credit_own.get(i, 0) or self._credit_spousal.get(i, 0)
            fy, m = ssa.fra_year_and_months(people[i].birth_year)
            if months and year == (fy if m == 0 else fy + 1):
                self.ss_notes.append(
                    f"{people[i].name}: Social Security raised at full retirement age for "
                    f"{months} month{'s' if months != 1 else ''} withheld under the earnings test")
        return {i: a for i, a in paid.items() if a > 0.005}

    def wages_of(self, i: int, year: int) -> float:
        """Person ``i``'s wages for the earnings test (before any pre-tax deferrals)."""
        return sum(self.stream_amount(s, year) for s in self.plan.incomes
                   if s.kind == "wages" and (s.owner or 0) == i)

    def _earnings_test(self, year: int, law: FederalLaw, paid: dict[int, float], spousal_paid: dict[int, float],
                       start_month: dict[int, int], survivor: set[int]) -> None:
        """Withhold benefits for wages over the limit, before full retirement age.

        $1 for every $2 over the lower limit before the year full retirement
        age is reached; $1 for every $3 over the higher limit in that year,
        counting only the wages and benefits of the months before it. A
        person's wages withhold their own check and a spouse's spousal
        top-up (it is paid on their record) in the same months. Whole months
        withheld are credited back at each one's full retirement age."""
        people = self.profile.people
        for i in list(paid):
            ss = self.plan.people[i].social_security
            if ss.ssdi:
                continue  # SSDI has its own work rules
            fy, m = ssa.fra_year_and_months(people[i].birth_year)
            if year < fy:
                limit, per, months = law.earnings_test_under_fra, 2, 12
            elif year == fy and m > 0:
                limit, per, months = law.earnings_test_fra_year, 3, m
            else:
                continue
            wages = self.wages_of(i, year) * months / 12
            due = max(0.0, wages - limit) / per
            if due <= 0:
                continue
            # The checks paid on this person's record in the months before
            # full retirement age: their own check (own benefit, spousal
            # top-up or survivor benefit) and the spouse's spousal top-up.
            # SSA withholds whole months of the family's checks together, so
            # each gives up the same share of its months.
            j = 1 - i if len(people) == 2 else None
            s = start_month.get(i, 0)
            avail_i = paid[i] * max(0, months - s) / (12 - s)
            rate_i = paid[i] / (12 - s)  # monthly
            avail_j = rate_j = 0.0
            if j is not None and spousal_paid.get(j, 0) > 0 and paid.get(j, 0) > 0:
                sj = start_month.get(j, 0)
                avail_j = min(spousal_paid[j] * max(0, months - sj) / (12 - sj), paid[j])
                rate_j = spousal_paid[j] / (12 - sj)
            pool = avail_i + avail_j
            if pool <= 0:
                continue
            share = min(1.0, due / pool)
            take = avail_i * share
            if take > 0:
                paid[i] -= take
                self.ss_withheld[i] = self.ss_withheld.get(i, 0.0) + take
                whole = int(take / rate_i + 1e-6) if rate_i > 0 else 0
                if i not in survivor:
                    self._credit_own[i] = self._credit_own.get(i, 0) + whole
                    if spousal_paid.get(i):
                        self._credit_spousal[i] = self._credit_spousal.get(i, 0) + whole
            take_j = avail_j * share
            if take_j > 0:
                paid[j] -= take_j
                spousal_paid[j] -= take_j
                self.ss_withheld[j] = self.ss_withheld.get(j, 0.0) + take_j
                whole = int(take_j / rate_j + 1e-6) if rate_j > 0 else 0
                # Credited at the spouse's own full retirement age.
                self._credit_spousal[j] = self._credit_spousal.get(j, 0) + whole
            took = take + take_j
            if took > 0.5:
                self.ss_notes.append(
                    f"{people[i].name}: ${took:,.0f} of Social Security withheld under the earnings test "
                    f"(wages ${wages:,.0f}{' before full retirement age' if months < 12 else ''}, "
                    f"over the ${limit:,.0f} limit)")

    # -- incomes ---------------------------------------------------------
    def stream_amount(self, s, year: int) -> float:
        first = s.start.resolve(self.profile, start=True) if s.start else self.plan.start_year
        last = s.end.resolve(self.profile) if s.end else 10**6
        if not first <= year <= last:
            return 0.0
        amount = s.amount * (1 + s.rate(self.plan)) ** (year - max(first, self.plan.start_year))
        if s.owner is not None and s.owner < len(self.profile.people) and not self.alive(s.owner, year):
            if s.kind in ("pension", "foreign_pension", "annuity"):
                return amount * s.survivor_pct
            if s.kind == "wages":
                return 0.0
        return amount

    # -- RMDs ------------------------------------------------------------
    def required_distribution(self, acct: Account, balance: float, year: int, first_periods: dict) -> float:
        if balance <= 0 or acct.owner is None:
            return 0.0
        people = self.profile.people
        owner = people[acct.owner]
        age = owner.age_in(year)
        if acct.is_traditional:
            if age < rmd_start_age(owner.birth_year):
                return 0.0
            spouse = self.survivor_of(acct.owner, year)
            spouse_age = people[spouse].age_in(year) if spouse is not None else None
            period = self.tables.owner_period(age, spouse_age, acct.spouse_sole_beneficiary)
            return balance / period
        if acct.kind == "inherited_ira" and acct.inherited_year is not None:
            rule = self.inherited_rules.get(acct.id) or acct.inherited_rule(self.profile)
            deadline = self.deadlines.get(acct.id)
            if deadline is not None and year >= deadline:
                return balance
            if year <= acct.inherited_year:
                return 0.0
            if not rule.annual_rmds:
                return 0.0
            if acct.id not in first_periods:
                if rule.owner_life and acct.original_owner_birth_year is not None:
                    # An estate or trust: the owner's remaining life expectancy.
                    first_periods[acct.id] = self.tables.single_period(
                        acct.inherited_year - acct.original_owner_birth_year) - 1
                else:
                    first_periods[acct.id] = inherited_first_period(
                        self.tables, owner.birth_year, acct.inherited_year,
                        acct.original_owner_birth_year, acct.owner_started_rmds,
                    )
            period = first_periods[acct.id] - (year - acct.inherited_year - 1)
            return balance if period <= 1 else balance / period
        return 0.0

    # -- the tax return --------------------------------------------------
    def build_profile(self, year: int, y: _Year, filing_status: str) -> Profile:
        base = self.profile
        infl = (1 + self.plan.inflation) ** (year - self.plan.start_year)
        health = (1 + self.plan.assumptions.health_premium_growth) ** (year - self.plan.start_year)
        people = []
        for i in self.living(year):
            src = base.people[i]
            people.append(
                Person(
                    name=src.name,
                    birth_year=src.birth_year,
                    medicare_part_b=src.medicare_part_b,
                    blind=src.blind,
                    aca_benchmark_premium=src.aca_benchmark_premium * health,
                    aca_plan_premium=src.aca_plan_premium * health,
                    social_security=y.ss.get(i, 0.0),
                    wages=y.wages.get(i, 0.0),
                    pension=y.pension.get(i, 0.0),
                    ira_distributions=y.ira.get(i, 0.0),
                )
            )
        h = base.household
        # Education credits are claimed from the plan's first year through
        # the year the baseline gives (the first year only if none).
        studying = year <= (h.education_last_year or base.year)
        household = Household(
            taxable_interest=y.interest,
            tax_exempt_interest=y.tax_exempt,
            qualified_dividends=y.qualified_div,
            nonqualified_dividends=y.nonqualified_div,
            long_term_capital_gains=y.ltcg,
            short_term_capital_gains=y.stcg,
            other_ordinary_income=y.other_ordinary,
            adjustments=h.adjustments * infl,
            hsa_deduction=y.hsa_deduction,
            itemized_deductions=h.itemized_deductions * infl,
            has_taxable_brokerage=h.has_taxable_brokerage,
            aca_marketplace=h.aca_marketplace,
            aca_plan_tier=h.aca_plan_tier,
            household_size=h.household_size if len(people) == len(base.people) else None,
            aotc_students=h.aotc_students if studying else 0,
            aotc_expenses_per_student=h.aotc_expenses_per_student * infl,
            llc_expenses=h.llc_expenses * infl if studying else 0.0,
            education_last_year=h.education_last_year,
        )
        return Profile(year=year, filing_status=filing_status, state=base.state, people=people,
                       household=household, name=base.name, inflation=self.plan.inflation)

    def law(self, year: int) -> FederalLaw:
        return self.library.federal(year, self.plan.inflation)

    # -- fill targets ----------------------------------------------------
    def _target(self, target: str, year: int, prof: Profile, law: FederalLaw):
        """Return (metric(TaxResult) -> float, limit)."""
        fs = status_key(prof.filing_status)
        kind, _, arg = target.partition(":")
        if kind == "bracket":
            rate = float(arg)
            sched = law.ordinary[fs]
            tops = [(b, r) for b, r in sched.brackets]
            limit = None
            for k, (b, r) in enumerate(tops):
                if abs(r - rate) < 1e-9 and k + 1 < len(tops):
                    limit = tops[k + 1][0]
            if limit is None:
                raise ValueError(f"No bracket at {rate:.0%} with a top in {year}")
            return (lambda r: r.taxable_income - r.preferential_income), limit
        if kind == "cg0":
            return (lambda r: r.taxable_income), law.capital_gains[fs].thresholds()[0]
        if kind == "irmaa":
            n = int(arg or 1)
            plaw = self.law(year + law.irmaa_lookback)
            tier = plaw.irmaa_tiers[n - 1]
            return (lambda r: r.magi_irmaa), tier.threshold(prof.filing_status) - 1
        if kind == "aca":
            pct = float(arg or 4.0)
            return (lambda r: r.magi_aca), pct * law.fpl(prof.fpl_household_size())
        if kind == "magi":
            return (lambda r: r.magi_irmaa), float(arg)
        raise ValueError(f"Unknown fill target {target!r}")

    def state_law(self, year: int) -> StateLaw | None:
        """The plan's state tax law for ``year`` (None without a state)."""
        if year not in self._state_laws:
            code = self.plan.profile.state
            self._state_laws[year] = self.library.state(code, year) if code else None
        return self._state_laws[year]

    def solve_extra_ordinary(self, prof: Profile, law: FederalLaw, metric, limit: float,
                             lo: float, hi: float, tol: float = 1.0, *, extrapolate: bool = False) -> float:
        """Largest extra ordinary income in [lo, hi] with metric <= limit.

        If even ``lo`` is over the limit, returns ``lo``; with ``extrapolate``,
        it instead goes below ``lo`` by the amount the metric is over, as if
        each further dollar removed lowered the metric by a dollar.
        """
        f = lambda x: metric(compute_tax(prof, law, self.state_law(prof.year), extra=x, source=IncomeSource.ORDINARY))  # noqa: E731
        at_lo = f(lo)
        if at_lo > limit:
            return lo - (at_lo - limit) if extrapolate else lo
        if f(hi) <= limit:
            return hi
        for _ in range(60):
            if hi - lo <= tol:
                break
            mid = (lo + hi) / 2
            if f(mid) <= limit:
                lo = mid
            else:
                hi = mid
        return lo

    # -- the run ---------------------------------------------------------
    def run(self, *, with_lines: bool = True) -> PlanResult:
        plan, profile = self.plan, self.profile
        self.deadlines = {a.id: a.deadline_year(profile) for a in plan.accounts}
        self._credit_own, self._credit_spousal = {}, {}
        accounts = [copy.deepcopy(a) for a in plan.accounts]
        by_id = {a.id: a for a in accounts}
        bal = {a.id: a.balance for a in accounts}
        basis = {a.id: (a.cost_basis if a.cost_basis is not None else a.balance) for a in accounts
                 if a.kind in ("taxable", "cash")}
        for a in accounts:
            if a.kind == "cash":
                basis[a.id] = bal[a.id]
        first_periods: dict[str, float] = {}
        magi_history: dict[int, tuple[float, str]] = {
            int(k): (float(v), profile.filing_status) for k, v in plan.assumptions.prior_magi.items()
        }
        labels = {a.id: a.label(profile) for a in accounts}
        result = PlanResult(plan=plan, years=[], account_labels=labels,
                            account_kinds={a.id: a.kind for a in accounts}, line_labels={})
        result.notes.extend(plan.timing_notes())
        if not any(a.kind in ("taxable", "cash") for a in accounts):
            # Somewhere for surpluses and deficits to go.
            cash = Account(id="_cash", kind="cash", owner=None, name="Cash (surplus)", balance=0.0)
            accounts.append(cash)
            by_id[cash.id] = cash
            bal[cash.id] = 0.0
            basis[cash.id] = 0.0
            labels[cash.id] = cash.name
            result.account_kinds[cash.id] = "cash"
        roth_for: dict[int, str] = {}
        for a in accounts:
            if a.kind == "roth_ira" and a.owner is not None:
                roth_for.setdefault(a.owner, a.id)

        for year in range(plan.start_year, plan.end_year + 1):
            living = self.living(year)
            if not living:
                break
            notes_next: list[str] = []
            # 1. accounts of someone who died last year pass to the survivor
            for a in accounts:
                if a.owner is not None and not self.alive(a.owner, year):
                    surv = self.survivor_of(a.owner, year)
                    if surv is not None:
                        if a.kind == "inherited_ira" and self.deadlines.get(a.id) is None:
                            # A successor beneficiary of a stretching account has 10 years.
                            self.deadlines[a.id] = self.plan.death_year(a.owner) + 10
                            notes_next.append(f"{labels[a.id]}: passes to {profile.people[surv].name}, who must "
                                              f"empty it by {self.deadlines[a.id]}")
                        a.owner = surv
                        labels[a.id] = a.label(profile) + " (inherited from spouse)" if not a.name else labels[a.id]
                        if a.kind == "roth_ira":
                            roth_for.setdefault(surv, a.id)
            start_bal = dict(bal)
            fs = self.filing_status(year)
            law = self.law(year)
            y = _Year()
            sources: list[Source] = []
            withdrawn: dict[str, float] = {a.id: 0.0 for a in accounts}
            rmds: dict[str, float] = {}
            # A first-year RMD already out of the entered balance: income
            # this year, but not taken from the balance again.
            self._taken_before: dict[str, float] = {}
            notes: list[str] = notes_next
            active = _active_rules(plan.rules, profile, year)

            def take_from(acct: Account, amount: float, category: str, label: str) -> float:
                amount = max(0.0, min(amount, bal[acct.id] - withdrawn[acct.id]))
                if amount <= 0:
                    return 0.0
                withdrawn[acct.id] += amount
                owner = acct.owner if acct.owner is not None else living[0]
                if acct.is_traditional or acct.kind == "inherited_ira":
                    y.add("ira", owner, amount)
                    sources.append(Source(category, f"{category}:{acct.id}", label, amount))
                elif acct.is_roth or acct.kind == "hsa":
                    sources.append(Source("roth_withdrawal", f"roth_withdrawal:{acct.id}", label, amount))
                return amount

            # 2. QCDs, then required distributions (less the QCD from that account)
            reqs = {a.id: self.required_distribution(a, bal[a.id], year, first_periods) for a in accounts}
            self._qcd = self._apply_qcds([r for r in active if r.kind == "qcd"], year, by_id, bal, withdrawn,
                                         reqs, labels, law, sources, notes)
            for a in accounts:
                req = reqs[a.id]
                if req > 0:
                    cat = "inherited" if a.kind == "inherited_ira" else "rmd"
                    what = "required withdrawal" if a.kind == "inherited_ira" else "RMD"
                    if year == plan.start_year and plan.rmd_taken_now(a) and req < bal[a.id] - 0.005:
                        # The entered balance B is after the RMD, so last year's
                        # end balance was B + R with R = (B + R) / period, which
                        # is R = req * B / (B - req) for req = B / period.
                        taken = req * bal[a.id] / (bal[a.id] - req)
                        y.add("ira", a.owner if a.owner is not None else living[0], taken)
                        sources.append(Source(cat, f"{cat}:{a.id}", f"{labels[a.id]} {what} (already taken)", taken))
                        self._taken_before[a.id] = taken
                        rmds[a.id] = taken
                        continue
                    # A QCD counts toward this account's RMD. (When RMDs are
                    # aggregated across a person's IRAs, the person's QCD
                    # total would come off their combined RMD instead.)
                    rmds[a.id] = take_from(a, max(0.0, req - self._qcd.get(a.id, 0.0)), cat,
                                           f"{labels[a.id]} {what}")
                    if self.deadlines.get(a.id) == year:
                        notes.append(f"{labels[a.id]}: deadline year, remaining balance withdrawn")

            # 3. incomes
            for s in plan.incomes:
                amt = self.stream_amount(s, year)
                if amt == 0 or (amt < 0 and s.kind not in GAIN_INCOME_KINDS):
                    continue
                owner = s.owner if s.owner is not None and s.owner < len(profile.people) else None
                if owner is not None and not self.alive(owner, year):
                    owner = self.survivor_of(owner, year)
                label = s.label(profile)
                key = f"income:{plan.incomes.index(s)}"
                taxable = amt * s.federal_taxable_pct
                if s.kind == "wages":
                    y.add("wages", owner, taxable)
                    sources.append(Source("wages", key, label, amt))
                elif s.kind in ("pension", "annuity"):
                    y.add("pension", owner, taxable)
                    sources.append(Source("pension", key, label, amt))
                elif s.kind == "foreign_pension":
                    y.add("other_ordinary", owner, taxable)
                    sources.append(Source("pension", key, label, amt))
                elif s.kind == "interest":
                    y.add("interest", owner, taxable)
                    sources.append(Source("other_ordinary", key, label, amt))
                elif s.kind == "qualified_dividends":
                    y.add("qualified_div", owner, taxable)
                    sources.append(Source("dividends", key, label, amt))
                elif s.kind == "ltcg":
                    # A loss is not income: it lowers the tax but adds no cash or bar.
                    y.add("ltcg", owner, taxable)
                    sources.append(Source("realized_gains", key, label, max(0.0, amt)))
                elif s.kind == "stcg":
                    y.add("stcg", owner, taxable)
                    sources.append(Source("other_ordinary", key, label, max(0.0, amt)))
                elif s.kind == "tax_exempt":
                    y.add("tax_exempt", owner, amt)
                    sources.append(Source("tax_exempt", key, label, amt))
                else:
                    y.add("other_ordinary", owner, taxable)
                    sources.append(Source("other_ordinary", key, label, amt))

            for i, amt in self.social_security(year, law).items():
                y.add("ss", i, amt)
                sources.append(Source("ss", f"ss:{i}", f"{profile.people[i].name} Social Security", amt))
            withheld = dict(self.ss_withheld)
            notes.extend(self.ss_notes)

            # taxable-account yields (reinvested)
            reinvested: dict[str, float] = {}
            for a in accounts:
                if a.kind != "taxable" or bal[a.id] <= 0:
                    continue
                b = bal[a.id]
                interest = b * (a.interest_yield + a.nonqualified_dividend_yield)
                qd = b * a.qualified_dividend_yield
                cgd = b * a.cap_gain_distribution_yield
                y.interest += b * a.interest_yield
                y.nonqualified_div += b * a.nonqualified_dividend_yield
                y.qualified_div += qd
                y.ltcg += cgd
                if interest:
                    sources.append(Source("other_ordinary", f"yield:{a.id}", f"{labels[a.id]} interest and non-qualified dividends", interest))
                if qd + cgd:
                    sources.append(Source("dividends", f"yield:{a.id}", f"{labels[a.id]} dividends and distributions", qd + cgd))
                reinvested[a.id] = interest + qd + cgd
            for a in accounts:
                if a.kind == "cash" and bal[a.id] > 0:
                    intr = bal[a.id] * plan.assumptions.cash_return
                    y.interest += intr
                    sources.append(Source("other_ordinary", f"yield:{a.id}", f"{labels[a.id]} interest", intr))
                    reinvested[a.id] = intr

            # 4. HSA contributions (they lower income, so before fill rules),
            # then rules: fixed, spread and all first, then fill rules
            contributed = self._apply_hsa_contributions(
                [r for r in active if r.kind == "hsa_contribution"], year, by_id, labels, law, notes)
            y.hsa_deduction = sum(contributed.values())
            conversions: dict[str, tuple[str, float]] = {}
            fills = []
            for r in active:
                if r.kind == "qcd" or r.kind in CONTRIBUTION_KINDS:
                    continue  # QCDs were applied with the RMDs, contributions above
                if r.mode == "fill":
                    fills.append(r)
                    continue
                self._apply_rule(r, year, by_id, bal, withdrawn, take_from, y, sources, conversions,
                                 labels, roth_for, None, law, fs, basis, notes)
            for r in fills:
                self._apply_rule(r, year, by_id, bal, withdrawn, take_from, y, sources, conversions,
                                 labels, roth_for, r.target, law, fs, basis, notes)
            for acct_id, a in by_id.items():  # Roth accounts created by conversions
                if a not in accounts:
                    accounts.append(a)
                    withdrawn.setdefault(acct_id, 0.0)
                    result.account_kinds[acct_id] = a.kind

            # 5-6. tax and cash flow, iterating on gains from sales that pay for a deficit
            spending = plan.assumptions.spending * (1 + plan.inflation) ** (year - plan.start_year)
            if len(profile.people) > 1 and len(living) < len(profile.people):
                spending *= plan.assumptions.survivor_spending_pct
            surcharge, irmaa_note = self._irmaa(year, magi_history, living, fs)
            inflow = sum(s.amount for s in sources if s.key.startswith(("income:", "ss:")))
            inflow += sum(s.amount for s in sources if s.category in ("rmd", "inherited", "ira_withdrawal", "roth_withdrawal"))
            inflow += sum(amt for (_, amt, _) in sale_plan_other(sources))
            # Sales and traditional withdrawals that pay for a deficit change
            # the tax, which changes the deficit: iterate to a fixed point.
            sale_plan: list[tuple[str, float, float]] = []
            taxed = (0.0, 0.0)
            for _ in range(20):
                y_try = copy.deepcopy(y)
                self._add_cover_income(y_try, sale_plan, by_id, living)
                prof = self.build_profile(year, y_try, fs)
                tax = compute_tax(prof, law, self.state_law(prof.year))
                net = inflow - (spending + tax.total_tax + surcharge + y.hsa_deduction + tax.aca_net_premium)
                new_plan, short = self._cover_deficit(-net, accounts, bal, withdrawn, basis)
                new_taxed = _taxed_cover(new_plan, by_id)
                done = all(abs(a - b) < 1.0 for a, b in zip(new_taxed, taxed))
                sale_plan, taxed = new_plan, new_taxed
                if done:
                    break
            shortfall = short if net < 0 else 0.0
            for acct_id, amount, gain in sale_plan:
                acct = by_id[acct_id]
                if acct.kind in ("taxable", "cash"):
                    withdrawn[acct_id] += amount
                    y.ltcg += gain
                    if gain > 0:
                        sources.append(Source("realized_gains", f"sale:{acct_id}", f"{labels[acct_id]} gains on sales", gain))
                    if amount - gain > 0:
                        sources.append(Source("principal", f"sale:{acct_id}", f"{labels[acct_id]} principal", amount - gain))
                else:
                    cat = "inherited" if acct.kind == "inherited_ira" else "ira_withdrawal"
                    take_from(acct, amount, cat, f"{labels[acct_id]} withdrawal for spending")
            prof = self.build_profile(year, y, fs)
            tax = compute_tax(prof, law, self.state_law(prof.year))
            surplus = max(0.0, net) if net >= 0 else 0.0
            if shortfall > 0:
                notes.append(f"Short ${shortfall:,.0f}: spending and tax exceed income and the accounts in the "
                             "spending order")

            # 7. balances
            for a in accounts:
                rate = plan.assumptions.cash_return if a.kind == "cash" else (
                    a.return_rate if a.return_rate is not None else plan.assumptions.return_rate)
                b = bal[a.id] - withdrawn[a.id]
                if a.id in basis and bal[a.id] > 0 and a.kind == "taxable":
                    sold_frac = min(1.0, withdrawn[a.id] / bal[a.id]) if bal[a.id] else 0.0
                    basis[a.id] *= (1 - sold_frac)
                elif a.kind == "cash":
                    basis[a.id] = max(0.0, b)
                bal[a.id] = b * (1 + rate)
                if a.id in reinvested and a.id in basis:
                    basis[a.id] += reinvested[a.id]
            for target_id, amount in _conversion_totals(conversions).items():
                dest = by_id.get(target_id)
                rate = dest.return_rate if dest is not None and dest.return_rate is not None \
                    else plan.assumptions.return_rate
                bal[target_id] = bal.get(target_id, 0.0) + amount * (1 + rate)
            for acct_id, amount in contributed.items():
                dest = by_id[acct_id]
                rate = dest.return_rate if dest.return_rate is not None else plan.assumptions.return_rate
                bal[acct_id] += amount * (1 + rate)
            if surplus > 0:
                dest = next((a for a in accounts if a.kind == "taxable"), None) or next(
                    a for a in accounts if a.kind == "cash")
                bal[dest.id] += surplus
                basis[dest.id] = basis.get(dest.id, 0.0) + surplus
            for a in accounts:
                if a.kind == "cash":
                    basis[a.id] = bal[a.id]

            magi_history[year] = (tax.magi_irmaa, fs)
            converted: dict[str, float] = {}
            for key, (_dest, amount) in conversions.items():
                src = key.split("->", 1)[0]
                converted[src] = converted.get(src, 0.0) + amount
            other_out = {k: v - rmds.get(k, 0.0) - converted.get(k, 0.0) - self._qcd.get(k, 0.0)
                         for k, v in withdrawn.items()}
            other_out.update({k: v + self._taken_before.get(k, 0.0) for k, v in other_out.items()
                              if k in self._taken_before})
            marginal = (compute_tax(prof, law, self.state_law(prof.year), extra=1000.0).total_tax - tax.total_tax) / 1000.0
            yr = YearResult(
                year=year,
                ages={i: profile.people[i].age_in(year) for i in living},
                filing_status=fs,
                sources=[s for s in sources if s.amount > 0.005],
                tax=tax,
                law=law,
                irmaa_surcharge=surcharge,
                irmaa_note=irmaa_note,
                balances_start=start_bal,
                balances_end={k: max(0.0, v) for k, v in bal.items()},
                spending=spending,
                shortfall=shortfall,
                notes=notes,
                rmds={k: v for k, v in rmds.items() if v > 0.005},
                conversions={k: v for k, v in converted.items() if v > 0.005},
                withdrawals={k: v for k, v in other_out.items() if v > 0.005},
                qcds={k: v for k, v in self._qcd.items() if v > 0.005},
                contributions={k: v for k, v in contributed.items() if v > 0.005},
                ss_withheld={k: v for k, v in withheld.items() if v > 0.005},
                net_cash_flow=net,
                marginal_rate=marginal,
                gains_by_rate=_gains_by_rate(tax, law, fs),
            )
            if with_lines:
                yr.lines = self.threshold_lines(yr, prof, law, result.line_labels)
            result.years.append(yr)

        projected = [yr.year for yr in result.years if yr.law.status == "projected"]
        if projected:
            result.notes.append(
                f"Tax law for {projected[0]} on is projected at {plan.inflation:.1%} inflation."
            )
        return result

    # -- rule application -------------------------------------------------
    def _apply_rule(self, r: Rule, year, by_id, bal, withdrawn, take_from, y, sources, conversions,
                    labels, roth_for, target, law, fs, basis, notes) -> None:
        acct = by_id.get(r.account)
        if acct is None:
            return
        available = bal[acct.id] - withdrawn[acct.id]
        if available <= 0:
            return
        first, last = r.years(self.profile)
        # A withdrawal rule's amount is the year's total from that account, so
        # an RMD already taken counts toward it (RMDs are a floor, not extra).
        # Conversions come on top of any RMD, which cannot be converted.
        # QCDs went to charity, so they don't count toward a withdrawal.
        already = (withdrawn[acct.id] + self._taken_before.get(acct.id, 0.0) - self._qcd.get(acct.id, 0.0)
                   if r.kind == "withdrawal" else 0.0)
        if r.mode == "fixed":
            amount = r.fixed_amount(self.plan, year) - already
        elif r.mode == "spread":
            deadline = self.deadlines.get(acct.id)
            if deadline is not None:
                last = min(last, deadline)  # the 10-year rule empties it then anyway
            amount = bal[acct.id] / max(1, last - year + 1) - already
        elif r.mode == "all":
            amount = available
        else:  # fill
            if acct.kind in ("taxable", "cash", "roth_ira", "roth_401k", "hsa"):
                notes.append(f"Fill rules apply to traditional and inherited accounts only ({labels[acct.id]})")
                return
            prof = self.build_profile(year, y, fs)
            try:
                metric, limit = self._target(target, year, prof, law)
            except (ValueError, IndexError) as exc:
                notes.append(str(exc))
                return
            amount = self.solve_extra_ordinary(prof, law, metric, limit, 0.0, available)
        amount = min(amount, available)
        if amount <= 0:
            return
        if r.kind == "roth_conversion":
            if not (acct.is_traditional or acct.kind == "inherited_ira") or acct.kind == "inherited_ira":
                notes.append(f"Roth conversions need a traditional account ({labels[acct.id]})")
                return
            owner = acct.owner if acct.owner is not None else 0
            dest = r.to_account or roth_for.get(owner)
            if dest is None:
                dest = f"_roth{owner}"
                roth_for[owner] = dest
                by_id[dest] = Account(id=dest, kind="roth_ira", owner=owner, balance=0.0)
                bal[dest] = 0.0
                labels[dest] = by_id[dest].label(self.profile)
            withdrawn[acct.id] += amount
            y.add("ira", owner, amount)
            sources.append(Source("roth_conversion", f"roth_conversion:{acct.id}",
                                  f"{labels[acct.id]} Roth conversion", amount))
            conversions[f"{acct.id}->{dest}:{len(conversions)}"] = (dest, amount)
            return
        # withdrawal
        if acct.kind in ("taxable", "cash"):
            b = bal[acct.id]
            gain = 0.0
            if acct.kind == "taxable" and b > 0:
                gain = amount * max(0.0, 1 - basis.get(acct.id, b) / b)
            withdrawn[acct.id] += amount
            y.ltcg += gain
            if gain:
                sources.append(Source("realized_gains", f"withdrawal:{acct.id}", f"{labels[acct.id]} gains on sales", gain))
            if amount - gain:
                sources.append(Source("principal", f"withdrawal:{acct.id}", f"{labels[acct.id]} principal", amount - gain))
            return
        cat = "inherited" if acct.kind == "inherited_ira" else "ira_withdrawal"
        take_from(acct, amount, cat, f"{labels[acct.id]} withdrawal")

    def _apply_qcds(self, rules: list[Rule], year, by_id, bal, withdrawn, reqs, labels, law,
                    sources, notes) -> dict[str, float]:
        """Give each QCD rule's amount to charity, in rule order. Returns
        the QCD from each account (account id -> amount).

        Each IRA owner has their own yearly limit, shared by all of their
        IRAs. A QCD is never taxed: it is left out of the return entirely.
        A first-year RMD already taken (before the plan) can't be replaced,
        so an "RMD" QCD gives nothing that year.
        """
        out: dict[str, float] = {}
        used: dict[int, float] = {}
        people = self.profile.people
        for r in rules:
            acct = by_id.get(r.account)
            if acct is None:
                continue
            if acct.kind not in QCD_KINDS:
                notes.append(f"QCDs come from traditional or inherited IRAs only ({labels[acct.id]}); "
                             "roll a 401(k) or 403(b) into an IRA first")
                continue
            if acct.owner is None:
                continue
            person = people[acct.owner]
            if person.age_in(year) < QCD_MIN_AGE:
                if person.age_in(year) == QCD_MIN_AGE - 1:
                    notes.append(f"{person.name}: QCDs start the year they turn {QCD_MIN_AGE} "
                                 "(70½ during the year)")
                continue
            if r.mode == "rmd":
                if year == self.plan.start_year and self.plan.rmd_taken_now(acct):
                    continue
                want = reqs.get(acct.id, 0.0)
            else:
                want = r.fixed_amount(self.plan, year)
            room = max(0.0, law.qcd_limit - used.get(acct.owner, 0.0))
            amount = max(0.0, min(want, room, bal[acct.id] - withdrawn[acct.id]))
            if want > room + 0.5:
                notes.append(f"{labels[acct.id]}: QCD capped at {person.name}'s {year} limit of "
                             f"${law.qcd_limit:,.0f}")
            if amount <= 0:
                continue
            withdrawn[acct.id] += amount
            used[acct.owner] = used.get(acct.owner, 0.0) + amount
            out[acct.id] = out.get(acct.id, 0.0) + amount
            sources.append(Source("qcd", f"qcd:{acct.id}", f"{labels[acct.id]} QCD", amount))
        return out

    def _apply_hsa_contributions(self, rules: list[Rule], year, by_id, labels, law: FederalLaw,
                                 notes: list[str]) -> dict[str, float]:
        """Put each HSA contribution rule's amount into its HSA, in rule
        order. Returns the contribution to each account (account id -> amount).

        There is no earned-income test. Each person's limit is the year's
        self-only or family limit for their coverage, plus the catch-up
        from 55, prorated to the months before Medicare. Spouses with family
        coverage share one family limit (the first rule in the list fills
        it first); a catch-up only goes into the HSA its person owns, which
        every HSA in the plan is (HSAs are individual).
        """
        out: dict[str, float] = {}
        if not rules:
            return out
        if law.hsa_self_only is None or law.hsa_family is None:
            notes.append(f"No HSA limits in the {year} tax law; HSA contributions skipped")
            return out
        people = self.profile.people
        base_used: dict[int, float] = {}  # each person's contributions under the self-only or family limit
        catch_used: dict[int, float] = {}
        family_used = 0.0  # spouses with family coverage share one limit
        noted: set[tuple[int, str]] = set()
        for r in rules:
            orig = self.plan.account(r.account)
            acct = by_id.get(r.account)
            if acct is None or orig is None or orig.owner is None or orig.kind != "hsa":
                continue
            i = orig.owner
            if not self.alive(i, year):
                continue
            name = people[i].name
            months = self.plan.hsa_months(i, year)
            if months <= 0:
                if self.plan.hsa_months(i, year - 1) > 0 and (i, "medicare") not in noted:
                    notes.append(f"{name}: HSA contributions stop with Medicare")
                    noted.add((i, "medicare"))
                continue
            frac = months / 12
            family = r.coverage == "family"
            limit = (law.hsa_family if family else law.hsa_self_only) * frac
            base_room = max(0.0, limit - base_used.get(i, 0.0))
            if family:
                base_room = min(base_room, max(0.0, law.hsa_family - family_used))
            catch_limit = law.hsa_catch_up * frac if people[i].age_in(year) >= HSA_CATCH_UP_AGE else 0.0
            catch_room = max(0.0, catch_limit - catch_used.get(i, 0.0))
            room = base_room + catch_room
            want = room if r.mode == "max" else r.fixed_amount(self.plan, year)
            amount = max(0.0, min(want, room))
            if want > room + 0.5:
                cover = HSA_COVERAGE.get(r.coverage, r.coverage).lower()
                notes.append(f"{labels[acct.id]}: HSA contribution capped at ${room:,.0f}, what is left of "
                             f"{name}'s {year} limit ({cover} coverage"
                             f"{', with the catch-up' if catch_limit else ''})")
            if months < 12 and amount > 0 and (i, "months") not in noted:
                notes.append(f"{name}: HSA limit prorated to {months} month{'s' if months != 1 else ''} "
                             "before Medicare")
                noted.add((i, "months"))
            if amount <= 0:
                continue
            base = min(amount, base_room)
            base_used[i] = base_used.get(i, 0.0) + base
            if family:
                family_used += base
            catch_used[i] = catch_used.get(i, 0.0) + (amount - base)
            out[acct.id] = out.get(acct.id, 0.0) + amount
        return out

    def _cover_deficit(self, deficit: float, accounts, bal, withdrawn, basis):
        """Take a deficit from the account types in the spending order, each
        in list order. Returns (takes as (account id, amount, gain), shortfall)."""
        sales: list[tuple[str, float, float]] = []
        need = max(0.0, deficit)
        order = list(self.plan.assumptions.spending_order)
        if "_cash" in bal and "cash" not in order:
            order.insert(0, "cash")  # the hidden surplus account always pays back first
        for source in order:
            for a in accounts:
                if need <= 0 or _spending_source(a) != source:
                    continue
                avail = bal[a.id] - withdrawn[a.id]
                take = min(need, max(0.0, avail))
                if take <= 0:
                    continue
                gain = 0.0
                if a.kind == "taxable" and bal[a.id] > 0:
                    gain = take * max(0.0, 1 - basis.get(a.id, bal[a.id]) / bal[a.id])
                sales.append((a.id, take, gain))
                need -= take
        return sales, need

    @staticmethod
    def _add_cover_income(y: "_Year", takes, by_id, living: list[int]) -> None:
        """Add the taxable income from deficit takes to a trial year."""
        for acct_id, amount, gain in takes:
            a = by_id[acct_id]
            if a.kind == "taxable":
                y.ltcg += gain
            elif a.kind in TRADITIONAL_KINDS or a.kind == "inherited_ira":
                y.add("ira", a.owner if a.owner is not None else living[0], amount)

    # -- IRMAA -----------------------------------------------------------
    def _irmaa(self, year: int, history: dict, living: list[int], fs: str) -> tuple[float, str]:
        law = self.law(year)
        enrolled = [i for i in living if self.profile.people[i].on_medicare(year)]
        if not enrolled:
            return 0.0, ""
        look = year - law.irmaa_lookback
        if look in history:
            magi, look_fs = history[look]
            note = f"IRMAA {year}: MAGI ${magi:,.0f} from {look}"
        elif history:
            first = min(history)
            magi, look_fs = history[first]
            note = f"IRMAA {year}: {look} MAGI not entered; using {first}"
        else:
            return 0.0, f"IRMAA {year}: needs {look} MAGI (Assumptions, prior MAGI); not charged"
        cost = 0.0
        for tier in law.irmaa_tiers:
            if magi > tier.threshold(look_fs):
                cost = (tier.part_b - law.irmaa_part_b_standard) + tier.part_d
        return cost * 12 * len(enrolled), note

    # -- threshold lines ---------------------------------------------------
    def threshold_lines(self, yr: YearResult, prof: Profile, law: FederalLaw,
                        labels: dict[str, str]) -> dict[str, float | None]:
        fs = status_key(prof.filing_status)
        base = yr.taxable_bar
        ordinary_now = sum(
            s.amount for s in yr.sources if s.category not in TAX_FREE and s.category not in PREFERENTIAL and s.category != "ss"
        )
        lo, hi = -ordinary_now - 1.0, 5_000_000.0
        out: dict[str, float | None] = {}

        totals = yr.category_totals()
        gains = sum(totals[c] for c in PREFERENTIAL)

        def line(key: str, label: str, metric, limit: float, ordinary_only: bool = False) -> None:
            # Ordinary bracket tops are read against the top of the ordinary
            # segments (gains sit above them, as they do on the return);
            # everything else is read against the whole taxable bar.
            # When the income that can't be removed is already over the
            # threshold, the line still goes where the threshold sits (inside
            # the bar), not at the removable-income limit, so lines never
            # collapse onto one value.
            delta = self.solve_extra_ordinary(prof, law, metric, limit, lo, hi, tol=5.0, extrapolate=True)
            out[key] = max(0.0, base - (gains if ordinary_only else 0.0) + delta)
            labels.setdefault(key, label)

        line("deduction", "Deductions (taxable income starts)", lambda r: r.taxable_income, 0.0)
        sched = law.ordinary[fs].brackets
        for k in range(len(sched) - 1):
            rate, top = sched[k][1], sched[k + 1][0]
            line(f"bracket:{rate:g}", f"Top of {rate:.0%} bracket", lambda r: r.taxable_income - r.preferential_income, top,
                 ordinary_only=True)
        cg_top = law.capital_gains[fs].thresholds()[0]
        line("cg0", "Top of 0% capital gains band", lambda r: r.taxable_income, cg_top)
        try:
            plaw = self.law(yr.year + law.irmaa_lookback)
            for n, tier in enumerate(plaw.irmaa_tiers, start=1):
                line(f"irmaa:{n}", f"IRMAA tier {n} (premiums 2 years later)", lambda r: r.magi_irmaa,
                     tier.threshold(prof.filing_status))
        except KeyError:
            pass
        fpl = law.fpl(prof.fpl_household_size())
        line("aca:4", "ACA 400% of poverty line", lambda r: r.magi_aca, law.aca_cliff_pct * fpl)
        return out


def _gains_by_rate(tax: TaxResult, law: FederalLaw, fs: str) -> dict[float, float]:
    """Qualified dividends and long-term gains by the federal rate they are
    taxed at. They stack on top of ordinary taxable income; any part under
    the deductions is untaxed and counts as 0%."""
    brackets = law.capital_gains[status_key(fs)].brackets
    lo, hi = tax.taxable_income - tax.preferential_income, tax.taxable_income
    out: dict[float, float] = {}
    for k, (bottom, rate) in enumerate(brackets):
        top = brackets[k + 1][0] if k + 1 < len(brackets) else math.inf
        out[rate] = out.get(rate, 0.0) + max(0.0, min(hi, top) - max(lo, bottom))
    sheltered = max(0.0, tax.qualified_dividends + tax.net_ltcg - tax.preferential_income)
    if brackets:
        first = brackets[0][1]
        out[first] = out.get(first, 0.0) + sheltered
    return out


def _spending_source(a: Account) -> str | None:
    """The spending-order type an account belongs to."""
    if a.kind in ("cash", "taxable", "hsa"):
        return a.kind
    if a.kind in TRADITIONAL_KINDS:
        return "traditional"
    if a.kind == "inherited_ira":
        return "inherited"
    if a.kind in ROTH_KINDS:
        return "roth"
    return None


def _taxed_cover(takes, by_id) -> tuple[float, float]:
    """(realized gains, traditional withdrawals) in a set of deficit takes."""
    gains = sum(g for i, _, g in takes if by_id[i].kind == "taxable")
    ira = sum(amt for i, amt, _ in takes if by_id[i].kind in TRADITIONAL_KINDS or by_id[i].kind == "inherited_ira")
    return gains, ira


def sale_plan_other(sources: list[Source]):
    """Cash from rule withdrawals out of brokerage and cash accounts (already in sources)."""
    for s in sources:
        if s.key.startswith("withdrawal:"):
            yield (s.key, s.amount, 0.0)


def _conversion_totals(conversions: dict[str, tuple[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for dest, amount in conversions.values():
        out[dest] = out.get(dest, 0.0) + amount
    return out


def run_plan(plan: Plan, library: TaxLawLibrary, **kw) -> PlanResult:
    """Run the plan, leaving out anything the user switched off."""
    return PlanEngine(plan.active(), library).run(**kw)


# -- default visibility ----------------------------------------------------------


def default_line_visibility(result: PlanResult) -> dict[str, bool]:
    """Suggested lines: the bracket tops and IRMAA tiers the bars come near,
    the 0% gains band if there are gains, ACA if the household buys
    marketplace coverage. Every line can be turned on or off."""

    def near(key: str, slack: float = 1.15) -> bool:
        return any((yr.lines.get(key) if yr.lines.get(key) is not None else math.inf)
                   <= max(yr.total_income, 1.0) * slack for yr in result.years)

    vis: dict[str, bool] = {}
    keys = list(result.line_labels)
    brackets = [k for k in keys if k.startswith("bracket:")]
    reached = [k for k in brackets if near(k)]
    for k in brackets:
        vis[k] = k in reached and k != "bracket:0.1"
    above = [k for k in brackets if k not in reached]
    if above:
        vis[above[0]] = True  # the next bracket up, so the headroom shows
    medicare = any(
        result.plan.profile.people[i].on_medicare(yr.year + 2) for yr in result.years for i in yr.ages
    )
    for k in keys:
        if k.startswith("irmaa:"):
            vis[k] = medicare and near(k, 1.05)
    vis["aca:4"] = result.plan.profile.household.aca_marketplace
    vis["deduction"] = False
    vis["cg0"] = any(yr.category_totals()[c] > 0 for yr in result.years for c in PREFERENTIAL)
    vis["tax_free"] = True
    return vis


# -- text table ------------------------------------------------------------------


@dataclass(frozen=True)
class TableColumn:
    key: str
    label: str  # in the Columns menu
    default: bool  # shown unless the user picks otherwise
    tip: str = ""


# Year table columns, in table order. Year is always shown first. Some keys
# stand for a group: one column per income type (or source), per account,
# or per capital gains rate.
TABLE_COLUMNS: tuple[TableColumn, ...] = (
    TableColumn("ages", "Ages", True),
    TableColumn("filing", "Filing status", True),
    TableColumn("income", "Income by type", True,
                "One column per income type, or per source with\n\"Show each source separately\""),
    TableColumn("ss_withheld", "Social Security withheld (earnings test)", True,
                "Benefits held back for wages over the earnings test limit\n"
                "before full retirement age, one column per person. They are\n"
                "not lost: the benefit is raised at full retirement age.\n"
                "Shown only when the plan has some."),
    TableColumn("total_income", "Total income", True),
    TableColumn("agi", "AGI", True),
    TableColumn("taxable_income", "Taxable income", True),
    TableColumn("irmaa_magi", "IRMAA MAGI", True,
                "AGI plus tax-exempt interest;\nsets Medicare premiums two years later"),
    TableColumn("gains_by_rate", "Gains and qualified dividends by rate (0% / 15% / 20%)", False,
                "Qualified dividends and long-term gains by the federal rate\n"
                "they are taxed at; any under the deductions count as 0%"),
    TableColumn("marginal_rate", "Marginal rate on the next $1,000", False,
                "Federal and state tax on $1,000 more ordinary income\n"
                "(an IRA withdrawal or Roth conversion), as a rate. Shows the\n"
                "years where Social Security or the gains \"bump zone\" raise it."),
    TableColumn("credits", "Education credits", True,
                "American Opportunity and Lifetime Learning credits used this\n"
                "year; federal tax is after them. Shown only when the plan has some."),
    TableColumn("federal_tax", "Federal tax", True),
    TableColumn("federal_by_type", "Federal tax by type (ordinary / capital gains / NIIT)", False,
                "Federal tax split as on the Taxes by source graph:\n"
                "tax on ordinary income, on gains and qualified dividends,\n"
                "and the 3.8% net investment income tax, after credits"),
    TableColumn("state_tax", "State tax", True),
    TableColumn("irmaa", "IRMAA surcharge", True),
    TableColumn("aca", "ACA premiums and premium tax credit", True,
                "Marketplace premiums for the plans bought (grown at the health\n"
                "premium rate), the premium tax credit that pays part of them,\n"
                "and the net cost the cash flow pays. Shown only when the plan\n"
                "has some."),
    TableColumn("total_tax", "Total tax", False, "Federal and state tax plus IRMAA surcharges"),
    TableColumn("spending", "Spending", True, "The year's spending (Assumptions), grown with inflation"),
    TableColumn("net_cash_flow", "Net cash flow", True,
                "Income, Social Security and withdrawals by rule, less\n"
                "spending, tax, IRMAA, net ACA premiums and HSA contributions.\n"
                "A surplus is saved; a deficit is taken from the accounts\n"
                "in the spending order."),
    TableColumn("shortfall", "Shortfall", False, "Spending and tax the spending order could not cover"),
    TableColumn("rmds", "RMDs by account", False,
                "RMDs and required inherited IRA withdrawals, less any QCD\n"
                "from the account, one column per account"),
    TableColumn("conversions", "Roth conversions by account", False, "One column per account converted from"),
    TableColumn("withdrawals", "Other withdrawals by account", False,
                "Withdrawal rules, and sales or withdrawals that pay for spending,\n"
                "one column per account"),
    TableColumn("qcds", "QCDs by account", True,
                "Qualified charitable distributions: given straight to charity,\n"
                "not taxed, and counted toward the account's RMD.\n"
                "Shown only when the plan has some."),
    TableColumn("contributions", "HSA contributions by account", True,
                "Paid into each HSA from the year's cash flow and deducted\n"
                "from income (not in California or New Jersey).\n"
                "Shown only when the plan has some."),
    TableColumn("balances", "Balances by account", False, "Each account's balance at the end of the year"),
    TableColumn("total_balances", "Total balances", True, "All accounts at the end of the year"),
    TableColumn("notes", "Notes", True),
)
TABLE_COLUMN_KEYS = tuple(c.key for c in TABLE_COLUMNS)
DEFAULT_TABLE_COLUMNS = tuple(c.key for c in TABLE_COLUMNS if c.default)


@dataclass
class TableData:
    header: list[str]
    tips: list[str]
    numeric: list[bool]  # right-aligned
    rows: list[list[str]]


def table_data(result: PlanResult, *, todays_dollars: bool = False, columns: list[str] | tuple[str, ...] | None = None,
               detail: bool = False) -> TableData:
    """The year table: Year, then the chosen columns (by default the
    suggested set) in table order. With ``detail`` the income columns are
    one per source instead of one per type."""
    chosen = set(columns or DEFAULT_TABLE_COLUMNS)
    people = result.plan.profile.people
    # Accounts by the names they start with (the run renames the ones a survivor inherits).
    names = dict(result.account_labels)
    names.update({a.id: a.label(result.plan.profile) for a in result.plan.accounts})
    years = result.years
    cols: list[tuple[str, str, bool, object]] = [("Year", "", False, lambda yr: str(yr.year))]

    def money_col(label: str, tip: str, fn) -> None:
        cols.append((label, tip, True, fn))

    def account_ids(attr: str) -> list[str]:
        seen: list[str] = []
        for yr in years:
            for k in getattr(yr, attr):
                if k not in seen:
                    seen.append(k)
        order = list(result.account_labels)
        return sorted(seen, key=lambda k: order.index(k) if k in order else len(order))

    for c in TABLE_COLUMNS:
        if c.key not in chosen:
            continue
        k = c.key
        if k == "ages":
            cols.append(("Ages", "Age at the end of the year", False,
                         lambda yr: ", ".join(f"{people[i].name} {a}" for i, a in yr.ages.items())))
        elif k == "filing":
            cols.append(("Filing", "", False, lambda yr: yr.filing_status.upper()))
        elif k == "income":
            if detail:
                keys: dict[str, str] = {}
                for yr in years:
                    for src in yr.sources:
                        keys.setdefault(f"{src.category}|{src.key}", src.label)
                order = {cat: n for n, cat in enumerate(CATEGORIES)}
                for kk in sorted(keys, key=lambda x: order.get(x.split("|")[0], 99)):
                    cat, key = kk.split("|", 1)
                    money_col(keys[kk], CATEGORY_LABELS.get(cat, ""),
                              lambda yr, cat=cat, key=key: sum(s.amount for s in yr.sources
                                                               if s.category == cat and s.key == key))
            else:
                for cat in CATEGORIES:
                    if any(yr.category_totals()[cat] for yr in years):
                        money_col(CATEGORY_LABELS[cat], "", lambda yr, cat=cat: yr.category_totals()[cat])
        elif k == "ss_withheld":
            for i in sorted({i for yr in years for i in yr.ss_withheld}):
                money_col(f"{people[i].name} SS withheld", c.tip, lambda yr, i=i: yr.ss_withheld.get(i, 0.0))
        elif k == "total_income":
            money_col("Total income", "", lambda yr: yr.total_income)
        elif k == "agi":
            money_col("AGI", "Adjusted gross income", lambda yr: yr.tax.agi)
        elif k == "taxable_income":
            money_col("Taxable income", "", lambda yr: yr.tax.taxable_income)
        elif k == "irmaa_magi":
            money_col("IRMAA MAGI", c.tip, lambda yr: yr.tax.magi_irmaa)
        elif k == "gains_by_rate":
            rates = sorted({r for yr in years for r in yr.gains_by_rate})
            for r in rates:
                money_col(f"Gains taxed at {r:.0%}", c.tip, lambda yr, r=r: yr.gains_by_rate.get(r, 0.0))
        elif k == "marginal_rate":
            cols.append(("Marginal rate", c.tip, True, lambda yr: f"{100 * yr.marginal_rate:.1f}%"))
        elif k == "credits":
            if any(yr.tax.credits > 0.5 for yr in years):
                money_col("Education credits", c.tip, lambda yr: yr.tax.credits)
        elif k == "federal_tax":
            money_col("Federal tax", "", lambda yr: yr.tax.federal_tax)
        elif k == "federal_by_type":
            for key, label in (("ordinary", "Ordinary income tax"), ("ltcg", "Capital gains tax"), ("niit", "NIIT")):
                money_col(label, c.tip, lambda yr, key=key: yr.tax_by_source()[key])
        elif k == "state_tax":
            money_col("State tax", "", lambda yr: yr.tax.state_tax)
        elif k == "irmaa":
            money_col("IRMAA surcharge", "Medicare Part B and D surcharges paid this year", lambda yr: yr.irmaa_surcharge)
        elif k == "aca":
            if any(yr.tax.aca_premium > 0.5 for yr in years):
                money_col("ACA premiums", c.tip, lambda yr: yr.tax.aca_premium)
                money_col("ACA premium tax credit", c.tip, lambda yr: yr.tax.aca_ptc)
                money_col("ACA net premiums", c.tip, lambda yr: yr.tax.aca_net_premium)
        elif k == "total_tax":
            money_col("Total tax", c.tip, lambda yr: yr.tax.total_tax + yr.irmaa_surcharge)
        elif k == "spending":
            money_col("Spending", c.tip, lambda yr: yr.spending)
        elif k == "net_cash_flow":
            money_col("Net cash flow", c.tip, lambda yr: yr.net_cash_flow)
        elif k == "shortfall":
            money_col("Shortfall", c.tip, lambda yr: yr.shortfall)
        elif k in ("rmds", "conversions", "withdrawals", "qcds", "contributions", "balances"):
            attr = "balances_end" if k == "balances" else k
            word = {"rmds": "RMD", "conversions": "conversion", "withdrawals": "withdrawals",
                    "qcds": "QCD", "contributions": "contribution", "balances": "balance"}[k]
            ids = account_ids(attr)
            if k == "balances":
                ids = [i for i in ids if any(yr.balances_end.get(i, 0) > 0.5 or yr.balances_start.get(i, 0) > 0.5
                                             for yr in years)]
            for acct in ids:
                money_col(f"{names.get(acct, acct)} {word}", c.tip,
                          lambda yr, attr=attr, acct=acct: getattr(yr, attr).get(acct, 0.0))
        elif k == "total_balances":
            money_col("Total balances", c.tip, lambda yr: sum(yr.balances_end.values()))
        elif k == "notes":
            cols.append(("Notes", "", False, lambda yr: "; ".join(yr.notes)))

    rows = []
    for yr in years:
        f = 1 / result.deflator(yr.year) if todays_dollars else 1.0
        row = []
        for _label, _tip, numeric, fn in cols:
            v = fn(yr)
            if isinstance(v, str):
                row.append(v)
            else:
                row.append(f"{round(v * f):,}")
        rows.append(row)
    return TableData([c[0] for c in cols], [c[1] for c in cols], [c[2] for c in cols], rows)


def year_table(result: PlanResult, *, todays_dollars: bool = False, columns: list[str] | tuple[str, ...] | None = None,
               detail: bool = False) -> tuple[list[str], list[list[str]]]:
    """The plan as rows of text: for the data table tab, CSV export and screen readers."""
    t = table_data(result, todays_dollars=todays_dollars, columns=columns, detail=detail)
    return t.header, t.rows
