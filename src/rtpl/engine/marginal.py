"""The marginal tax graph: effective marginal rate on each extra dollar.

For each step of the X axis the engine adds ``step`` more dollars of the
chosen income source, recomputes the whole return, and splits the change in
tax into layers. Because the split uses counterfactual recomputations, the
layers always add up exactly to the true change in total tax:

* ``ordinary``  bracket tax on the added dollars themselves
* ``ss``        extra federal income tax because more Social Security became
                taxable (its ordinary tax, plus any gains it pushes out of
                the 0% band)
* ``cg``        tax on added gains or dividends, and gains pushed out of the
                0% band by the added ordinary income itself
* ``senior``    extra federal income tax because the senior deduction shrank
                (again including gains it pushes out of the 0% band)
* ``edu``       education credits lost to their MAGI phase-out (both the
                nonrefundable and refundable parts)
* ``niit``      Net Investment Income Tax
* ``state``     state income tax
* ``state_cg``  state tax under separate capital gains rules (Washington's
                gains tax, Montana's gains rates, Hawaii's gains rate cap)
* ``ptc``       ACA premium tax credit lost to its gradual phase-out (a
                premium subsidy rather than a tax, but each dollar of income
                costs it the same way)
* ``nonref``    a negative layer: tax that an unused nonrefundable credit
                absorbs. With income tax below the credit, the ordinary
                layer still shows the bracket rate and this cancels it, so
                the total is 0% until the credit is used up.

So the capital gains layer stays at the statutory 0/15/20% on the added
income; the knock-on effects of the Social Security and senior deduction
phase-ins are charged to those layers, where they come from.

IRMAA and the ACA thresholds are cliffs in premiums rather than taxes, so
they are returned as annotations (where along the X axis each threshold is
crossed) instead of being folded into the rate. The ACA 400% line carries
the subsidy lost there; the gradual phase-out below it is the ``ptc`` layer.

State tax cliffs (an exemption, exclusion or credit lost all at once, such
as New Mexico's Social Security exemption above $150K) are annotations too:
a jump of thousands of dollars inside one $100 step would otherwise draw a
spike of thousands of percent and flatten the rest of the graph. The jump
is taken out of the step's ``state`` rate and shown as a line with its cost.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from rtpl.model.profile import Profile
from rtpl.taxlaw.loader import FederalLaw, StateLaw, TaxLawLibrary, status_key

from .tax import IncomeSource, TaxResult, aca_premiums, compute_tax, premium_tax_credit

# Stack order, bottom up. Negative rates (the nonref layer) stack down from 0.
LAYERS = ("ordinary", "ss", "cg", "senior", "edu", "niit", "state", "state_cg", "ptc", "nonref")
ANNOTATIONS = ("irmaa", "aca", "state_cliff")

LAYER_LABELS = {
    "ordinary": "Tax on Ordinary Income",
    "ss": "Social Security Taxation Phase-In",
    "senior": "Enhanced Senior Deduction Phase-Out",
    "cg": "Capital Gains and Qualified Dividends Tax",
    "edu": "Education Credit Phase-Out",
    "niit": "Net Investment Income Tax",
    "state": "State Tax",
    "state_cg": "State CG&QD Tax",
    "ptc": "ACA Premium Tax Credit Phase-Out",
    "nonref": "Nonrefundable Credit Absorbing Tax",
}


@dataclass(frozen=True)
class Annotation:
    kind: str  # "irmaa", "aca" or "state_cliff"
    x: float  # additional income at which the threshold is crossed
    magi: float  # the MAGI threshold itself
    label: str
    detail: str
    annual_cost: float | None = None  # IRMAA: extra premium per year
    sublabel: str = ""  # second line on the chart, e.g. "+$2,413/year"


@dataclass(frozen=True)
class Breakpoint:
    x: float  # additional income where these rates start, to the cent
    rates: dict[str, float]  # marginal rate per layer from here on

    @property
    def total(self) -> float:
        return sum(self.rates[k] for k in LAYERS)


@dataclass
class MarginalSeries:
    profile: Profile
    source: IncomeSource
    law: FederalLaw
    state: StateLaw | None
    x: list[float]  # left edge of each step
    step: float
    layers: dict[str, list[float]]  # marginal rate per step, as a fraction
    baseline: TaxResult
    results: list[TaxResult]  # one per grid point, len(x) + 1
    annotations: list[Annotation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    irmaa_note: str = ""  # footnote shown when the IRMAA lines are visible

    @property
    def total(self) -> list[float]:
        return [sum(self.layers[k][i] for k in LAYERS) for i in range(len(self.x))]

    def layer_label(self, key: str) -> str:
        if key == "state":
            return self.state.label if self.state else "State Tax"
        if key == "state_cg":
            return self.state.gains_label if self.state else "State CG&QD Tax"
        return LAYER_LABELS[key]

    def nonzero_layers(self, tol: float = 1e-9) -> set[str]:
        return {k for k in LAYERS if any(abs(v) > tol for v in self.layers[k])}

    def breakpoints(self, tol: float = 0.01) -> list[Breakpoint]:
        """Every exact point where any layer's rate changes, with the new rates.

        The chart works in steps of ``step`` dollars, so a step that
        straddles a threshold (say the top of the 12% bracket at $72,685)
        shows a blend of the rates on each side, such as 13.5%. This finds
        each threshold to within ``tol`` dollars by bisection on the real
        tax calculation, and reports the pure rate that starts there, so the
        data table never shows a blended rate.
        """
        def rates(xa: float, xb: float) -> dict[str, float]:
            return _layer_rates(self.profile, self.law, self.state, self.source, xa, xb)

        probe = min(1.0, self.step / 2)
        x_end = self.x[-1] + self.step if self.x else 0.0
        cur_x = 0.0
        cur = rates(0.0, probe)
        out = [Breakpoint(0.0, cur)]
        for i, x0 in enumerate(self.x):
            x1 = x0 + self.step
            step_rates = {k: self.layers[k][i] for k in LAYERS}
            if _same_rates(step_rates, cur):
                continue
            lo = max(x0, cur_x)
            # One step can hold more than one threshold, so keep going until
            # the rest of the step is a straight line at the current rates.
            while lo < x1 - tol and not _same_rates(rates(lo, x1), cur):
                a, b = lo, x1
                while b - a > tol:
                    m = (a + b) / 2
                    if _same_rates(rates(lo, m), cur):
                        a = m
                    else:
                        b = m
                # Always move forward, even when the rate drifts so fast that
                # the bisection ends within a cent of where it started.
                k = max(round(b, 2), round(lo + 0.01, 2))
                cur = rates(k, min(k + probe, x_end))
                cur_x = lo = k
                if not _same_rates(cur, out[-1].rates):
                    out.append(Breakpoint(k, cur))
        return out

    def rate_changes(self) -> list[tuple[float, float]]:
        """(x, total rate) at every exact point where the total rate changes.

        Used for the accessible data table, so screen reader users get the
        same information as the chart.
        """
        out: list[tuple[float, float]] = []
        for bp in self.breakpoints():
            r = round(bp.total, 4)
            if not out or abs(r - out[-1][1]) > 1e-4:
                out.append((bp.x, r))
        return out


def build_marginal_series(
    profile: Profile,
    library: TaxLawLibrary,
    *,
    source: IncomeSource = IncomeSource.ORDINARY,
    x_max: float = 250_000,
    step: float = 100,
    include_state: bool = True,
    irmaa_basis: str = "premium_year",
) -> MarginalSeries:
    """Build the graph data.

    ``irmaa_basis``: "premium_year" (default) draws the IRMAA tiers that
    will apply to this year's income, i.e. the thresholds for the premium
    year two years later, projected with ``profile.inflation`` when that
    year has no data file. "current_year" draws this year's published
    thresholds instead, for comparison with tools that show those.
    """
    if step <= 0 or x_max <= 0:
        raise ValueError("x_max and step must be positive")
    law = library.federal(profile.year, profile.inflation)
    state = library.state(profile.state) if include_state and profile.state else None

    n = int(round(x_max / step))
    grid = [i * step for i in range(n + 1)]
    results = [compute_tax(profile, law, state, extra=g, source=source) for g in grid]
    layers: dict[str, list[float]] = {k: [] for k in LAYERS}

    for i in range(n):
        rates = _layer_rates(profile, law, state, source, grid[i], grid[i + 1], results[i], results[i + 1])
        for k in LAYERS:
            layers[k].append(rates[k])
    cliffs = _state_cliffs(profile, law, state, source, grid, layers["state"]) if state is not None else []
    for x, jump in cliffs:
        layers["state"][min(n - 1, int(x // step))] -= jump / step

    series = MarginalSeries(
        profile=profile,
        source=source,
        law=law,
        state=state,
        x=grid[:-1],
        step=step,
        layers=layers,
        baseline=results[0],
        results=results,
    )
    if law.status == "projected":
        series.notes.append(
            f"{profile.year} tax law is projected from {law.projected_from} "
            f"at {law.inflation_used:.1%} inflation."
        )
    series.annotations.extend(state_cliff_annotations(series, cliffs))
    series.annotations.extend(irmaa_annotations(series, library, irmaa_basis))
    series.annotations.extend(aca_annotations(series))
    return series


# A step whose state rate tops both neighbours by this much is checked for
# a cliff; a cliff smaller than CLIFF_MIN dollars is left in the rate.
_CLIFF_CHECK = 0.02
CLIFF_MIN = 1.0


def _state_income_tax_at(profile, law, state, source, x: float) -> float:
    r = compute_tax(profile, law, state, extra=x, source=source)
    return r.state_tax - r.state_gains_tax


def _state_cliffs(
    profile: Profile,
    law: FederalLaw,
    state: StateLaw,
    source: IncomeSource,
    grid: list[float],
    state_rates: list[float],
) -> list[tuple[float, float]]:
    """``(x, jump)`` for each point where state income tax jumps.

    A step that holds a jump shows a rate well above the steps on either
    side. Bisection then finds the jump to the cent: the half kept is the
    one where tax rises faster than the neighbouring rates explain.
    """
    out: list[tuple[float, float]] = []
    n = len(state_rates)
    for i in range(n):
        near = [state_rates[j] for j in (i - 1, i + 1) if 0 <= j < n]
        if not near:
            continue
        slope = max(near) + _CLIFF_CHECK
        if state_rates[i] <= slope:
            continue

        def tax(x: float) -> float:
            return _state_income_tax_at(profile, law, state, source, x)

        def excess(a: float, b: float, ta: float, tb: float) -> float:
            return (tb - ta) - (b - a) * slope

        a, b = grid[i], grid[i + 1]
        ta, tb = tax(a), tax(b)
        if excess(a, b, ta, tb) < CLIFF_MIN:
            continue
        while b - a > 0.01:
            m = (a + b) / 2
            tm = tax(m)
            if excess(a, m, ta, tm) >= excess(m, b, tm, tb):
                b, tb = m, tm
            else:
                a, ta = m, tm
        jump = (tb - ta) - (b - a) * state_rates[i - 1 if i > 0 else i + 1]
        if jump >= CLIFF_MIN:
            out.append((round(b, 2), jump))
    return out


def state_cliff_annotations(series: "MarginalSeries", cliffs: list[tuple[float, float]]) -> list[Annotation]:
    st = series.state
    if st is None:
        return []
    out = []
    for x, jump in cliffs:
        r = compute_tax(series.profile, series.law, st, extra=x, source=series.source)
        out.append(
            Annotation(
                kind="state_cliff",
                x=x,
                magi=r.agi,
                label=f"{st.code} cliff",
                detail=(
                    f"{st.name} tax rises by about ${jump:,.0f} in one dollar at AGI ${r.agi:,.0f} "
                    f"(an exemption, exclusion or credit ends or steps down)"
                ),
                annual_cost=jump,
                sublabel=f"+${jump:,.0f} tax",
            )
        )
    return out


def _layer_rates(
    profile: Profile,
    law: FederalLaw,
    state: StateLaw | None,
    source: IncomeSource,
    xa: float,
    xb: float,
    a: TaxResult | None = None,
    b: TaxResult | None = None,
) -> dict[str, float]:
    """Average marginal rate of each layer between ``xa`` and ``xb`` added dollars.

    The split uses counterfactual recomputations (taxable Social Security
    and the senior deduction frozen at ``xa``), so the layers always add up
    to the true change in total tax.
    """
    if a is None:
        a = compute_tax(profile, law, state, extra=xa, source=source)
    if b is None:
        b = compute_tax(profile, law, state, extra=xb, source=source)
    width = xb - xa
    frozen_both = compute_tax(
        profile, law, state, extra=xb, source=source,
        taxable_ss_override=a.taxable_social_security,
        senior_deduction_override=a.senior_deduction,
    )
    frozen_senior = compute_tax(
        profile, law, state, extra=xb, source=source,
        senior_deduction_override=a.senior_deduction,
    )

    def income_tax(r: TaxResult) -> float:
        return r.ordinary_tax + r.capital_gains_tax

    # Nonrefundable credits: the part used changes because the tax changed
    # (credit held at xa: the nonref layer) or the credit phased out (edu).
    ca, la, lb = a.nonrefundable_credits, income_tax(a), income_tax(b)
    absorbed = min(ca, lb) - min(ca, la)
    phased = (b.nonrefundable_used - min(ca, lb)) + (b.refundable_credits - a.refundable_credits)
    ptc = 0.0
    if a.aca_premium or b.aca_premium:
        ptc = (premium_tax_credit(profile, law, b.magi_aca, continuous=True)
               - premium_tax_credit(profile, law, a.magi_aca, continuous=True))

    return {
        "ordinary": (frozen_both.ordinary_tax - a.ordinary_tax) / width,
        "cg": (frozen_both.capital_gains_tax - a.capital_gains_tax) / width,
        "ss": (income_tax(frozen_senior) - income_tax(frozen_both)) / width,
        "senior": (income_tax(b) - income_tax(frozen_senior)) / width,
        "niit": (b.niit - a.niit) / width,
        "state": ((b.state_tax - b.state_gains_tax) - (a.state_tax - a.state_gains_tax)) / width,
        "state_cg": (b.state_gains_tax - a.state_gains_tax) / width,
        "edu": -phased / width,
        "nonref": -absorbed / width,
        "ptc": -ptc / width,
    }


# The premium tax credit's rate drifts continuously across each income
# band (the expected share of income rises in a straight line), so it has
# no exact thresholds: a new breakpoint starts once it moves a whole point.
# A state rate drifts too where a straight-line phase-out of a Social
# Security exemption (Minnesota, Vermont) overlaps the federal phase-in of
# taxable Social Security: there a breakpoint starts once it moves a tenth
# of a point.
_DRIFT_TOL = {"ptc": 0.01, "state": 0.001}


def _same_rates(r1: dict[str, float], r2: dict[str, float], tol: float = 1e-6) -> bool:
    return all(abs(r1[k] - r2[k]) <= _DRIFT_TOL.get(k, tol) for k in LAYERS)


# -- annotations -------------------------------------------------------------


def _crossing(grid: list[float], values: list[float], threshold: float, strict: bool = True) -> float | None:
    """First X where a non-decreasing ``values`` goes above ``threshold``."""
    if not values:
        return None
    if (values[0] > threshold) if strict else (values[0] >= threshold):
        return None  # already past it at the baseline
    idx = bisect.bisect_right(values, threshold) if strict else bisect.bisect_left(values, threshold)
    if idx >= len(values):
        return None
    lo, hi = idx - 1, idx
    v0, v1 = values[lo], values[hi]
    if v1 == v0:
        return grid[hi]
    return grid[lo] + (threshold - v0) / (v1 - v0) * (grid[hi] - grid[lo])


def medicare_people_in(profile: Profile, year: int) -> int:
    count = 0
    for p in profile.people:
        if p.medicare_part_b is not None:
            count += p.medicare_part_b
        else:
            count += p.age_in(year) >= 65
    return count


def irmaa_annotations(
    series: MarginalSeries, library: TaxLawLibrary, basis: str = "premium_year"
) -> list[Annotation]:
    profile = series.profile
    if basis not in ("premium_year", "current_year"):
        raise ValueError(f"Unknown IRMAA basis {basis!r}")
    premium_year = profile.year + series.law.irmaa_lookback
    threshold_year = premium_year if basis == "premium_year" else profile.year
    try:
        law = library.federal(threshold_year, profile.inflation)
    except KeyError:
        return []
    grid = [i * series.step for i in range(len(series.results))]
    magi = [r.magi_irmaa for r in series.results]
    enrolled = medicare_people_in(profile, premium_year)
    people = enrolled or 1
    if basis == "current_year":
        estimate = f" (using {threshold_year} published thresholds and premiums)"
    elif law.status == "projected":
        estimate = f" (projected at {law.inflation_used:.1%} inflation)"
    else:
        estimate = ""
    who = f"{people} person" if people == 1 else f"{people} people"
    if basis == "current_year":
        basis_text = f"{threshold_year} published brackets and premiums"
    elif law.status == "projected":
        basis_text = (
            f"{threshold_year} brackets (premium year for {profile.year} income), "
            f"projected at {law.inflation_used:.1%} inflation"
        )
    else:
        basis_text = f"{threshold_year} brackets (premium year for {profile.year} income)"
    series.irmaa_note = (
        f"IRMAA lines: {basis_text}. Amounts are the added yearly Medicare premium "
        f"(Part B + Part D) at each step, for {who}."
    )
    out = []
    prev_b, prev_d = law.irmaa_part_b_standard, 0.0
    for n, tier in enumerate(law.irmaa_tiers, start=1):
        threshold = tier.threshold(profile.filing_status)
        x = _crossing(grid, magi, threshold, strict=True)
        step_cost = ((tier.part_b - prev_b) + (tier.part_d - prev_d)) * 12 * people
        prev_b, prev_d = tier.part_b, tier.part_d
        if x is None:
            continue
        out.append(
            Annotation(
                kind="irmaa",
                x=x,
                magi=threshold,
                label=f"IRMAA {n}",
                detail=(
                    f"IRMAA tier {n}: MAGI over ${threshold:,.0f} in {profile.year} raises "
                    f"{premium_year} Medicare premiums by about ${step_cost:,.0f}/yr "
                    f"({who}){estimate}"
                ),
                annual_cost=step_cost,
                sublabel=f"+${step_cost:,.0f}/year",
            )
        )
    return out


def aca_annotations(series: MarginalSeries) -> list[Annotation]:
    profile, law = series.profile, series.law
    size = profile.fpl_household_size()
    fpl = law.fpl(size)
    grid = [i * series.step for i in range(len(series.results))]
    magi = [r.magi_aca for r in series.results]
    marks = [
        (law.aca_medicaid_pct, "Medicaid 138%", "Medicaid eligibility ends (expansion states) at {pct:.0%} FPL, MAGI ${t:,.0f}"),
    ]
    if profile.household.aca_plan_tier == "silver":
        marks.append(
            (law.aca_csr_pct, "CSR 250%", "Cost-sharing reductions (Silver plans) end at {pct:.0%} FPL, MAGI ${t:,.0f}")
        )
    if not law.aca_enhanced_subsidies:
        marks.append(
            (law.aca_cliff_pct, "ACA cliff 400%",
             "ACA premium tax credit ends entirely above {pct:.0%} FPL, MAGI ${t:,.0f}")
        )
    bench, _ = aca_premiums(profile)
    out = []
    for pct, label, text in marks:
        t = pct * fpl
        x = _crossing(grid, magi, t, strict=True)
        if x is None:
            continue
        detail = text.format(pct=pct, t=t)
        cost = None
        sub = ""
        if pct == law.aca_cliff_pct and bench > 0 and law.aca_applicable is not None:
            cost = premium_tax_credit(profile, law, t, continuous=True)
            sub = f"-${cost:,.0f} subsidy"
            detail += f": about ${cost:,.0f}/yr of premium tax credit lost in one dollar"
        out.append(Annotation(kind="aca", x=x, magi=t, label=label, detail=detail,
                              annual_cost=cost, sublabel=sub))
    if law.aca_enhanced_subsidies:
        series.notes.append(
            f"Enhanced ACA credits apply in {law.year}: no 400% FPL subsidy cliff."
        )
    if bench > 0 and law.aca_applicable is None:
        series.notes.append(f"No ACA applicable percentage table for {law.year}: premium tax credit not shown.")
    return out


# -- smart defaults ------------------------------------------------------------


def default_visibility(series: MarginalSeries) -> dict[str, bool]:
    """Which layers and annotations to show unless the user says otherwise.

    The idea is to hide what cannot matter for this household, never to
    stop someone from turning a layer on.
    """
    p = series.profile
    active = series.nonzero_layers()
    vis = {k: k in active for k in LAYERS}
    vis["ordinary"] = True
    # Capital gains layer: on if the household holds a taxable brokerage
    # account, already has gains or dividends, or we are adding them.
    vis["cg"] = (
        "cg" in active
        or series.source is not IncomeSource.ORDINARY
        or p.household.has_taxable_brokerage
    )
    st = series.state
    vis["state"] = st is not None and st.has_income_tax
    vis["state_cg"] = st is not None and st.has_separate_gains_tax and (
        "state_cg" in active
        or series.source is not IncomeSource.ORDINARY
        or p.household.has_taxable_brokerage
    )
    # IRMAA uses income from two years before the premium year, so it
    # matters from age 63, or for anyone already on Medicare (e.g. SSDI).
    lookahead = p.year + series.law.irmaa_lookback
    vis["irmaa"] = medicare_people_in(p, lookahead) > 0
    under_65_no_medicare = any(
        person.age_in(p.year) < 65 and not person.on_medicare(p.year) for person in p.people
    )
    vis["aca"] = p.household.aca_marketplace and under_65_no_medicare
    vis["state_cliff"] = any(a.kind == "state_cliff" for a in series.annotations)
    return vis


def filing_status_key(profile: Profile) -> str:
    return status_key(profile.filing_status)
