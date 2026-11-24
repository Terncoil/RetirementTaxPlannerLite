"""State tax law kept by year, and Michigan's retirement deductions by birth year."""

from pathlib import Path

import pytest

from rtpl.engine.marginal import build_marginal_series
from rtpl.engine.tax import compute_tax
from rtpl.model.profile import Person, Profile
from rtpl.taxlaw.loader import TaxLawLibrary

from conftest import make

DATA = Path(__file__).parent / "data"


def test_every_state_file_loads_for_its_year(lib):
    for code in lib.available_states():
        for year in lib.state_years(code):
            st = lib.state(code, year)
            assert st.year == year and st.law_year_requested is None
            p = make(year=year, state=code, people=[Person(birth_year=1955, social_security=30_000,
                                                           ira_distributions=50_000)])
            assert compute_tax(p, lib.federal(year), st).state_tax >= 0


def test_year_without_a_file_uses_the_nearest_law(lib):
    years = list(lib.state_years("MI"))
    assert years[0] == 2020 and years[-1] >= 2026
    assert lib.state("MI").year == years[-1]
    later = lib.state("MI", years[-1] + 1)
    assert later.year == years[-1] and later.law_year_requested == years[-1] + 1
    assert any("uses the" in n for n in later.chart_notes)
    earlier = lib.state("MI", 2019)
    assert earlier.year == 2020 and any("no earlier law" in n for n in earlier.chart_notes)
    # A state with only a 2026 file uses it for every year, with a footnote.
    only = lib.state("CA", 2023)
    assert only.year == 2026 and only.law_year_requested == 2023


def test_user_files_override_by_year_and_undated(tmp_path):
    (tmp_path / "states" / "2022").mkdir(parents=True)
    (tmp_path / "states" / "2022" / "MI.toml").write_text(
        'code = "MI"\nyear = 2022\n[brackets]\nsingle = [[0, 0.09]]\nmfj = [[0, 0.09]]\n'
    )
    lib = TaxLawLibrary(user_dirs=[tmp_path])
    assert lib.state("MI", 2022).brackets["single"].rate_at(1) == 0.09
    assert lib.state("MI", 2021).brackets["single"].rate_at(1) == pytest.approx(0.0425)
    (tmp_path / "states" / "MI.toml").write_text(
        'code = "MI"\n[brackets]\nsingle = [[0, 0.08]]\nmfj = [[0, 0.08]]\n'
    )
    lib = TaxLawLibrary(user_dirs=[tmp_path])
    assert lib.state("MI", 2021).brackets["single"].rate_at(1) == 0.08


def test_file_year_must_match_its_folder(tmp_path):
    (tmp_path / "states" / "2021").mkdir(parents=True)
    (tmp_path / "states" / "2021" / "MI.toml").write_text('code = "MI"\nyear = 2020\n')
    with pytest.raises(ValueError, match="2021 folder"):
        TaxLawLibrary(user_dirs=[tmp_path]).state("MI", 2021)


def _mi_tax(lib, year, birth_year, ira, fs="single", people=None, **household):
    people = people or [Person(birth_year=birth_year, ira_distributions=ira)]
    p = make(year=year, fs=fs, state="MI", people=people, **household)
    return compute_tax(p, lib.federal(year), lib.state("MI", year)).state_tax


def test_michigan_public_act_4_phase_in(lib):
    # Born 1957: nothing before 2023, then 25%, 50% and 75% of the Tier 1 limit.
    assert _mi_tax(lib, 2022, 1957, 30_000) == pytest.approx(0.0425 * (30_000 - 5_000))
    assert _mi_tax(lib, 2023, 1957, 30_000) == pytest.approx(0.0405 * (30_000 - 15_380 - 5_400))
    assert _mi_tax(lib, 2024, 1957, 50_000) == pytest.approx(0.0425 * (50_000 - 32_020 - 5_600))
    assert _mi_tax(lib, 2025, 1957, 70_000) == pytest.approx(0.0425 * (70_000 - 49_423 - 5_800))
    # Born 1959 misses the 2023 phase-in but gets 2024's.
    assert _mi_tax(lib, 2023, 1959, 30_000) == pytest.approx(0.0405 * (30_000 - 5_400))
    assert _mi_tax(lib, 2024, 1959, 50_000) == pytest.approx(0.0425 * (50_000 - 32_020 - 5_600))
    # Born before 1946: the full Tier 1 limit.
    assert _mi_tax(lib, 2021, 1944, 60_000) == pytest.approx(0.0425 * (60_000 - 54_404 - 4_900))


def test_michigan_tier_2_takes_the_larger_deduction(lib):
    # Born 1950: $20,000 against all income (dividends too) before 2023 ...
    assert _mi_tax(lib, 2022, 1950, 0, qualified_dividends=30_000) == pytest.approx(0.0425 * (30_000 - 20_000 - 5_000))
    # ... and in 2023, $20,000 beats the 25% phase-in on a small IRA ...
    assert _mi_tax(lib, 2023, 1950, 10_000, qualified_dividends=30_000) == pytest.approx(
        0.0405 * (40_000 - 20_000 - 5_400))
    # ... while in 2025 the 75% phase-in ($49,423 of IRA) is larger.
    assert _mi_tax(lib, 2025, 1950, 60_000) == pytest.approx(0.0425 * (60_000 - 49_423 - 5_800))


def test_michigan_joint_return_shares_one_limit_by_the_older_spouse(lib):
    people = [Person(birth_year=1957, ira_distributions=40_000), Person(birth_year=1962, ira_distributions=40_000)]
    tax = _mi_tax(lib, 2023, 0, 0, fs="mfj", people=people)
    assert tax == pytest.approx(0.0405 * (80_000 - 30_759 - 2 * 5_400))


def test_michigan_2023_single_filer_matches_the_reference_graph(lib):
    """Single, 2023, born 1957, $40,000 Social Security and $8,600 qualified
    dividends: the reference graph shows state tax from about $15k, 26.2%
    there, 54% and 44.8% in the dividend and Social Security bumps, 26.1%
    from about $40k and 28% from $77k."""
    profile = Profile.load(DATA / "mi_single_2023.json")
    s = build_marginal_series(profile, lib, x_max=100_000, step=100)
    total = [sum(s.layers[k][i] for k in s.layers) for i in range(len(s.x))]

    def rate(x):
        return total[int(x // 100)]

    assert s.layers["state"][int(14_000 // 100)] == pytest.approx(0, abs=1e-9)
    assert rate(20_000) == pytest.approx(0.2625, abs=0.002)
    assert rate(30_000) == pytest.approx(0.54, abs=0.003)
    assert rate(36_000) == pytest.approx(0.448, abs=0.003)
    assert rate(50_000) == pytest.approx(0.261, abs=0.003)
    assert rate(90_000) == pytest.approx(0.281, abs=0.003)
