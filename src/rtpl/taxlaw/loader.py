"""Load federal and state tax law from TOML files.

Search order: any user folders passed in (newest wins), then the data that
ships with the package. A user folder that contains ``federal/2026.toml``
replaces the shipped 2026 file entirely, so people can correct or extend the
law without touching code.

Years with no data file are projected from the newest enacted year using
``federal/projection.toml``: indexed amounts grow with inflation and policy
switches listed under ``[sunsets]`` turn off after their last year.
"""

from __future__ import annotations

import copy
import math
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Iterable

FILING_STATUSES = ("single", "mfj", "mfs", "hoh")


def status_key(filing_status: str) -> str:
    """Map a filing status to the key used in the data files."""
    fs = filing_status.lower()
    if fs == "qss":  # qualifying surviving spouse uses joint values
        return "mfj"
    if fs not in FILING_STATUSES:
        raise ValueError(f"Unknown filing status: {filing_status!r}")
    return fs


@dataclass(frozen=True)
class Schedule:
    """A progressive rate schedule: sorted ``(lower_bound, rate)`` pairs."""

    brackets: tuple[tuple[float, float], ...]

    @classmethod
    def from_pairs(cls, pairs: Iterable[Iterable[float]]) -> "Schedule":
        rows = tuple(sorted((float(a), float(r)) for a, r in pairs))
        if not rows or rows[0][0] != 0:
            raise ValueError("A schedule must start at 0")
        return cls(rows)

    def tax(self, amount: float) -> float:
        """Tax on ``amount`` under this schedule."""
        return self.tax_between(0.0, amount)

    def tax_between(self, lo: float, hi: float) -> float:
        """Tax on the slice of income from ``lo`` to ``hi`` (stacked)."""
        if hi <= lo:
            return 0.0
        total = 0.0
        for i, (start, rate) in enumerate(self.brackets):
            end = self.brackets[i + 1][0] if i + 1 < len(self.brackets) else math.inf
            a, b = max(lo, start), min(hi, end)
            if b > a:
                total += (b - a) * rate
        return total

    def rate_at(self, amount: float) -> float:
        rate = self.brackets[0][1]
        for start, r in self.brackets:
            if amount >= start:
                rate = r
        return rate

    def thresholds(self) -> list[float]:
        return [b for b, _ in self.brackets[1:]]

    def scaled(self, factor: float, step: float) -> "Schedule":
        return Schedule(
            tuple((_round_down(b * factor, step) if b else 0.0, r) for b, r in self.brackets)
        )


def _round_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step + 1e-9) * step


def _by_status(table: dict, cast=float) -> dict[str, float]:
    return {k: cast(table[k]) for k in FILING_STATUSES}


@dataclass(frozen=True)
class IrmaaTier:
    single: float
    mfj: float
    mfs: float
    part_b: float
    part_d: float

    def threshold(self, filing_status: str) -> float:
        key = status_key(filing_status)
        return {"single": self.single, "hoh": self.single, "mfj": self.mfj, "mfs": self.mfs}[key]


@dataclass(frozen=True)
class FederalLaw:
    year: int
    status: str  # "enacted" or "projected"
    sources: tuple[str, ...]
    personal_exemption: float
    senior_deduction_enabled: bool
    aca_enhanced_subsidies: bool
    ordinary: dict[str, Schedule]
    standard_deduction: dict[str, float]
    additional_std_unmarried: float
    additional_std_married: float
    senior_amount: float
    senior_phaseout_rate: float
    senior_phaseout_per_person: bool
    senior_first_year: int
    senior_last_year: int
    senior_mfs_eligible: bool
    senior_phaseout_start: dict[str, float]
    capital_gains: dict[str, Schedule]
    capital_loss_limit: dict[str, float]
    niit_rate: float
    niit_threshold: dict[str, float]
    ss_base: dict[str, float]
    ss_adjusted: dict[str, float]
    irmaa_premium_year: int
    irmaa_lookback: int
    irmaa_part_b_standard: float
    irmaa_tiers: tuple[IrmaaTier, ...]
    aca_fpl_first: float
    aca_fpl_additional: float
    aca_medicaid_pct: float
    aca_csr_pct: float
    aca_cliff_pct: float
    # Qualified charitable distributions: yearly limit per IRA owner, and
    # the one-time split-interest gift (inside that limit; 0 before 2023).
    qcd_limit: float = 100_000.0
    qcd_split_interest_limit: float = 0.0
    projected_from: int | None = None
    inflation_used: float | None = None
    # Social Security retirement earnings test: yearly wages allowed before
    # benefits are withheld (None = not applied, for a user file without it).
    earnings_test_under_fra: float | None = None
    earnings_test_fra_year: float | None = None
    # Health savings accounts: yearly contribution limits for self-only and
    # family coverage, and the catch-up at 55 or older (None = not in the file).
    hsa_self_only: float | None = None
    hsa_family: float | None = None
    hsa_catch_up: float = 1000.0
    # ACA premium tax credit applicable percentage table (IRC 36B(b)(3)):
    # (FPL ratio where the band starts, initial %, final %), sorted. Inside a
    # band the percentage rises in a straight line; past the last start it
    # stays at that row's final %. None = not in the file (no credit shown).
    aca_applicable: tuple[tuple[float, float, float], ...] | None = None
    # Education credits (IRC 25A). None = not in the file (no credits).
    education: "EducationCredits | None" = None
    # Charitable deduction for non-itemizers (IRC 170(p)): the most cash
    # gifts deducted with the standard deduction, by filing status, and
    # the first year it applies. None = not in the file (no deduction).
    charitable_nonitemizer_limit: dict[str, float] | None = None
    charitable_nonitemizer_first_year: int | None = None

    @property
    def senior_deduction_active(self) -> bool:
        return (
            self.senior_deduction_enabled
            and self.senior_first_year <= self.year <= self.senior_last_year
        )

    def charitable_nonitemizer_cap(self, filing_status: str) -> float:
        """Most cash gifts deductible this year alongside the standard deduction."""
        limits = self.charitable_nonitemizer_limit
        first = self.charitable_nonitemizer_first_year
        if limits is None or (first is not None and self.year < first):
            return 0.0
        if filing_status.lower() == "qss":  # not a joint return
            return limits["single"]
        return limits[status_key(filing_status)]

    def fpl(self, household_size: int) -> float:
        return self.aca_fpl_first + self.aca_fpl_additional * max(0, household_size - 1)

    def applicable_percentage(self, fpl_ratio: float) -> float:
        """Share of income a household is expected to pay toward the benchmark
        plan, at ``fpl_ratio`` (income / poverty line, 2.5 = 250%)."""
        rows = self.aca_applicable or ()
        if not rows:
            return 0.0
        pct = rows[0][1]
        for i, (start, initial, final) in enumerate(rows):
            if fpl_ratio < start:
                break
            nxt = rows[i + 1][0] if i + 1 < len(rows) else None
            if nxt is None or nxt <= start:
                pct = final
            else:
                t = min(1.0, (fpl_ratio - start) / (nxt - start))
                pct = initial + (final - initial) * t
        return pct


@dataclass(frozen=True)
class EducationCredits:
    """American Opportunity (AOTC) and Lifetime Learning (LLC) credits."""

    aotc_full_expenses: float  # first slice of expenses credited at 100%
    aotc_partial_expenses: float  # next slice, credited at aotc_partial_rate
    aotc_partial_rate: float
    aotc_refundable_share: float  # share of the AOTC paid even with no tax
    aotc_phaseout_start: dict[str, float]
    aotc_phaseout_width: dict[str, float]
    llc_rate: float
    llc_expense_limit: float  # per return
    llc_phaseout_start: dict[str, float]
    llc_phaseout_width: dict[str, float]
    mfs_eligible: bool = False

    @property
    def aotc_max(self) -> float:
        return self.aotc_full_expenses + self.aotc_partial_rate * self.aotc_partial_expenses


def _parse_education(raw: dict | None) -> EducationCredits | None:
    if not raw:
        return None
    return EducationCredits(
        aotc_full_expenses=float(raw["aotc_full_expenses"]),
        aotc_partial_expenses=float(raw["aotc_partial_expenses"]),
        aotc_partial_rate=float(raw["aotc_partial_rate"]),
        aotc_refundable_share=float(raw["aotc_refundable_share"]),
        aotc_phaseout_start=_by_status(raw["aotc_phaseout_start"]),
        aotc_phaseout_width=_by_status(raw["aotc_phaseout_width"]),
        llc_rate=float(raw["llc_rate"]),
        llc_expense_limit=float(raw["llc_expense_limit"]),
        llc_phaseout_start=_by_status(raw["llc_phaseout_start"]),
        llc_phaseout_width=_by_status(raw["llc_phaseout_width"]),
        mfs_eligible=bool(raw.get("mfs_eligible", False)),
    )


def _parse_federal(raw: dict) -> FederalLaw:
    pol = raw.get("policy", {})
    sd = raw["senior_deduction"]
    irmaa = raw["irmaa"]
    aca = raw["aca"]
    cg = raw["capital_gains"]
    et = raw["social_security"].get("earnings_test", {})
    hsa = raw.get("hsa", {})
    charity = raw.get("charitable", {})
    return FederalLaw(
        year=int(raw["year"]),
        status=str(raw.get("status", "enacted")),
        sources=tuple(raw.get("sources", ())),
        personal_exemption=float(pol.get("personal_exemption", 0.0)),
        senior_deduction_enabled=bool(pol.get("senior_deduction", True)),
        aca_enhanced_subsidies=bool(pol.get("aca_enhanced_subsidies", False)),
        ordinary={k: Schedule.from_pairs(raw["ordinary_brackets"][k]) for k in FILING_STATUSES},
        standard_deduction=_by_status(raw["standard_deduction"]),
        additional_std_unmarried=float(raw["additional_standard_deduction"]["unmarried"]),
        additional_std_married=float(raw["additional_standard_deduction"]["married"]),
        senior_amount=float(sd["amount_per_person"]),
        senior_phaseout_rate=float(sd["phaseout_rate"]),
        senior_phaseout_per_person=bool(sd.get("phaseout_per_person", True)),
        senior_first_year=int(sd["first_year"]),
        senior_last_year=int(sd["last_year"]),
        senior_mfs_eligible=bool(sd.get("mfs_eligible", False)),
        senior_phaseout_start=_by_status(sd["phaseout_start"]),
        capital_gains={k: Schedule.from_pairs(cg[k]) for k in FILING_STATUSES},
        capital_loss_limit=_by_status(cg["capital_loss_limit"]),
        niit_rate=float(raw["niit"]["rate"]),
        niit_threshold=_by_status(raw["niit"]["threshold"]),
        ss_base=_by_status(raw["social_security"]["base"]),
        ss_adjusted=_by_status(raw["social_security"]["adjusted"]),
        irmaa_premium_year=int(irmaa["premium_year"]),
        irmaa_lookback=int(irmaa.get("lookback_years", 2)),
        irmaa_part_b_standard=float(irmaa["part_b_standard"]),
        irmaa_tiers=tuple(
            IrmaaTier(
                single=float(t["single"]),
                mfj=float(t["mfj"]),
                mfs=float(t["mfs"]),
                part_b=float(t["part_b"]),
                part_d=float(t["part_d"]),
            )
            for t in irmaa["tiers"]
        ),
        aca_fpl_first=float(aca["fpl_first_person"]),
        aca_fpl_additional=float(aca["fpl_each_additional"]),
        aca_medicaid_pct=float(aca["medicaid_expansion_pct"]),
        aca_csr_pct=float(aca["csr_limit_pct"]),
        aca_cliff_pct=float(aca["subsidy_cliff_pct"]),
        qcd_limit=float(raw.get("qcd", {}).get("annual_limit", 100_000)),
        qcd_split_interest_limit=float(raw.get("qcd", {}).get("split_interest_limit", 0)),
        earnings_test_under_fra=float(et["under_fra"]) if "under_fra" in et else None,
        earnings_test_fra_year=float(et["fra_year"]) if "fra_year" in et else None,
        hsa_self_only=float(hsa["self_only"]) if "self_only" in hsa else None,
        hsa_family=float(hsa["family"]) if "family" in hsa else None,
        hsa_catch_up=float(hsa.get("catch_up", 1000)),
        aca_applicable=(
            tuple(sorted((float(a), float(b), float(c)) for a, b, c in aca["applicable_percentage"]))
            if "applicable_percentage" in aca else None
        ),
        education=_parse_education(raw.get("education_credits")),
        charitable_nonitemizer_limit=(
            _by_status(charity["nonitemizer_limit"]) if "nonitemizer_limit" in charity else None
        ),
        charitable_nonitemizer_first_year=(
            int(charity["nonitemizer_first_year"]) if "nonitemizer_first_year" in charity else None
        ),
    )


# Income measures a state phase-out can be based on. The first three are
# federal amounts; "state_income" is the state's own income before its
# standard deduction and exemptions (after its Social Security and
# retirement exclusions), so exclusions can't phase out on it.
PHASEOUT_INCOMES = ("agi", "magi", "agi_ex_ss", "state_income")
FEDERAL_PHASEOUT_INCOMES = ("agi", "magi", "agi_ex_ss")


@dataclass(frozen=True)
class Phaseout:
    """How a state deduction, exemption, exclusion or credit shrinks with income.

    One of these shapes, above ``start``:

    * ``rate``: the amount falls by ``rate`` dollars for each dollar of
      income over ``start`` (Utah's credits, Virginia's age deduction).
    * ``width``: the amount falls in a straight line to ``keep`` of itself
      over ``width`` dollars; a width of 0 is a cliff.
    * ``step`` and ``step_fraction``: the amount falls by ``step_fraction``
      of itself for each ``step`` dollars, or part of one, over ``start``.
    * ``tiers``: the share kept is that of the first tier whose ``up_to``
      the income doesn't pass, else ``keep`` (New Jersey's pension
      exclusion, Ohio's exemption).

    ``income`` names the measure tested; see ``PHASEOUT_INCOMES``.
    """

    start: dict[str, float] = field(default_factory=dict)
    rate: float | None = None
    width: dict[str, float] | None = None
    step: float | None = None
    step_fraction: float = 0.0
    tiers: tuple[tuple[dict[str, float], dict[str, float]], ...] = ()  # (up_to, keep)
    keep: float = 0.0  # share still allowed once fully phased out
    income: str = "agi"

    def apply(self, amount: float, income: float, fs: str) -> float:
        """What is left of ``amount`` at this ``income``."""
        if amount <= 0:
            return amount
        if self.tiers:
            for up_to, share in self.tiers:
                if income <= up_to[fs]:
                    return amount * share[fs]
            return amount * self.keep
        over = income - self.start.get(fs, 0.0)
        if over <= 0:
            return amount
        floor = amount * self.keep
        if self.rate is not None:
            return max(floor, amount - self.rate * over)
        if self.step:
            steps = math.ceil(over / self.step - 1e-9)
            return max(floor, amount * (1.0 - steps * self.step_fraction))
        if self.width is not None and self.width[fs] > 0:
            return max(floor, amount * (1.0 - over / self.width[fs]))
        return floor  # a cliff


def _by_status_or_number(value) -> dict[str, float]:
    if isinstance(value, dict):
        return _status_table(value)
    return {k: float(value) for k in FILING_STATUSES}


def _parse_phaseout(raw: dict | None, *, federal_only: bool = False) -> Phaseout | None:
    if not raw:
        return None
    income = str(raw.get("income", "agi"))
    allowed = FEDERAL_PHASEOUT_INCOMES if federal_only else PHASEOUT_INCOMES
    if income not in allowed:
        raise ValueError(f"phase-out income must be one of {', '.join(allowed)}, not {income!r}")
    return Phaseout(
        start=_status_table(raw.get("start")),
        rate=float(raw["rate"]) if "rate" in raw else None,
        width=_status_table(raw["width"]) if "width" in raw else None,
        step=float(raw["step"]) if "step" in raw else None,
        step_fraction=float(raw.get("step_fraction", 0.0)),
        tiers=tuple(
            (_status_table(t["up_to"], default=math.inf), _by_status_or_number(t["keep"]))
            for t in raw.get("tiers", ())
        ),
        keep=float(raw.get("keep", 0.0)),
        income=income,
    )


@dataclass(frozen=True)
class StateCredit:
    """A nonrefundable state credit, optionally phased out with income.

    The amount is the sum of whichever of these the file gives: a flat
    ``amount`` by filing status, ``per_person`` for each person who meets
    ``min_age`` and ``born_before``, ``federal_deduction_rate`` times the
    federal standard (or itemized) deduction, and ``social_security_rate``
    times the Social Security the state taxes. Of credits sharing a
    ``group``, only the largest counts (a filer picks one).
    """

    name: str
    amount: dict[str, float] | None = None
    per_person: float = 0.0
    min_age: int = 0
    born_before: int | None = None
    federal_deduction_rate: float = 0.0
    social_security_rate: float = 0.0
    group: str | None = None
    phaseout: Phaseout | None = None


@dataclass(frozen=True)
class RetirementExclusion:
    """Per-person exclusion of pension and/or IRA income from state tax."""

    amount: float  # math.inf = unlimited
    min_age: int = 0
    applies_to: tuple[str, ...] = ("pension", "ira")
    phaseout: Phaseout | None = None  # federal income measures only
    # A cap for the whole return by filing status instead of ``amount`` per
    # person (New Jersey's pension exclusion).
    per_return: dict[str, float] | None = None
    # What a phase-out that keeps less than all takes its share of: "cap",
    # the capped amount (the default), or "income", all the eligible
    # income with no cap (New Jersey: 37.5% of all pension income from
    # $100K to $125K, where the $75,000 cap no longer applies).
    phased_share_of: str = "cap"
    # Birth years that qualify: ``born_from`` <= birth year < ``born_before``.
    born_from: int | None = None
    born_before: int | None = None
    # Whose birth year counts: "self", or "older_spouse" (Michigan judges a
    # joint return by the older spouse).
    birth_year_of: str = "self"
    # Of the exclusions in one group, each person gets only the largest
    # (Michigan's choice between its retirement deductions).
    group: str | None = None

    def qualifies(self, birth_year: int, age: int) -> bool:
        if age < self.min_age:
            return False
        if self.born_from is not None and birth_year < self.born_from:
            return False
        return self.born_before is None or birth_year < self.born_before


@dataclass(frozen=True)
class SocialSecurityRule:
    """How a state that taxes Social Security exempts some of it."""

    exempt_from_age: int | None = None  # benefits of people this age+ are exempt
    exempt_if_agi_at_most: dict[str, float] | None = None
    phaseout_width: dict[str, float] | None = None  # linear phase-out above the limit
    exempt_fraction_above: float = 0.0  # share still exempt above the limit
    # Benefits of people this age+ (and under exempt_from_age) are exempt,
    # subject to limited_phaseout (Colorado at 55-64).
    limited_from_age: int | None = None
    limited_phaseout: Phaseout | None = None


@dataclass(frozen=True)
class StateLaw:
    code: str
    name: str
    year: int | None
    taxes_social_security: bool
    brackets: dict[str, Schedule]
    standard_deduction: dict[str, float]
    # The state adds HSA contributions back (no deduction): California, New Jersey.
    taxes_hsa_contributions: bool = False
    exemption_per_person: float = 0.0
    credit_per_person: float = 0.0  # personal exemption given as a credit
    filer_credit: dict[str, float] | None = None  # flat nonrefundable credit by status
    surcharges: tuple[tuple[float, float], ...] = ()  # (rate, threshold)
    has_income_tax: bool = True
    social_security: SocialSecurityRule = field(default_factory=SocialSecurityRule)
    retirement_exclusions: tuple[RetirementExclusion, ...] = ()
    age_deduction: tuple[float, int] | None = None  # (amount per person, min age)
    # Income phase-outs of the amounts above (None = no phase-out).
    standard_deduction_phaseout: Phaseout | None = None
    exemption_phaseout: Phaseout | None = None  # per-person exemptions and credits
    age_deduction_phaseout: Phaseout | None = None
    credits: tuple[StateCredit, ...] = ()
    capital_gains_exclusion: float = 0.0  # share of net long-term gains excluded
    capital_gains_max_rate: float | None = None  # alternative cap on the rate for gains
    gains_only_tax: Schedule | None = None  # e.g. Washington: tax on long-term gains only
    gains_only_deduction: float = 0.0
    gains_rates: dict[str, Schedule] | None = None  # separate stacked rates for long-term gains (MT)
    notes: tuple[str, ...] = ()
    # Footnotes for the marginal graph where the file approximates the law
    # (a smoothed phase-out, say). Remove one when the file gets the exact rule.
    chart_notes: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    # The tax year asked for when it has no file of its own and ``year``
    # is the file used in its place.
    law_year_requested: int | None = None

    @property
    def label(self) -> str:
        return f"State Tax ({self.code})"

    @property
    def gains_label(self) -> str:
        return f"State CG&QD Tax ({self.code})"

    @property
    def has_separate_gains_tax(self) -> bool:
        return (
            self.gains_only_tax is not None
            or self.gains_rates is not None
            or self.capital_gains_max_rate is not None
        )


def _status_table(table: dict | None, cast=float, default=0.0) -> dict[str, float]:
    """Read a by-status table where hoh and mfs fall back to single."""
    table = table or {}
    out = {}
    for k in FILING_STATUSES:
        if k in table:
            out[k] = cast(table[k])
        elif "single" in table and k != "mfj":
            out[k] = cast(table["single"])
        else:
            out[k] = cast(default)
    return out


def _choice(raw: dict, key: str, allowed: tuple[str, ...]) -> str:
    value = str(raw.get(key, allowed[0]))
    if value not in allowed:
        raise ValueError(f"{key} must be one of {', '.join(allowed)}, not {value!r}")
    return value


def _parse_state(raw: dict) -> StateLaw:
    surcharges = tuple(
        (float(v["rate"]), float(v["threshold"])) for v in raw.get("surcharges", {}).values()
    )
    br = raw.get("brackets", {"single": [[0, 0.0]], "mfj": [[0, 0.0]]})
    brackets = {k: Schedule.from_pairs(br.get(k, br["single"] if k != "mfj" else br["mfj"])) for k in FILING_STATUSES}
    ss_raw = raw.get("social_security", {})
    ss = SocialSecurityRule(
        exempt_from_age=ss_raw.get("exempt_from_age"),
        exempt_if_agi_at_most=_status_table(ss_raw["exempt_if_agi_at_most"]) if "exempt_if_agi_at_most" in ss_raw else None,
        phaseout_width=_status_table(ss_raw["phaseout_width"]) if "phaseout_width" in ss_raw else None,
        exempt_fraction_above=float(ss_raw.get("exempt_fraction_above", 0.0)),
        limited_from_age=ss_raw.get("limited_from_age"),
        limited_phaseout=_parse_phaseout(ss_raw.get("limited_phaseout"), federal_only=True),
    )
    exclusions = []
    if raw.get("exempt_retirement_income"):
        exclusions.append(RetirementExclusion(math.inf, int(raw.get("retirement_min_age", 0))))
    for ex in raw.get("retirement_exclusions", []):
        amt = ex.get("amount", "unlimited")
        exclusions.append(
            RetirementExclusion(
                amount=math.inf if amt == "unlimited" else float(amt),
                per_return=_status_table(ex["per_return"]) if "per_return" in ex else None,
                min_age=int(ex.get("min_age", 0)),
                applies_to=tuple(ex.get("applies_to", ("pension", "ira"))),
                phaseout=_parse_phaseout(ex.get("phaseout"), federal_only=True),
                born_from=ex.get("born_from"),
                born_before=ex.get("born_before"),
                birth_year_of=_choice(ex, "birth_year_of", ("self", "older_spouse")),
                group=ex.get("group"),
                phased_share_of=_choice(ex, "phased_share_of", ("cap", "income")),
            )
        )
        bad = set(exclusions[-1].applies_to) - {"pension", "ira", "any"}
        if bad:
            raise ValueError(f"retirement exclusion applies_to has unknown kinds: {', '.join(sorted(bad))}")
    age_ded = raw.get("age_deduction")
    credits = []
    for c in raw.get("credits", []):
        credits.append(
            StateCredit(
                name=str(c.get("name", "Credit")),
                amount=_status_table(c["amount"]) if "amount" in c else None,
                per_person=float(c.get("per_person", 0.0)),
                min_age=int(c.get("min_age", 0)),
                born_before=c.get("born_before"),
                federal_deduction_rate=float(c.get("federal_deduction_rate", 0.0)),
                social_security_rate=float(c.get("social_security_rate", 0.0)),
                group=c.get("group"),
                phaseout=_parse_phaseout(c.get("phaseout")),
            )
        )
    gains = raw.get("capital_gains_tax")
    return StateLaw(
        code=str(raw["code"]).upper(),
        name=str(raw.get("name", raw["code"])),
        year=raw.get("year"),
        taxes_social_security=bool(raw.get("taxes_social_security", False)),
        taxes_hsa_contributions=bool(raw.get("taxes_hsa_contributions", False)),
        brackets=brackets,
        standard_deduction=_status_table(raw.get("standard_deduction")),
        exemption_per_person=float(raw.get("exemption", {}).get("per_person", 0.0)),
        credit_per_person=float(raw.get("exemption", {}).get("credit_per_person", 0.0)),
        filer_credit=_status_table(raw["filer_credit"]) if "filer_credit" in raw else None,
        surcharges=surcharges,
        has_income_tax=bool(raw.get("has_income_tax", True)),
        social_security=ss,
        retirement_exclusions=tuple(exclusions),
        age_deduction=(float(age_ded["amount"]), int(age_ded["min_age"])) if age_ded else None,
        standard_deduction_phaseout=_parse_phaseout(raw.get("standard_deduction", {}).get("phaseout")),
        exemption_phaseout=_parse_phaseout(raw.get("exemption", {}).get("phaseout")),
        age_deduction_phaseout=_parse_phaseout(age_ded.get("phaseout")) if age_ded else None,
        credits=tuple(credits),
        capital_gains_exclusion=float(raw.get("capital_gains_exclusion", 0.0)),
        capital_gains_max_rate=raw.get("capital_gains_max_rate"),
        gains_only_tax=Schedule.from_pairs(gains["brackets"]) if gains else None,
        gains_only_deduction=float(gains.get("deduction", 0.0)) if gains else 0.0,
        gains_rates=(
            {k: Schedule.from_pairs(raw["capital_gains_rates"].get(k, raw["capital_gains_rates"]["single"] if k != "mfj" else raw["capital_gains_rates"]["mfj"])) for k in FILING_STATUSES}
            if "capital_gains_rates" in raw else None
        ),
        notes=tuple(raw.get("notes", ())),
        chart_notes=tuple(raw.get("chart_notes", ())),
        sources=tuple(raw.get("sources", ())),
    )


def _no_tax_state(code: str) -> StateLaw:
    zero = Schedule.from_pairs([[0, 0.0]])
    return StateLaw(
        code=code,
        name=code,
        year=None,
        taxes_social_security=False,
        brackets={k: zero for k in FILING_STATUSES},
        standard_deduction={k: 0.0 for k in FILING_STATUSES},
        has_income_tax=False,
    )


@dataclass
class TaxLawLibrary:
    """Finds and caches tax law by year (federal) and code (state)."""

    user_dirs: list[Path] = field(default_factory=list)
    inflation: float | None = None  # None = use projection.toml default
    _federal_raw: dict[int, dict] = field(default_factory=dict, init=False, repr=False)
    _cache: dict[tuple[int, float], FederalLaw] = field(default_factory=dict, init=False, repr=False)
    _projection: dict = field(default_factory=dict, init=False, repr=False)
    _state_cache: dict[tuple[str, int | None], StateLaw] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.user_dirs = [Path(p) for p in self.user_dirs]
        self._load_federal_files()

    # -- file discovery -------------------------------------------------
    def _roots(self) -> list[Path]:
        shipped = Path(str(resources.files("rtpl") / "data"))
        return [*self.user_dirs, shipped]

    def _first_existing(self, rel: str) -> Path | None:
        for root in self._roots():
            p = root / rel
            if p.is_file():
                return p
        return None

    def _load_federal_files(self) -> None:
        # Lowest priority first so user files override shipped ones.
        for root in reversed(self._roots()):
            fed = root / "federal"
            if not fed.is_dir():
                continue
            for p in fed.glob("*.toml"):
                with p.open("rb") as fh:
                    raw = tomllib.load(fh)
                if p.stem == "projection":
                    self._projection = raw
                elif p.stem.isdigit():
                    self._federal_raw[int(p.stem)] = raw
        if not self._federal_raw:
            raise FileNotFoundError("No federal tax law files found")

    # -- federal --------------------------------------------------------
    @property
    def enacted_years(self) -> list[int]:
        return sorted(self._federal_raw)

    @property
    def default_inflation(self) -> float:
        if self.inflation is not None:
            return self.inflation
        return float(self._projection.get("default_inflation", 0.025))

    def federal(self, year: int, inflation: float | None = None) -> FederalLaw:
        """Law for ``year``; ``inflation`` only matters for projected years."""
        rate = self.default_inflation if inflation is None else float(inflation)
        key = (year, 0.0 if year in self._federal_raw else rate)
        if key not in self._cache:
            self._cache[key] = self._build_federal(year, rate)
        return self._cache[key]

    def _build_federal(self, year: int, rate: float) -> FederalLaw:
        if year in self._federal_raw:
            return _parse_federal(self._federal_raw[year])
        years = self.enacted_years
        if year < years[0]:
            raise KeyError(f"No tax law data for {year} (earliest is {years[0]})")
        base_year = max(y for y in years if y < year)
        raw = copy.deepcopy(self._federal_raw[base_year])
        n = year - base_year
        factor = (1 + rate) ** n
        idx = self._projection.get("indexed", {})
        sunsets = self._projection.get("sunsets", {})

        def scale_pairs(table: dict, step: float) -> None:
            for k in FILING_STATUSES:
                table[k] = [
                    [(_round_down(b * factor, step) if b else 0), r] for b, r in table[k]
                ]

        if "ordinary_brackets" in idx:
            scale_pairs(raw["ordinary_brackets"], idx["ordinary_brackets"])
        if "capital_gains" in idx:
            scale_pairs(raw["capital_gains"], idx["capital_gains"])
        if "standard_deduction" in idx:
            step = idx["standard_deduction"]
            for k in FILING_STATUSES:
                raw["standard_deduction"][k] = _round_down(raw["standard_deduction"][k] * factor, step)
        if "additional_standard_deduction" in idx:
            step = idx["additional_standard_deduction"]
            for k in ("unmarried", "married"):
                raw["additional_standard_deduction"][k] = _round_down(
                    raw["additional_standard_deduction"][k] * factor, step
                )
        if "irmaa_thresholds" in idx:
            step = idx["irmaa_thresholds"]
            # The top tier was frozen in law and starts indexing in a given year.
            top_from = int(self._projection.get("irmaa_top_tier_indexed_from", 0))
            top_n = max(0, year - max(base_year, top_from - 1)) if top_from else n
            tiers = raw["irmaa"]["tiers"]
            for i, t in enumerate(tiers):
                f = (1 + rate) ** top_n if i == len(tiers) - 1 else factor
                for k in ("single", "mfj", "mfs"):
                    t[k] = _round_down(t[k] * f, step)
            # Premiums themselves grow too; use the same factor as an estimate.
            raw["irmaa"]["part_b_standard"] *= factor
            for t in tiers:
                t["part_b"] *= factor
                t["part_d"] *= factor
        raw["irmaa"]["premium_year"] = year
        if "aca_fpl" in idx:
            step = idx["aca_fpl"]
            raw["aca"]["fpl_first_person"] = _round_down(raw["aca"]["fpl_first_person"] * factor, step)
            raw["aca"]["fpl_each_additional"] = _round_down(
                raw["aca"]["fpl_each_additional"] * factor, step
            )
        if "qcd_limits" in idx and "qcd" in raw:
            step = idx["qcd_limits"]
            for k in ("annual_limit", "split_interest_limit"):
                if k in raw["qcd"]:
                    raw["qcd"][k] = _round_down(raw["qcd"][k] * factor, step)
        if "earnings_test" in idx and "earnings_test" in raw["social_security"]:
            # SSA raises these with the national average wage; inflation stands in for it.
            step = idx["earnings_test"]
            et = raw["social_security"]["earnings_test"]
            for k in ("under_fra", "fra_year"):
                if k in et:
                    et[k] = _round_down(et[k] * factor, step)
        if "hsa_limits" in idx and "hsa" in raw:
            # The catch-up is fixed in statute; only the two limits are indexed.
            step = idx["hsa_limits"]
            for k in ("self_only", "family"):
                if k in raw["hsa"]:
                    raw["hsa"][k] = _round_down(raw["hsa"][k] * factor, step)
        for flag, last in sunsets.items():
            if year > int(last):
                raw.setdefault("policy", {})[flag] = False
        raw["year"] = year
        raw["status"] = "projected"
        law = _parse_federal(raw)
        return _replace(law, projected_from=base_year, inflation_used=rate)

    # -- state ----------------------------------------------------------
    def available_states(self) -> list[str]:
        codes: set[str] = set()
        for root in self._roots():
            sdir = root / "states"
            if not sdir.is_dir():
                continue
            codes.update(p.stem.upper() for p in sdir.glob("*.toml") if not p.stem.startswith("_"))
            for ydir in sdir.iterdir():
                if ydir.is_dir() and ydir.name.isdigit():
                    codes.update(p.stem.upper() for p in ydir.glob("*.toml") if not p.stem.startswith("_"))
        return sorted(codes | set(self._no_tax_codes()))

    def _no_tax_codes(self) -> list[str]:
        p = self._first_existing("states/_no_income_tax.toml")
        if not p:
            return []
        with p.open("rb") as fh:
            return [c.upper() for c in tomllib.load(fh).get("codes", [])]

    def state_years(self, code: str) -> dict[int, Path]:
        """Files for one state by tax year, user folders winning over shipped ones."""
        code = code.upper()
        found: dict[int, Path] = {}
        for root in reversed(self._roots()):
            sdir = root / "states"
            if not sdir.is_dir():
                continue
            for ydir in sdir.iterdir():
                p = ydir / f"{code}.toml"
                if ydir.is_dir() and ydir.name.isdigit() and p.is_file():
                    found[int(ydir.name)] = p
        return dict(sorted(found.items()))

    def state(self, code: str | None, year: int | None = None) -> StateLaw | None:
        """State law for ``year`` (the newest file when ``year`` is None).

        Files live in ``states/<year>/XX.toml``. A year without a file uses
        the newest earlier file (a later year keeps the newest law), or the
        earliest file for a year before them all. A ``states/XX.toml`` with
        no year folder in a user folder replaces the shipped files for every
        year, as it did before state law was kept by year.
        """
        if not code:
            return None
        code = code.upper()
        key = (code, year)
        if key not in self._state_cache:
            self._state_cache[key] = self._build_state(code, year)
        return self._state_cache[key]

    def _build_state(self, code: str, year: int | None) -> StateLaw:
        for root in self.user_dirs:
            p = root / "states" / f"{code}.toml"
            if p.is_file():
                with p.open("rb") as fh:
                    return _parse_state(tomllib.load(fh))
        files = self.state_years(code)
        if not files:
            if code in self._no_tax_codes():
                return _no_tax_state(code)
            raise KeyError(f"No state tax data for {code}")
        years = list(files)
        if year is None or year >= years[-1]:
            used = years[-1]
        elif year in files:
            used = year
        elif year < years[0]:
            used = years[0]
        else:
            used = max(y for y in years if y < year)
        with files[used].open("rb") as fh:
            raw = tomllib.load(fh)
        if raw.get("year") not in (None, used):
            raise ValueError(f"{files[used]} says year {raw['year']} but is in the {used} folder")
        raw["year"] = used
        law = _parse_state(raw)
        if year is not None and year != used and law.has_income_tax:
            from dataclasses import replace

            note = f"{year} uses the {used} state tax law (the newest earlier law on file)." if used < year \
                else f"{year} uses the {used} state tax law (no earlier law on file)."
            law = replace(law, chart_notes=(*law.chart_notes, note), law_year_requested=year)
        return law


def _replace(law: FederalLaw, **changes) -> FederalLaw:
    from dataclasses import replace

    return replace(law, **changes)
