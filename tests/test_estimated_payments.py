"""Estimated-payment schedules in the federal and state law files."""

from datetime import date

import pytest

from rtpl.taxlaw.estimated import next_business_day, parse_estimated_payments, FEDERAL_HOLIDAYS


def fed(lib, year):
    return lib.federal(year).estimated_payments


def test_federal_due_dates_move_past_weekends_and_dc_holidays(lib):
    assert fed(lib, 2026).due_dates(2026) == (
        date(2026, 4, 15), date(2026, 6, 15), date(2026, 9, 15), date(2027, 1, 15))
    # Emancipation Day: Friday April 15, 2022 and Monday April 17, 2023.
    assert fed(lib, 2022).due_dates(2022)[0] == date(2022, 4, 18)
    assert fed(lib, 2023).due_dates(2023)[0] == date(2023, 4, 18)
    # Martin Luther King Jr. Day on January 15, 2024, and after a Saturday in 2022.
    assert fed(lib, 2023).due_dates(2023)[3] == date(2024, 1, 16)
    assert fed(lib, 2021).due_dates(2021)[3] == date(2022, 1, 18)
    # June 15, 2025 was a Sunday.
    assert fed(lib, 2025).due_dates(2025)[1] == date(2025, 6, 16)
    # IRS Notice 2020-23.
    assert fed(lib, 2020).due_dates(2020)[:2] == (date(2020, 7, 15), date(2020, 7, 15))


def test_projected_federal_years_drop_the_base_years_postponements(lib):
    years = lib.enacted_years
    later = years[-1] + 1
    sched = fed(lib, later)
    assert lib.federal(later).status == "projected"
    assert all(i.due_date is None for i in sched.installments)
    assert sched.due_dates(later)[0] == next_business_day(date(later, 4, 15), FEDERAL_HOLIDAYS)


def test_every_state_that_taxes_income_has_a_schedule(lib):
    for code in lib.available_states():
        for year in [*lib.state_years(code), 2027]:
            st = lib.state(code, year)
            sched = st.estimated_payments
            if not st.has_any_tax:
                assert sched is None, (code, year)
                continue
            assert sched is not None, (code, year)
            if sched.installments:
                assert sum(sched.shares) == pytest.approx(1.0)
                dates = [d for d in sched.due_dates(year) if d is not None]
                ends = sched.period_ends(year)
                assert all(d > e for d, e in zip(sched.due_dates(year), ends) if d is not None)
                assert all(d.weekday() < 5 for d in dates)
                # A postponed first installment can fall after the second (NY 2020).
                if not any(i.due_date for i in sched.installments):
                    assert dates == sorted(dates), (code, year)


def test_california_30_40_0_30(lib):
    ca = lib.state("CA", 2026).estimated_payments
    assert ca.shares == (0.30, 0.40, 0.0, 0.30)
    assert ca.due_dates(2026) == (date(2026, 4, 15), date(2026, 6, 15), None, date(2027, 1, 15))
    assert ca.annualized_percentages() == (0.27, 0.63, 0.63, 0.90)
    assert fed(lib, 2026).annualized_percentages() == (0.225, 0.45, 0.675, 0.90)
    assert ca.differs_from(fed(lib, 2026), 2026) == (False, False, False, False)
    assert lib.state("CA", 2020).estimated_payments.due_dates(2020)[:2] == (date(2020, 7, 15),) * 2


@pytest.mark.parametrize("code, year, dates", [
    ("IA", 2026, (date(2026, 4, 30), date(2026, 6, 30), date(2026, 9, 30), date(2027, 2, 1))),
    ("HI", 2025, (date(2025, 4, 21), date(2025, 6, 20), date(2025, 9, 22), date(2026, 1, 20))),
    # Juneteenth (observed Monday June 20, 2022) isn't a Hawaii state holiday.
    ("HI", 2022, (date(2022, 4, 20), date(2022, 6, 20), date(2022, 9, 20), date(2023, 1, 20))),
    ("DE", 2026, (date(2026, 4, 30), date(2026, 6, 15), date(2026, 9, 15), date(2027, 1, 15))),
    ("VA", 2026, (date(2026, 5, 1), date(2026, 6, 15), date(2026, 9, 15), date(2027, 1, 15))),
    # Patriots' Day was April 15, 2024, then DC Emancipation Day.
    ("MA", 2024, (date(2024, 4, 17), date(2024, 6, 17), date(2024, 9, 16), date(2025, 1, 15))),
    ("ME", 2024, (date(2024, 4, 17), date(2024, 6, 17), date(2024, 9, 16), date(2025, 1, 15))),
    ("KY", 2023, (date(2023, 4, 18), date(2023, 6, 15), date(2023, 9, 15), date(2024, 1, 16))),
])
def test_state_due_dates(lib, code, year, dates):
    assert lib.state(code, year).estimated_payments.due_dates(year) == dates


def test_dates_that_differ_from_federal(lib):
    f = fed(lib, 2026)
    assert lib.state("DE", 2026).estimated_payments.differs_from(f, 2026) == (True, False, False, False)
    assert lib.state("IA", 2026).estimated_payments.differs_from(f, 2026) == (True,) * 4
    assert lib.state("NY", 2026).estimated_payments.differs_from(f, 2026) == (False,) * 4
    assert lib.state("MA", 2024).estimated_payments.differs_from(fed(lib, 2024), 2024)[0]


def test_washington_pays_once_with_the_return(lib):
    wa = lib.state("WA", 2026).estimated_payments
    assert wa.annual_only and not wa.required
    assert wa.due_dates(2026) == (date(2027, 4, 15),)
    assert wa.period_ends(2026) == (date(2026, 12, 31),)
    assert lib.state("WA", 2025).estimated_payments.due_dates(2025) == (date(2026, 5, 1),)
    # A later year on the 2025 file's dates would not keep the 2025 postponement.
    assert lib.state("WA", 2027).estimated_payments.due_dates(2027) == (date(2028, 4, 18),)
    assert lib.state("WA", 2021).estimated_payments is None


def test_states_without_required_estimates(lib):
    for code in ("ID", "UT"):
        sched = lib.state(code, 2026).estimated_payments
        assert not sched.required and sched.installments == () and sched.notes
    tn = lib.state("TN", 2020).estimated_payments
    assert tn.annual_only and tn.due_dates(2020) == (date(2021, 5, 17),)
    nh = lib.state("NH", 2024).estimated_payments
    assert nh.required and nh.shares == (0.25,) * 4
    assert lib.state("NH", 2025).estimated_payments is None
    assert lib.state("FL", 2026).estimated_payments is None


def test_virginia_first_period_runs_to_april_30(lib):
    va = lib.state("VA", 2026).estimated_payments
    assert va.period_ends(2026) == (date(2026, 4, 30), date(2026, 5, 31), date(2026, 8, 31), date(2026, 12, 31))
    assert va.differs_from(fed(lib, 2026), 2026) == (True, False, False, False)
    assert va.annualized_percentages() == (0.225, 0.45, 0.675, 0.90)
    # Every other state counts income through the usual period ends.
    usual = fed(lib, 2026).period_ends(2026)
    for code in lib.available_states():
        sched = lib.state(code, 2026).estimated_payments
        if code != "VA" and sched is not None and len(sched.installments) == 4:
            assert sched.period_ends(2026) == usual, code


@pytest.mark.parametrize("raw, message", [
    ({"installments": [{"due": "04-15", "share": 0.5}] * 4}, "add up to 1"),
    ({"installments": [{"due": "04-15", "share": 0.5}] * 2}, "four installments"),
    ({"installments": [{"due": "4/15", "share": 1.0}]}, "MM-DD"),
    ({"installments": [{"share": 1.0}]}, "needs a due date"),
    ({"holidays": ["arbor"], "installments": []}, "Unknown holiday"),
    ({"annual_only": True, "installments": []}, "exactly one"),
    ({"installments": [{"due": "07-15", "period_end": "06-30", "share": 0.25}] * 4}, "in order"),
    ({"installments": [{"due": "04-15", "period_end": "4/30", "share": 1.0}]}, "period_end must be"),
])
def test_bad_schedules_are_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_estimated_payments(raw, 2026)
