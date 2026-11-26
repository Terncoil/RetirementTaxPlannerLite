"""State income tax for one household and year.

A simplified, data-driven model that covers what matters most for retirees:
brackets, standard deduction, personal exemptions (as deductions or
credits), Social Security exemptions, pension/IRA exclusions, an age-based
deduction, capital gains exclusions or rate caps, and capital-gains-only
taxes such as Washington's, and credits. Deductions, exemptions,
exclusions and credits can shrink with income (``Phaseout``). Local income
taxes are not modelled; each state file's ``notes`` say what is left out.
"""

from __future__ import annotations


import math

from rtpl.model.profile import Profile
from rtpl.taxlaw.loader import InvestmentIncomeTax, Phaseout, StateCredit, StateLaw, status_key


def _phased(amount: float, phaseout: Phaseout | None, incomes: dict[str, float], fs: str) -> float:
    if phaseout is None:
        return amount
    return phaseout.apply(amount, incomes[phaseout.income], fs)


def _exempt_social_security(
    profile: Profile, state: StateLaw, agi: float, taxable_ss: float, incomes: dict[str, float] | None = None
) -> float:
    """Part of federally taxable Social Security the state does not tax."""
    if taxable_ss <= 0:
        return 0.0
    if not state.taxes_social_security:
        return taxable_ss
    rule = state.social_security
    fs = status_key(profile.filing_status)
    exempt = 0.0
    if rule.exempt_from_age is not None:
        total = profile.total("social_security")
        if total > 0:
            share = sum(
                p.social_security for p in profile.people if p.age_in(profile.year) >= rule.exempt_from_age
            ) / total
            exempt = max(exempt, share * taxable_ss)
    if rule.exempt_if_agi_at_most is not None:
        limit = rule.exempt_if_agi_at_most[fs]
        if agi <= limit:
            fraction = 1.0
        elif rule.phaseout_width is not None and rule.phaseout_width[fs] > 0:
            fraction = max(rule.exempt_fraction_above, 1.0 - (agi - limit) / rule.phaseout_width[fs])
        else:
            fraction = rule.exempt_fraction_above
        exempt = max(exempt, fraction * taxable_ss)
    if rule.limited_from_age is not None:
        total = profile.total("social_security")
        top = rule.exempt_from_age if rule.exempt_from_age is not None else math.inf
        if total > 0:
            share = sum(
                p.social_security for p in profile.people
                if rule.limited_from_age <= p.age_in(profile.year) < top
            ) / total
            limited = share * taxable_ss
            if incomes is not None:
                limited = _phased(limited, rule.limited_phaseout, incomes, fs)
            exempt = min(taxable_ss, exempt + limited)
    return exempt


def _retirement_exclusion(
    profile: Profile, state: StateLaw, extra_ira: float, incomes: dict[str, float]
) -> float:
    """Pension/IRA income excluded, person by person.

    Extra ordinary income on the marginal graph counts as Person 1's IRA
    withdrawal. An exclusion with a ``per_return`` cap shares it between
    the people on the return; a phase-out scales each person's exclusion.
    An exclusion that applies to ``any`` income is a deduction of its whole
    amount. Of the exclusions in one ``group`` the return uses the one that
    excludes the most.
    """
    exclusions = state.retirement_exclusions
    groups: dict[str, list] = {}
    for ex in exclusions:
        if ex.group is not None:
            groups.setdefault(ex.group, []).append(ex)
    if not groups:
        return _excluded(profile, state, exclusions, extra_ira, incomes)
    chosen = [ex for ex in exclusions if ex.group is None]
    for members in groups.values():
        chosen.append(max(
            members,
            key=lambda ex: _excluded(profile, state, (*chosen, ex), extra_ira, incomes),
        ))
    order = {id(ex): i for i, ex in enumerate(exclusions)}
    chosen.sort(key=lambda ex: order[id(ex)])
    return _excluded(profile, state, chosen, extra_ira, incomes)


def _excluded(profile: Profile, state: StateLaw, exclusions, extra_ira: float, incomes: dict[str, float]) -> float:
    fs = status_key(profile.filing_status)
    total = 0.0
    return_left = {id(ex): ex.per_return[fs] for ex in exclusions if ex.per_return}
    oldest = min(p.birth_year for p in profile.people)
    for i, person in enumerate(profile.people):
        remaining = {
            "pension": person.pension,
            "ira": person.ira_distributions + (extra_ira if i == 0 else 0.0),
        }
        age = person.age_in(profile.year)
        for ex in exclusions:
            if not ex.qualifies(oldest if ex.birth_year_of == "older_spouse" else person.birth_year, age):
                continue
            if "any" in ex.applies_to:
                eligible = math.inf
            else:
                eligible = sum(remaining[k] for k in ex.applies_to if k in remaining)
            cap = return_left[id(ex)] if ex.per_return else ex.amount
            used = min(eligible, cap)
            if ex.per_return:
                return_left[id(ex)] -= used
            # Count only the part a phase-out leaves, but use up the
            # eligible buckets in order as if the whole exclusion applied.
            if ex.phased_share_of == "income" and _phased(1.0, ex.phaseout, incomes, fs) < 1.0:
                # Above the full tier the share is of all eligible income
                # and the cap no longer applies.
                total += _phased(eligible, ex.phaseout, incomes, fs)
            else:
                total += _phased(used, ex.phaseout, incomes, fs)
            for k in ex.applies_to:
                if k in remaining and used > 0:
                    take = min(remaining[k], used)
                    remaining[k] -= take
                    used -= take
    return total


def _credit_amount(
    credit: StateCredit,
    profile: Profile,
    fs: str,
    *,
    federal_deduction: float,
    state_taxed_ss: float,
    incomes: dict[str, float],
) -> float:
    amount = credit.amount[fs] if credit.amount is not None else 0.0
    if credit.per_person:
        amount += credit.per_person * sum(
            1
            for p in profile.people
            if p.age_in(profile.year) >= credit.min_age
            and (credit.born_before is None or p.birth_year < credit.born_before)
        )
    amount += credit.federal_deduction_rate * federal_deduction
    amount += credit.social_security_rate * state_taxed_ss
    return _phased(amount, credit.phaseout, incomes, fs)


def state_credits(
    profile: Profile,
    state: StateLaw,
    *,
    federal_deduction: float,
    state_taxed_ss: float,
    incomes: dict[str, float],
) -> float:
    """Total nonrefundable credits before they are limited to the tax."""
    fs = status_key(profile.filing_status)
    total = state.credit_per_person * len(profile.people)
    if state.exemption_phaseout is not None:
        total = _phased(total, state.exemption_phaseout, incomes, fs)
    if state.filer_credit is not None:
        total += state.filer_credit[fs]
    best_in_group: dict[str, float] = {}
    for credit in state.credits:
        amount = _credit_amount(
            credit, profile, fs,
            federal_deduction=federal_deduction, state_taxed_ss=state_taxed_ss, incomes=incomes,
        )
        if credit.group is None:
            total += amount
        else:
            best_in_group[credit.group] = max(best_in_group.get(credit.group, 0.0), amount)
    return total + sum(best_in_group.values())


def total_income_all_sources(profile: Profile, *, extra_ira: float = 0.0, extra_qd: float = 0.0,
                             extra_ltcg: float = 0.0) -> float:
    """Income from every source, Social Security and tax-exempt interest
    included and losses not subtracted (Tennessee's 65+ Hall tax test)."""
    h = profile.household
    return (
        profile.total("wages") + profile.total("pension") + profile.total("ira_distributions") + extra_ira
        + profile.total("social_security")
        + h.taxable_interest + h.tax_exempt_interest
        + h.qualified_dividends + h.nonqualified_dividends + extra_qd
        + max(0.0, h.short_term_capital_gains) + max(0.0, h.long_term_capital_gains + extra_ltcg)
        + max(0.0, h.other_ordinary_income) + max(0.0, h.other_investment_income)
    )


def investment_income_tax(
    profile: Profile, tax: InvestmentIncomeTax, *, extra_ira: float = 0.0, extra_qd: float = 0.0,
    extra_ltcg: float = 0.0,
) -> float:
    """Tax on interest and dividends only (NH's I&D tax, TN's Hall tax)."""
    h = profile.household
    fs = status_key(profile.filing_status)
    amounts = {
        "interest": h.taxable_interest,
        "tax_exempt_interest": h.tax_exempt_interest,
        "dividends": h.qualified_dividends + h.nonqualified_dividends + extra_qd,
    }
    income = sum(max(0.0, amounts[k]) for k in tax.income)
    income = max(0.0, income + h.state_investment_income_adjustment)
    if income <= 0:
        return 0.0
    ages = profile.ages()
    if tax.exempt_from_age is not None and any(a >= tax.exempt_from_age for a in ages):
        limit = tax.exempt_if_total_income_at_most
        total = total_income_all_sources(profile, extra_ira=extra_ira, extra_qd=extra_qd, extra_ltcg=extra_ltcg)
        if limit is None or total <= limit[fs]:
            return 0.0
    exemption = tax.exemption[fs]
    if tax.blind_exempt:
        # Each blind person's share of the income and of the exemption is exempt.
        sighted = sum(1 for p in profile.people if not p.blind) / len(profile.people)
        income *= sighted
        exemption *= sighted
    boxes = 0
    if tax.extra_exemption_age is not None:
        boxes += sum(1 for a in ages if a >= tax.extra_exemption_age)
    if tax.extra_exemption_blind:
        boxes += sum(1 for p in profile.people if p.blind)
    exemption += tax.extra_exemption * boxes
    return tax.rate * max(0.0, income - exemption)


def state_income_tax(
    profile: Profile,
    state: StateLaw,
    *,
    agi: float,
    taxable_ss: float,
    net_ltcg: float,
    extra_ira: float = 0.0,
    federal_deduction: float = 0.0,
    tax_exempt_interest: float = 0.0,
    extra_qd: float = 0.0,
    extra_ltcg: float = 0.0,
) -> tuple[float, float]:
    """Return ``(income_tax, gains_tax)``.

    ``gains_tax`` is tax levied under the state's own rules for capital
    gains: a gains-only tax (Washington), separate stacked gains rates
    (Montana) or a rate cap on gains (Hawaii). Where a state simply taxes
    gains as regular income, that tax stays in ``income_tax``.

    ``federal_deduction`` is the federal standard or itemized deduction,
    which some state credits are figured from (Utah's taxpayer credit).

    A tax on interest and dividends only (New Hampshire, Tennessee) is in
    ``income_tax``. ``extra_qd`` and ``extra_ltcg`` are the marginal
    graph's added dividends and gains, which that tax needs on their own.
    """
    fs = status_key(profile.filing_status)
    income_tax = gains_tax = 0.0
    if state.has_income_tax:
        incomes = {
            "agi": agi,
            "magi": agi + tax_exempt_interest,
            "agi_ex_ss": agi - taxable_ss,
        }
        base = agi
        if state.taxes_hsa_contributions:
            base += profile.household.hsa_deduction
        ss_exempt = _exempt_social_security(profile, state, agi, taxable_ss, incomes)
        base -= ss_exempt
        base -= _retirement_exclusion(profile, state, extra_ira, incomes)
        cg_excluded = state.capital_gains_exclusion * max(0.0, net_ltcg)
        base -= cg_excluded
        incomes["state_income"] = base
        if state.age_deduction is not None:
            amount, min_age = state.age_deduction
            age_total = amount * sum(1 for a in profile.ages() if a >= min_age)
            base -= _phased(age_total, state.age_deduction_phaseout, incomes, fs)
        base -= _phased(state.standard_deduction[fs], state.standard_deduction_phaseout, incomes, fs)
        base -= _phased(state.exemption_per_person * len(profile.people), state.exemption_phaseout, incomes, fs)
        base = max(0.0, base)
        sched = state.brackets[fs]
        gains = min(base, max(0.0, net_ltcg) - cg_excluded)
        if state.gains_rates is not None:
            income_tax = sched.tax(base - gains)
            gains_tax = state.gains_rates[fs].tax_between(base - gains, base)
        elif state.capital_gains_max_rate is not None:
            income_tax = sched.tax(base - gains)
            gains_tax = min(sched.tax(base) - income_tax, float(state.capital_gains_max_rate) * gains)
        else:
            income_tax = sched.tax(base)
        for rate, threshold in state.surcharges:
            income_tax += rate * max(0.0, base - threshold)
        credits = state_credits(
            profile, state,
            federal_deduction=federal_deduction,
            state_taxed_ss=max(0.0, taxable_ss - ss_exempt),
            incomes=incomes,
        )
        used = min(credits, income_tax)
        income_tax -= used
        gains_tax = max(0.0, gains_tax - (credits - used))
    if state.investment_income_tax is not None:
        income_tax += investment_income_tax(
            profile, state.investment_income_tax, extra_ira=extra_ira, extra_qd=extra_qd, extra_ltcg=extra_ltcg,
        )
    if state.gains_only_tax is not None:
        taxable_gains = max(0.0, net_ltcg - state.gains_only_deduction)
        gains_tax += state.gains_only_tax.tax(taxable_gains)
    return income_tax, gains_tax


def has_state_tax(state: StateLaw | None) -> bool:
    """The state taxes some kind of income in the law's year."""
    return state is not None and state.has_any_tax


__all__ = ["state_income_tax", "state_credits", "has_state_tax", "investment_income_tax",
           "total_income_all_sources"]
