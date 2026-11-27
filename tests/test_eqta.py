"""The Estimated Quarterly Tax Assistant: file model and worksheet engine."""

from datetime import date

import pytest

from rtpl.engine.estimated import PLAN, build_profile, compute_worksheet, detail_rows, worksheet_groups
from rtpl.engine.tax import compute_tax
from rtpl.model.eqta import (
    EqtaFile,
    Entry,
    IncomeSource,
    NewYearDefaults,
    evaluate_amount,
    parse_date,
)
from rtpl.model.profile import Person


def year_with(eq=None, year=2026, state=None, fs="single", people=None):
    eq = eq or EqtaFile()
    ty = eq.new_year(year)
    ty.profile.filing_status = fs
    ty.profile.state = state
    ty.profile.people = people or [Person(name="Pat", birth_year=year - 66)]
    return eq, ty


def src(ty, tax_type, entries=(), plan="", person=None, share=None, name=None):
    s = IncomeSource(name=name or tax_type, tax_type=tax_type, person=person, plan_text=plan,
                     qualified_share=share, entries=[Entry(d, str(a)) for d, a in entries])
    ty.sources.append(s)
    return s


def pay(ty, key, entries):
    ty.payments[key].entries = [Entry(d, str(a)) for d, a in entries]


@pytest.mark.parametrize("text, value", [
    ("3662.90*12", 43954.80), ("1,250.50", 1250.50), ("$2,000", 2000.0), ("-500", -500.0),
    ("(100+50)/2", 75.0), ("", 0.0), ("1,234,567", 1234567.0),
])
def test_amount_arithmetic(text, value):
    assert evaluate_amount(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["abc", "2**10", "A1+3", "1/0", "__import__('os')", "1,2"])
def test_amount_arithmetic_rejects_anything_else(text):
    with pytest.raises(ValueError):
        evaluate_amount(text)


def test_dates():
    assert parse_date("3/15", 2026) == date(2026, 3, 15)
    assert parse_date("01/15/27", 2026) == date(2027, 1, 15)
    assert parse_date("2026-12-31", 2025) == date(2026, 12, 31)
    with pytest.raises(ValueError):
        parse_date("2/30", 2026)


def test_income_counts_through_each_period_end(lib):
    eq, ty = year_with()
    s = src(ty, "taxable_interest", [
        (date(2026, 3, 31), 100), (date(2026, 4, 1), 10), (date(2026, 5, 31), 1000),
        (date(2026, 6, 1), 20), (date(2026, 8, 31), 3000), (date(2026, 12, 31), 7),
        (date(2025, 12, 31), 99999), (date(2027, 1, 1), 99999),  # other years: in no column
    ], plan="5000")
    ws = compute_worksheet(ty, lib, eq)
    assert [a[0] for a in ws.amounts] == [100, 1110, 4130, 4137, 5000]
    assert ws.period_ends == (date(2026, 3, 31), date(2026, 5, 31), date(2026, 8, 31), date(2026, 12, 31))
    assert ws.amounts[PLAN] == [s.plan]


def test_payments_count_through_each_due_date(lib):
    eq, ty = year_with()
    src(ty, "ira", [(date(2026, 1, 10), 80_000)])
    pay(ty, "federal_estimated", [
        (date(2026, 4, 15), 1000), (date(2026, 4, 16), 1), (date(2026, 6, 15), 1000),
        (date(2026, 9, 15), 1000), (date(2027, 1, 15), 1000), (date(2027, 1, 16), 5),
    ])
    ws = compute_worksheet(ty, lib, eq)
    assert ws.due_dates == (date(2026, 4, 15), date(2026, 6, 15), date(2026, 9, 15), date(2027, 1, 15))
    assert ws.paid["federal_estimated"][:4] == [1000, 2001, 3001, 4001]
    tax = ws.federal[0].federal_tax
    assert ws.federal_owed[0] == pytest.approx(tax - 1000)


def test_federal_tax_matches_the_engine(lib):
    eq, ty = year_with(fs="mfj", people=[Person(name="A", birth_year=1958), Person(name="B", birth_year=1962)])
    src(ty, "social_security", [(date(2026, m, 3), 2500) for m in range(1, 13)], person=0)
    src(ty, "social_security", [(date(2026, m, 3), 1500) for m in range(1, 13)], person=1)
    src(ty, "ira", [(date(2026, 2, 1), 30_000)], person=1)
    src(ty, "dividends_to_split", [(date(2026, 3, 20), 4000), (date(2026, 6, 20), 4000)], share=0.75)
    src(ty, "ltcg", [(date(2026, 5, 2), -2000)])
    src(ty, "stcg", [(date(2026, 7, 2), 1500)])
    ws = compute_worksheet(ty, lib, eq)
    p = build_profile(ty, ws.amounts[3])
    assert p.people[0].social_security == 30_000 and p.people[1].social_security == 18_000
    assert p.people[1].ira_distributions == 30_000
    assert p.household.qualified_dividends == 6000 and p.household.nonqualified_dividends == 2000
    assert p.household.long_term_capital_gains == -2000 and p.household.short_term_capital_gains == 1500
    expected = compute_tax(p, lib.federal(2026))
    assert ws.federal[3].federal_tax == pytest.approx(expected.federal_tax)
    labels = [g for g, _ in worksheet_groups(ty)]
    assert labels == ["Social Security", "IRA distribution", "Qualified dividends", "Non-qualified dividends",
                      "Long-term capital gains", "Short-term capital gains"]
    qd = dict(worksheet_groups(ty))["Qualified dividends"]
    assert qd == [(3, 0.75)]
    assert any(label == "Taxable Social Security" for label, _ in detail_rows(ws))


def test_virginia_first_period_runs_to_april_30(lib):
    eq, ty = year_with(state="VA")
    src(ty, "wages", [(date(2026, 4, 15), 50_000)])
    pay(ty, "state_estimated", [(date(2026, 5, 1), 500)])
    ws = compute_worksheet(ty, lib, eq)
    assert ws.amounts[0] == [0]  # federal: after March 31
    ss = ws.state_schedule
    assert ss.period_ends[0] == date(2026, 4, 30) and ss.period_differs == (True, False, False, False)
    assert ss.due_dates[0] == date(2026, 5, 1) and ss.differs[0]
    assert ws.state_amounts[0] == [50_000]
    assert ws.state_tax[0] > 0
    assert ws.paid["state_estimated"][0] == 500  # paid on the state's due date
    assert ws.state_annualized_factors[0] == 3  # 12 / 4 months


def test_california_schedule_and_annualized_percentages(lib):
    eq, ty = year_with(state="CA")
    src(ty, "pension", [(date(2026, m, 1), 5000) for m in range(1, 13)])
    ws = compute_worksheet(ty, lib, eq)
    ss = ws.state_schedule
    assert ss.due_dates[2] is None
    assert ss.annualized_percentages == (0.27, 0.63, 0.63, 0.90)
    # With income spread evenly, the annualized tax is about the full year's
    # each period, so the amount due tracks the 30/40/0/30 schedule.
    year_tax = ws.state_tax[3]
    assert ws.state_annualized_required[0] == pytest.approx(0.27 * year_tax, rel=0.02)
    assert ws.state_annualized_required[2] == ws.state_annualized_required[1]
    assert ws.paid_through["state_estimated"][2] == ws.due_dates[2]  # no CA date: the federal one


def test_washington_shows_state_lines_for_gains_only(lib):
    eq, ty = year_with(state="WA")
    src(ty, "wages", [(date(2026, 2, 1), 100_000)])
    assert compute_worksheet(ty, lib, eq).state is not None  # WA taxes gains in 2026
    ws = compute_worksheet(ty, lib, eq)
    assert ws.state_tax[3] == 0
    src(ty, "ltcg", [(date(2026, 6, 1), 600_000)])
    ws = compute_worksheet(ty, lib, eq)
    ss = ws.state_schedule
    assert ss.annual_only and ss.due_dates[:3] == (None, None, None)
    assert ss.due_dates[3] == date(2027, 4, 15)
    assert ws.state_tax[3] > 0 and ws.state_tax[0] == 0


def test_no_state_lines_without_a_state_tax(lib):
    eq, ty = year_with(state="FL")
    src(ty, "wages", [(date(2026, 2, 1), 100_000)])
    ws = compute_worksheet(ty, lib, eq)
    assert ws.state is None and "state_estimated" not in ws.paid


def test_annualized_method_follows_schedule_ai(lib):
    eq, ty = year_with()
    # All income in December: nothing is due by the first three dates under
    # the annualized method, while the regular installments are 25% each.
    src(ty, "ira", [(date(2026, 12, 20), 100_000)])
    ws = compute_worksheet(ty, lib, eq)
    tax = ws.federal[3].federal_tax
    assert ws.current_tax == pytest.approx(tax) and ws.current_tax_basis == "YTD December 31"
    assert ws.required_annual_payment == pytest.approx(0.9 * tax, abs=0.01)
    assert ws.annualized_required[:3] == [0, 0, 0]
    assert ws.annualized_required[3] == pytest.approx(0.9 * tax, abs=0.02)
    assert ws.regular_required == pytest.approx([0.225 * tax, 0.45 * tax, 0.675 * tax, 0.9 * tax], abs=0.02)
    assert ws.annualized_factors == (4, 2.4, 1.5, 1)


def test_annualized_installment_never_more_than_regular_plus_carry(lib):
    eq, ty = year_with()
    # All income in January: the annualized amount (x4) is far above the
    # regular installment, so the regular one (plus nothing carried) wins.
    src(ty, "ira", [(date(2026, 1, 5), 100_000)])
    ws = compute_worksheet(ty, lib, eq)
    assert ws.annualized_required == pytest.approx(ws.regular_required, abs=0.05)


def test_withholding_is_spread_over_the_due_dates(lib):
    eq, ty = year_with()
    src(ty, "ira", [(date(2026, 1, 5), 100_000)])
    pay(ty, "federal_withholding", [(date(2026, 12, 15), 8000)])
    pay(ty, "federal_estimated", [(date(2026, 4, 1), 1000)])
    ws = compute_worksheet(ty, lib, eq)
    assert ws.paid["federal_withholding"][0] == 0  # shown as entered
    assert ws.credited == [3000, 5000, 7000, 9000]


def test_safe_harbor_uses_last_years_tab_and_the_110_percent_rule(lib):
    eq = EqtaFile()
    _, last = year_with(eq, year=2025)
    src(last, "wages", [(date(2025, 6, 1), 160_000)])
    _, ty = year_with(eq, year=2026)
    src(ty, "wages", [(date(2026, 6, 1), 400_000)])
    ws = compute_worksheet(ty, lib, eq)
    last_tax = compute_worksheet(last, lib, eq).federal[3].federal_tax
    assert ws.prior_tax == pytest.approx(last_tax) and "2025" in ws.prior_basis
    assert ws.prior_agi > 150_000 and ws.prior_share == 1.10
    assert ws.required_annual_payment == pytest.approx(1.10 * last_tax, abs=0.01)
    # Typed figures win over the tab.
    ty.prior_year_tax, ty.prior_year_agi = 10_000.0, 100_000.0
    ws = compute_worksheet(ty, lib, eq)
    assert ws.prior_share == 1.00 and ws.required_annual_payment == 10_000


def test_safe_harbor_amounts_come_from_the_law_file(lib):
    sh = lib.federal(2026).estimated_payments.safe_harbor
    assert (sh.current_year_share, sh.prior_year_share, sh.high_income_prior_year_share) == (0.9, 1.0, 1.1)
    assert sh.high_income_agi["mfs"] == 75_000 and sh.penalty_floor == 1000
    assert lib.federal(2030).estimated_payments.safe_harbor == sh  # projected years keep it


def test_plan_column_and_safe_harbor_basis(lib):
    eq, ty = year_with()
    src(ty, "pension", [(date(2026, 1, 31), 3000)], plan="3000*12")
    pay(ty, "federal_withholding", [], )
    ty.payments["federal_withholding"].plan_text = "2400"
    ws = compute_worksheet(ty, lib, eq)
    assert ws.amounts[PLAN] == [36_000]
    assert ws.current_tax_basis == "the Plan column"
    assert ws.federal_owed[PLAN] == pytest.approx(ws.federal[PLAN].federal_tax - 2400)


def test_clone_year_zeroes_amounts_and_sets_the_qualified_share():
    eq, ty = year_with()
    s = src(ty, "dividends_to_split", [(date(2026, 3, 1), 100)], plan="400", share=0.6, name="Fund")
    s.notes = "Vanguard"
    ty.notes = "year notes"
    eq.defaults = NewYearDefaults(qualified_share=0.8, clone_mode="set", clone_share=0.7)
    clone = eq.clone_year(ty, 2027, "Projection")
    assert clone.title == "2027 · Projection" and clone.profile.year == 2027
    c = clone.sources[0]
    assert (c.name, c.notes, c.entries, c.plan_text, c.qualified_share) == ("Fund", "Vanguard", [], "", 0.7)
    eq.defaults.clone_mode = "retain"
    assert eq.clone_year(ty, 2028).sources[0].qualified_share == 0.6
    assert eq.new_source("dividends_to_split").qualified_share == 0.8
    assert eq.new_source("wages").qualified_share is None
    assert [y.year for y in eq.years] == [2028, 2027, 2026]  # newest first


def test_file_round_trip(tmp_path):
    eq, ty = year_with(state="OR", fs="mfj", people=[Person(name="A"), Person(name="B")])
    ty.description = "Actuals"
    ty.dependents = ["Kid"]
    ty.prior_year_tax = 1234.0
    src(ty, "ira", [(date(2026, 3, 1), "3662.90*12")], person=1, plan="50000")
    pay(ty, "state_estimated", [(date(2026, 4, 15), 300)])
    path = tmp_path / "taxes.eqta"
    eq.save(path)
    back = EqtaFile.load(path)
    assert back.to_dict() == eq.to_dict()
    assert back.years[0].sources[0].entries[0].text == "3662.90*12"
    assert back.years[0].sources[0].entries[0].amount == pytest.approx(43954.80)
    with pytest.raises(ValueError):
        EqtaFile.from_dict({"year": 2026})  # an RTPL profile, not an EQTA file
