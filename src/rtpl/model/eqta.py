"""Estimated Quarterly Tax Assistant (EQTA) files.

An ``.eqta`` file is JSON holding any number of tax years. Each year has a
profile (RTPL's own ``Profile``, used for filing status, state and people),
income sources with dated registers, the four built-in payment lines, and
notes. The engine in ``rtpl.engine.estimated`` turns a year into the
worksheet: year-to-date totals at each period end, tax, paid and owed.

Amounts are typed as simple arithmetic (``3662.90*12``); the text is kept
next to the value so the register shows what was typed.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import json
import operator
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rtpl.model.profile import Person, Profile

SCHEMA_VERSION = 1
FILE_KIND = "eqta"


@dataclass(frozen=True)
class TaxType:
    key: str
    label: str  # group heading on the worksheet and in the tree
    # Profile field the amounts go to: a Person attribute when per_person,
    # otherwise a Household attribute. "split_dividends" is special.
    field: str
    per_person: bool = False  # needs a person (their exclusions and SS)
    deduction: bool = False  # lowers income (shown as a positive amount)


TAX_TYPES: tuple[TaxType, ...] = (
    TaxType("social_security", "Social Security", "social_security", per_person=True),
    TaxType("wages", "Wages", "wages", per_person=True),
    TaxType("pension", "Pension", "pension", per_person=True),
    TaxType("ira", "IRA distribution", "ira_distributions", per_person=True),
    TaxType("taxable_interest", "Taxable interest", "taxable_interest"),
    TaxType("tax_exempt_interest", "Tax-exempt interest", "tax_exempt_interest"),
    TaxType("dividends_to_split", "Ordinary dividends to split", "split_dividends"),
    TaxType("qualified_dividends", "Qualified dividends", "qualified_dividends"),
    TaxType("nonqualified_dividends", "Non-qualified dividends", "nonqualified_dividends"),
    TaxType("ltcg", "Long-term capital gains", "long_term_capital_gains"),
    TaxType("stcg", "Short-term capital gains", "short_term_capital_gains"),
    TaxType("other_ordinary", "Other ordinary income", "other_ordinary_income"),
    TaxType("hsa", "HSA contribution", "hsa_deduction", deduction=True),
    TaxType("ira_contribution", "Deductible traditional IRA contribution", "adjustments", deduction=True),
    TaxType("other_adjustment", "Other above-the-line deduction", "adjustments", deduction=True),
    TaxType("charity", "Charitable gift (non-itemizer)", "charitable_cash_gifts", deduction=True),
    TaxType("itemized", "Itemized deduction", "itemized_deductions", deduction=True),
)
TAX_TYPE = {t.key: t for t in TAX_TYPES}

# The four built-in payment lines, in worksheet order.
PAYMENT_LINES = ("federal_withholding", "federal_estimated", "state_withholding", "state_estimated")


def payment_label(key: str, state_name: str | None = None) -> str:
    state = state_name or "State"
    return {
        "federal_withholding": "Federal withholding",
        "federal_estimated": "Federal estimated payments (EFTPS)",
        "state_withholding": f"{state} withholding",
        "state_estimated": f"{state} estimated payments",
    }[key]


# -- arithmetic in amount cells ------------------------------------------
_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


def evaluate_amount(text: str) -> float:
    """The value of an amount typed as simple arithmetic: numbers, + - * /
    and parentheses (``3662.90*12``, ``-1,250.50``). Dollar signs and
    thousands commas are ignored. Raises ValueError for anything else."""
    cleaned = re.sub(r"(?<=\d),(?=\d{3}\b)", "", str(text).replace("$", "")).strip()
    if not cleaned:
        return 0.0
    try:
        tree = ast.parse(cleaned, mode="eval")
    except SyntaxError:
        raise ValueError(f"Not a number or simple sum: {text!r}") from None

    def ev(node) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            try:
                return _OPS[type(node.op)](ev(node.left), ev(node.right))
            except ZeroDivisionError:
                raise ValueError("Division by zero") from None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        raise ValueError(f"Only numbers, + - * / and parentheses are allowed: {text!r}")

    return round(ev(tree), 2)


# -- dates ----------------------------------------------------------------
def parse_date(text: str, default_year: int) -> dt.date:
    """A register date: ``2026-03-15``, ``3/15/2026``, ``3/15/26`` or
    ``3/15`` (in ``default_year``)."""
    text = str(text).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    try:
        if m:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2}|\d{4}))?", text)
        if m:
            year = int(m[3]) if m[3] else default_year
            if year < 100:
                year += 2000
            return dt.date(year, int(m[1]), int(m[2]))
    except ValueError:
        pass
    raise ValueError(f"Not a date: {text!r} (use MM/DD/YYYY)")


# -- the file -------------------------------------------------------------
@dataclass
class Entry:
    """One register row: a date and an amount (as typed)."""

    date: dt.date
    text: str  # the amount as typed, e.g. "3662.90*12"

    @property
    def amount(self) -> float:
        return evaluate_amount(self.text)

    def to_dict(self) -> dict:
        return {"date": self.date.isoformat(), "amount": self.text}

    @classmethod
    def from_dict(cls, data: dict) -> "Entry":
        text = str(data.get("amount", "0"))
        evaluate_amount(text)  # reject a bad file early
        return cls(date=dt.date.fromisoformat(data["date"]), text=text)


@dataclass
class IncomeSource:
    name: str = ""  # empty: shown by its tax type (see label)
    tax_type: str = "taxable_interest"
    person: int | None = None  # index into the profile's people
    plan_text: str = ""  # annual plan amount, as typed ("" = $0)
    notes: str = ""
    qualified_share: float | None = None  # dividends to split only (0..1)
    entries: list[Entry] = field(default_factory=list)

    @property
    def type(self) -> TaxType:
        return TAX_TYPE[self.tax_type]

    @property
    def plan(self) -> float:
        return evaluate_amount(self.plan_text) if self.plan_text else 0.0

    def person_index(self, people: int) -> int | None:
        """The person whose fields the amounts go to: the source's own, or
        the first person for a type that needs one."""
        if self.person is not None and 0 <= self.person < people:
            return self.person
        return 0 if self.type.per_person else None

    def total_through(self, end: dt.date, start: dt.date | None = None) -> float:
        """Entries dated from ``start`` (if given) through ``end``."""
        return round(sum(e.amount for e in self.entries if e.date <= end and (start is None or e.date >= start)), 2)

    def label(self, people: list) -> str:
        """The name shown in the tree and on the worksheet: the source's name,
        or its tax type when it has none, then " - " and the person's name
        when it belongs to someone."""
        text = self.name.strip() or self.type.label
        idx = self.person_index(len(people))
        return f"{text} - {people[idx].name}" if idx is not None else text

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "tax_type": self.tax_type,
            "person": self.person,
            "plan": self.plan_text,
            "notes": self.notes,
            "entries": [e.to_dict() for e in self.entries],
        }
        if self.tax_type == "dividends_to_split":
            out["qualified_share"] = self.qualified_share
        return out

    @classmethod
    def from_dict(cls, data: dict) -> "IncomeSource":
        tax_type = data.get("tax_type", "taxable_interest")
        if tax_type not in TAX_TYPE:
            raise ValueError(f"Unknown tax type {tax_type!r}")
        plan = str(data.get("plan", "") or "")
        if plan:
            evaluate_amount(plan)
        share = data.get("qualified_share")
        return cls(
            name=str(data.get("name", "")),
            tax_type=tax_type,
            person=data.get("person"),
            plan_text=plan,
            notes=str(data.get("notes", "")),
            qualified_share=None if share is None else float(share),
            entries=[Entry.from_dict(e) for e in data.get("entries", [])],
        )


@dataclass
class PaymentLine:
    """A built-in payment line (withholding or estimated payments)."""

    key: str
    plan_text: str = ""
    notes: str = ""
    entries: list[Entry] = field(default_factory=list)

    @property
    def plan(self) -> float:
        return evaluate_amount(self.plan_text) if self.plan_text else 0.0

    def total_through(self, end: dt.date, start: dt.date | None = None) -> float:
        """Entries dated from ``start`` (if given) through ``end``."""
        return round(sum(e.amount for e in self.entries if e.date <= end and (start is None or e.date >= start)), 2)

    @property
    def total(self) -> float:
        return round(sum(e.amount for e in self.entries), 2)

    def to_dict(self) -> dict:
        return {"plan": self.plan_text, "notes": self.notes, "entries": [e.to_dict() for e in self.entries]}

    @classmethod
    def from_dict(cls, key: str, data: dict) -> "PaymentLine":
        plan = str(data.get("plan", "") or "")
        if plan:
            evaluate_amount(plan)
        return cls(key=key, plan_text=plan, notes=str(data.get("notes", "")),
                   entries=[Entry.from_dict(e) for e in data.get("entries", [])])


def _default_payments() -> dict[str, PaymentLine]:
    return {k: PaymentLine(k) for k in PAYMENT_LINES}


def eqta_profile(year: int) -> Profile:
    """A new year's profile: one person, single, no state."""
    return Profile(year=year, filing_status="single", people=[Person(name="Person 1", birth_year=year - 65)],
                   name=str(year))


@dataclass
class TaxYear:
    year: int
    description: str = ""
    profile: Profile = field(default_factory=lambda: eqta_profile(2026))
    sources: list[IncomeSource] = field(default_factory=list)
    payments: dict[str, PaymentLine] = field(default_factory=_default_payments)
    notes: str = ""  # Year notes (Markdown)
    dependents: list[str] = field(default_factory=list)  # names, a tag only for now
    # Last year's tax and AGI for the safe harbor, as typed (None = take
    # them from last year's tab in the same file, if there is one).
    prior_year_tax: float | None = None
    prior_year_agi: float | None = None

    def __post_init__(self) -> None:
        self.profile.year = self.year
        for k in PAYMENT_LINES:
            self.payments.setdefault(k, PaymentLine(k))

    @property
    def title(self) -> str:
        return f"{self.year} · {self.description}" if self.description else str(self.year)

    def to_dict(self) -> dict:
        return {
            "year": self.year,
            "description": self.description,
            "profile": self.profile.to_dict(),
            "dependents": list(self.dependents),
            "prior_year_tax": self.prior_year_tax,
            "prior_year_agi": self.prior_year_agi,
            "notes": self.notes,
            "sources": [s.to_dict() for s in self.sources],
            "payments": {k: self.payments[k].to_dict() for k in PAYMENT_LINES},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaxYear":
        year = int(data["year"])
        profile = Profile.from_dict(data.get("profile", {"year": year}))
        pays = data.get("payments", {})
        return cls(
            year=year,
            description=str(data.get("description", "")),
            profile=profile,
            sources=[IncomeSource.from_dict(s) for s in data.get("sources", [])],
            payments={k: PaymentLine.from_dict(k, pays.get(k, {})) for k in PAYMENT_LINES},
            notes=str(data.get("notes", "")),
            dependents=[str(d) for d in data.get("dependents", [])],
            prior_year_tax=_opt_float(data.get("prior_year_tax")),
            prior_year_agi=_opt_float(data.get("prior_year_agi")),
        )


def _opt_float(v) -> float | None:
    return None if v is None else float(v)


@dataclass
class NewYearDefaults:
    """View > New Year Defaults, saved in the file."""

    qualified_share: float = 0.75  # for new ordinary-dividend sources
    clone_mode: str = "set"  # "retain" the source's share, or "set" it to clone_share
    clone_share: float = 0.75

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NewYearDefaults":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        out = cls(**known)
        if out.clone_mode not in ("retain", "set"):
            out.clone_mode = "set"
        return out


@dataclass
class EqtaFile:
    years: list[TaxYear] = field(default_factory=list)  # newest year first
    defaults: NewYearDefaults = field(default_factory=NewYearDefaults)
    notes: str = ""  # File notes (Markdown), for the whole file; each year has its own too
    schema_version: int = SCHEMA_VERSION

    def year(self, year: int) -> TaxYear | None:
        """The first tab for ``year`` (a file can hold two, say a plan and actuals)."""
        return next((y for y in self.years if y.year == year), None)

    def prior_year(self, ty: TaxYear) -> TaxYear | None:
        return self.year(ty.year - 1)

    def new_year(self, year: int, description: str = "", like: TaxYear | None = None) -> TaxYear:
        """A blank year. Its profile comes from ``like`` (another year's tab) when given."""
        profile = copy.deepcopy(like.profile) if like is not None else eqta_profile(year)
        ty = TaxYear(year=year, description=description, profile=profile,
                     dependents=list(like.dependents) if like else [])
        self.years.append(ty)
        self.years.sort(key=lambda y: y.year, reverse=True)  # newest first
        return ty

    def clone_year(self, source: TaxYear, year: int, description: str = "") -> TaxYear:
        """A copy of ``source`` with the same profile and income sources and
        every amount zeroed: no register entries and no plan amounts."""
        ty = self.new_year(year, description, like=source)
        for s in source.sources:
            share = s.qualified_share
            if s.tax_type == "dividends_to_split" and self.defaults.clone_mode == "set":
                share = self.defaults.clone_share
            ty.sources.append(IncomeSource(name=s.name, tax_type=s.tax_type, person=s.person,
                                           notes=s.notes, qualified_share=share))
        return ty

    def new_source(self, tax_type: str = "taxable_interest") -> IncomeSource:
        share = self.defaults.qualified_share if tax_type == "dividends_to_split" else None
        return IncomeSource(tax_type=tax_type, qualified_share=share)

    # -- persistence ------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "kind": FILE_KIND,
            "schema_version": self.schema_version,
            "defaults": self.defaults.to_dict(),
            "notes": self.notes,
            "years": [y.to_dict() for y in self.years],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EqtaFile":
        if not isinstance(data, dict) or data.get("kind") != FILE_KIND:
            raise ValueError("Not an estimated tax (.eqta) file")
        years = [TaxYear.from_dict(y) for y in data.get("years", [])]
        years.sort(key=lambda y: y.year, reverse=True)  # newest first, as the tabs show
        return cls(years=years, defaults=NewYearDefaults.from_dict(data.get("defaults", {})),
                   notes=str(data.get("notes", "")),
                   schema_version=int(data.get("schema_version", SCHEMA_VERSION)))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "EqtaFile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
