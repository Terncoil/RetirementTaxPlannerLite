from itertools import pairwise

import pytest

from rtpl.engine.marginal import LAYERS, build_marginal_series, default_visibility
from rtpl.engine.tax import IncomeSource
from rtpl.model.profile import Person, Profile

from conftest import make


def rate_at(series, x, key=None):
    i = int(x // series.step)
    if key:
        return series.layers[key][i]
    return series.total[i]


@pytest.mark.parametrize("name", ["retired_couple_ca", "early_retiree_aca", "ssdi_under_65"])
@pytest.mark.parametrize("source", list(IncomeSource))
def test_layers_add_up_to_total_tax_change(lib, name, source):
    p = Profile.load(f"examples/profiles/{name}.json")
    s = build_marginal_series(p, lib, source=source, x_max=250_000, step=250)
    summed = sum(sum(s.layers[k]) for k in LAYERS) * s.step
    assert summed == pytest.approx(s.results[-1].total_tax - s.baseline.total_tax, abs=1e-6)


def test_plain_bracket_rate(lib):
    p = make(people=[Person(birth_year=1986, wages=60_000)])
    s = build_marginal_series(p, lib, x_max=20_000)
    assert rate_at(s, 1_000, "ordinary") == pytest.approx(0.12)
    assert rate_at(s, 15_000, "ordinary") == pytest.approx(0.22)
    assert rate_at(s, 1_000, "ss") == 0


def test_social_security_phase_in_layer(lib):
    # SS 30,000 and IRA 30,000: provisional income 45,000, already in the 85% zone.
    p = make(people=[Person(birth_year=1986, social_security=30_000, ira_distributions=30_000)])
    s = build_marginal_series(p, lib, x_max=5_000)
    assert rate_at(s, 1_000, "ordinary") == pytest.approx(0.12)
    assert rate_at(s, 1_000, "ss") == pytest.approx(0.12 * 0.85)


def test_senior_deduction_phase_out_layer(lib):
    # Age 70, IRA 120,000: MAGI past 75k, taxable income in the 22% bracket.
    p = make(people=[Person(birth_year=1956, ira_distributions=120_000)])
    s = build_marginal_series(p, lib, x_max=5_000)
    assert rate_at(s, 1_000, "senior") == pytest.approx(0.22 * 0.06)
    assert rate_at(s, 1_000, "ordinary") == pytest.approx(0.22)


def test_ordinary_income_pushes_gains_out_of_zero_band(lib):
    p = make(
        people=[Person(birth_year=1986, wages=50_000)],
        long_term_capital_gains=20_000,
        has_taxable_brokerage=True,
    )
    s = build_marginal_series(p, lib, x_max=10_000)
    # TI 53,900 incl. 20k gains; ordinary part 33,900, so the gains straddle
    # the 49,450 top of the 0% band. Each extra ordinary dollar moves a dollar
    # of gains from 0% to 15% until the ordinary part reaches 49,450.
    assert rate_at(s, 1_000, "cg") == pytest.approx(0.15)
    assert rate_at(s, 1_000) == pytest.approx(0.12 + 0.15)


def test_ltcg_source(lib):
    p = make(people=[Person(birth_year=1986, wages=100_000)])
    s = build_marginal_series(p, lib, source=IncomeSource.LTCG, x_max=5_000)
    assert rate_at(s, 1_000, "cg") == pytest.approx(0.15)
    assert rate_at(s, 1_000, "ordinary") == 0


def test_irmaa_uses_two_year_lookback(lib):
    p = make(ages=(66,), people=[Person(birth_year=1960, ira_distributions=50_000)])
    p.inflation = 0.0
    s = build_marginal_series(p, lib, x_max=250_000)
    irmaa = [a for a in s.annotations if a.kind == "irmaa"]
    assert irmaa[0].magi == 112_000  # 2028 thresholds = 2027 values at 0% inflation
    assert irmaa[0].x == pytest.approx(62_000, abs=1)
    assert "2028" in irmaa[0].detail
    expected = (293.10 - 209.40 + 15.40) * 12
    assert irmaa[0].annual_cost == pytest.approx(expected)


def test_aca_cliff_annotations(lib):
    p = make(people=[Person(birth_year=1966, ira_distributions=30_000)], aca_marketplace=True)
    s = build_marginal_series(p, lib, x_max=100_000)
    cliff = [a for a in s.annotations if a.label.startswith("ACA cliff")]
    assert cliff and cliff[0].magi == pytest.approx(4 * 15_650)
    assert cliff[0].x == pytest.approx(62_600 - 30_000, abs=1)
    p2025 = make(year=2025, people=[Person(birth_year=1965, ira_distributions=30_000)], aca_marketplace=True)
    s2025 = build_marginal_series(p2025, lib, x_max=100_000)
    assert not [a for a in s2025.annotations if a.label.startswith("ACA cliff")]


class TestDefaultVisibility:
    def vis(self, lib, profile, source=IncomeSource.ORDINARY):
        return default_visibility(build_marginal_series(profile, lib, source=source, x_max=50_000, step=500))

    def test_young_household_hides_medicare_and_gains(self, lib):
        v = self.vis(lib, make(people=[Person(birth_year=1966, ira_distributions=40_000)]))
        assert v["ordinary"] and not v["irmaa"] and not v["cg"] and not v["state"] and not v["aca"]

    def test_irmaa_from_age_63(self, lib):
        assert not self.vis(lib, make(people=[Person(birth_year=2026 - 62, ira_distributions=1)]))["irmaa"]
        assert self.vis(lib, make(people=[Person(birth_year=2026 - 63, ira_distributions=1)]))["irmaa"]

    def test_medicare_before_65_checkbox(self, lib):
        p = make(people=[Person(birth_year=1980, social_security=20_000, medicare_part_b=True)])
        assert self.vis(lib, p)["irmaa"]

    def test_gains_layer_follows_brokerage_or_source(self, lib):
        base = make(people=[Person(birth_year=1966, ira_distributions=40_000)])
        assert self.vis(lib, base, IncomeSource.QUALIFIED_DIVIDENDS)["cg"]
        broker = make(people=[Person(birth_year=1966, ira_distributions=40_000)], has_taxable_brokerage=True)
        assert self.vis(lib, broker)["cg"]

    def test_state_layer_when_state_given(self, lib):
        assert self.vis(lib, make(state="CA", people=[Person(birth_year=1966, wages=50_000)]))["state"]
        assert not self.vis(lib, make(state="TX", people=[Person(birth_year=1966, wages=50_000)]))["state"]

    def test_aca_only_for_marketplace_households_under_65(self, lib):
        p = make(people=[Person(birth_year=1966, ira_distributions=30_000)], aca_marketplace=True)
        assert self.vis(lib, p)["aca"]
        old = make(people=[Person(birth_year=1955, ira_distributions=30_000)], aca_marketplace=True)
        assert not self.vis(lib, old)["aca"]


def test_senior_phase_out_pushing_gains_is_charged_to_senior_layer(lib):
    """test case MFA KDbFfVK1BsY @ 5_20: MFJ, both 65+, adding LTCG while the senior deduction phases out.

    Taxable ordinary income (59,550+) sits below the 98,900 top of the 0% band
    and gains straddle it. Each added gain dollar costs 15%, and the shrinking
    senior deduction (12 cents per dollar for two people) adds 12 cents of
    ordinary income at 12% plus pushes 12 cents of gains from 0% to 15%.
    """
    p = make(
        fs="mfj",
        people=[
            Person(birth_year=1958, social_security=35_000, pension=15_000, ira_distributions=40_000),
            Person(birth_year=1959, social_security=18_000, ira_distributions=2_000),
        ],
        taxable_interest=5_000,
        qualified_dividends=16_500,
    )
    s = build_marginal_series(p, lib, source=IncomeSource.LTCG, x_max=60_000)
    assert rate_at(s, 10_000, "cg") == 0  # still filling the 0% band
    assert rate_at(s, 40_000, "cg") == pytest.approx(0.15)
    assert rate_at(s, 40_000, "senior") == pytest.approx(0.12 * (0.12 + 0.15))
    assert rate_at(s, 40_000, "ordinary") == 0


def test_irmaa_thresholds_follow_profile_inflation(lib):
    p = make(people=[Person(birth_year=1960, ira_distributions=50_000)])
    p.inflation = 0.03
    s = build_marginal_series(p, lib, x_max=250_000)
    first = [a for a in s.annotations if a.kind == "irmaa"][0]
    # 2028 is projected one year from the official 2027 file.
    assert first.magi == (112_000 * 1.03) // 1000 * 1000
    assert "projected at 3.0% inflation" in first.detail


def test_irmaa_current_year_basis(lib):
    p = make(people=[Person(birth_year=1960, ira_distributions=50_000)])
    s = build_marginal_series(p, lib, x_max=250_000, irmaa_basis="current_year")
    first = [a for a in s.annotations if a.kind == "irmaa"][0]
    assert first.magi == 109_000
    assert "2026 published thresholds" in first.detail


def test_irmaa_top_tier_frozen_until_2028():
    from rtpl.taxlaw.loader import TaxLawLibrary

    lib = TaxLawLibrary()
    assert lib.federal(2027, 0.03).irmaa_tiers[-1].mfj == 750_000
    assert lib.federal(2028, 0.03).irmaa_tiers[0].mfj == (224_000 * 1.03) // 1000 * 1000
    assert lib.federal(2028, 0.03).irmaa_tiers[-1].mfj == (750_000 * 1.03) // 1000 * 1000


def test_irmaa_labels_show_added_premium_and_footnote(lib):
    p = make(people=[Person(birth_year=1960, ira_distributions=50_000)])
    s = build_marginal_series(p, lib, x_max=250_000, irmaa_basis="current_year")
    first = [a for a in s.annotations if a.kind == "irmaa"][0]
    assert first.sublabel == f"+${(284.10 - 202.90 + 14.50) * 12:,.0f}/year"
    assert s.irmaa_note.startswith("IRMAA lines: 2026 published brackets")
    s2 = build_marginal_series(p, lib, x_max=250_000)
    assert "2028 brackets (premium year for 2026 income), projected at 2.5%" in s2.irmaa_note


def test_washington_gains_tax_has_its_own_layer(lib):
    p = Profile.load("examples/profiles/couple_selling_stock_wa.json")
    s = build_marginal_series(p, lib, source=IncomeSource.LTCG, x_max=150_000)
    assert rate_at(s, 100_000, "state_cg") == pytest.approx(0.07)
    assert rate_at(s, 100_000, "state") == 0
    assert s.layer_label("state_cg") == "State CG&QD Tax (WA)"
    v = default_visibility(s)
    assert v["state_cg"] and not v["state"]
    ca = build_marginal_series(Profile.load("examples/profiles/retired_couple_ca.json"), lib, x_max=50_000)
    assert not default_visibility(ca)["state_cg"]


def _blind_ssdi_couple():
    # MFJ 2026: one spouse 60, blind, on SSDI; the other 52. SS fully 85% taxable.
    return make(
        fs="mfj",
        state="WA",
        people=[
            Person(name="A", birth_year=1966, blind=True, social_security=43_955, ira_distributions=20_500),
            Person(name="B", birth_year=1974),
        ],
        taxable_interest=1_904,
        qualified_dividends=32_342,
        nonqualified_dividends=1_160,
        long_term_capital_gains=26_649,
        short_term_capital_gains=5,
        other_ordinary_income=2_034,
        adjustments=1_000,
        itemized_deductions=2_000,
        has_taxable_brokerage=True,
    )


def test_breakpoints_are_exact_thresholds_not_blended_steps(lib):
    s = build_marginal_series(_blind_ssdi_couple(), lib)
    bps = s.breakpoints()
    # Worked by hand: AGI 120,955.75 (SS taxable 37,361.75), deduction
    # 32,200 + 1,650 blind = 33,850, taxable 87,105.75 of which 58,991 is
    # gains and qualified dividends, so the ordinary part is 28,114.75.
    expected = [
        (0, 0.12),
        (98_900 - 87_105.75, 0.27),  # gains start leaving the 0% band
        (98_900 - 28_114.75, 0.12),  # all gains now at 15%
        (100_800 - 28_114.75, 0.22),  # top of the 12% bracket
        (250_000 - 120_955.75, 0.258),  # NIIT starts
        (211_400 - 28_114.75, 0.278),  # 24% bracket
        (250_000 + 62_060 - 120_955.75, 0.24),  # NIIT capped at investment income
    ]
    assert [(round(b.x), round(b.total, 4)) for b in bps] == [(round(x), r) for x, r in expected]
    for b in bps:
        assert b.x == pytest.approx(next(x for x, _ in expected if round(x) == round(b.x)), abs=0.02)
    # No blended rates like 13.5% or 0.9% anywhere.
    rates = {round(v, 4) for b in bps for v in b.rates.values()}
    assert rates <= {0.0, 0.12, 0.15, 0.22, 0.24, 0.038}


@pytest.mark.parametrize("name", ["retired_couple_ca", "early_retiree_aca", "ssdi_under_65"])
@pytest.mark.parametrize("source", list(IncomeSource))
def test_breakpoints_agree_with_the_steps(lib, name, source):
    p = Profile.load(f"examples/profiles/{name}.json")
    s = build_marginal_series(p, lib, source=source, x_max=250_000, step=100)
    bps = s.breakpoints()
    assert bps[0].x == 0
    assert all(a.x < b.x for a, b in pairwise(bps))
    # Any step that lies wholly between two breakpoints has exactly their rates.
    for i, x0 in enumerate(s.x):
        inside = [b for b in bps if b.x <= x0]
        nxt = [b for b in bps if b.x > x0]
        if nxt and nxt[0].x < x0 + s.step:
            continue  # this step straddles a threshold
        for k in LAYERS:
            assert s.layers[k][i] == pytest.approx(inside[-1].rates[k], abs=1e-6)


@pytest.mark.parametrize("state", ["MN", "VT"])
def test_breakpoints_finish_where_a_state_rate_drifts(lib, state):
    # A married couple with $55K of Social Security: the state's straight-line
    # phase-out of its Social Security exemption overlaps the federal
    # phase-in, so the state rate drifts with no exact thresholds. This used
    # to loop forever and lock up the app.
    p = Profile(
        year=2025,
        filing_status="mfj",
        state=state,
        people=[
            Person(name="A", birth_year=1959, social_security=36_667),
            Person(name="B", birth_year=1960, social_security=18_333),
        ],
    )
    s = build_marginal_series(p, lib, x_max=250_000, step=100)
    bps = s.breakpoints()
    assert all(a.x < b.x for a, b in pairwise(bps))
    assert len(bps) < 200
