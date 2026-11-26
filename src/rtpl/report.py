"""One PDF report: a page of profile details, then a page for each chart,
so someone can print or share everything at once.

Charts are drawn by the caller (each gets a fresh landscape figure), so the
report uses the same drawing code as the screen. Later versions may add
tables after the charts.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure

from rtpl import __version__
from rtpl.model.profile import ACA_PLAN_TIERS, FILING_STATUS_LABELS, Profile

PORTRAIT = (8.5, 11.0)  # US Letter, inches
LANDSCAPE = (11.0, 8.5)

HOUSEHOLD_LABELS = {
    "taxable_interest": "Taxable interest",
    "tax_exempt_interest": "Tax-exempt interest",
    "qualified_dividends": "Qualified dividends",
    "nonqualified_dividends": "Nonqualified dividends",
    "long_term_capital_gains": "Long-term capital gains",
    "short_term_capital_gains": "Short-term capital gains",
    "other_ordinary_income": "Other ordinary income",
    "other_investment_income": "Other investment income",
    "adjustments": "Adjustments to income",
    "itemized_deductions": "Itemized deductions",
    "charitable_cash_gifts": "Cash gifts to charity",
}


@dataclass
class Section:
    heading: str
    rows: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ReportChart:
    title: str
    draw: Callable[[Figure], None]


def _money(v: float) -> str:
    return f"-${-v:,.0f}" if v < 0 else f"${v:,.0f}"


def profile_sections(profile: Profile, *, state_name: str | None = None) -> list[Section]:
    """The profile as headed lists of label and value."""
    year = profile.year
    main = Section("Profile", [
        ("Name", profile.name),
        ("Tax year", str(year)),
        ("Filing status", FILING_STATUS_LABELS.get(profile.filing_status, profile.filing_status)),
        ("State", state_name or profile.state or "None"),
        ("Inflation", f"{profile.inflation:.1%}"),
    ])
    sections = [main]
    for person in profile.people:
        rows = [
            ("Born", f"{person.birth_year} (age {person.age_in(year)} in {year})"),
            ("Life expectancy", str(person.life_expectancy)),
            ("Social Security", _money(person.social_security)),
            ("Wages", _money(person.wages)),
            ("Pension", _money(person.pension)),
            ("IRA distributions", _money(person.ira_distributions)),
        ]
        if person.medicare_part_b is not None:
            rows.append(("On Medicare Part B", "Yes" if person.medicare_part_b else "No"))
        if person.blind:
            rows.append(("Blind", "Yes"))
        if person.aca_benchmark_premium:
            rows.append(("ACA benchmark Silver", f"{_money(person.aca_benchmark_premium)}/month"))
            if person.aca_plan_premium:
                rows.append(("ACA plan premium", f"{_money(person.aca_plan_premium)}/month"))
        sections.append(Section(person.name, rows))
    h = profile.household
    rows = [(label, _money(getattr(h, key))) for key, label in HOUSEHOLD_LABELS.items() if getattr(h, key)]
    if not h.itemized_deductions:
        rows.append(("Deduction", "Standard"))
    if h.has_taxable_brokerage:
        rows.append(("Taxable brokerage account", "Yes"))
    if h.aca_marketplace:
        rows.append(("ACA marketplace coverage", f"Yes ({ACA_PLAN_TIERS.get(h.aca_plan_tier, h.aca_plan_tier)})"))
    if h.aotc_students:
        rows.append(("College students (AOTC)",
                     f"{h.aotc_students}, {_money(h.aotc_expenses_per_student)} expenses each"))
    if h.llc_expenses:
        rows.append(("Lifetime Learning expenses", _money(h.llc_expenses)))
    if (h.aotc_students or h.llc_expenses) and h.education_last_year:
        rows.append(("Education credits through", str(h.education_last_year)))
    if h.household_size:
        rows.append(("Household size", str(h.household_size)))
    sections.append(Section("Household", rows))
    return sections


def _info_pages(title: str, subtitle: str, sections: list[Section]) -> list[Figure]:
    """Lay the sections out on as many portrait pages as they need."""
    line = 0.019  # one 10-point line, as a fraction of the page height
    pages: list[Figure] = []
    fig: Figure | None = None
    y = 0.0

    def new_page() -> None:
        nonlocal fig, y
        fig = Figure(figsize=PORTRAIT)
        fig.set_facecolor("white")
        pages.append(fig)
        y = 0.94
        if len(pages) == 1:
            fig.text(0.08, y, title, fontsize=18, weight="bold", va="top")
            y -= 0.035
            fig.text(0.08, y, subtitle, fontsize=9, color="#555555", va="top")
            y -= 0.045

    new_page()
    for section in sections:
        needed = line * (2 + min(len(section.rows), 3))
        if y - needed < 0.06:
            new_page()
        assert fig is not None
        fig.text(0.08, y, section.heading, fontsize=12, weight="bold", va="top")
        y -= line * 1.4
        for label, value in section.rows:
            wrapped = textwrap.wrap(value, 55) or [""]
            if y - line * len(wrapped) < 0.06:
                new_page()
            fig.text(0.10, y, label, fontsize=10, color="#333333", va="top")
            for part in wrapped:
                fig.text(0.42, y, part, fontsize=10, va="top")
                y -= line
        y -= line * 0.8
    return pages


def write_report(path: str | Path, title: str, sections: list[Section], charts: list[ReportChart]) -> int:
    """Write the PDF. Returns the number of pages."""
    today = date.today()
    subtitle = (f"Made {today:%B} {today.day}, {today.year} with RetirementTaxPlannerLite {__version__}. "
                "A planning aid, not tax advice.")
    figures = _info_pages(title, subtitle, sections)
    for chart in charts:
        fig = Figure(figsize=LANDSCAPE)
        chart.draw(fig)
        figures.append(fig)
    with PdfPages(path, metadata={"Title": title, "Creator": f"RetirementTaxPlannerLite {__version__}"}) as pdf:
        for fig in figures:
            pdf.savefig(fig, facecolor=fig.get_facecolor())
    return len(figures)

