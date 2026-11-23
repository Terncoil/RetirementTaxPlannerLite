"""One-year federal (and optional state) income tax for a household.

This is deliberately a plain function of (profile, law, extra income) so the
marginal graph, and later the forward-looking plan, can call it many times
with different inputs.

What is modelled for the marginal graph:
  * ordinary income tax brackets, standard or itemized deduction, the
    additional 65+/blind standard deduction, personal exemptions (0 today)
  * taxation of Social Security benefits (provisional income, 50%/85% tiers)
  * the OBBBA enhanced senior deduction and its 6% MAGI phase-out
  * qualified dividends and long-term gains stacked on ordinary income
  * Net Investment Income Tax
  * an optional state layer (bracketed or flat, with SS / retirement exemptions)
  * education credits (American Opportunity and Lifetime Learning), with
    the nonrefundable part limited to income tax and the refundable part
    paid regardless
  * the ACA premium tax credit, from each person's benchmark premium (a
    premium subsidy rather than tax, so it is kept out of ``federal_tax``)

What is not modelled yet: other credits, AMT, QBI, the 0.9% additional
Medicare tax, self-employment tax, state credits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rtpl.model.profile import Profile
from rtpl.taxlaw.loader import FederalLaw, StateLaw, status_key

from .state import state_income_tax


class IncomeSource(str, Enum):
    """What kind of income the marginal graph adds along its X axis."""

    ORDINARY = "ordinary"
    QUALIFIED_DIVIDENDS = "qualified_dividends"
    LTCG = "ltcg"

    @property
    def label(self) -> str:
        return {
            IncomeSource.ORDINARY: "ordinary income (IRA withdrawal / Roth conversion)",
            IncomeSource.QUALIFIED_DIVIDENDS: "qualified dividends",
            IncomeSource.LTCG: "long-term capital gains",
        }[self]

    @property
    def short_label(self) -> str:
        return {
            IncomeSource.ORDINARY: "ordinary income",
            IncomeSource.QUALIFIED_DIVIDENDS: "qualified dividends",
            IncomeSource.LTCG: "long-term capital gains",
        }[self]


@dataclass(frozen=True)
class TaxResult:
    # income measures
    ordinary_income: float
    qualified_dividends: float
    net_ltcg: float
    social_security: float
    provisional_income: float
    taxable_social_security: float
    agi: float
    magi_irmaa: float
    magi_aca: float
    # deductions
    deduction: float  # standard or itemized, plus exemptions
    senior_deduction: float
    taxable_income: float
    preferential_income: float
    # taxes
    ordinary_tax: float
    capital_gains_tax: float
    niit: float
    state_tax: float  # all state tax, including state_gains_tax
    state_gains_tax: float = 0.0  # under the state's separate capital gains rules
    # credits
    nonrefundable_credits: float = 0.0  # education credits allowed before the tax limit
    nonrefundable_used: float = 0.0  # the part income tax was large enough to use
    refundable_credits: float = 0.0  # refundable part of the American Opportunity credit
    # ACA marketplace: yearly premiums for the plans bought (before any
    # subsidy) and the premium tax credit that pays part of them
    aca_premium: float = 0.0
    aca_ptc: float = 0.0

    @property
    def income_tax_before_credits(self) -> float:
        """Regular income tax, the tax nonrefundable credits can offset."""
        return self.ordinary_tax + self.capital_gains_tax

    @property
    def credits(self) -> float:
        return self.nonrefundable_used + self.refundable_credits

    @property
    def federal_tax(self) -> float:
        """Federal income tax and NIIT, less credits (negative when a
        refundable credit is more than the tax)."""
        return self.ordinary_tax + self.capital_gains_tax + self.niit - self.credits

    @property
    def aca_net_premium(self) -> float:
        return self.aca_premium - self.aca_ptc

    @property
    def total_tax(self) -> float:
        return self.federal_tax + self.state_tax


def taxable_social_security(benefits: float, provisional: float, base: float, adjusted: float) -> float:
    """IRS Social Security Benefits Worksheet (Pub. 915)."""
    if benefits <= 0 or provisional <= base:
        return 0.0
    if provisional <= adjusted:
        return min(0.5 * (provisional - base), 0.5 * benefits)
    return min(
        0.85 * (provisional - adjusted) + min(0.5 * (adjusted - base), 0.5 * benefits),
        0.85 * benefits,
    )


def senior_deduction(profile: Profile, law: FederalLaw, magi: float) -> float:
    if not law.senior_deduction_active:
        return 0.0
    fs = status_key(profile.filing_status)
    if fs == "mfs" and not law.senior_mfs_eligible:
        return 0.0
    eligible = sum(1 for a in profile.ages() if a >= 65)
    if not eligible:
        return 0.0
    excess = max(0.0, magi - law.senior_phaseout_start[fs])
    cut = law.senior_phaseout_rate * excess
    if law.senior_phaseout_per_person:
        return eligible * max(0.0, law.senior_amount - cut)
    return max(0.0, eligible * law.senior_amount - cut)


def _phaseout_keep(magi: float, start: float, width: float) -> float:
    """Share of a credit left after a straight-line phase-out."""
    if width <= 0:
        return 1.0 if magi <= start else 0.0
    return max(0.0, min(1.0, 1.0 - (magi - start) / width))


def education_credits(profile: Profile, law: FederalLaw, magi: float) -> tuple[float, float]:
    """(nonrefundable, refundable) education credits after the MAGI phase-out.

    The American Opportunity credit is per student (40% refundable); the
    Lifetime Learning credit is per return and nonrefundable. Their MAGI
    is AGI for anyone without foreign income.
    """
    h = profile.household
    ed = law.education
    if ed is None or (h.aotc_students <= 0 and h.llc_expenses <= 0):
        return 0.0, 0.0
    if h.education_last_year is not None and profile.year > h.education_last_year:
        return 0.0, 0.0
    fs = status_key(profile.filing_status)
    if fs == "mfs" and not ed.mfs_eligible:
        return 0.0, 0.0
    exp = max(0.0, h.aotc_expenses_per_student)
    each = min(exp, ed.aotc_full_expenses) + ed.aotc_partial_rate * min(
        max(0.0, exp - ed.aotc_full_expenses), ed.aotc_partial_expenses
    )
    aotc = max(0, h.aotc_students) * each
    aotc *= _phaseout_keep(magi, ed.aotc_phaseout_start[fs], ed.aotc_phaseout_width[fs])
    llc = ed.llc_rate * min(max(0.0, h.llc_expenses), ed.llc_expense_limit)
    llc *= _phaseout_keep(magi, ed.llc_phaseout_start[fs], ed.llc_phaseout_width[fs])
    refundable = ed.aotc_refundable_share * aotc
    return aotc - refundable + llc, refundable


def aca_premiums(profile: Profile) -> tuple[float, float]:
    """Yearly (benchmark, plan) premiums of the people on marketplace coverage
    (everyone with a benchmark premium who isn't on Medicare)."""
    if not profile.household.aca_marketplace:
        return 0.0, 0.0
    bench = plan = 0.0
    for p in profile.people:
        if p.aca_benchmark_premium <= 0 or p.on_medicare(profile.year):
            continue
        bench += 12 * p.aca_benchmark_premium
        plan += 12 * (p.aca_plan_premium if p.aca_plan_premium > 0 else p.aca_benchmark_premium)
    return bench, plan


def premium_tax_credit(profile: Profile, law: FederalLaw, magi: float, *, continuous: bool = False) -> float:
    """The ACA premium tax credit for the year at ``magi`` (the ACA MAGI).

    The credit is the benchmark premium less the applicable percentage of
    MAGI, but no more than the premium of the plan bought. Below 100% of
    the poverty line there is no credit, and without the enhanced credits
    none above the 400% cliff either. ``continuous`` leaves out those two
    jumps (MAGI is held at the cliff above it) so the marginal graph can
    draw the gradual phase-out as a rate; the jumps are drawn as lines.
    """
    bench, plan = aca_premiums(profile)
    if bench <= 0 or law.aca_applicable is None:
        return 0.0
    if status_key(profile.filing_status) == "mfs":
        return 0.0  # married filing separately can't claim it (outside rare exceptions)
    fpl = law.fpl(profile.fpl_household_size())
    cliff = law.aca_cliff_pct * fpl
    if continuous:
        if not law.aca_enhanced_subsidies:
            magi = min(magi, cliff)
    else:
        if magi < fpl:
            return 0.0
        if not law.aca_enhanced_subsidies and magi > cliff:
            return 0.0
    pct = law.applicable_percentage(max(0.0, magi) / fpl)
    return max(0.0, min(plan, bench - pct * max(0.0, magi)))


def _standard_deduction(profile: Profile, law: FederalLaw) -> float:
    fs = status_key(profile.filing_status)
    extra_each = law.additional_std_married if profile.married or fs == "mfj" else law.additional_std_unmarried
    boxes = sum(1 for p in profile.people if p.age_in(profile.year) >= 65)
    boxes += sum(1 for p in profile.people if p.blind)
    return law.standard_deduction[fs] + extra_each * boxes


def compute_tax(
    profile: Profile,
    law: FederalLaw,
    state: StateLaw | None = None,
    *,
    extra: float = 0.0,
    source: IncomeSource = IncomeSource.ORDINARY,
    taxable_ss_override: float | None = None,
    senior_deduction_override: float | None = None,
    nonrefundable_override: float | None = None,
) -> TaxResult:
    """Tax for ``profile`` plus ``extra`` dollars of ``source`` income.

    The overrides freeze an intermediate quantity; the marginal engine
    uses them to split a change in tax into its causes.
    """
    h = profile.household
    fs = status_key(profile.filing_status)

    add_ord = extra if source is IncomeSource.ORDINARY else 0.0
    add_qd = extra if source is IncomeSource.QUALIFIED_DIVIDENDS else 0.0
    add_lt = extra if source is IncomeSource.LTCG else 0.0

    retirement = profile.total("pension") + profile.total("ira_distributions") + add_ord
    ordinary = (
        profile.total("wages")
        + retirement
        + h.taxable_interest
        + h.nonqualified_dividends
        + h.short_term_capital_gains
        + h.other_ordinary_income
        + h.other_investment_income
    )
    qd = h.qualified_dividends + add_qd
    ltcg = h.long_term_capital_gains + add_lt
    if ltcg < 0:
        ordinary += max(ltcg, -law.capital_loss_limit[fs])
        ltcg = 0.0

    ss = profile.total("social_security")
    agi_ex_ss = ordinary + qd + ltcg - h.adjustments - h.hsa_deduction
    provisional = agi_ex_ss + h.tax_exempt_interest + 0.5 * ss
    if taxable_ss_override is None:
        taxable_ss = taxable_social_security(ss, provisional, law.ss_base[fs], law.ss_adjusted[fs])
    else:
        taxable_ss = taxable_ss_override
    agi = agi_ex_ss + taxable_ss

    std = _standard_deduction(profile, law)
    deduction = max(std, h.itemized_deductions) + law.personal_exemption * len(profile.people)
    if senior_deduction_override is None:
        senior = senior_deduction(profile, law, agi)
    else:
        senior = senior_deduction_override

    taxable = max(0.0, agi - deduction - senior)
    pref = min(qd + ltcg, taxable)
    ord_taxable = taxable - pref
    ordinary_tax = law.ordinary[fs].tax(ord_taxable)
    cg_tax = law.capital_gains[fs].tax_between(ord_taxable, ord_taxable + pref)

    nii = (
        h.taxable_interest
        + h.nonqualified_dividends
        + qd
        + ltcg
        + max(0.0, h.short_term_capital_gains)
        + h.other_investment_income
    )
    niit = law.niit_rate * max(0.0, min(nii, agi - law.niit_threshold[fs]))

    nonref, refundable = education_credits(profile, law, agi)
    if nonrefundable_override is not None:
        nonref = nonrefundable_override
    used = min(nonref, ordinary_tax + cg_tax)

    magi_aca = agi + h.tax_exempt_interest + (ss - taxable_ss)
    _, plan_premium = aca_premiums(profile)
    ptc = premium_tax_credit(profile, law, magi_aca) if plan_premium else 0.0

    state_tax = state_gains_tax = 0.0
    if state is not None:
        state_income, state_gains_tax = state_income_tax(
            profile, state, agi=agi, taxable_ss=taxable_ss, net_ltcg=ltcg, extra_ira=add_ord
        )
        state_tax = state_income + state_gains_tax

    return TaxResult(
        ordinary_income=ordinary,
        qualified_dividends=qd,
        net_ltcg=ltcg,
        social_security=ss,
        provisional_income=provisional,
        taxable_social_security=taxable_ss,
        agi=agi,
        magi_irmaa=agi + h.tax_exempt_interest,
        magi_aca=magi_aca,
        deduction=deduction,
        senior_deduction=senior,
        taxable_income=taxable,
        preferential_income=pref,
        ordinary_tax=ordinary_tax,
        capital_gains_tax=cg_tax,
        niit=niit,
        state_tax=state_tax,
        state_gains_tax=state_gains_tax,
        nonrefundable_credits=nonref,
        nonrefundable_used=used,
        refundable_credits=refundable,
        aca_premium=plan_premium,
        aca_ptc=ptc,
    )
