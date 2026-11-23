"""Education credits, the ACA premium tax credit, and their marginal layers."""

import pytest

from rtpl.engine.forward import run_plan, year_table
from rtpl.engine.marginal import LAYERS, build_marginal_series, default_visibility
from rtpl.engine.tax import compute_tax, education_credits, premium_tax_credit
from rtpl.model.plan import Assumptions, Plan
from rtpl.model.profile import Household, Person, Profile

from conftest import make

EXAMPLE = "examples/profiles/early_retirees_college_aca.json"


def college(year=2026, **household):
    """The early-retired couple with two in college, without the ACA."""
    h = dict(taxable_interest=18_000, qualified_dividends=9_500, long_term_capital_gains=35_000,
             aotc_students=2, household_size=4)
    h.update(household)
    return Profile(year=year, filing_status="mfj",
                   people=[Person(birth_year=year - 55), Person(birth_year=year - 55)],
                   household=Household(**h))


def rate_at(series, x, key):
    return series.layers[key][int(x // series.step)]


# -- the law ---------------------------------------------------------------------


def test_applicable_percentage_table(lib):
    law = lib.federal(2026)
    assert law.applicable_percentage(1.0) == pytest.approx(0.021)
    assert law.applicable_percentage(1.75) == pytest.approx(0.0419 + (0.066 - 0.0419) / 2)
    assert law.applicable_percentage(3.5) == pytest.approx(0.0996)
    enhanced = lib.federal(2025)
    assert enhanced.applicable_percentage(1.4) == 0.0
    assert enhanced.applicable_percentage(6.0) == pytest.approx(0.085)
    assert lib.federal(2027).applicable_percentage(3.2) == pytest.approx(0.1022)
    # Projected years carry the newest table.
    assert lib.federal(2030).applicable_percentage(3.2) == pytest.approx(0.1022)


# -- education credits -------------------------------------------------------------


def test_education_credits_and_phase_out(lib):
    law = lib.federal(2026)
    p = college()
    assert education_credits(p, law, 100_000) == pytest.approx((3_000, 2_000))
    assert education_credits(p, law, 170_000) == pytest.approx((1_500, 1_000))
    assert education_credits(p, law, 180_000) == (0.0, 0.0)
    # $3,000 of expenses: $2,000 + 25% of $1,000 each.
    assert sum(education_credits(college(aotc_expenses_per_student=3_000), law, 0)) == pytest.approx(4_500)
    # Lifetime Learning: 20% of up to $10,000 a return, nonrefundable.
    llc = college(aotc_students=0, llc_expenses=15_000)
    assert education_credits(llc, law, 50_000) == pytest.approx((2_000, 0))
    # Married filing separately can't claim them; past the last year neither.
    mfs = college()
    mfs.filing_status = "mfs"
    assert education_credits(mfs, law, 50_000) == (0.0, 0.0)
    assert education_credits(college(education_last_year=2025), law, 50_000) == (0.0, 0.0)


def test_nonrefundable_credit_limited_to_income_tax(lib):
    law = lib.federal(2026)
    t = compute_tax(college(), law)
    # All the gains sit in the 0% band, so there is no tax to offset: only
    # the refundable $2,000 is paid.
    assert t.income_tax_before_credits == 0
    assert t.nonrefundable_used == 0
    assert t.federal_tax == pytest.approx(-2_000)
    t = compute_tax(college(), law, extra=40_000)
    assert t.nonrefundable_used == pytest.approx(min(3_000, t.income_tax_before_credits))


def test_credit_layers_reproduce_the_college_example(lib):
    s = build_marginal_series(college(), lib, x_max=150_000, step=100)
    # The standard deduction covers the first dollars, then the unused
    # nonrefundable credit cancels the 10% bracket.
    assert rate_at(s, 5_000, "ordinary") == 0
    assert rate_at(s, 20_000, "ordinary") == pytest.approx(0.10)
    assert rate_at(s, 20_000, "nonref") == pytest.approx(-0.10)
    assert s.total[int(20_000 // s.step)] == pytest.approx(0.0)
    # Credit used up: the bracket rate shows.
    assert rate_at(s, 50_000, "nonref") == 0
    # MAGI $160,000 to $180,000: $5,000 of credit lost over $20,000.
    assert rate_at(s, 105_000, "edu") == pytest.approx(0.25)
    assert s.total[int(105_000 // s.step)] == pytest.approx(0.52)
    assert rate_at(s, 130_000, "edu") == 0
    vis = default_visibility(s)
    assert vis["edu"] and vis["nonref"] and not vis["ptc"]
    # The layers still add up to the change in tax.
    summed = sum(sum(s.layers[k]) for k in LAYERS) * s.step
    assert summed == pytest.approx(s.results[-1].total_tax - s.baseline.total_tax, abs=1e-6)


def test_chart_draws_below_zero(lib):
    from matplotlib.figure import Figure

    from rtpl.charts.marginal_chart import render_marginal
    from rtpl.theme import Theme

    s = build_marginal_series(college(), lib, x_max=150_000, step=100)
    fig = Figure()
    render_marginal(fig, s, default_visibility(s), Theme.load().palette("light"))
    assert fig.axes[0].get_ylim()[0] < -0.09


# -- the premium tax credit ---------------------------------------------------------


def aca_single(year=2026, bench=800.0, plan=0.0, **household):
    return make(year=year, people=[Person(birth_year=year - 60, aca_benchmark_premium=bench,
                                          aca_plan_premium=plan)], aca_marketplace=True, **household)


def test_premium_tax_credit(lib):
    law = lib.federal(2026)
    p = aca_single()
    fpl = law.fpl(1)
    magi = 3.5 * fpl
    assert premium_tax_credit(p, law, magi) == pytest.approx(9_600 - 0.0996 * magi)
    # The cliff: nothing above 400% FPL, and nothing below 100%.
    assert premium_tax_credit(p, law, 4.0 * fpl + 1) == 0
    assert premium_tax_credit(p, law, 4.0 * fpl + 1, continuous=True) == pytest.approx(9_600 - 0.0996 * 4 * fpl)
    assert premium_tax_credit(p, law, 0.9 * fpl) == 0
    # A cheaper (Bronze) plan caps the credit.
    bronze = aca_single(plan=300)
    assert premium_tax_credit(bronze, law, 1.5 * fpl) == pytest.approx(3_600)
    # Enhanced 2025 credits: no cliff.
    p25 = aca_single(year=2025)
    law25 = lib.federal(2025)
    assert premium_tax_credit(p25, law25, 4.5 * law25.fpl(1)) == pytest.approx(
        9_600 - 0.085 * 4.5 * law25.fpl(1))
    # Not on the marketplace, on Medicare, or no benchmark: no credit.
    assert premium_tax_credit(make(people=[Person(birth_year=1966, aca_benchmark_premium=800)]), law, magi) == 0
    assert premium_tax_credit(make(people=[Person(birth_year=1950, aca_benchmark_premium=800)],
                                   aca_marketplace=True), law, magi) == 0


def test_ptc_layer_and_cliff_label(lib):
    p = aca_single(bench=800)
    law = lib.federal(2026)
    p.people[0].ira_distributions = 3.2 * law.fpl(1)
    s = build_marginal_series(p, lib, x_max=40_000, step=100)
    # 300%-400% FPL: the expected share is flat at 9.96%, so each dollar
    # costs 9.96 cents of credit.
    assert rate_at(s, 1_000, "ptc") == pytest.approx(0.0996)
    cliff = next(a for a in s.annotations if a.label == "ACA cliff 400%")
    assert cliff.annual_cost == pytest.approx(9_600 - 0.0996 * 4 * law.fpl(1))
    assert cliff.sublabel.startswith("-$")
    # Past the cliff nothing more is lost.
    assert rate_at(s, cliff.x + 1_000, "ptc") == 0
    assert default_visibility(s)["ptc"]
    # The table's breakpoints stay few despite the drifting rate.
    assert len(s.breakpoints()) < 20


def test_csr_line_only_for_silver(lib):
    p = aca_single()
    p.people[0].ira_distributions = 20_000
    labels = {a.label for a in build_marginal_series(p, lib, x_max=60_000).annotations}
    assert "CSR 250%" in labels
    p.household.aca_plan_tier = "bronze"
    labels = {a.label for a in build_marginal_series(p, lib, x_max=60_000).annotations}
    assert "CSR 250%" not in labels and "ACA cliff 400%" in labels


def test_example_profile_loads_and_charts(lib):
    p = Profile.load(EXAMPLE)
    assert not p.validate()
    s = build_marginal_series(p, lib, x_max=250_000, step=100)
    assert s.baseline.aca_ptc > 0
    assert {"edu", "nonref", "ptc"} <= s.nonzero_layers()
    # Everything but the premium layer adds up to the change in tax.
    summed = sum(sum(s.layers[k]) for k in LAYERS if k != "ptc") * s.step
    assert summed == pytest.approx(s.results[-1].total_tax - s.baseline.total_tax, abs=1e-6)


# -- forward plan --------------------------------------------------------------------


def test_forward_plan_pays_net_premiums_and_claims_credits(lib):
    prof = Profile.load(EXAMPLE)
    prof.household.taxable_interest = 100_000  # the plan keeps interest, not one-off gains
    for person in prof.people:
        person.life_expectancy = 70
    plan = Plan.from_profile(prof)
    plan.assumptions = Assumptions(spending=0.0, health_premium_growth=0.10)
    result = run_plan(plan, lib)
    by_year = {yr.year: yr for yr in result.years}
    y0, y1 = by_year[2026], by_year[2027]
    # Premiums grow at the health rate; both turn 65 in 2036 and go to Medicare.
    assert y0.tax.aca_premium == pytest.approx(2 * 12 * 800)
    assert y1.tax.aca_premium == pytest.approx(2 * 12 * 800 * 1.10)
    assert by_year[2036].tax.aca_premium == 0
    # The net premium comes out of the cash flow.
    assert 0 < y0.tax.aca_net_premium < y0.tax.aca_premium
    off = Plan.from_profile(prof)
    off.assumptions = plan.assumptions
    off.profile.household.aca_marketplace = False
    y0_off = run_plan(off, lib).years[0]
    assert y0_off.net_cash_flow - y0.net_cash_flow == pytest.approx(y0.tax.aca_net_premium, abs=2)
    # Education credits through 2028 only.
    assert by_year[2028].tax.credits > 0
    assert by_year[2029].tax.credits == 0
    header, _rows = year_table(result)
    assert "ACA premium tax credit" in header and "Education credits" in header
    # The taxes graph keeps every source at zero or above.
    assert all(v >= 0 for v in y0.tax_by_source().values())
