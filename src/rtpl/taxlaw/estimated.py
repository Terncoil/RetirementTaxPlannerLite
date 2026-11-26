"""Estimated-payment schedules: due dates and the share due in each period.

Federal and state law files can hold an ``[estimated_payments]`` table::

    [estimated_payments]
    required = true            # individuals must make estimated payments
    annual_only = false        # true: one payment with the return (WA)
    holidays = ["federal"]     # days a due date moves past (see HOLIDAYS)
    notes = ["..."]
    installments = [
      { due = "04-15", share = 0.30 },
      { due = "06-15", share = 0.40 },
      { share = 0.0 },                       # no payment for this period
      { due = "01-15", share = 0.30 },
    ]

``due`` is a month and day, in the tax year if that falls after the end of
the installment's period and otherwise in the next year (January 15 for
the last quarter, April 15 for a tax paid once with the return). A date on a Saturday, Sunday or
one of the ``holidays`` moves to the next day that is none of them, as
federal law (IRC 7503) and the states do. ``due_date`` (a TOML date) gives
the exact date instead, for the file's own year only: a postponement, or a
date the state published that the rule doesn't give. A later year that
uses the same file goes back to ``due``.

Four installments cover the periods ending March 31, May 31, August 31
and December 31, and one covers the whole year. An installment's
``period_end`` ("MM-DD") replaces that: Virginia's first period runs to
April 30 (Form 760C), so income through April 30 counts toward May 1.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache

PERIOD_ENDS = ((3, 31), (5, 31), (8, 31), (12, 31))

# Every holiday a due date can move past. "federal" stands for the legal
# holidays in the District of Columbia that IRC 7503 counts (the federal
# holidays plus Inauguration Day and DC Emancipation Day).
HOLIDAY_NAMES = (
    "new_year", "mlk", "inauguration", "washington", "emancipation",
    "patriots", "good_friday", "memorial", "juneteenth", "independence",
    "labor", "columbus", "veterans", "thanksgiving", "christmas",
)
FEDERAL_HOLIDAYS = (
    "new_year", "mlk", "inauguration", "washington", "emancipation",
    "memorial", "juneteenth", "independence", "labor", "columbus",
    "veterans", "thanksgiving", "christmas",
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The ``n``th ``weekday`` (Monday = 0) of a month; n = -1 is the last."""
    if n > 0:
        first = dt.date(year, month, 1)
        return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: dt.date) -> dt.date:
    """A fixed-date holiday on a Saturday is kept on Friday, on a Sunday on Monday."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def _easter(year: int) -> dt.date:
    # Anonymous Gregorian algorithm.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(year, month, day)


def _holiday(name: str, year: int) -> dt.date | None:
    if name == "new_year":
        return _observed(dt.date(year, 1, 1))
    if name == "mlk":
        return _nth_weekday(year, 1, 0, 3)
    if name == "inauguration":
        # January 20 after a presidential election, Monday if a Sunday.
        if year % 4 != 1:
            return None
        day = dt.date(year, 1, 20)
        return day + dt.timedelta(days=1) if day.weekday() == 6 else day
    if name == "washington":
        return _nth_weekday(year, 2, 0, 3)
    if name == "emancipation":  # DC Emancipation Day, April 16
        return _observed(dt.date(year, 4, 16))
    if name == "patriots":  # Massachusetts and Maine
        return _nth_weekday(year, 4, 0, 3)
    if name == "good_friday":
        return _easter(year) - dt.timedelta(days=2)
    if name == "memorial":
        return _nth_weekday(year, 5, 0, -1)
    if name == "juneteenth":
        return _observed(dt.date(year, 6, 19)) if year >= 2021 else None
    if name == "independence":
        return _observed(dt.date(year, 7, 4))
    if name == "labor":
        return _nth_weekday(year, 9, 0, 1)
    if name == "columbus":
        return _nth_weekday(year, 10, 0, 2)
    if name == "veterans":
        return _observed(dt.date(year, 11, 11))
    if name == "thanksgiving":
        return _nth_weekday(year, 11, 3, 4)
    if name == "christmas":
        return _observed(dt.date(year, 12, 25))
    raise ValueError(f"Unknown holiday {name!r}")


def expand_holidays(names) -> tuple[str, ...]:
    out: list[str] = []
    for n in names:
        for m in (FEDERAL_HOLIDAYS if n == "federal" else (n,)):
            if m not in HOLIDAY_NAMES:
                raise ValueError(f"Unknown holiday {m!r}; use one of federal, {', '.join(HOLIDAY_NAMES)}")
            if m not in out:
                out.append(m)
    return tuple(out)


@lru_cache(maxsize=None)
def holidays_in(year: int, names: tuple[str, ...]) -> frozenset[dt.date]:
    days = set()
    for y in (year, year + 1):  # New Year's Day on a Saturday is kept on Dec 31
        for n in names:
            d = _holiday(n, y)
            if d is not None:
                days.add(d)
    return frozenset(days)


def next_business_day(day: dt.date, names: tuple[str, ...]) -> dt.date:
    """``day``, or the next day that is not a weekend or one of the holidays."""
    while day.weekday() >= 5 or day in holidays_in(day.year, names) or day in holidays_in(day.year - 1, names):
        day += dt.timedelta(days=1)
    return day


@dataclass(frozen=True)
class Installment:
    share: float  # of the year's required annual payment
    due: tuple[int, int] | None = None  # (month, day); None = nothing due
    due_date: dt.date | None = None  # exact date, for the file's own year only
    period_end: tuple[int, int] | None = None  # (month, day) when not the usual one


@dataclass(frozen=True)
class EstimatedPayments:
    """When estimated tax is due and how much of it by each date."""

    year: int | None  # the law file's year (``due_date`` applies to it only)
    required: bool
    installments: tuple[Installment, ...]
    annual_only: bool = False
    holidays: tuple[str, ...] = FEDERAL_HOLIDAYS
    notes: tuple[str, ...] = ()

    def period_ends(self, tax_year: int) -> tuple[dt.date, ...]:
        """The last day of the income period each installment covers."""
        usual = ((12, 31),) if len(self.installments) == 1 else PERIOD_ENDS
        return tuple(
            dt.date(tax_year, *(inst.period_end or default))
            for inst, default in zip(self.installments, usual)
        )

    def due_dates(self, tax_year: int) -> tuple[dt.date | None, ...]:
        """Each installment's due date for ``tax_year`` (None when nothing is due)."""
        out: list[dt.date | None] = []
        for inst, end in zip(self.installments, self.period_ends(tax_year)):
            if inst.due_date is not None and tax_year == self.year:
                out.append(inst.due_date)
            elif inst.due is None or inst.share == 0:
                out.append(None)
            else:
                month, day = inst.due
                nominal = dt.date(tax_year, month, day)
                if nominal <= end:
                    nominal = dt.date(tax_year + 1, month, day)
                out.append(next_business_day(nominal, self.holidays))
        return tuple(out)

    @property
    def shares(self) -> tuple[float, ...]:
        return tuple(i.share for i in self.installments)

    @property
    def cumulative_shares(self) -> tuple[float, ...]:
        """Share of the required annual payment due by each installment
        (California: 30%, 70%, 70%, 100%)."""
        total, out = 0.0, []
        for s in self.shares:
            total += s
            out.append(round(total, 10))
        return tuple(out)

    def annualized_percentages(self, current_year_share: float = 0.90) -> tuple[float, ...]:
        """The annualized-income method's percentage of annualized tax due by
        each installment: the cumulative shares times the current-year safe
        harbor (California: 27%, 63%, 63%, 90%; federal 22.5%, 45%, 67.5%, 90%)."""
        return tuple(round(c * current_year_share, 10) for c in self.cumulative_shares)

    def differs_from(self, other: "EstimatedPayments | None", tax_year: int) -> tuple[bool, ...]:
        """For each installment, whether its due date differs from the one in
        ``other`` (the federal schedule) for the period ending the same day."""
        mine = self.due_dates(tax_year)
        if other is None:
            return tuple(False for _ in mine)
        theirs = dict(zip(other.period_ends(tax_year), other.due_dates(tax_year)))
        return tuple(
            d is not None and theirs.get(end) != d
            for end, d in zip(self.period_ends(tax_year), mine)
        )


def _parse_due(value, key: str = "due") -> tuple[int, int]:
    try:
        month, day = (int(p) for p in str(value).split("-"))
        dt.date(2000, month, day)  # validates (2000 is a leap year)
    except (ValueError, TypeError):
        raise ValueError(f"estimated_payments {key} must be \"MM-DD\", not {value!r}") from None
    return month, day


def parse_estimated_payments(raw: dict | None, year: int | None) -> EstimatedPayments | None:
    if raw is None:
        return None
    insts = []
    for item in raw.get("installments", []):
        due_date = item.get("due_date")
        if due_date is not None and not isinstance(due_date, dt.date):
            raise ValueError(f"estimated_payments due_date must be a TOML date, not {due_date!r}")
        insts.append(
            Installment(
                share=float(item.get("share", 0.0)),
                due=_parse_due(item["due"]) if "due" in item else None,
                due_date=due_date,
                period_end=_parse_due(item["period_end"], "period_end") if "period_end" in item else None,
            )
        )
    if len(insts) not in (0, 1, 4):
        raise ValueError("estimated_payments needs four installments, one, or none")
    if insts and abs(sum(i.share for i in insts) - 1.0) > 1e-9:
        raise ValueError("estimated_payments installment shares must add up to 1")
    for i in insts:
        if i.share > 0 and i.due is None and i.due_date is None:
            raise ValueError("an installment with a share needs a due date")
    ends = [i.period_end or PERIOD_ENDS[n] for n, i in enumerate(insts)] if len(insts) == 4 else []
    if any(a >= b for a, b in zip(ends, ends[1:])):
        raise ValueError("estimated_payments period ends must be in order")
    annual_only = bool(raw.get("annual_only", False))
    if annual_only and len(insts) != 1:
        raise ValueError("annual_only estimated_payments has exactly one installment")
    return EstimatedPayments(
        year=year,
        required=bool(raw.get("required", True)),
        installments=tuple(insts),
        annual_only=annual_only,
        holidays=expand_holidays(raw.get("holidays", ["federal"])),
        notes=tuple(raw.get("notes", ())),
    )


__all__ = [
    "EstimatedPayments", "Installment", "parse_estimated_payments", "next_business_day",
    "PERIOD_ENDS", "HOLIDAY_NAMES", "FEDERAL_HOLIDAYS",
]
