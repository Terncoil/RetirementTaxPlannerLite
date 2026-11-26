"""The household's baseline financial starting point.

A profile is a plain JSON document so it can be saved, shared and later
extended for the forward-looking plan (which will add assumptions and an
event list alongside this baseline rather than changing it).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

FILING_STATUS_LABELS = {
    "single": "Single",
    "mfj": "Married filing jointly",
    "mfs": "Married filing separately",
    "hoh": "Head of household",
    "qss": "Qualifying surviving spouse",
}


ACA_PLAN_TIERS = {
    "bronze": "Bronze",
    "silver": "Silver",
    "gold": "Gold or Platinum",
}


@dataclass
class Person:
    name: str = "Person 1"
    birth_year: int = 1960
    # Age at death in the forward plan (alive through the year they reach
    # it). The plan ends when the last person dies. The marginal graph
    # doesn't use it.
    life_expectancy: int = 95
    # None means "work it out from age" (enrolled at 65+). Set True for
    # people on Medicare before 65 (for example SSDI recipients), or False
    # for people 65+ who are not enrolled in Part B.
    medicare_part_b: bool | None = None
    blind: bool = False
    social_security: float = 0.0  # annual gross benefit
    wages: float = 0.0
    pension: float = 0.0
    ira_distributions: float = 0.0  # traditional IRA / 401(k), taxable part
    # ACA marketplace coverage for this person, as monthly premiums before
    # any subsidy: the benchmark (second-lowest-cost Silver) plan, which
    # sets the premium tax credit, and the plan they would buy (0 = the
    # benchmark). Used only while they are not on Medicare.
    aca_benchmark_premium: float = 0.0
    aca_plan_premium: float = 0.0

    def age_in(self, year: int) -> int:
        """Age at the end of ``year`` (what the IRS uses for 65+ tests)."""
        return year - self.birth_year

    def on_medicare(self, year: int) -> bool:
        if self.medicare_part_b is not None:
            return self.medicare_part_b
        return self.age_in(year) >= 65


@dataclass
class Household:
    """Income that belongs to the household rather than one person."""

    taxable_interest: float = 0.0
    tax_exempt_interest: float = 0.0
    qualified_dividends: float = 0.0
    nonqualified_dividends: float = 0.0
    long_term_capital_gains: float = 0.0  # net; may be negative
    short_term_capital_gains: float = 0.0  # net; may be negative; taxed as ordinary
    other_ordinary_income: float = 0.0
    other_investment_income: float = 0.0  # rents, royalties, passive (in NIIT)
    # Signed change to the interest and dividends a state taxes on their own
    # (NH to 2024, TN in 2020): minus U.S. Treasury or in-state municipal
    # interest the state exempts, plus interest it taxes that the profile
    # leaves out (TN's corporate bond interest). No effect on other taxes.
    state_investment_income_adjustment: float = 0.0
    adjustments: float = 0.0  # above-the-line deductions (HSA, etc.)
    # HSA contributions deducted (forward plans; the marginal graph puts
    # them in adjustments). Kept apart so states without the deduction can
    # add them back.
    hsa_deduction: float = 0.0
    itemized_deductions: float = 0.0  # 0 = take the standard deduction
    # Cash gifts to charity this year. From 2026, those who take the
    # standard deduction also deduct them up to a cap ($1,000, or $2,000
    # married filing jointly); itemizers count them in itemized_deductions.
    charitable_cash_gifts: float = 0.0
    has_taxable_brokerage: bool = False
    aca_marketplace: bool = False  # someone buys ACA marketplace coverage
    # Metal tier of the plan bought on the marketplace: cost-sharing
    # reductions (the 250% FPL line) only come with a Silver plan.
    aca_plan_tier: str = "silver"
    # Education credits: students in their first four years of college
    # (American Opportunity credit), each one's qualified expenses, and
    # expenses for the Lifetime Learning credit (other students, such as
    # graduate school). The forward plan claims them through
    # ``education_last_year`` (None = the profile's year only).
    aotc_students: int = 0
    aotc_expenses_per_student: float = 4000.0
    llc_expenses: float = 0.0
    education_last_year: int | None = None
    household_size: int | None = None  # for FPL; None = number of people


@dataclass
class Profile:
    year: int = 2026
    filing_status: str = "single"
    state: str | None = None
    people: list[Person] = field(default_factory=lambda: [Person()])
    household: Household = field(default_factory=Household)
    name: str = "My plan"
    # Inflation used to project tax law for years with no data file yet
    # (for example the IRMAA brackets two years out), and the forward
    # plan's one inflation rate.
    inflation: float = 0.025
    schema_version: int = SCHEMA_VERSION

    # -- derived ------------------------------------------------------
    @property
    def married(self) -> bool:
        return self.filing_status in ("mfj", "mfs")

    def ages(self) -> list[int]:
        return [p.age_in(self.year) for p in self.people]

    def fpl_household_size(self) -> int:
        return self.household.household_size or max(1, len(self.people))

    def total(self, attr: str) -> float:
        return sum(getattr(p, attr) for p in self.people)

    # -- persistence --------------------------------------------------
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        data = dict(data)
        fields = Person.__dataclass_fields__
        people = [Person(**{k: v for k, v in p.items() if k in fields}) for p in data.pop("people", [])] or [Person()]
        hfields = Household.__dataclass_fields__
        household = Household(**{k: v for k, v in data.pop("household", {}).items() if k in hfields})
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(people=people, household=household, **known)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def validate(self) -> list[str]:
        problems = []
        if self.filing_status not in FILING_STATUS_LABELS:
            problems.append(f"Unknown filing status {self.filing_status!r}")
        if self.filing_status in ("mfj", "mfs") and len(self.people) != 2:
            problems.append("Married filing statuses need two people")
        if self.filing_status in ("single", "hoh", "qss") and len(self.people) != 1:
            problems.append("This filing status takes one person")
        if self.household.aca_plan_tier not in ACA_PLAN_TIERS:
            problems.append(f"Unknown ACA plan tier {self.household.aca_plan_tier!r}")
        return problems
