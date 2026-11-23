"""State deductions, exemptions and credits that shrink with income."""

import math

import pytest

from rtpl.engine.marginal import build_marginal_series
from rtpl.engine.tax import compute_tax
from rtpl.model.profile import Person, Profile
from rtpl.taxlaw.loader import Phaseout, _parse_phaseout

from conftest import make

S = {"single": 0.0, "mfj": 0.0, "mfs": 0.0, "hoh": 0.0}


def at(value):
    return {k: float(value) for k in S}


def test_phaseout_rate_shape():
    p = Phaseout(start=at(1_000), rate=0.25)
    assert p.apply(100, 900, "single") == 100
    assert p.apply(100, 1_200, "single") == pytest.approx(50)
    assert p.apply(100, 5_000, "single") == 0


def test_phaseout_width_step_and_cliff_shapes():
    line = Phaseout(start=at(1_000), width=at(1_000), keep=0.25)
    assert line.apply(100, 1_500, "mfj") == pytest.approx(50)
    assert line.apply(100, 9_000, "mfj") == pytest.approx(25)
    steps = Phaseout(start=at(1_000), step=400, step_fraction=0.1)
    assert steps.apply(100, 1_000, "single") == 100
    assert steps.apply(100, 1_001, "single") == pytest.approx(90)  # part of a step counts
    assert steps.apply(100, 1_800, "single") == pytest.approx(80)
    cliff = Phaseout(start=at(1_000), width=at(0))
    assert cliff.apply(100, 1_000, "single") == 100
    assert cliff.apply(100, 1_000.01, "single") == 0


def test_phaseout_tiers_by_status():
    p = _parse_phaseout({"tiers": [{"up_to": {"single": 100, "mfj": 100}, "keep": 1},
                                   {"up_to": {"single": 200, "mfj": 200}, "keep": {"single": 0.5, "mfj": 0.75}}]})
    assert p.apply(10, 100, "single") == 10
    assert p.apply(10, 150, "single") == 5
    assert p.apply(10, 150, "mfj") == 7.5
    assert p.apply(10, 250, "mfj") == 0


def test_phaseout_income_must_be_known():
    with pytest.raises(ValueError):
        _parse_phaseout({"start": {"single": 1}, "rate": 1, "income": "wages"})
    with pytest.raises(ValueError):  # exclusions can't depend on state income
        _parse_phaseout({"start": {"single": 1}, "rate": 1, "income": "state_income"}, federal_only=True)


def utah_couple(state="UT"):
    """Retired couple over 65 with $55K of Social Security (the slide's example)."""
    return Profile(
        year=2025,
        filing_status="mfj",
        state=state,
        people=[
            Person(name="A", birth_year=1959, social_security=36_667),
            Person(name="B", birth_year=1960, social_security=18_333),
        ],
    )


def test_utah_credits_shape_the_marginal_rate(lib):
    s = build_marginal_series(utah_couple(), lib, x_max=250_000, step=100)
    ut = 0.0445
    taxed_ss = 1.85  # each IRA dollar also makes 85 cents of benefits taxable

    def state(x):
        return s.layers["state"][int(x // s.step)]

    # The taxpayer and Social Security credits cover the tax at first.
    assert state(35_000) == pytest.approx(0)
    # The Social Security credit gives back the tax on the benefits, so only
    # the taxpayer credit's 1.3% phase-out adds to 4.45%.
    assert state(47_000) == pytest.approx(ut + 0.013 * taxed_ss, abs=1e-4)
    # Past $90K of income the Social Security credit phases out too.
    assert state(58_000) == pytest.approx(ut + (0.013 + 0.025) * taxed_ss, abs=1e-4)
    # Benefits fully taxable: both phase-outs at their plain rates.
    assert state(80_000) == pytest.approx(ut + 0.013 + 0.025, abs=1e-4)
    # Social Security credit used up, taxpayer credit still phasing out.
    assert state(140_000) == pytest.approx(ut + 0.013, abs=1e-4)
    # Both gone: the flat rate.
    assert state(200_000) == pytest.approx(ut, abs=1e-4)


def test_utah_retirement_credit_or_social_security_credit_not_both(lib):
    law, ut = lib.federal(2026), lib.state("UT")
    old = make(state="UT", people=[Person(birth_year=1950, social_security=20_000, pension=30_000)])
    r = compute_tax(old, law, ut)
    # Credits: the taxpayer credit plus the larger of the Social Security
    # and retirement credits (Utah allows only one of the two).
    taxable_ss = r.taxable_social_security
    magi = r.agi
    ss_credit = max(0.0, 0.0445 * taxable_ss - 0.025 * max(0.0, magi - 54_000))
    ret_credit = max(0.0, 450 - 0.025 * max(0.0, magi - 25_000))
    std = law.standard_deduction["single"] + law.additional_std_unmarried
    taxpayer = max(0.0, 0.06 * std - 0.013 * max(0.0, r.agi - 18_696))
    expected = max(0.0, 0.0445 * r.agi - taxpayer - max(ss_credit, ret_credit))
    assert r.state_tax == pytest.approx(expected)


def test_virginia_age_deduction_phases_out_for_the_couple(lib):
    law, va = lib.federal(2026), lib.state("VA")

    def tax(ira):
        p = make(state="VA", fs="mfj", people=[Person(birth_year=1955, ira_distributions=ira),
                                               Person(birth_year=1955)])
        return compute_tax(p, law, va).state_tax

    # Below $75K the couple keeps $24,000 of age deductions.
    assert tax(70_000) - tax(69_000) == pytest.approx(0.0575 * 1_000)
    # Over $75K they lose $1 of deduction per $1, together.
    assert tax(86_000) - tax(85_000) == pytest.approx(0.0575 * 2_000)
    # Fully gone after $24,000 more.
    assert tax(110_000) - tax(109_000) == pytest.approx(0.0575 * 1_000)


def test_connecticut_exemption_phases_out(lib):
    law, ct = lib.federal(2026), lib.state("CT")

    def tax(wages):
        return compute_tax(make(state="CT", people=[Person(birth_year=1980, wages=wages)]), law, ct).state_tax

    assert tax(20_000) == pytest.approx(ct.brackets["single"].tax(5_000))
    assert tax(40_000) == pytest.approx(ct.brackets["single"].tax(40_000 - 5_000))
    assert tax(50_000) == pytest.approx(ct.brackets["single"].tax(50_000))


def test_wisconsin_subtracts_retirement_income_at_67(lib):
    law, wi = lib.federal(2026), lib.state("WI")

    def tax(birth_year):
        p = make(state="WI", people=[Person(birth_year=birth_year, ira_distributions=40_000)])
        return compute_tax(p, law, wi).state_tax

    young, old = tax(1965), tax(1955)
    assert old < young
    assert not math.isclose(old, young)


def test_new_jersey_style_per_return_cap_is_shared(lib):
    import dataclasses

    law = lib.federal(2026)
    nj = lib.state("NJ")
    from rtpl.taxlaw.loader import RetirementExclusion

    capped = dataclasses.replace(
        nj, retirement_exclusions=(RetirementExclusion(amount=math.inf, min_age=62, per_return=at(50_000)),)
    )
    p = make(state="NJ", fs="mfj", people=[Person(birth_year=1955, pension=40_000),
                                           Person(birth_year=1955, pension=40_000)])
    none = dataclasses.replace(nj, retirement_exclusions=())
    diff = compute_tax(p, law, none).state_tax - compute_tax(p, law, capped).state_tax
    assert diff == pytest.approx(none.brackets["mfj"].tax(80_000 - 2_000) - none.brackets["mfj"].tax(30_000 - 2_000))


def test_state_cliff_is_a_marker_not_a_spike(lib):
    # New Mexico stops exempting Social Security above $150K of AGI.
    s = build_marginal_series(utah_couple("NM"), lib, x_max=250_000, step=100)
    cliffs = [a for a in s.annotations if a.kind == "state_cliff"]
    assert len(cliffs) == 1
    c = cliffs[0]
    assert c.magi == pytest.approx(150_000, abs=1)
    before = compute_tax(s.profile, s.law, s.state, extra=c.x - 0.02).state_tax
    after = compute_tax(s.profile, s.law, s.state, extra=c.x).state_tax
    assert c.annual_cost == pytest.approx(after - before, abs=0.5)
    # No step shows the jump as a rate.
    assert max(s.layers["state"]) < 0.10
    assert s.layers["state"][int(c.x // s.step)] == pytest.approx(s.layers["state"][int(c.x // s.step) + 1], abs=0.01)


def test_new_jersey_pension_exclusion_cliffs(lib):
    s = build_marginal_series(utah_couple("NJ"), lib, x_max=250_000, step=100)
    cliffs = [a for a in s.annotations if a.kind == "state_cliff"]
    # Income tiers at $100K, $125K and $150K (AGI less taxable Social Security).
    assert [round(a.magi - s.results[-1].taxable_social_security, -2) for a in cliffs] == [100_000, 125_000, 150_000]
    assert max(s.layers["state"]) < 0.15


def test_smooth_states_have_no_cliffs(lib):
    for code in ("UT", "VA", "MN", "VT"):
        s = build_marginal_series(utah_couple(code), lib, x_max=250_000, step=100)
        assert not [a for a in s.annotations if a.kind == "state_cliff"], code
