"""Forward-looking plan: RMD tables, Social Security, rules and the year loop."""

from pathlib import Path

import pytest

from rtpl.engine import social_security as ssa
from rtpl.engine.forward import PlanEngine, default_line_visibility, run_plan, year_table
from rtpl.engine.rmd import default_tables, rmd_start_age
from rtpl.model.plan import Account, Assumptions, IncomeStream, Plan, PersonPlan, Rule, SocialSecurity, When
from rtpl.model.profile import Household, Person, Profile

EXAMPLE = Path("examples/plans/couple_conversions_inherited.rtpl")


def couple(year=2026, births=(1956, 1958), **household):
    return Profile(
        year=year, filing_status="mfj",
        people=[Person(name="A", birth_year=births[0]), Person(name="B", birth_year=births[1])],
        household=Household(**household),
    )


def lives_to(prof, age):
    for p in prof.people:
        p.life_expectancy = age


def single(year=2026, birth=1951):
    return Profile(year=year, filing_status="single", people=[Person(name="A", birth_year=birth)])


# -- tables -----------------------------------------------------------------------


def test_rmd_tables():
    t = default_tables()
    assert t.uniform_period(73) == 26.5
    assert t.uniform_period(130) == 2.0  # 120 and older
    assert t.single_period(60) == 27.1
    # The derived joint table reproduces the Uniform Lifetime table (10-year gap) to 0.1 year.
    for age in range(72, 100):
        assert abs(t.joint_period(age, age - 10) - t.uniform_period(age)) <= 0.11
    # A much younger spouse gives a longer period than the uniform table.
    assert t.owner_period(75, 55, True) > t.uniform_period(75) + 5
    assert t.owner_period(75, 55, False) == t.uniform_period(75)
    assert t.owner_period(75, 70, True) == t.uniform_period(75)


def test_rmd_start_age():
    assert rmd_start_age(1950) == 72
    assert rmd_start_age(1951) == 73
    assert rmd_start_age(1959) == 73
    assert rmd_start_age(1960) == 75


def test_social_security_factors():
    assert ssa.full_retirement_age(1960) == 67
    assert ssa.full_retirement_age(1957) == pytest.approx(66.5)
    assert ssa.own_benefit_factor(1960, 62) == pytest.approx(0.70)
    assert ssa.own_benefit_factor(1960, 70) == pytest.approx(1.24)
    assert ssa.own_benefit_factor(1960, 67) == pytest.approx(1.0)
    assert ssa.spousal_factor(1960, 62) == pytest.approx(0.65)
    assert ssa.spousal_factor(1960, 68) == 1.0
    assert ssa.survivor_amount(30000, 21000) == pytest.approx(24750)  # 82.5% floor
    assert ssa.survivor_amount(30000, None) == 30000


# -- engine -------------------------------------------------------------------------


def test_rmd_uniform_first_year(lib):
    plan = Plan(profile=single(), accounts=[Account(id="ira", kind="traditional_ira", balance=1_000_000)])
    res = run_plan(plan, lib, with_lines=False)
    y0 = res.years[0]
    assert y0.ages[0] == 75
    rmd = y0.category_totals()["rmd"]
    assert rmd == pytest.approx(1_000_000 / 24.6)
    # The balance after the RMD grows at the default return.
    assert y0.balances_end["ira"] == pytest.approx((1_000_000 - rmd) * 1.05)


def test_rmd_joint_table_for_younger_spouse(lib):
    prof = couple(births=(1951, 1970))
    plan = Plan(profile=prof, accounts=[Account(id="ira", kind="traditional_ira", owner=0, balance=1_000_000)])
    rmd = run_plan(plan, lib, with_lines=False).years[0].category_totals()["rmd"]
    assert rmd == pytest.approx(1_000_000 / default_tables().joint_period(75, 56))
    assert rmd < 1_000_000 / 24.6


def test_inherited_ira_ten_year_rule_and_spread(lib):
    prof = single(birth=1970)
    lives_to(prof, 70)
    acct = Account(id="inh", kind="inherited_ira", owner=0, balance=500_000, inherited_year=2024,
                   original_owner_birth_year=1940, owner_started_rmds=True)
    plan = Plan(profile=prof, accounts=[acct], assumptions=Assumptions(return_rate=0.0))
    res = run_plan(plan, lib, with_lines=False)
    by_year = {y.year: y for y in res.years}
    # RMD years 2025+: Single Life at the beneficiary's age in 2025 (55 -> 31.6), less one per year.
    assert by_year[2026].category_totals()["inherited"] == pytest.approx(500_000 / 30.6)
    assert by_year[2034].balances_end["inh"] == pytest.approx(0.0)
    assert any("deadline year" in n for n in by_year[2034].notes)

    # 1/N over the final 4 years: equal withdrawals when returns are zero.
    plan.rules = [Rule(kind="withdrawal", account="inh", start=When(year=2031), end=When(year=2034), mode="spread")]
    res = run_plan(plan, lib, with_lines=False)
    amounts = [y.category_totals()["inherited"] for y in res.years if 2031 <= y.year <= 2034]
    assert max(amounts) - min(amounts) < 1.0


def test_one_year_rule_overrides_range(lib):
    prof = single(birth=1964)
    lives_to(prof, 68)
    plan = Plan(
        profile=prof,
        accounts=[Account(id="ira", kind="traditional_ira", balance=800_000)],
        rules=[
            Rule(kind="roth_conversion", account="ira", start=When(age=62), end=When(age=66), mode="fixed", amount=50_000),
            Rule(kind="roth_conversion", account="ira", start=When(year=2028), mode="fixed", amount=0),
        ],
        assumptions=Assumptions(),
    )
    conv = {y.year: y.category_totals()["roth_conversion"] for y in run_plan(plan, lib, with_lines=False).years}
    assert conv[2026] == 50_000 and conv[2027] == 50_000
    assert conv[2028] == 0
    assert conv[2029] == 50_000 and conv[2030] == 50_000
    assert conv[2031] == 0  # after the range


def test_fill_to_bracket_top_and_line_matches(lib):
    prof = couple(births=(1962, 1963))
    lives_to(prof, 67)
    plan = Plan(
        profile=prof,
        accounts=[Account(id="ira", kind="traditional_ira", owner=0, balance=2_000_000)],
        incomes=[IncomeStream(kind="pension", amount=30_000, owner=0)],
        rules=[Rule(kind="roth_conversion", account="ira", start=When(year=2026), end=When(year=2028),
                    mode="fill", target="bracket:0.22")],
        assumptions=Assumptions(),
    )
    res = run_plan(plan, lib)
    for y in res.years[:3]:
        top = [b for b, r in y.law.ordinary["mfj"].brackets if r == 0.24][0]
        ordinary_taxable = y.tax.taxable_income - y.tax.preferential_income
        assert ordinary_taxable == pytest.approx(top, abs=2.0)
        # The 22% line sits at the top of the bar.
        assert y.lines["bracket:0.22"] == pytest.approx(y.taxable_bar, abs=10.0)
    assert res.years[3].category_totals()["roth_conversion"] == 0
    # Conversions land in an automatically created Roth IRA.
    assert any(k.startswith("_roth") for k in res.years[0].balances_end)


def test_social_security_spousal_and_survivor(lib):
    prof = couple(births=(1960, 1960))
    lives_to(prof, 82)
    prof.people[0].life_expectancy = 80
    plan = Plan(
        profile=prof,
        people=[
            PersonPlan(social_security=SocialSecurity(fra_benefit=40_000, claim_age=67)),
            PersonPlan(social_security=SocialSecurity(fra_benefit=0, claim_age=67)),
        ],
        incomes=[IncomeStream(kind="pension", amount=20_000, owner=0, survivor_pct=0.5)],
        assumptions=Assumptions(ss_cola=0.0),
    )
    by_year = {y.year: y for y in run_plan(plan, lib, with_lines=False).years}
    y67 = by_year[2027]
    assert y67.category_totals()["ss"] == pytest.approx(40_000 + 20_000)  # own + 50% spousal
    assert by_year[2040].filing_status == "mfj"  # year of death: still joint
    after = by_year[2041]
    assert after.filing_status == "single"
    assert after.category_totals()["ss"] == pytest.approx(40_000)  # survivor steps up
    assert after.category_totals()["pension"] == pytest.approx(10_000)


def test_spousal_reduced_when_early(lib):
    prof = couple(births=(1964, 1964))
    lives_to(prof, 63)
    plan = Plan(
        profile=prof,
        people=[
            PersonPlan(social_security=SocialSecurity(fra_benefit=40_000, claim_age=62)),
            PersonPlan(social_security=SocialSecurity(fra_benefit=10_000, claim_age=62)),
        ],
        assumptions=Assumptions(ss_cola=0.0),
    )
    y62 = run_plan(plan, lib, with_lines=False).years[0]
    assert y62.ages[0] == 62
    own = 40_000 * 0.7 + 10_000 * 0.7
    spousal = (20_000 - 10_000) * 0.65
    assert y62.category_totals()["ss"] == pytest.approx(own + spousal)


def test_earnings_test_withholds_and_credits_back_at_fra(lib):
    # Born 1964: full retirement age 67, reached in January 2031.
    prof = single(birth=1964)
    lives_to(prof, 70)
    plan = Plan(
        profile=prof,
        people=[PersonPlan(social_security=SocialSecurity(fra_benefit=24_000, claim_age=62))],
        incomes=[IncomeStream(kind="wages", amount=44_480, owner=0, end=When(year=2027))],
        assumptions=Assumptions(ss_cola=0.0),
    )
    res = run_plan(plan, lib, with_lines=False)
    by = {y.year: y for y in res.years}
    early = 24_000 * 0.70
    # 2026: $1 for every $2 over $24,480.
    assert by[2026].ss_withheld == {0: pytest.approx(10_000)}
    assert by[2026].category_totals()["ss"] == pytest.approx(early - 10_000)
    assert any("earnings test" in n for n in by[2026].notes)
    # 2027: the limit is $25,560.
    assert by[2027].ss_withheld[0] == pytest.approx((44_480 - 25_560) / 2)
    # No wages: paid in full, not yet raised.
    assert by[2028].ss_withheld == {}
    assert by[2030].category_totals()["ss"] == pytest.approx(early)
    # At FRA, 7 + 6 whole months withheld (at $1,400 a month) are credited:
    # as if claimed at 63 and 1 month (47 months early).
    raised = 24_000 * (1 - (36 * 5 / 900 + 11 * 5 / 1200))
    assert by[2031].category_totals()["ss"] == pytest.approx(raised)
    assert any("raised at full retirement age for 13 months" in n for n in by[2031].notes)
    header, rows = year_table(res)
    assert "A SS withheld" in header
    assert rows[0][header.index("A SS withheld")] == "10,000"


def test_earnings_test_in_the_fra_year_counts_months_before_fra(lib):
    # Born 1959: full retirement age 66 and 10 months, reached in November 2025.
    prof = single(year=2025, birth=1959)
    lives_to(prof, 68)
    plan = Plan(
        profile=prof,
        people=[PersonPlan(social_security=SocialSecurity(fra_benefit=30_000, claim_age=62))],
        incomes=[IncomeStream(kind="wages", amount=120_000, owner=0, end=When(year=2026))],
        assumptions=Assumptions(ss_cola=0.0),
    )
    by = {y.year: y for y in run_plan(plan, lib, with_lines=False).years}
    # $1 for every $3 of the 10 months' wages over $62,160.
    assert by[2025].ss_withheld[0] == pytest.approx((100_000 - 62_160) / 3)
    assert by[2026].ss_withheld == {}  # past full retirement age: no limit


def test_earnings_test_reaches_the_spousal_top_up(lib):
    prof = couple(births=(1964, 1964))
    lives_to(prof, 63)
    plan = Plan(
        profile=prof,
        people=[
            PersonPlan(social_security=SocialSecurity(fra_benefit=40_000, claim_age=62)),
            PersonPlan(social_security=SocialSecurity(fra_benefit=0, claim_age=62)),
        ],
        incomes=[IncomeStream(kind="wages", amount=200_000, owner=0)],
        assumptions=Assumptions(ss_cola=0.0),
    )
    y = run_plan(plan, lib, with_lines=False).years[0]
    # A's wages withhold A's own check and then B's spousal benefit, paid on A's record.
    assert y.ss_withheld == {0: pytest.approx(28_000), 1: pytest.approx(13_000)}
    assert y.category_totals()["ss"] == 0


def test_earnings_test_shares_months_with_the_spouse_and_credits_each_at_their_fra(lib):
    # A (born 1964, FRA in 2031) works; B (born 1963, FRA in 2030) is on a
    # spousal benefit from A's record. Both filed in 2026.
    prof = couple(births=(1964, 1963))
    lives_to(prof, 68)
    own_a = 40_000 * 0.70
    spousal_b = 20_000 * (1 - (36 * 25 / 3600 + 12 * 5 / 1200))  # started at 63
    due = (own_a + spousal_b) / 2  # half of the family's checks
    plan = Plan(
        profile=prof,
        people=[
            PersonPlan(social_security=SocialSecurity(fra_benefit=40_000, claim_age=62)),
            PersonPlan(social_security=SocialSecurity(fra_benefit=0, claim_age=63)),
        ],
        incomes=[IncomeStream(kind="wages", amount=24_480 + 2 * due, owner=0, end=When(year=2026))],
        assumptions=Assumptions(ss_cola=0.0),
    )
    by = {y.year: y for y in run_plan(plan, lib, with_lines=False).years}
    # Six months of each check are withheld, not A's first.
    assert by[2026].ss_withheld == {0: pytest.approx(own_a / 2), 1: pytest.approx(spousal_b / 2)}
    assert by[2029].category_totals()["ss"] == pytest.approx(own_a + spousal_b)
    # B's spousal benefit is raised at B's FRA (as if started at 63 and 6 months) ...
    spousal_b_raised = 20_000 * (1 - (36 * 25 / 3600 + 6 * 5 / 1200))
    assert by[2030].category_totals()["ss"] == pytest.approx(own_a + spousal_b_raised)
    # ... and A's own benefit at A's FRA (as if claimed at 62 and 6 months).
    own_a_raised = 40_000 * (1 - (36 * 5 / 900 + 18 * 5 / 1200))
    assert by[2031].category_totals()["ss"] == pytest.approx(own_a_raised + spousal_b_raised)


def test_deficit_sells_brokerage_and_realizes_gains(lib):
    prof = single(birth=1960)
    lives_to(prof, 68)
    plan = Plan(
        profile=prof,
        accounts=[Account(id="brk", kind="taxable", owner=None, balance=500_000, cost_basis=250_000)],
        assumptions=Assumptions(spending=60_000, return_rate=0.0),
    )
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    ct = y0.category_totals()
    assert ct["principal"] + ct["realized_gains"] == pytest.approx(60_000 + y0.tax.total_tax, abs=2)
    assert ct["realized_gains"] == pytest.approx(ct["principal"], rel=1e-6)
    assert ct["dividends"] == 0  # no yields set on this account  # half the balance is gain


def test_example_plan_runs_and_round_trips(lib, tmp_path):
    plan = Plan.load(EXAMPLE)
    res = run_plan(plan, lib)
    assert res.years[0].year == 2026 and res.years[-1].year == 2060
    assert all(y.shortfall == 0 for y in res.years)
    vis = default_line_visibility(res)
    assert vis["bracket:0.22"] and vis["cg0"] and not vis["aca:4"]
    header, rows = year_table(res)
    assert len(rows) == len(res.years) and header[0] == "Year"
    out = tmp_path / "plan.json"
    plan.save(out)
    again = Plan.load(out)
    assert again.to_dict() == plan.to_dict()


def test_taxes_by_source_add_up_and_chart(lib):
    """The taxes graph's sources add up to the year table's total tax, and
    the RMD example shows the low-tax stretch, the RMD jump at 75 and the
    jump when the survivor files single."""
    from matplotlib.figure import Figure

    from rtpl.charts.forward_chart import render_taxes
    from rtpl.engine.forward import TAX_SOURCES, table_data
    from rtpl.theme import Theme

    res = run_plan(Plan.load(Path("examples/plans/couple_rmd_tax_problem.rtpl")), lib)
    assert all(y.shortfall == 0 for y in res.years)
    for y in res.years:
        parts = y.tax_by_source()
        assert list(parts) == list(TAX_SOURCES)
        assert sum(parts.values()) == pytest.approx(y.tax.total_tax + y.irmaa_surcharge)
    total = {y.year: y.tax.total_tax + y.irmaa_surcharge for y in res.years}
    assert max(total[y] for y in range(2026, 2039)) < 10_000 < 2 * 10_000 < total[2039]
    assert total[2052] > 1.15 * total[2051]  # the survivor files single
    assert any(y.irmaa_surcharge > 0 for y in res.years)

    t = table_data(res, columns=["federal_tax", "federal_by_type"])
    assert t.header == ["Year", "Federal tax", "Ordinary income tax", "Capital gains tax", "NIIT"]
    for row in t.rows:
        fed, *parts = (int(v.replace(",", "")) for v in row[1:])
        assert abs(fed - sum(parts)) <= 2

    palette = Theme.load("default").palette("dark")
    fig = Figure()
    render_taxes(fig, res, palette, todays_dollars=True)
    ax = fig.axes[0]
    assert "today's (2026) dollars" in ax.get_title(loc="left")
    labels = [t.get_text() for t in fig.legends[0].get_texts()]
    assert "Federal ordinary income tax" in labels and "Medicare IRMAA surcharges" in labels
    assert "State capital gains tax" not in labels  # Wisconsin has none of its own
    # The top of each bar is that year's total in today's dollars.
    tops: dict[float, float] = {}
    for bar in ax.patches:
        x = round(bar.get_x() + bar.get_width() / 2)
        tops[x] = max(tops.get(x, 0.0), bar.get_y() + bar.get_height())
    for y in res.years:
        assert tops[y.year] == pytest.approx(total[y.year] / res.deflator(y.year), rel=1e-6)


def test_plan_validation():
    plan = Plan(profile=single(), accounts=[Account(id="x", kind="bogus")],
                rules=[Rule(kind="roth_conversion", account="nope", start=When(year=2026), mode="fill")])
    problems = plan.validate()
    assert any("unknown kind" in p for p in problems)
    assert any("no account" in p for p in problems)
    assert any("target" in p for p in problems)
    with pytest.raises(ValueError):
        PlanEngine(plan, None)


def test_plan_cli(tmp_path):
    from rtpl.plan_cli import main

    out, bal, csv_path = tmp_path / "i.png", tmp_path / "b.svg", tmp_path / "y.csv"
    taxes = tmp_path / "t.png"
    assert main([str(EXAMPLE), "-o", str(out), "--balances", str(bal), "--taxes", str(taxes), "--csv", str(csv_path),
                 "--detail", "--todays-dollars", "--line", "bracket:0.32"]) == 0
    assert out.stat().st_size > 10_000 and bal.stat().st_size > 10_000 and taxes.stat().st_size > 10_000
    assert csv_path.read_text(encoding="utf-8").startswith("Year,")


def test_from_profile_carries_income():
    prof = couple()
    prof.people[0].social_security = 30_000
    prof.people[1].pension = 12_000
    plan = Plan.from_profile(prof)
    assert plan.people[0].social_security.current_benefit == 30_000
    assert [s.kind for s in plan.incomes] == ["pension"]


def test_ssdi_suspend_at_fra_until_70(lib):
    prof = single(birth=1966)
    lives_to(prof, 72)
    plan = Plan(profile=prof, people=[PersonPlan(social_security=SocialSecurity(
        fra_benefit=30_000, claim_age=70, ssdi=True))], assumptions=Assumptions(ss_cola=0.0))
    ss = {y.ages[0]: y.category_totals()["ss"] for y in run_plan(plan, lib, with_lines=False).years}
    assert ss[60] == pytest.approx(30_000) and ss[66] == pytest.approx(30_000)  # SSDI at the full benefit
    assert ss[67] == 0 and ss[69] == 0  # suspended from full retirement age
    assert ss[70] == pytest.approx(30_000 * 1.24)  # restarts with 36 months of delayed credits


def test_ssdi_default_continues_and_early_end(lib):
    prof = single(birth=1966)
    lives_to(prof, 69)
    plan = Plan(profile=prof, people=[PersonPlan(social_security=SocialSecurity(
        fra_benefit=30_000, claim_age=67, ssdi=True))], assumptions=Assumptions(ss_cola=0.0))
    ss = {y.ages[0]: y.category_totals()["ss"] for y in run_plan(plan, lib, with_lines=False).years}
    assert ss[66] == ss[67] == ss[68] == pytest.approx(30_000)
    plan.people[0].social_security = SocialSecurity(fra_benefit=30_000, claim_age=62, ssdi=True, ssdi_end_age=61)
    ss = {y.ages[0]: y.category_totals()["ss"] for y in run_plan(plan, lib, with_lines=False).years}
    assert ss[60] == pytest.approx(30_000) and ss[61] == 0
    assert ss[62] == pytest.approx(21_000)  # ordinary early claim afterwards


def test_threshold_lines_sit_at_the_threshold_when_income_cannot_get_under_it(lib):
    # Dividends alone are over the 0% gains band and the IRMAA tiers. The
    # lines still run every year, at the threshold (inside the bar), each on
    # its own value, not all stacked on the dividends.
    prof = single(birth=1951)
    lives_to(prof, 76)
    plan = Plan(profile=prof, incomes=[IncomeStream(kind="qualified_dividends", amount=400_000, owner=0)],
                assumptions=Assumptions())
    res = run_plan(plan, lib)
    y = res.years[0]
    std = y.tax.agi - y.tax.taxable_income
    cg_top = y.law.capital_gains["single"].thresholds()[0]
    assert y.lines["cg0"] == pytest.approx(cg_top + std, abs=10.0)
    tiers = [y.lines[f"irmaa:{n}"] for n in (1, 2, 3)]
    assert tiers[0] < tiers[1] < tiers[2] < y.total_income
    assert all(v is not None for yr in res.years for v in yr.lines.values())
    # The chart labels every shown line at the right.
    from matplotlib.figure import Figure

    from rtpl.charts.forward_chart import render_income
    from rtpl.theme import Theme

    fig = Figure()
    render_income(fig, res, {"cg0": True, "irmaa:1": True, "irmaa:2": True}, Theme.load("default").palette("light"))
    texts = [t.get_text() for t in fig.axes[0].texts]
    assert "0% CG" in texts and "IRMAA 1" in texts and "IRMAA 2" in texts


def test_nothing_starts_before_the_plan(lib):
    prof = single(birth=1960)
    lives_to(prof, 70)
    plan = Plan(
        profile=prof,
        accounts=[Account(id="ira", kind="traditional_ira", owner=0, balance=500_000)],
        incomes=[IncomeStream(kind="pension", amount=10_000, owner=0, start=When(year=2015), name="Old pension")],
        rules=[Rule(kind="withdrawal", account="ira", start=When(year=2015), end=When(year=2027), mode="spread"),
               Rule(kind="withdrawal", account="ira", start=When(year=2030), end=When(year=2029), mode="fixed",
                    amount=5_000)],
        assumptions=Assumptions(),
    )
    assert plan.rules[0].years(prof) == (2026, 2027)
    res = run_plan(plan, lib, with_lines=False)
    assert res.years[0].category_totals()["ira_withdrawal"] == pytest.approx(500_000 / 2 * 1, rel=0.02)
    assert res.years[0].category_totals()["pension"] == pytest.approx(10_000)
    notes = " ".join(res.notes)
    assert "Old pension: starts in 2015" in notes and "ends before it starts" in notes


def test_spread_rule_on_inherited_ira_stops_at_the_deadline(lib):
    prof = single(birth=1980)
    lives_to(prof, 62)
    plan = Plan(
        profile=prof,
        accounts=[Account(id="inh", kind="inherited_ira", owner=0, balance=100_000, inherited_year=2024,
                          original_owner_birth_year=1940, owner_started_rmds=False, return_rate=0.0)],
        rules=[Rule(kind="withdrawal", account="inh", start=When(year=2026), end=When(year=2040), mode="spread")],
        assumptions=Assumptions(),
    )
    res = run_plan(plan, lib, with_lines=False)
    taken = {y.year: y.category_totals()["inherited"] for y in res.years}
    # Nine years to the 2034 deadline, not 15 years to the rule's end.
    assert taken[2026] == pytest.approx(100_000 / 9, rel=1e-6)
    assert taken[2034] == pytest.approx(taken[2026], rel=1e-6)
    assert taken.get(2035, 0) == 0


def test_conversion_grows_at_the_roth_accounts_own_return(lib):
    prof = single(birth=1960)
    lives_to(prof, 67)
    plan = Plan(
        profile=prof,
        accounts=[Account(id="ira", kind="traditional_ira", owner=0, balance=500_000, return_rate=0.0),
                  Account(id="roth", kind="roth_ira", owner=0, balance=0.0, return_rate=0.10)],
        rules=[Rule(kind="roth_conversion", account="ira", start=When(year=2026), mode="fixed", amount=50_000,
                    to_account="roth")],
        assumptions=Assumptions(return_rate=0.02),
    )
    res = run_plan(plan, lib, with_lines=False)
    assert res.years[0].balances_end["roth"] == pytest.approx(55_000)


# -- plan file version 2 ---------------------------------------------------------

V1 = Path("tests/data/plan_v1.json")


def test_version_1_plan_files_migrate_and_give_the_same_result(lib, tmp_path):
    old = Plan.load(V1)
    new = Plan.load(EXAMPLE)
    assert [p.life_expectancy for p in old.profile.people] == [88, 94]
    assert [s.cola for s in old.incomes] == ["fixed", "none", "fixed", "none"]
    assert old.incomes[0].growth == pytest.approx(0.03)
    a, b = run_plan(old, lib, with_lines=False), run_plan(new, lib, with_lines=False)
    assert [y.year for y in a.years] == [y.year for y in b.years]
    assert sum(y.tax.total_tax for y in a.years) == pytest.approx(sum(y.tax.total_tax for y in b.years))
    # Saving writes version 2, without the old fields.
    old.save(tmp_path / "p.json")
    import json

    data = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert "death_age" not in data["people"][0] and "end_age" not in data["assumptions"]
    assert "inflation" not in data["assumptions"]
    assert data["profile"]["people"][0]["life_expectancy"] == 88


def test_migration_moves_inflation_end_age_and_beneficiary():
    data = {
        "profile": {"year": 2026, "filing_status": "mfj", "inflation": 0.025,
                    "people": [{"name": "A", "birth_year": 1960}, {"name": "B", "birth_year": 1962}]},
        "people": [{"death_age": 85}, {"death_age": None}],
        "accounts": [{"id": "inh", "kind": "inherited_ira", "inherited_year": 2022,
                      "eligible_designated_beneficiary": True}],
        "assumptions": {"inflation": 0.03, "end_age": 92},
    }
    plan = Plan.from_dict(data)
    assert [p.life_expectancy for p in plan.profile.people] == [85, 92]
    assert plan.inflation == pytest.approx(0.03) and plan.profile.inflation == pytest.approx(0.03)
    assert plan.accounts[0].beneficiary == "close_in_age"
    assert plan.accounts[0].deadline_year(plan.profile) is None
    assert plan.end_year == 1962 + 92
    assert plan.assumptions.spending_order == ["cash", "taxable"]


def test_death_and_end_of_plan_timings():
    prof = couple(births=(1960, 1964))
    prof.people[0].life_expectancy = 80  # dies 2040
    prof.people[1].life_expectancy = 90  # dies 2054
    assert When(event="death:0").resolve(prof) == 2040
    assert When(event="death:0").resolve(prof, start=True) == 2041  # the first year without them
    assert When(event="first_death").resolve(prof) == 2040
    assert When(event="last_death").resolve(prof) == 2054
    assert When(event="death:1").describe(prof) == "B dies"
    assert When(event="first_death").describe(prof, start=True) == "After the first death"
    plan = Plan(profile=prof, incomes=[
        IncomeStream(kind="annuity", amount=10_000, owner=1, start=When(event="first_death"), cola="none"),
        IncomeStream(kind="pension", amount=5_000, owner=0, end=When(event="first_death"), cola="none"),
    ])
    assert plan.end_year == 2054
    engine = PlanEngine(plan, None)
    assert engine.stream_amount(plan.incomes[0], 2040) == 0
    assert engine.stream_amount(plan.incomes[0], 2041) == 10_000
    assert engine.stream_amount(plan.incomes[1], 2040) == 5_000
    assert engine.stream_amount(plan.incomes[1], 2041) == 0


def test_cola_choices():
    prof = single(birth=1960)
    prof.inflation = 0.02
    plan = Plan(profile=prof, assumptions=Assumptions(ss_cola=0.03))
    engine = PlanEngine(plan, None)
    streams = {k: IncomeStream(kind="pension", amount=10_000, cola=k, growth=0.05)
               for k in ("none", "inflation", "ss_cola", "fixed")}
    got = {k: engine.stream_amount(s, 2028) for k, s in streams.items()}
    assert got["none"] == 10_000
    assert got["inflation"] == pytest.approx(10_000 * 1.02 ** 2)
    assert got["ss_cola"] == pytest.approx(10_000 * 1.03 ** 2)
    assert got["fixed"] == pytest.approx(10_000 * 1.05 ** 2)


def test_fixed_rule_amounts_can_grow(lib):
    prof = single(birth=1964)
    prof.inflation = 0.03
    lives_to(prof, 70)
    rule = Rule(kind="roth_conversion", account="ira", start=When(year=2028), end=When(year=2031),
                mode="fixed", amount=50_000)
    plan = Plan(profile=prof, accounts=[Account(id="ira", kind="traditional_ira", balance=2_000_000)], rules=[rule])
    conv = lambda: {y.year: y.category_totals()["roth_conversion"]  # noqa: E731
                    for y in run_plan(plan, lib, with_lines=False).years}
    assert conv()[2031] == 50_000  # flat
    rule.growth = "inflation"  # today's dollars
    assert conv()[2028] == pytest.approx(50_000 * 1.03 ** 2)
    rule.growth, rule.growth_rate = "fixed", -0.10  # a falling glide path from the first year
    got = conv()
    assert got[2028] == pytest.approx(50_000) and got[2030] == pytest.approx(50_000 * 0.9 ** 2)


def test_survivor_spending(lib):
    prof = couple(births=(1960, 1960))
    prof.people[0].life_expectancy = 70  # dies 2030
    prof.people[1].life_expectancy = 72
    plan = Plan(profile=prof, accounts=[Account(id="cash", kind="cash", owner=None, balance=2_000_000)],
                assumptions=Assumptions(spending=100_000, survivor_spending_pct=0.6))
    by_year = {y.year: y for y in run_plan(plan, lib, with_lines=False).years}
    f = (1 + prof.inflation) ** (2031 - 2026)
    assert by_year[2030].spending == pytest.approx(100_000 * (1 + prof.inflation) ** 4)
    assert by_year[2031].spending == pytest.approx(60_000 * f)


def test_spending_order_can_use_traditional_and_roth_accounts(lib):
    prof = single(birth=1958)
    lives_to(prof, 70)
    accounts = [Account(id="ira", kind="traditional_ira", balance=1_000_000, return_rate=0.0),
                Account(id="roth", kind="roth_ira", balance=100_000, return_rate=0.0)]
    plan = Plan(profile=prof, accounts=accounts, assumptions=Assumptions(spending=60_000))
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    assert y0.shortfall > 0 and "Short" in " ".join(y0.notes)  # cash and brokerage only, and there are none
    plan.assumptions.spending_order = ["traditional", "roth"]
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    ct = y0.category_totals()
    assert y0.shortfall == 0
    # The withdrawal pays the spending and its own tax.
    assert ct["ira_withdrawal"] == pytest.approx(60_000 + y0.tax.total_tax, abs=2)
    assert y0.tax.federal_tax > 0 and ct["roth_withdrawal"] == 0
    plan.assumptions.spending_order = ["roth", "traditional"]
    ct = run_plan(plan, lib, with_lines=False).years[0].category_totals()
    assert ct["roth_withdrawal"] == pytest.approx(60_000, abs=2) and ct["ira_withdrawal"] == 0


def test_inherited_ira_rules_from_who_inherited_it():
    prof = single(birth=1990)
    acct = Account(id="inh", kind="inherited_ira", owner=0, inherited_year=2024, original_owner_birth_year=1950)
    assert acct.inherited_rule(prof).deadline == 2034  # other person: 10 years
    acct.beneficiary = "spouse"
    assert acct.inherited_rule(prof).stretch and acct.deadline_year(prof) is None
    acct.beneficiary = "other"
    acct.inherited_year = 2018
    assert acct.deadline_year(prof) is None  # deaths before 2020 stretch for anyone
    acct.inherited_year = 2024
    acct.beneficiary = "minor_child"
    prof.people[0].birth_year = 2010  # turns 21 in 2031
    assert acct.deadline_year(prof) == 2041
    acct.beneficiary = "estate"
    acct.owner_started_rmds = False
    assert acct.deadline_year(prof) == 2029  # 5-year rule
    acct.owner_started_rmds = True
    rule = acct.inherited_rule(prof)
    assert rule.owner_life and rule.deadline is None


def test_estate_uses_the_owners_remaining_life(lib):
    prof = single(birth=1970)
    lives_to(prof, 60)
    acct = Account(id="inh", kind="inherited_ira", owner=0, balance=100_000, inherited_year=2024,
                   original_owner_birth_year=1944, owner_started_rmds=True, beneficiary="estate", return_rate=0.0)
    y = run_plan(Plan(profile=prof, accounts=[acct]), lib, with_lines=False).years[0]
    period = default_tables().single_period(2024 - 1944) - 1 - (2026 - 2025)
    assert y.category_totals()["inherited"] == pytest.approx(100_000 / period)


def test_successor_of_a_stretching_inherited_ira_gets_ten_years(lib):
    prof = couple(births=(1960, 1962))
    prof.people[0].life_expectancy = 70  # dies 2030
    prof.people[1].life_expectancy = 90
    acct = Account(id="inh", kind="inherited_ira", owner=0, balance=300_000, inherited_year=2018,
                   original_owner_birth_year=1935, return_rate=0.0)
    res = run_plan(Plan(profile=prof, accounts=[acct]), lib, with_lines=False)
    by_year = {y.year: y for y in res.years}
    assert any("empty it by 2040" in n for n in by_year[2031].notes)
    assert by_year[2040].balances_end["inh"] == pytest.approx(0.0)
    assert by_year[2039].balances_end["inh"] > 0


# -- group 3: year table columns, spousal top-up, RMD already taken ----------------


def test_year_table_default_and_all_columns(lib):
    from rtpl.engine.forward import DEFAULT_TABLE_COLUMNS, TABLE_COLUMN_KEYS, table_data

    plan = Plan.load(EXAMPLE)
    res = run_plan(plan, lib, with_lines=False)
    header, rows = year_table(res)
    assert header[0] == "Year" and header[-1] == "Notes"
    assert "Spending" in header and "Net cash flow" in header
    assert "Marginal rate" not in header and not any(h.endswith(" balance") for h in header)
    assert year_table(res, columns=list(DEFAULT_TABLE_COLUMNS)) == (header, rows)
    full = table_data(res, columns=TABLE_COLUMN_KEYS)
    assert len(full.header) == len(full.tips) == len(full.numeric) == len(full.rows[0])
    for name in ("Marginal rate", "Gains taxed at 0%", "Gains taxed at 15%", "Total tax", "Shortfall"):
        assert name in full.header
    # Per-account columns use the names the accounts start with.
    assert "Alex Traditional IRA balance" in full.header
    assert "Alex Traditional IRA conversion" in full.header
    assert not any("inherited from spouse" in h for h in full.header)
    # Only the picked columns (in table order, Year always first).
    h, r = year_table(res, columns=["notes", "spending"])
    assert h == ["Year", "Spending", "Notes"] and len(r[0]) == 3


def test_year_table_values_add_up(lib):
    plan = Plan.load(EXAMPLE)
    res = run_plan(plan, lib, with_lines=False)
    for yr in res.years:
        ct = yr.category_totals()
        required = sum(s.amount for s in yr.sources if s.label.endswith(("RMD", "required withdrawal")))
        assert sum(yr.rmds.values()) == pytest.approx(required, abs=1)
        assert sum(yr.conversions.values()) == pytest.approx(ct["roth_conversion"], abs=1)
        # Preferential income by rate is all of the year's dividends and gains.
        assert sum(yr.gains_by_rate.values()) == pytest.approx(yr.tax.qualified_dividends + yr.tax.net_ltcg, abs=1)
        assert 0 <= yr.marginal_rate < 0.7


def test_net_cash_flow_and_shortfall(lib):
    prof = single(birth=1960)
    lives_to(prof, 67)
    plan = Plan(
        profile=prof,
        accounts=[Account(id="cash", kind="cash", owner=None, balance=20_000)],
        incomes=[IncomeStream(kind="pension", amount=30_000, owner=0, cola="none")],
        assumptions=Assumptions(spending=50_000, return_rate=0.0, cash_return=0.0),
    )
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    assert y0.net_cash_flow == pytest.approx(30_000 - 50_000 - y0.tax.total_tax, abs=1)
    assert y0.withdrawals["cash"] == pytest.approx(20_000)
    assert y0.shortfall == pytest.approx(-y0.net_cash_flow - 20_000, abs=1)
    _h, r = year_table(run_plan(plan, lib, with_lines=False), columns=["net_cash_flow", "shortfall"])
    assert r[0][1].startswith("-") and r[0][2] != "0"


def test_marginal_rate_on_the_next_thousand(lib):
    prof = single(birth=1980)
    lives_to(prof, 46)
    plan = Plan(profile=prof, incomes=[IncomeStream(kind="wages", amount=100_000, owner=0, cola="none")])
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    assert y0.marginal_rate == pytest.approx(0.22)  # federal only, no state


def test_table_columns_round_trip(tmp_path):
    plan = Plan.load(EXAMPLE)
    assert plan.table_columns == []
    plan.table_columns = ["ages", "balances", "notes"]
    path = tmp_path / "p.json"
    plan.save(path)
    assert Plan.load(path).table_columns == ["ages", "balances", "notes"]


def test_spousal_topup_summary():
    from rtpl.engine.forward import spousal_topup

    prof = couple(births=(1964, 1964))
    lives_to(prof, 90)
    plan = Plan(
        profile=prof,
        people=[
            PersonPlan(social_security=SocialSecurity(fra_benefit=40_000, claim_age=67)),
            PersonPlan(social_security=SocialSecurity(fra_benefit=10_000, claim_age=62)),
        ],
    )
    big, small = spousal_topup(plan, 0), spousal_topup(plan, 1)
    assert big.amount == 0 and "at least half" in big.reason
    # B files at 62 but the top-up waits for A at 67, B's full retirement age: no reduction.
    assert small.amount == pytest.approx(10_000) and small.start_year == 2031 and small.age == 67
    assert small.waits_for_spouse and small.spouse == "A"
    plan.people[0].social_security.claim_age = 62
    early = spousal_topup(plan, 1)
    assert early.amount == pytest.approx(10_000 * 0.65) and early.start_year == 2026 and not early.waits_for_spouse
    assert spousal_topup(Plan(profile=single()), 0) is None


def test_rmd_already_taken_counts_as_income_but_not_twice(lib):
    def run(taken):
        plan = Plan(profile=single(), accounts=[
            Account(id="ira", kind="traditional_ira", balance=1_000_000, rmd_taken_year=2026 if taken else None)])
        return run_plan(plan, lib, with_lines=False)

    normal, taken = run(False).years[0], run(True).years[0]
    assert normal.category_totals()["rmd"] == pytest.approx(1_000_000 / 24.6)
    # The balance entered is after the RMD: RMD = B / (period - 1).
    assert taken.category_totals()["rmd"] == pytest.approx(1_000_000 / 23.6)
    assert taken.rmds["ira"] == pytest.approx(1_000_000 / 23.6)
    assert taken.balances_end["ira"] == pytest.approx(1_000_000 * 1.05)
    assert "ira" not in taken.withdrawals
    # Only while that year is the plan's first year.
    plan = Plan(profile=single(year=2027, birth=1952), accounts=[
        Account(id="ira", kind="traditional_ira", balance=1_000_000, rmd_taken_year=2026)])
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    assert y0.balances_end["ira"] < 1_000_000 * 1.05


# -- qualified charitable distributions ---------------------------------------------


def qcd_plan(prof, rules, accounts=None, **assumptions):
    accounts = accounts or [Account(id="ira", kind="traditional_ira", balance=1_000_000)]
    return Plan(profile=prof, accounts=accounts, rules=rules, assumptions=Assumptions(**assumptions))


def test_qcd_of_the_whole_rmd_is_not_taxed(lib):
    prof = single()  # age 75 in 2026
    lives_to(prof, 76)
    rmd = 1_000_000 / 24.6
    plan = qcd_plan(prof, [Rule(kind="qcd", account="ira", start=When(year=2026), end=When(year=2027), mode="rmd")])
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    assert y0.qcds == {"ira": pytest.approx(rmd)}
    assert y0.rmds == {}  # the QCD satisfied the RMD
    assert y0.category_totals()["rmd"] == 0 and y0.category_totals()["qcd"] == pytest.approx(rmd)
    assert y0.tax.agi == pytest.approx(0.0)
    assert y0.total_income == pytest.approx(0.0)  # it went to charity, not to the household
    assert y0.net_cash_flow == pytest.approx(0.0)
    assert y0.balances_end["ira"] == pytest.approx((1_000_000 - rmd) * 1.05)


def test_qcd_counts_toward_the_rmd_and_the_rest_is_taxed(lib):
    prof = single()
    lives_to(prof, 75)
    rmd = 1_000_000 / 24.6
    base = run_plan(qcd_plan(prof, []), lib, with_lines=False).years[0]
    plan = qcd_plan(prof, [Rule(kind="qcd", account="ira", start=When(year=2026), amount=10_000)])
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    assert y0.qcds["ira"] == pytest.approx(10_000)
    assert y0.rmds["ira"] == pytest.approx(rmd - 10_000)
    assert y0.tax.agi == pytest.approx(base.tax.agi - 10_000)
    assert y0.withdrawals == {}
    # Same money out of the account as the RMD alone.
    assert y0.balances_end["ira"] == pytest.approx(base.balances_end["ira"])

    # A QCD larger than the RMD: no taxable RMD, and the extra also leaves the account.
    plan.rules[0].amount = 60_000
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    assert y0.rmds == {} and y0.qcds["ira"] == pytest.approx(60_000)
    assert y0.balances_end["ira"] == pytest.approx((1_000_000 - 60_000) * 1.05)


def test_qcd_does_not_count_toward_a_withdrawal_rule(lib):
    prof = single()
    lives_to(prof, 75)
    rmd = 1_000_000 / 24.6
    plan = qcd_plan(prof, [Rule(kind="qcd", account="ira", start=When(year=2026), amount=10_000),
                           Rule(kind="withdrawal", account="ira", start=When(year=2026), amount=60_000)])
    y0 = run_plan(plan, lib, with_lines=False).years[0]
    taxable = y0.category_totals()["rmd"] + y0.category_totals()["ira_withdrawal"]
    assert taxable == pytest.approx(60_000)  # the withdrawal is spendable money; the QCD is on top
    assert y0.rmds["ira"] == pytest.approx(rmd - 10_000)
    assert y0.qcds["ira"] == pytest.approx(10_000)


def test_qcd_limits_by_year_and_person(lib):
    prof = couple(births=(1950, 1952))
    lives_to(prof, 90)
    accounts = [Account(id="a1", kind="traditional_ira", owner=0, balance=2_000_000),
                Account(id="a2", kind="inherited_ira", owner=0, balance=500_000, inherited_year=2010,
                        original_owner_birth_year=1925),
                Account(id="b1", kind="traditional_ira", owner=1, balance=2_000_000)]
    rules = [Rule(kind="qcd", account=a, start=When(year=2026), end=When(year=2029), amount=80_000)
             for a in ("a1", "a2", "b1")]
    plan = qcd_plan(prof, rules, accounts)
    years = {y.year: y for y in run_plan(plan, lib, with_lines=False).years}
    # A's two IRAs share A's limit; B has a limit of their own.
    assert years[2026].qcds["a1"] == pytest.approx(80_000)
    assert years[2026].qcds["a2"] == pytest.approx(111_000 - 80_000)
    assert years[2026].qcds["b1"] == pytest.approx(80_000)
    assert sum(v for k, v in years[2027].qcds.items() if k.startswith("a")) == pytest.approx(114_000)
    assert any("limit of $111,000" in n for n in years[2026].notes)
    # Projected years grow with inflation and round down to $1,000.
    law2028 = lib.federal(2028, plan.inflation)
    assert law2028.qcd_limit == 114_000 * (1 + plan.inflation) // 1000 * 1000
    assert sum(v for k, v in years[2028].qcds.items() if k.startswith("a")) == pytest.approx(law2028.qcd_limit)


def test_qcd_age_and_account_rules(lib):
    prof = single(birth=1956)  # 70 in 2026, 71 in 2027
    lives_to(prof, 72)
    accounts = [Account(id="ira", kind="traditional_ira", balance=500_000),
                Account(id="k", kind="401k", balance=500_000),
                Account(id="roth", kind="roth_ira", balance=100_000)]
    rules = [Rule(kind="qcd", account=a, start=When(year=2026), end=When(year=2027), amount=5_000)
             for a in ("ira", "k", "roth")]
    years = {y.year: y for y in run_plan(qcd_plan(prof, rules, accounts), lib, with_lines=False).years}
    assert years[2026].qcds == {}
    assert any("turn 71" in n for n in years[2026].notes)
    assert years[2027].qcds == {"ira": pytest.approx(5_000)}  # not the 401(k) or the Roth IRA
    assert any("traditional or inherited IRAs only" in n for n in years[2027].notes)


def test_qcd_rules_validate_their_amount_mode():
    prof = single()
    plan = qcd_plan(prof, [Rule(kind="qcd", account="ira", start=When(year=2026), mode="fill", target="cg0")])
    assert any("can't use the amount mode" in p for p in plan.validate())
    plan.rules[0].mode = "rmd"
    assert plan.validate() == []
    assert "Qualified charitable distribution from" in plan.rules[0].describe(prof, {a.id: a for a in plan.accounts})


def test_qcd_year_table_column(lib):
    prof = single()
    lives_to(prof, 76)
    plan = qcd_plan(prof, [Rule(kind="qcd", account="ira", start=When(year=2026), end=When(year=2027), mode="rmd")])
    res = run_plan(plan, lib, with_lines=False)
    header, rows = year_table(res)
    assert "Traditional IRA QCD" in " ".join(header)
    assert "Qualified charitable distributions (to charity, not taxed)" in header
    # Without QCDs there is no QCD column.
    header, _ = year_table(run_plan(qcd_plan(prof, []), lib, with_lines=False))
    assert not any("QCD" in h for h in header)


# -- HSA contributions --------------------------------------------------------------


def hsa_plan(prof, rules, accounts=None, **kw):
    accounts = accounts or [Account(id="hsa", kind="hsa", owner=0, balance=10_000),
                            Account(id="cash", kind="cash", owner=None, balance=200_000)]
    incomes = kw.pop("incomes", [IncomeStream(kind="pension", amount=60_000, owner=0, cola="none")])
    return Plan(profile=prof, accounts=accounts, incomes=incomes, rules=rules, assumptions=Assumptions(**kw))


def test_hsa_contributions_need_no_wages_and_stop_with_medicare(lib):
    prof = single(birth=1966)  # 60 in 2026, 65 in 2031
    lives_to(prof, 67)
    rule = Rule(kind="hsa_contribution", account="hsa", start=When(year=2026), end=When(event="last_death"),
                mode="max")
    base = {y.year: y for y in run_plan(hsa_plan(prof, []), lib, with_lines=False).years}
    years = {y.year: y for y in run_plan(hsa_plan(prof, [rule]), lib, with_lines=False).years}
    # A pension, no wages: the self-only limit plus the catch-up from 55.
    assert years[2026].contributions == {"hsa": pytest.approx(4_400 + 1_000)}
    assert years[2027].contributions == {"hsa": pytest.approx(4_500 + 1_000)}
    assert years[2026].tax.agi == pytest.approx(base[2026].tax.agi - 5_400)
    assert years[2026].net_cash_flow == pytest.approx(base[2026].net_cash_flow - 5_400 +
                                                      (base[2026].tax.total_tax - years[2026].tax.total_tax))
    assert years[2026].balances_end["hsa"] == pytest.approx((10_000 + 5_400) * 1.05)
    # Projected limits grow with inflation (rounded down to $50); the catch-up stays $1,000.
    law = lib.federal(2028, prof.inflation)
    assert law.hsa_self_only == 4_500 * (1 + prof.inflation) // 50 * 50 and law.hsa_catch_up == 1_000
    assert years[2030].contributions["hsa"] == pytest.approx(lib.federal(2030, prof.inflation).hsa_self_only + 1_000)
    # Medicare from 65: with only a birth year, nothing in the year they turn 65.
    assert years[2031].contributions == {} and years[2032].contributions == {}
    assert any("stop with Medicare" in n for n in years[2031].notes)


def test_hsa_family_limit_is_shared_and_catch_up_goes_to_each_owners_hsa(lib):
    prof = couple(births=(1968, 1972))  # 58 and 54 in 2026; B turns 55 in 2027
    lives_to(prof, 80)
    accounts = [Account(id="a", kind="hsa", owner=0), Account(id="b", kind="hsa", owner=1),
                Account(id="cash", kind="cash", owner=None, balance=100_000)]
    rules = [Rule(kind="hsa_contribution", account=k, start=When(year=2026), end=When(year=2027), mode="max",
                  coverage="family") for k in ("a", "b")]
    years = {y.year: y for y in run_plan(hsa_plan(prof, rules, accounts), lib, with_lines=False).years}
    assert years[2026].contributions == {"a": pytest.approx(8_750 + 1_000)}  # A's rule fills the family limit
    assert years[2027].contributions == {"a": pytest.approx(9_000 + 1_000), "b": pytest.approx(1_000)}
    # A fixed amount on the first rule splits the family limit.
    rules[0].mode, rules[0].amount = "fixed", 5_000
    years = {y.year: y for y in run_plan(hsa_plan(prof, rules, accounts), lib, with_lines=False).years}
    assert years[2026].contributions == {"a": pytest.approx(5_000), "b": pytest.approx(3_750)}
    # Self-only coverage: each has a limit of their own.
    for r in rules:
        r.coverage, r.mode = "self", "max"
    years = {y.year: y for y in run_plan(hsa_plan(prof, rules, accounts), lib, with_lines=False).years}
    assert years[2026].contributions == {"a": pytest.approx(5_400), "b": pytest.approx(4_400)}


def test_hsa_fixed_amount_is_capped_at_the_limit(lib):
    prof = single(birth=1980)
    lives_to(prof, 47)
    rule = Rule(kind="hsa_contribution", account="hsa", start=When(year=2026), amount=6_000)
    y0 = run_plan(hsa_plan(prof, [rule]), lib, with_lines=False).years[0]
    assert y0.contributions == {"hsa": pytest.approx(4_400)}
    assert any("capped at $4,400" in n for n in y0.notes)


def test_hsa_contributions_past_65_without_medicare_until_social_security(lib):
    prof = single(birth=1960)  # 66 in 2026
    prof.people[0].medicare_part_b = False  # not enrolled at 65 (an employer's plan)
    lives_to(prof, 70)
    rule = Rule(kind="hsa_contribution", account="hsa", start=When(year=2026), end=When(event="last_death"),
                mode="max")
    plan = hsa_plan(prof, [rule])
    plan.people[0].social_security = SocialSecurity(fra_benefit=30_000, claim_age=67.25)
    years = {y.year: y for y in run_plan(plan, lib, with_lines=False).years}
    # Social Security from April 2027 backdates Part A 6 months, to October 2026: 9 months.
    assert years[2026].contributions["hsa"] == pytest.approx((4_400 + 1_000) * 9 / 12)
    assert any("prorated to 9 months" in n for n in years[2026].notes)
    assert years[2027].contributions == {}
    assert plan.hsa_last_year(0) == 2026
    # Claiming at 68 instead: all of 2026, half of 2027 (Part A from July 2027).
    plan.people[0].social_security.claim_age = 68
    years = {y.year: y for y in run_plan(plan, lib, with_lines=False).years}
    assert years[2026].contributions["hsa"] == pytest.approx(5_400)
    assert years[2027].contributions["hsa"] == pytest.approx(5_500 / 2)
    # On Medicare (the baseline says so): never.
    prof.people[0].medicare_part_b = True
    assert run_plan(plan, lib, with_lines=False).years[0].contributions == {}


def test_hsa_deduction_is_added_back_in_california(lib):
    prof = single(birth=1970)
    prof.state = "CA"
    lives_to(prof, 57)
    rule = Rule(kind="hsa_contribution", account="hsa", start=When(year=2026), mode="max")
    base = run_plan(hsa_plan(prof, []), lib, with_lines=False).years[0]
    y0 = run_plan(hsa_plan(prof, [rule]), lib, with_lines=False).years[0]
    assert y0.tax.federal_tax < base.tax.federal_tax
    assert y0.tax.state_tax == pytest.approx(base.tax.state_tax)
    prof.state = "OR"
    base = run_plan(hsa_plan(prof, []), lib, with_lines=False).years[0]
    y0 = run_plan(hsa_plan(prof, [rule]), lib, with_lines=False).years[0]
    assert y0.tax.state_tax < base.tax.state_tax


def test_hsa_rules_validate_and_show_in_the_year_table(lib):
    prof = single(birth=1970)
    lives_to(prof, 57)
    plan = hsa_plan(prof, [Rule(kind="hsa_contribution", account="cash", start=When(year=2026), mode="max")])
    assert any("into an HSA owned by one person" in p for p in plan.validate())
    plan.rules[0].account = "hsa"
    plan.rules[0].mode = "spread"
    assert any("can't use the amount mode" in p for p in plan.validate())
    plan.rules[0].mode = "max"
    assert plan.validate() == []
    assert plan.rules[0].describe(prof, {a.id: a for a in plan.accounts}).startswith(
        "HSA contribution to A HSA (self-only coverage)")
    header, rows = year_table(run_plan(plan, lib, with_lines=False))
    assert "A HSA contribution" in header
    assert rows[0][header.index("A HSA contribution")] == "5,400"
    header, _ = year_table(run_plan(hsa_plan(prof, []), lib, with_lines=False))
    assert "A HSA contribution" not in header


def test_short_term_gain_and_loss_streams_net_with_long_term_gains(lib):
    prof = single(birth=1960)
    lives_to(prof, 70)
    plan = Plan(
        profile=prof,
        incomes=[
            IncomeStream(kind="wages", amount=60_000, owner=0, cola="none"),
            IncomeStream(kind="ltcg", amount=10_000, owner=0, cola="none"),
            IncomeStream(kind="stcg", amount=-4_000, owner=0, cola="none", end=When(year=2026)),
            IncomeStream(kind="stcg", amount=2_000, owner=0, cola="none", start=When(year=2027), end=When(year=2027)),
            IncomeStream(kind="ltcg", amount=-20_000, owner=0, cola="none", start=When(year=2028), end=When(year=2028)),
        ],
        assumptions=Assumptions(cash_return=0.0),  # no interest on the surplus
    )
    res = run_plan(plan, lib)
    y26, y27, y28 = res.years[:3]
    assert y26.tax.net_ltcg == pytest.approx(6_000)  # the short-term loss offsets the long-term gain
    assert y26.tax.ordinary_income == pytest.approx(60_000)
    assert y27.tax.net_ltcg == pytest.approx(10_000)
    assert y27.tax.ordinary_income == pytest.approx(62_000)  # a short-term gain is ordinary income
    assert y28.tax.net_ltcg == 0
    assert y28.tax.ordinary_income == pytest.approx(57_000)  # net loss capped at $3,000
    # A loss is not shown as negative income.
    assert all(s.amount > 0 for yr in res.years for s in yr.sources)


def test_charitable_gifts_carry_into_each_plan_year(lib):
    prof = single(birth=1960)
    lives_to(prof, 68)
    prof.household.charitable_cash_gifts = 800
    plan = Plan(profile=prof, incomes=[IncomeStream(kind="wages", amount=60_000, owner=0, cola="none")])
    res = run_plan(plan, lib)
    assert all(yr.tax.charitable_deduction > 0 for yr in res.years)
    assert all(yr.tax.charitable_deduction <= 1_000 for yr in res.years)
