"""Draw the forward-looking graphs: income by source with threshold lines,
and account balances. Like the marginal chart, these only need a
matplotlib Figure, so the app and the command line share them.
"""

from __future__ import annotations

import math

from matplotlib import patheffects

from matplotlib.colors import to_hex, to_rgb
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter

from rtpl.engine.forward import CATEGORIES, CATEGORY_LABELS, TAX_FREE, TAX_SOURCE_LABELS, TAX_SOURCES, PlanResult
from rtpl.model.plan import ACCOUNT_KINDS
from rtpl.theme import Palette

from .marginal_chart import _escape, _money

# Texture as a second cue beside color (gains and the tax-free segments).
HATCH = {"dividends": "////", "realized_gains": "\\\\\\\\", "tax_exempt": "....", "roth_withdrawal": "xxxx", "principal": "----", "qcd": "++"}

# Balances use the colors of the income they feed.
ACCOUNT_COLOR_KEY = {
    "traditional_ira": "rmd",
    "401k": "rmd",
    "inherited_ira": "inherited",
    "roth_ira": "roth_conversion",
    "roth_401k": "roth_conversion",
    "hsa": "roth_withdrawal",
    "taxable": "dividends",
    "cash": "principal",
}
ACCOUNT_HATCH = {"401k": "....", "roth_401k": "....", "hsa": "xxxx", "taxable": "////", "cash": "\\\\\\\\"}
# Taxes by source use the marginal graph's layer colors, so a tax looks the
# same on both pages. IRMAA takes the IRMAA line color from the income graph.
TAX_COLOR_KEY = {"ordinary": "ordinary", "ltcg": "cg", "niit": "niit", "state": "state", "state_gains": "state_cg"}
TAX_HATCH = {"state_gains": "////", "irmaa": "...."}
ACCOUNT_ORDER = ("taxable", "cash", "traditional_ira", "401k", "inherited_ira", "roth_ira", "roth_401k", "hsa")



def _plan_label(plan) -> str:
    """Baseline name and, if it has one, the plan's own name."""
    return " · ".join(p for p in (plan.profile.name.strip(), plan.name.strip()) if p) or "Plan"

def _mix(color: str, other: str, t: float) -> str:
    a, b = to_rgb(color), to_rgb(other)
    return to_hex(tuple(x + (y - x) * t for x, y in zip(a, b)))


def _shades(color: str, palette: Palette, n: int) -> list[str]:
    """n distinguishable steps of one hue, for the 'each source' view."""
    if n <= 1:
        return [color]
    steps = [0.0, 0.45, -0.3, 0.65, -0.5, 0.25, 0.8]
    out = []
    for k in range(n):
        t = steps[k % len(steps)]
        out.append(_mix(color, palette.surface, t) if t >= 0 else _mix(color, palette.text_primary, -t))
    return out


def _year_axis(ax, result: PlanResult, palette: Palette) -> None:
    years = [yr.year for yr in result.years]
    if not years:
        return
    span = years[-1] - years[0]
    every = 1 if span <= 12 else 2 if span <= 24 else 5
    ticks = [y for y in years if (y - years[0]) % every == 0]
    names = result.plan.profile.people
    labels = []
    for y in ticks:
        yr = next(r for r in result.years if r.year == y)
        ages = " · ".join(str(yr.ages[i]) for i in sorted(yr.ages))
        labels.append(f"{y}\n{ages}")
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels)
    who = " · ".join(p.name for p in names)
    ax.set_xlabel(f"Year and age ({who})", color=palette.text_secondary)
    ax.set_xlim(years[0] - 0.6, years[-1] + 0.6)


def _style(ax, palette: Palette) -> None:
    ax.set_facecolor(palette.surface)
    ax.tick_params(colors=palette.text_secondary, labelsize=8.5)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(palette.grid)
    ax.grid(axis="y", color=palette.grid, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.yaxis.set_major_formatter(FuncFormatter(_money))


def line_style(key: str, palette: Palette, bracket_keys: list[str], irmaa_keys: list[str] | None = None) -> dict:
    lc = palette.lines
    if key.startswith("bracket:"):
        k = bracket_keys.index(key)
        t = k / max(1, len(bracket_keys) - 1)
        return dict(color=_mix(lc.get("bracket_low", palette.text_secondary),
                               lc.get("bracket_high", palette.text_primary), t),
                    linestyle="-", linewidth=1.5)
    # Tight dots with a thin outline in the background colour, so they stay
    # visible where they cross bars of a similar colour.
    dots = dict(linestyle=(0, (0.1, 1.6)), linewidth=2.2, dash_capstyle="round",
                path_effects=[patheffects.Stroke(linewidth=3.6, foreground=palette.surface, alpha=0.8),
                              patheffects.Normal()])
    if key.startswith("irmaa:"):
        # Tight dots in shades of one colour, light for tier 1 to full for the
        # top tier, so tiers that bunch together (single filer) stay in order.
        keys = irmaa_keys or [key]
        t = keys.index(key) / max(1, len(keys) - 1) if key in keys else 1.0
        return dict(color=_mix(lc.get("irmaa", palette.irmaa), palette.surface, 0.45 * (1 - t)), **dots)
    if key.startswith("aca"):
        return dict(color=lc.get("aca", palette.aca), linestyle=(0, (6, 2, 1, 2)), linewidth=1.3)
    if key == "cg0":
        return dict(color=lc.get("cg0", palette.text_secondary), **dots)
    return dict(color=lc.get("deduction", palette.text_secondary), linestyle="-", linewidth=1.0)


def line_short_label(key: str) -> str:
    if key.startswith("bracket:"):
        return f"{float(key.split(':')[1]):.0%}"
    if key.startswith("irmaa:"):
        return f"IRMAA {key.split(':')[1]}"
    if key.startswith("aca"):
        return "ACA 400%"
    if key == "cg0":
        return "0% CG"
    return "Deductions"


def render_income(
    fig: Figure,
    result: PlanResult,
    visibility: dict[str, bool],
    palette: Palette,
    *,
    detail: bool = False,
    todays_dollars: bool = False,
) -> None:
    fig.clear()
    fig.set_facecolor(palette.surface)
    ax = fig.add_subplot(111)
    _style(ax, palette)
    years = [yr.year for yr in result.years]
    scale = {yr.year: (1 / result.deflator(yr.year) if todays_dollars else 1.0) for yr in result.years}
    show_free = visibility.get("tax_free", True)
    cats = [c for c in CATEGORIES if show_free or c not in TAX_FREE]

    # Series: one per category, or one per source within each category.
    series: list[tuple[str, str, str, str | None, dict[int, float]]] = []  # key, label, color, hatch, values
    for cat in cats:
        base = palette.sources.get(cat, palette.text_secondary)
        if detail:
            keys: dict[str, str] = {}
            for yr in result.years:
                for s in yr.sources:
                    if s.category == cat:
                        keys.setdefault(s.key, s.label)
            for (key, label), color in zip(keys.items(), _shades(base, palette, len(keys))):
                vals = {yr.year: sum(s.amount for s in yr.sources if s.key == key and s.category == cat)
                        for yr in result.years}
                series.append((f"{cat}|{key}", label, color, HATCH.get(cat), vals))
        else:
            vals = {yr.year: yr.category_totals()[cat] for yr in result.years}
            if any(v > 0 for v in vals.values()):
                series.append((cat, CATEGORY_LABELS[cat], base, HATCH.get(cat), vals))

    bottom = {y: 0.0 for y in years}
    handles = []
    for key, label, color, hatch, vals in series:
        heights = [vals[y] * scale[y] for y in years]
        if not any(h > 0 for h in heights):
            continue
        ax.bar(
            years, heights, bottom=[bottom[y] for y in years], width=0.78, color=color,
            edgecolor=palette.surface, linewidth=0.6, hatch=None, label=label,
        )
        if hatch:
            # Hatch in a contrasting ink, drawn as a second layer so the edge gap stays surface-colored.
            ax.bar(years, heights, bottom=[bottom[y] for y in years], width=0.78, color="none",
                   edgecolor=_mix(color, palette.text_primary if palette.mode == "light" else palette.surface, 0.45),
                   linewidth=0, hatch=hatch)
        handles.append(Patch(facecolor=color, edgecolor=_mix(color, palette.text_primary, 0.45) if hatch else color,
                             hatch=hatch, label=label))
        for y, h in zip(years, heights):
            bottom[y] += h

    # Threshold lines, drawn as steps (each year has its own value).
    bracket_keys = [k for k in result.line_labels if k.startswith("bracket:")]
    line_handles = []
    peak_bar = max(bottom.values(), default=0.0)
    shown_lines = [k for k in result.line_labels if visibility.get(k, False)]
    end_labels: list[tuple[float, str]] = []
    irmaa_keys = [k for k in result.line_labels if k.startswith("irmaa:")]
    for key in shown_lines:
        ys = [yr.lines.get(key) for yr in result.years]
        if all(v is None for v in ys):
            continue
        vals = [math.nan if v is None else v * scale[y] for v, y in zip(ys, years)]
        style = line_style(key, palette, bracket_keys, irmaa_keys)
        (ln,) = ax.plot(years, vals, drawstyle="steps-mid", label=result.line_labels[key], **style)
        line_handles.append(ln)
        last = next((v for v in reversed(vals) if not math.isnan(v)), None)
        if last is not None:
            end_labels.append((last, line_short_label(key)))

    # Y range: fit the bars, plus lines that are not far above them.
    line_top = 0.0
    for key in shown_lines:
        for yr in result.years:
            v = yr.lines.get(key)
            if v is not None and v * scale[yr.year] <= max(peak_bar * 1.6, 1.0):
                line_top = max(line_top, v * scale[yr.year])
    ax.set_ylim(0, max(peak_bar, line_top, 10_000) * 1.08)
    _year_axis(ax, result, palette)
    # Direct labels at the right end of each line, nudged apart so they never overlap.
    ymax = ax.get_ylim()[1]
    gap = ymax * 0.035
    placed: list[float] = []
    for v, text in sorted((e for e in end_labels if e[0] <= ymax), key=lambda e: e[0]):
        y = max(v, placed[-1] + gap) if placed else v
        placed.append(y)
        ax.text(years[-1] + 0.65, y, text, color=palette.text_secondary, fontsize=7.5,
                va="center", ha="left", clip_on=False)

    p = result.plan.profile
    dollars = f"today's ({result.plan.start_year}) dollars" if todays_dollars else "future (nominal) dollars"
    ax.set_title(_escape(f"Income by source and tax thresholds\n{_plan_label(result.plan)} · {dollars}"),
                 color=palette.text_primary, fontsize=11, loc="left")
    ax.set_ylabel("Income", color=palette.text_secondary)

    all_handles = handles[::-1] + line_handles
    notes = list(result.notes)
    if any(k.startswith("irmaa:") for k in shown_lines):
        notes.append("IRMAA lines: income in each year sets Medicare premiums two years later; "
                     "tiers for that premium year (projected where needed).")
    notes.append("Lines show the total income at which that year's return reaches each threshold, "
                 "allowing for Social Security taxation and deductions.")
    short = [yr.year for yr in result.years if yr.shortfall > 0]
    if short:
        notes.append(f"Spending not covered in {short[0]}–{short[-1]}: add a withdrawal rule.")
    _legend_and_notes(fig, all_handles, notes, palette, ncol=3, right=0.92)


def _legend_and_notes(fig: Figure, handles, notes: list[str], palette: Palette, *, ncol: int, right: float) -> None:
    """Stack, from the bottom: notes, legend, then the axes with room for two-line tick labels."""
    h = fig.get_figheight()
    notes_in = 0.08 + 0.125 * len(notes)
    rows = (len(handles) + ncol - 1) // ncol
    legend_in = 0.1 + 0.18 * rows if handles else 0.0
    ticks_in = 0.75
    if notes:
        fig.text(0.01, 0.01, _escape("\n".join(notes)), color=palette.text_secondary, fontsize=7, va="bottom")
    if handles:
        leg = fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, notes_in / h),
                         ncol=ncol, frameon=False, fontsize=7.5)
        for t in leg.get_texts():
            t.set_color(palette.text_primary)
    bottom = min(0.62, (notes_in + legend_in + ticks_in) / h)
    fig.subplots_adjust(left=0.08, right=right, top=1 - 0.75 / h, bottom=bottom)


def render_balances(
    fig: Figure,
    result: PlanResult,
    palette: Palette,
    *,
    detail: bool = False,
    todays_dollars: bool = False,
) -> None:
    fig.clear()
    fig.set_facecolor(palette.surface)
    ax = fig.add_subplot(111)
    _style(ax, palette)
    years = [yr.year for yr in result.years]
    scale = {yr.year: (1 / result.deflator(yr.year) if todays_dollars else 1.0) for yr in result.years}
    kinds = result.account_kinds
    groups: list[tuple[str, str, list[str], str]] = []  # key, label, account ids, kind
    for kind in ACCOUNT_ORDER:
        ids = [a for a, k in kinds.items() if k == kind]
        if not ids:
            continue
        if detail:
            groups += [(a, result.account_labels.get(a, a), [a], kind) for a in ids]
        else:
            groups.append((kind, ACCOUNT_KINDS[kind], ids, kind))
    bottom = {y: 0.0 for y in years}
    handles = []
    by_kind_count: dict[str, int] = {}
    for key, label, ids, kind in groups:
        heights = [sum(yr.balances_end.get(a, 0.0) for a in ids) * scale[yr.year] for yr in result.years]
        if not any(h > 0.5 for h in heights):
            continue
        base = palette.sources.get(ACCOUNT_COLOR_KEY[kind], palette.text_secondary)
        n = by_kind_count.get(kind, 0)
        by_kind_count[kind] = n + 1
        color = _shades(base, palette, n + 1)[n]
        hatch = ACCOUNT_HATCH.get(kind)
        ax.bar(years, heights, bottom=[bottom[y] for y in years], width=0.78, color=color,
               edgecolor=palette.surface, linewidth=0.6)
        if hatch:
            ax.bar(years, heights, bottom=[bottom[y] for y in years], width=0.78, color="none",
                   edgecolor=_mix(color, palette.text_primary if palette.mode == "light" else palette.surface, 0.45),
                   linewidth=0, hatch=hatch)
        handles.append(Patch(facecolor=color, hatch=hatch, edgecolor=_mix(color, palette.text_primary, 0.45) if hatch else color,
                             label=label))
        for y, h in zip(years, heights):
            bottom[y] += h
    ax.set_ylim(0, max(max(bottom.values(), default=0), 10_000) * 1.08)
    _year_axis(ax, result, palette)
    dollars = f"today's ({result.plan.start_year}) dollars" if todays_dollars else "future (nominal) dollars"
    ax.set_title(_escape(f"Account balances at year end\n{_plan_label(result.plan)} · {dollars}"),
                 color=palette.text_primary, fontsize=11, loc="left")
    ax.set_ylabel("Balance", color=palette.text_secondary)
    _legend_and_notes(fig, handles[::-1], list(result.notes), palette, ncol=4, right=0.97)


def tax_color(key: str, palette: Palette) -> str:
    if key == "irmaa":
        return palette.lines.get("irmaa", palette.irmaa)
    return palette.layers.get(TAX_COLOR_KEY[key], palette.text_secondary)


def render_taxes(
    fig: Figure,
    result: PlanResult,
    palette: Palette,
    *,
    todays_dollars: bool = False,
    **_kw,
) -> None:
    """Stacked bars of each year's taxes by source: federal ordinary income,
    capital gains and NIIT, state, and IRMAA surcharges."""
    fig.clear()
    fig.set_facecolor(palette.surface)
    ax = fig.add_subplot(111)
    _style(ax, palette)
    years = [yr.year for yr in result.years]
    scale = {yr.year: (1 / result.deflator(yr.year) if todays_dollars else 1.0) for yr in result.years}
    by_year = {yr.year: yr.tax_by_source() for yr in result.years}
    bottom = {y: 0.0 for y in years}
    handles = []
    totals: dict[str, float] = {}
    for key in TAX_SOURCES:
        heights = [max(0.0, by_year[y][key]) * scale[y] for y in years]
        if not any(h > 0.5 for h in heights):
            continue
        totals[key] = sum(heights)
        color = tax_color(key, palette)
        hatch = TAX_HATCH.get(key)
        label = TAX_SOURCE_LABELS[key]
        ax.bar(years, heights, bottom=[bottom[y] for y in years], width=0.78, color=color,
               edgecolor=palette.surface, linewidth=0.6, label=label)
        if hatch:
            ax.bar(years, heights, bottom=[bottom[y] for y in years], width=0.78, color="none",
                   edgecolor=_mix(color, palette.text_primary if palette.mode == "light" else palette.surface, 0.45),
                   linewidth=0, hatch=hatch)
        handles.append(Patch(facecolor=color, hatch=hatch,
                             edgecolor=_mix(color, palette.text_primary, 0.45) if hatch else color, label=label))
        for y, h in zip(years, heights):
            bottom[y] += h
    ax.set_ylim(0, max(max(bottom.values(), default=0), 10_000) * 1.08)
    _year_axis(ax, result, palette)
    dollars = f"today's ({result.plan.start_year}) dollars" if todays_dollars else "future (nominal) dollars"
    ax.set_title(_escape(f"Taxes by source each year\n{_plan_label(result.plan)} · {dollars}"),
                 color=palette.text_primary, fontsize=11, loc="left")
    ax.set_ylabel("Tax", color=palette.text_secondary)
    notes = []
    if totals:
        whole = sum(totals.values())
        short = {"ordinary": "federal ordinary", "ltcg": "federal capital gains", "niit": "NIIT",
                 "state": "state", "state_gains": "state capital gains", "irmaa": "IRMAA"}
        parts = ", ".join(f"{short[k]} {_money(v)}" for k, v in totals.items())
        notes.append(f"Lifetime total {_money(whole)}: {parts}." if len(totals) > 1
                     else f"Lifetime total {_money(whole)}.")
    if totals.get("irmaa"):
        notes.append("IRMAA surcharges are Medicare Part B and D premiums set by income two years earlier.")
    notes.append("FICA payroll taxes and local income taxes are not included.")
    _legend_and_notes(fig, handles[::-1], notes, palette, ncol=3, right=0.97)
