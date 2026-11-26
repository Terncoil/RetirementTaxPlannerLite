"""The forward-looking plan: a baseline profile plus accounts, income
streams, Social Security choices, assumptions and a short rule list.

Design points (see docs/design.md, "Forward-looking plan"):

* Ages are the age a person reaches by December 31 of the year. Timings
  are stored against one person's age (or, if the user prefers, a
  calendar year) and shown in whatever form is convenient.
* Accounts are individual: each spouse can have several of each type.
  The graph groups them by type unless asked to show each one.
* Rules move money between accounts (Roth conversions, withdrawals), out
  to charity (qualified charitable distributions) or into an HSA from the
  household's cash flow (HSA contributions). For
  each account and kind of rule, the rule with the narrowest time span
  wins in a year, so a one-year rule overrides a range rule.
* Withdrawals happen where a rule (or an RMD) says so. Money left over
  after spending and taxes goes into the taxable account; a deficit is
  taken from the account types in the spending order (cash, then the
  taxable account, unless the plan says otherwise), and anything still
  short is reported as a shortfall.
* The plan keeps its own copy of the baseline (people, birth years, life
  expectancies, inflation), so two plan files can differ in who dies when.

Plan files carry a schema version. Older files are migrated on load (see
``_migrate``); saving writes the current version.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .profile import Profile

PLAN_SCHEMA_VERSION = 2

ACCOUNT_KINDS = {
    "traditional_ira": "Traditional IRA",
    "401k": "401(k)/403(b)",
    "inherited_ira": "Inherited IRA",
    "roth_ira": "Roth IRA",
    "roth_401k": "Roth 401(k)",
    "hsa": "HSA",
    "taxable": "Taxable brokerage",
    "cash": "Cash / savings",
}
# Income kinds whose yearly amount may be negative (a capital loss).
GAIN_INCOME_KINDS = ("ltcg", "stcg")
TRADITIONAL_KINDS = ("traditional_ira", "401k")
ROTH_KINDS = ("roth_ira", "roth_401k")

INCOME_KINDS = {
    "wages": "Wages",
    "pension": "Pension",
    "foreign_pension": "Foreign pension",
    "annuity": "Annuity",
    "interest": "Interest",
    "qualified_dividends": "Qualified dividends",
    "ltcg": "Long-term capital gain",
    "stcg": "Short-term capital gain",
    "other_ordinary": "Other ordinary income",
    "tax_exempt": "Tax-exempt income",
}

# Who inherited an inherited IRA. The app works out the withdrawal rule
# from this and the year the original owner died.
BENEFICIARY_KINDS = {
    "spouse": "Spouse",
    "minor_child": "Minor child of the original owner",
    "disabled": "Disabled or chronically ill",
    "close_in_age": "Not more than 10 years younger than the owner",
    "other": "Other person",
    "estate": "Estate or trust",
}
# Eligible designated beneficiaries (deaths in 2020 or later): lifetime
# withdrawals, no 10-year limit. A minor child stretches only until 21.
EDB_KINDS = ("spouse", "disabled", "close_in_age")

# How an income stream's amount rises after its first year.
COLA_KINDS = {
    "none": "None",
    "inflation": "Inflation",
    "ss_cola": "Social Security COLA",
    "fixed": "Fixed %",
}
# Income kinds that are said to have a COLA (others have a yearly increase).
COLA_INCOME_KINDS = ("wages", "pension", "foreign_pension", "annuity")

# How a fixed-amount rule's amount changes from year to year.
RULE_GROWTH_KINDS = {
    "none": "None (the same dollars every year)",
    "inflation": "Inflation (amount in today's dollars)",
    "fixed": "Fixed % a year after the first year",
}

# Where money for spending comes from when income falls short, in order.
SPENDING_SOURCES = {
    "cash": "Cash / savings",
    "taxable": "Taxable brokerage",
    "traditional": "Traditional IRA / 401(k)",
    "inherited": "Inherited IRA",
    "roth": "Roth IRA / Roth 401(k)",
    "hsa": "HSA",
}
DEFAULT_SPENDING_ORDER = ("cash", "taxable")

# Timing events: points in time tied to deaths rather than an age or year.
#   death:<i>    person i dies
#   first_death  the first of the two to die
#   last_death   the end of the plan (the last person dies)
WHEN_EVENTS = ("death:0", "death:1", "first_death", "last_death")

RULE_KINDS = {
    "roth_conversion": "Roth conversion",
    "withdrawal": "Withdrawal",
    "qcd": "Qualified charitable distribution",
    "hsa_contribution": "HSA contribution",
}
# Rules that put money into their account rather than take it out.
CONTRIBUTION_KINDS = ("hsa_contribution",)

# HSA contributions (IRC 223) need high-deductible health plan coverage
# and no Medicare; there is no earned-income test. The limit depends on
# the coverage: self-only, or family (shared by spouses, who can split it
# between their HSAs). From 55 each person can add a catch-up, but only to
# an HSA they own (FederalLaw.hsa_self_only, hsa_family, hsa_catch_up).
HSA_COVERAGE = {
    "self": "Self-only",
    "family": "Family",
}
HSA_CATCH_UP_AGE = 55
# Medicare Part A starts the month a person turns 65 (so, with only a
# birth year, as early as January of that year). If they enroll later, by
# claiming Social Security after 65 for example, Part A is backdated up
# to 6 months, but not before 65.
HSA_MEDICARE_AGE = 65
HSA_PART_A_LOOKBACK = 0.5  # years

# Qualified charitable distributions (QCDs) go straight from an IRA to a
# charity: left out of income, counted toward the account's RMD, up to a
# yearly limit per IRA owner (FederalLaw.qcd_limit). Only IRAs qualify:
# traditional (including SEP and SIMPLE IRAs no longer receiving employer
# contributions) and inherited IRAs; not 401(k)s or 403(b)s.
QCD_KINDS = ("traditional_ira", "inherited_ira")
# The owner must be 70 1/2 on the day of the gift. With only a birth year,
# that is certain from the year they turn 71.
QCD_MIN_AGE = 71

# How a rule decides its amount.
AMOUNT_MODES = {
    "fixed": "Fixed amount each year",
    "fill": "Fill up to a target",
    "spread": "Spread the balance evenly over the remaining years of the rule (1/N)",
    "all": "Take the whole balance",
    "rmd": "The year's RMD from the account (QCDs only)",
    "max": "The most allowed (contributions only)",
}
# The amount modes each rule kind can use.
RULE_MODES = {
    "roth_conversion": ("fixed", "fill", "spread", "all"),
    "withdrawal": ("fixed", "fill", "spread", "all"),
    "qcd": ("fixed", "rmd"),
    "hsa_contribution": ("max", "fixed"),
}

# Targets for "fill" rules: what to fill up to.
#   bracket:<rate>   top of an ordinary bracket, e.g. bracket:0.22
#   cg0              top of the 0% capital gains band
#   irmaa:<n>        just under IRMAA tier n (income two years before the premium year)
#   aca:<pct>        ACA MAGI at this % of the poverty line, e.g. aca:4.0
#   magi:<amount>    a MAGI amount in that year's dollars
FILL_TARGET_HELP = "bracket:0.22, cg0, irmaa:1, aca:4.0 or magi:150000"


def death_year(profile: Profile, i: int) -> int:
    """The last year person ``i`` is alive in the plan."""
    p = profile.people[min(i, len(profile.people) - 1)]
    return p.birth_year + int(p.life_expectancy)


@dataclass
class When:
    """A point in time: a person's age, a calendar year, or an event
    (a death, or the end of the plan; see WHEN_EVENTS).

    An event is the year of the death. As a start it means the year after
    (the first year without that person); as an end, the death year itself.
    """

    age: int | None = None
    person: int = 0
    year: int | None = None
    event: str | None = None

    def resolve(self, profile: Profile, *, start: bool = False) -> int:
        if self.event is not None:
            return self.event_year(profile) + (1 if start else 0)
        if self.year is not None:
            return int(self.year)
        if self.age is None:
            raise ValueError("A timing needs an age, a year or an event")
        idx = min(self.person, len(profile.people) - 1)
        return profile.people[idx].birth_year + int(self.age)

    def event_year(self, profile: Profile) -> int:
        years = [death_year(profile, i) for i in range(len(profile.people))]
        kind, _, arg = (self.event or "").partition(":")
        if kind == "death":
            return years[min(int(arg or 0), len(years) - 1)]
        if kind == "first_death":
            return min(years)
        if kind == "last_death":
            return max(years)
        raise ValueError(f"Unknown timing event {self.event!r}")

    def describe(self, profile: Profile, *, start: bool = False) -> str:
        if self.event is not None:
            return event_label(self.event, profile, start=start)
        if self.year is not None:
            return str(self.year)
        idx = min(self.person, len(profile.people) - 1)
        return f"{profile.people[idx].name} age {self.age}"

    @classmethod
    def from_dict(cls, d: dict | int | None) -> "When | None":
        if d is None:
            return None
        if isinstance(d, int):
            return cls(year=d)
        return cls(**d)


def event_label(event: str, profile: Profile, *, start: bool = False) -> str:
    """ "Alex dies", "After the first death", "End of plan"."""
    kind, _, arg = event.partition(":")
    if kind == "death":
        name = profile.people[min(int(arg or 0), len(profile.people) - 1)].name
        return f"After {name} dies" if start else f"{name} dies"
    if kind == "first_death":
        return "After the first death" if start else "First death"
    if kind == "last_death":
        return "End of plan"
    return event


def events_for(profile: Profile, *, start: bool = False) -> list[str]:
    """The events a timing can use: each person's death, the first death
    (couples only), and, for ends, the end of the plan."""
    out = [f"death:{i}" for i in range(len(profile.people))]
    if len(profile.people) > 1:
        out.append("first_death")
    if not start:
        out.append("last_death")
    return out


@dataclass(frozen=True)
class InheritedRule:
    """How an inherited IRA must be emptied, worked out from who inherited it."""

    stretch: bool  # yearly RMDs over a life expectancy, no 10-year limit (yet)
    deadline: int | None  # last year: the account must be empty by its end
    annual_rmds: bool  # yearly RMDs before the deadline
    owner_life: bool = False  # RMDs over the original owner's remaining life (estate or trust)
    text: str = ""


@dataclass
class Account:
    id: str
    kind: str
    owner: int | None = 0  # index into profile.people; None for joint taxable/cash
    name: str = ""
    balance: float = 0.0
    return_rate: float | None = None  # None = the plan's default return
    # Taxable brokerage
    cost_basis: float | None = None  # None = equal to the balance
    interest_yield: float = 0.0  # taxed as ordinary income, reinvested
    qualified_dividend_yield: float = 0.0
    nonqualified_dividend_yield: float = 0.0
    cap_gain_distribution_yield: float = 0.0
    # Traditional accounts
    spouse_sole_beneficiary: bool = True  # allows the Joint and Last Survivor table
    # Inherited IRA
    inherited_year: int | None = None  # year the original owner died
    original_owner_birth_year: int | None = None
    owner_started_rmds: bool = True  # the owner died on or after their RMD start
    beneficiary: str = "other"  # who inherited it: a key of BENEFICIARY_KINDS
    # Traditional and inherited: the year whose RMD is already out of the
    # entered balance (a plan started mid-year). It applies only while that
    # year is the plan's first year, so it never needs unticking.
    rmd_taken_year: int | None = None
    enabled: bool = True  # False leaves the account (and rules on it) out, for what-ifs

    def label(self, profile: Profile) -> str:
        if self.name:
            return self.name
        kind = ACCOUNT_KINDS.get(self.kind, self.kind)
        if self.owner is None or self.owner >= len(profile.people):
            return kind
        return f"{profile.people[self.owner].name} {kind}"

    @property
    def is_traditional(self) -> bool:
        return self.kind in TRADITIONAL_KINDS

    @property
    def is_roth(self) -> bool:
        return self.kind in ROTH_KINDS

    def inherited_rule(self, profile: Profile) -> InheritedRule | None:
        """The withdrawal rule for an inherited IRA (None for other accounts).

        * Estate or trust: the 5-year rule if the owner died before their
          RMDs started, otherwise yearly RMDs over the owner's remaining
          life expectancy.
        * Deaths before 2020: lifetime withdrawals for any person.
        * Deaths in 2020 or later: lifetime withdrawals for an eligible
          designated beneficiary (spouse, disabled or chronically ill, not
          more than 10 years younger); a minor child stretches until 21,
          then has 10 years; anyone else has the 10-year rule, with yearly
          RMDs if the owner had started them.
        """
        if self.kind != "inherited_ira" or self.inherited_year is None:
            return None
        y = self.inherited_year
        if self.beneficiary == "estate":
            if self.owner_started_rmds:
                return InheritedRule(True, None, True, owner_life=True,
                                     text="Yearly withdrawals over the original owner's remaining life expectancy")
            return InheritedRule(False, y + 5, False, text=f"5-year rule: empty by {y + 5}")
        if y < 2020:
            return InheritedRule(True, None, True, text="Lifetime withdrawals: inherited before 2020")
        if self.beneficiary in EDB_KINDS:
            return InheritedRule(True, None, True, text="Lifetime withdrawals (eligible designated beneficiary)")
        if self.beneficiary == "minor_child" and self.owner is not None and self.owner < len(profile.people):
            turns_21 = profile.people[self.owner].birth_year + 21
            if turns_21 > y:
                deadline = turns_21 + 10
                return InheritedRule(True, deadline, True,
                                     text=f"Lifetime withdrawals until age 21 ({turns_21}), then the 10-year "
                                          f"rule: empty by {deadline}")
        return InheritedRule(False, y + 10, self.owner_started_rmds, text=f"10-year rule: empty by {y + 10}")

    def deadline_year(self, profile: Profile) -> int | None:
        """Last year of the 10-year (or 5-year) rule, if one applies."""
        rule = self.inherited_rule(profile)
        return rule.deadline if rule else None


@dataclass
class IncomeStream:
    """A recurring (or one-time) income: wages, a pension, a home sale gain."""

    kind: str
    amount: float  # yearly amount in the first year it is paid
    owner: int | None = 0
    name: str = ""
    start: When | None = None  # None = from the plan's first year
    end: When | None = None  # None = for life (or to the end of the plan); inclusive
    cola: str = "fixed"  # how the amount rises after the first year: a key of COLA_KINDS
    growth: float = 0.0  # the yearly rate for a "fixed" COLA (raises, a pension's set COLA)
    survivor_pct: float = 0.0  # share that continues to the survivor (pensions)
    federal_taxable_pct: float = 1.0
    state_taxable_pct: float = 1.0  # not used yet: the state taxes what the federal return does
    enabled: bool = True  # False leaves the stream out, for what-ifs

    def label(self, profile: Profile) -> str:
        if self.name:
            return self.name
        kind = INCOME_KINDS.get(self.kind, self.kind)
        if self.owner is None or self.owner >= len(profile.people):
            return kind
        return f"{profile.people[self.owner].name} {kind.lower()}"

    def rate(self, plan: "Plan") -> float:
        """The yearly increase this stream gets."""
        return {"none": 0.0, "inflation": plan.inflation, "ss_cola": plan.ss_cola}.get(self.cola, self.growth)


@dataclass
class SocialSecurity:
    fra_benefit: float = 0.0  # yearly benefit at full retirement age, today's dollars
    claim_age: float = 67.0  # years, may include months as a fraction (67.5)
    current_benefit: float | None = None  # already claimed: the yearly amount being paid now
    ssdi: bool = False  # receiving disability; becomes a retirement benefit at FRA
    # With SSDI: a claim_age above full retirement age suspends the benefit
    # at FRA and restarts it at claim_age with delayed credits (8% a year).
    # ssdi_end_age ends SSDI early (a return to work); the benefit then
    # starts again at claim_age as an ordinary claim.
    ssdi_end_age: float | None = None


@dataclass
class PersonPlan:
    """A person's plan choices. Birth year and life expectancy are on the
    baseline (the plan's own copy of it)."""

    social_security: SocialSecurity = field(default_factory=SocialSecurity)


@dataclass
class Assumptions:
    # Inflation is the baseline's (one rate for the whole plan).
    ss_cola: float | None = None  # None = inflation
    return_rate: float = 0.05  # default for accounts with no return of their own
    cash_return: float = 0.03
    spending: float = 0.0  # yearly spending, today's dollars, grows with inflation
    survivor_spending_pct: float = 1.0  # share of spending after the first death (couples)
    # Account types that pay for spending when income falls short, in order.
    spending_order: list[str] = field(default_factory=lambda: list(DEFAULT_SPENDING_ORDER))
    prior_magi: dict[str, float] = field(default_factory=dict)  # {"2025": 180000} for IRMAA
    # Yearly growth of ACA marketplace premiums (the baseline's benchmark
    # and plan premiums), usually faster than inflation.
    health_premium_growth: float = 0.05


@dataclass
class Rule:
    kind: str  # a key of RULE_KINDS
    account: str  # source account id (for a contribution, the account paid into)
    start: When
    end: When | None = None  # None = a one-year rule
    mode: str = "fixed"
    amount: float = 0.0  # for "fixed"
    target: str = ""  # for "fill"
    growth: str = "none"  # for "fixed": how the amount changes, a key of RULE_GROWTH_KINDS
    growth_rate: float = 0.0  # for growth "fixed"
    to_account: str | None = None  # Roth account for conversions (default: owner's Roth IRA)
    coverage: str = "self"  # HSA contributions: the owner's health plan coverage, a key of HSA_COVERAGE
    enabled: bool = True
    note: str = ""

    def years(self, profile: Profile) -> tuple[int, int]:
        """First and last year the rule applies. Nothing in a plan starts
        before the plan's first year (the profile's year), so an earlier
        start counts from then."""
        first = self.start.resolve(profile, start=True)
        last = self.end.resolve(profile) if self.end else first
        return max(first, profile.year), last

    def fixed_amount(self, plan: "Plan", year: int) -> float:
        """The "fixed" amount for ``year``: flat, grown with inflation from
        today's dollars, or grown by the rule's own rate after its first year."""
        if self.growth == "inflation":
            return self.amount * (1 + plan.inflation) ** (year - plan.start_year)
        if self.growth == "fixed":
            first, _ = self.years(plan.profile)
            return self.amount * (1 + self.growth_rate) ** max(0, year - first)
        return self.amount

    def describe(self, profile: Profile, accounts: dict[str, Account]) -> str:
        acct = accounts.get(self.account)
        name = acct.label(profile) if acct else self.account
        when = self.start.describe(profile, start=True)
        if self.end:
            when += f" to {self.end.describe(profile)}"
        how = {
            "fixed": f"${self.amount:,.0f}",
            "fill": f"fill to {self.target}",
            "spread": "spread evenly (1/N)",
            "all": "whole balance",
            "rmd": "the RMD",
            "max": "the most allowed",
        }.get(self.mode, self.mode)
        if self.kind == "hsa_contribution":
            cover = HSA_COVERAGE.get(self.coverage, self.coverage).lower()
            return f"HSA contribution to {name} ({cover} coverage), {when}, {how}"
        return f"{RULE_KINDS.get(self.kind, self.kind)} from {name}, {when}, {how}"


@dataclass
class Plan:
    profile: Profile
    name: str = ""  # the plan's own name, e.g. "Roth conversions until Social Security"
    people: list[PersonPlan] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    incomes: list[IncomeStream] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    assumptions: Assumptions = field(default_factory=Assumptions)
    # Year table columns the user picked (keys of TABLE_COLUMNS in
    # rtpl.engine.forward); empty means the suggested set.
    table_columns: list[str] = field(default_factory=list)
    schema_version: int = PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        while len(self.people) < len(self.profile.people):
            self.people.append(PersonPlan())

    @property
    def start_year(self) -> int:
        return self.profile.year

    @property
    def inflation(self) -> float:
        """The plan's one inflation rate, from its baseline."""
        return self.profile.inflation

    @property
    def ss_cola(self) -> float:
        c = self.assumptions.ss_cola
        return self.inflation if c is None else c

    def death_year(self, i: int) -> int:
        """The last year person ``i`` is alive (from the baseline's life expectancy)."""
        return death_year(self.profile, i)

    @property
    def end_year(self) -> int:
        """The plan ends when the last person dies."""
        return max([self.start_year] + [self.death_year(i) for i in range(len(self.profile.people))])

    @property
    def title(self) -> str:
        """Baseline name and plan name, e.g. "Smith household - Roth conversions until SS"."""
        parts = [p for p in (self.profile.name.strip(), self.name.strip()) if p]
        return " - ".join(parts) or "Plan"

    def active(self) -> "Plan":
        """The plan without the accounts, income streams and rules the user
        switched off (and without rules on a switched-off account)."""
        accounts = [a for a in self.accounts if a.enabled]
        ids = {a.id for a in accounts}
        return replace(
            self,
            accounts=accounts,
            incomes=[s for s in self.incomes if s.enabled],
            rules=[r for r in self.rules if r.enabled and r.account in ids],
        )

    def new_account_id(self, kind: str, owner: int | None) -> str:
        """A readable id not used by another account, e.g. "p1_traditional_ira_2"."""
        stem = f"{'joint' if owner is None else f'p{owner + 1}'}_{kind}"
        used = {a.id for a in self.accounts}
        if stem not in used:
            return stem
        n = 2
        while f"{stem}_{n}" in used:
            n += 1
        return f"{stem}_{n}"

    def rmd_taken_now(self, acct: Account) -> bool:
        """True when the account's first-year RMD is already out of its balance."""
        return acct.rmd_taken_year is not None and acct.rmd_taken_year == self.start_year

    def account(self, acct_id: str) -> Account | None:
        return next((a for a in self.accounts if a.id == acct_id), None)

    def medicare_start(self, i: int) -> float | None:
        """When person ``i``'s Medicare Part A starts, as a year with the
        month as a fraction (2031.25 = April 2031), for HSA contributions.
        None when they are on Medicare for the whole plan.

        * Medicare on the baseline (before 65, for example on SSDI): always.
        * By default, the month they turn 65; with only a birth year, the
          plan takes January of that year so contributions never run over.
        * Not enrolled at 65 (the baseline says no Medicare Part B, say on
          an employer's plan): Part A starts when Social Security does
          (claiming it after 65 enrolls them), backdated up to 6 months,
          but not before 65.
        """
        p = self.profile.people[i]
        if p.medicare_part_b is True:
            return None
        at_65 = float(p.birth_year + HSA_MEDICARE_AGE)
        if p.medicare_part_b is None:
            return at_65
        ss = self.people[i].social_security if i < len(self.people) else None
        if ss is None or ss.current_benefit is not None or ss.ssdi:
            claimed = float(self.start_year - 1)
        else:
            claimed = p.birth_year + float(ss.claim_age)
        return max(at_65, claimed - HSA_PART_A_LOOKBACK)

    def hsa_months(self, i: int, year: int) -> int:
        """Months of ``year`` person ``i`` can contribute to an HSA (before
        Medicare). The plan assumes HSA-eligible health coverage while the
        contribution rule runs."""
        start = self.medicare_start(i)
        if start is None:
            return 0
        return round(12 * min(1.0, max(0.0, start - year)))

    def hsa_last_year(self, i: int) -> int | None:
        """The last year person ``i`` can put anything into an HSA (None if never)."""
        start = self.medicare_start(i)
        if start is None:
            return None
        last = math.ceil(start) - 1
        return last if last >= self.start_year else None

    def timing_notes(self) -> list[str]:
        """Rules and income streams that start before the plan's first year
        or end before they start. Not errors: the plan counts an early start
        from its first year, and an end before the start means never."""
        notes = []
        y0, prof = self.start_year, self.profile
        items = [(r.describe(prof, {a.id: a for a in self.accounts}), r.start, r.end) for r in self.rules]
        items += [(s.label(prof), s.start, s.end) for s in self.incomes]
        for name, start, end in items:
            first = start.resolve(prof, start=True) if start else y0
            if first < y0:
                notes.append(f"{name}: starts in {first}, before the plan's first year; counted from {y0}")
            if end is not None and end.resolve(prof) < max(first, y0):
                notes.append(f"{name}: ends before it starts, so it never applies")
        return notes

    def validate(self) -> list[str]:
        problems = self.profile.validate()
        ids = [a.id for a in self.accounts]
        if len(ids) != len(set(ids)):
            problems.append("Account ids must be unique")
        for a in self.accounts:
            if a.kind not in ACCOUNT_KINDS:
                problems.append(f"Account {a.id}: unknown kind {a.kind!r}")
            if a.kind == "inherited_ira" and a.inherited_year is None:
                problems.append(f"Account {a.id}: an inherited IRA needs the year it was inherited")
            if a.beneficiary not in BENEFICIARY_KINDS:
                problems.append(f"Account {a.id}: unknown beneficiary {a.beneficiary!r}")
        for s in self.incomes:
            if s.kind not in INCOME_KINDS:
                problems.append(f"Income {s.name or s.kind}: unknown kind {s.kind!r}")
            if s.cola not in COLA_KINDS:
                problems.append(f"Income {s.name or s.kind}: unknown COLA {s.cola!r}")
        for k in self.assumptions.spending_order:
            if k not in SPENDING_SOURCES:
                problems.append(f"Spending order: unknown account type {k!r}")
        for w in [r.start for r in self.rules] + [r.end for r in self.rules] + \
                [s.start for s in self.incomes] + [s.end for s in self.incomes]:
            if w is not None and w.event is not None and w.event not in WHEN_EVENTS:
                problems.append(f"Unknown timing event {w.event!r}")
        for r in self.rules:
            if r.kind not in RULE_KINDS:
                problems.append(f"Rule: unknown kind {r.kind!r}")
            if r.mode not in AMOUNT_MODES:
                problems.append(f"Rule: unknown amount mode {r.mode!r}")
            elif r.kind in RULE_MODES and r.mode not in RULE_MODES[r.kind]:
                problems.append(f"Rule: {RULE_KINDS[r.kind]} can't use the amount mode {r.mode!r}")
            if r.account not in ids:
                problems.append(f"Rule: no account with id {r.account!r}")
            if r.mode == "fill" and not r.target:
                problems.append("Rule: a fill rule needs a target such as " + FILL_TARGET_HELP)
            if r.growth not in RULE_GROWTH_KINDS:
                problems.append(f"Rule: unknown amount growth {r.growth!r}")
            if r.kind == "hsa_contribution":
                acct = self.account(r.account)
                if acct is not None and (acct.kind != "hsa" or acct.owner is None):
                    problems.append(f"Rule: HSA contributions go into an HSA owned by one person ({r.account!r})")
                if r.coverage not in HSA_COVERAGE:
                    problems.append(f"Rule: unknown HSA coverage {r.coverage!r}")
        return problems

    # -- persistence --------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        data = _migrate(data)
        profile = Profile.from_dict(data.pop("profile"))
        people = []
        for p in data.get("people", []):
            p = dict(p)
            ss = SocialSecurity(**p.pop("social_security", {}))
            people.append(PersonPlan(social_security=ss, **p))
        accounts = [Account(**a) for a in data.get("accounts", [])]
        incomes = []
        for s in data.get("incomes", []):
            s = dict(s)
            s["start"] = When.from_dict(s.get("start"))
            s["end"] = When.from_dict(s.get("end"))
            incomes.append(IncomeStream(**s))
        rules = []
        for r in data.get("rules", []):
            r = dict(r)
            r["start"] = When.from_dict(r["start"])
            r["end"] = When.from_dict(r.get("end"))
            rules.append(Rule(**r))
        assumptions = Assumptions(**data.get("assumptions", {}))
        return cls(profile=profile, name=data.get("name", ""), people=people, accounts=accounts,
                   incomes=incomes, rules=rules, assumptions=assumptions,
                   table_columns=list(data.get("table_columns", [])))

    def save(self, path: str | Path) -> None:
        data = self.to_dict()
        data["schema_version"] = PLAN_SCHEMA_VERSION
        Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Plan":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_profile(cls, profile: Profile) -> "Plan":
        """Start a plan from a marginal-graph profile.

        Wages, pensions and Social Security become streams that continue;
        IRA withdrawals and one-off household amounts are left out, since
        the plan works those out from accounts and rules.
        """
        people = []
        incomes = []
        for i, p in enumerate(profile.people):
            ss = SocialSecurity()
            if p.social_security:
                age = p.age_in(profile.year)
                ss = SocialSecurity(fra_benefit=p.social_security, claim_age=min(age, 70),
                                    current_benefit=p.social_security)
            people.append(PersonPlan(social_security=ss))
            if p.wages:
                incomes.append(IncomeStream(kind="wages", amount=p.wages, owner=i, cola="inflation"))
            if p.pension:
                incomes.append(IncomeStream(kind="pension", amount=p.pension, owner=i, cola="none"))
        h = profile.household
        for kind, amount in (("interest", h.taxable_interest), ("qualified_dividends", h.qualified_dividends),
                             ("tax_exempt", h.tax_exempt_interest), ("other_ordinary", h.other_ordinary_income)):
            if amount:
                incomes.append(IncomeStream(kind=kind, amount=amount, owner=None, cola="none"))
        return cls(profile=profile, people=people, incomes=incomes,
                   assumptions=Assumptions())


def _migrate(data: dict) -> dict:
    """Bring an older plan file's dictionary up to the current schema.

    Version 1 (to 0.2.x) to 2:

    * ``people[i].death_age`` and ``assumptions.end_age`` become each
      person's ``life_expectancy`` on the plan's baseline (a person with no
      death age lived to the end age, so that is their life expectancy).
    * ``assumptions.inflation`` (a plan-level override) becomes the
      baseline's inflation.
    * Income ``growth`` becomes a "Fixed %" COLA (or "None" when it was 0).
    * ``eligible_designated_beneficiary`` becomes the beneficiary choice.
    * ``spending_order`` starts as cash, then brokerage, as before.
    """
    data = copy.deepcopy(data)
    version = int(data.get("schema_version", 1))
    if version < 2:
        profile = data.setdefault("profile", {})
        assumptions = data.setdefault("assumptions", {})
        end_age = assumptions.pop("end_age", 95)
        inflation = assumptions.pop("inflation", None)
        if inflation is not None:
            profile["inflation"] = inflation
        people = data.get("people", [])
        for i, person in enumerate(profile.get("people", [])):
            death = people[i].get("death_age") if i < len(people) else None
            person.setdefault("life_expectancy", int(death if death is not None else end_age))
        for p in people:
            p.pop("death_age", None)
        for s in data.get("incomes", []):
            if "cola" not in s:
                s["cola"] = "fixed" if s.get("growth") else "none"
        for a in data.get("accounts", []):
            edb = a.pop("eligible_designated_beneficiary", False)
            a.setdefault("beneficiary", "close_in_age" if edb else "other")
        assumptions.setdefault("spending_order", list(DEFAULT_SPENDING_ORDER))
    data["schema_version"] = PLAN_SCHEMA_VERSION
    return data
