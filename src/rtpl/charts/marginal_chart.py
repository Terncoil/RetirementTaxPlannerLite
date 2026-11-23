"""Draw the marginal tax graph with matplotlib.

The renderer only needs a ``matplotlib.figure.Figure``, so the same code
draws inside the Qt window and in the headless command-line exporter.
"""

from __future__ import annotations

from dataclasses import replace

from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, PercentFormatter

from rtpl.engine.marginal import LAYERS, Annotation, MarginalSeries
from rtpl.model.profile import FILING_STATUS_LABELS
from rtpl.theme import Palette


def _money(v: float, _pos=None) -> str:
    if v < 0:
        return "-" + _money(-v)
    if abs(v) >= 1000:
        return f"${v / 1000:,.0f}K"
    return f"${v:,.0f}"


def _escape(text: str) -> str:
    """Stop matplotlib from reading dollar signs as math mode."""
    return text.replace("$", r"\$")


def render_marginal(
    fig: Figure,
    series: MarginalSeries,
    visibility: dict[str, bool],
    palette: Palette,
    *,
    show_total: bool = True,
) -> None:
    fig.clear()
    fig.set_facecolor(palette.surface)
    ax = fig.add_subplot(111)
    ax.set_facecolor(palette.surface)

    xs = list(series.x) + [series.x[-1] + series.step]
    shown = [k for k in LAYERS if visibility.get(k, False)]
    # Positive rates stack up from 0% and negative ones (a nonrefundable
    # credit absorbing tax) stack down from it, so a band never hides
    # another and the total line is their sum.
    up = [0.0] * len(series.x)
    down = [0.0] * len(series.x)
    handles = []
    for key in shown:
        vals = series.layers[key]
        drawn = None
        for sign, base in ((1, up), (-1, down)):
            part = [max(0.0, v) if sign > 0 else min(0.0, v) for v in vals]
            if not any(abs(v) > 1e-9 for v in part):
                continue
            top = [b + v for b, v in zip(base, part)]
            drawn = ax.fill_between(
                xs,
                base + [base[-1]],
                top + [top[-1]],
                step="post",
                color=palette.layers[key],
                linewidth=0.6,
                edgecolor=palette.surface,  # thin surface gap between bands
                label=series.layer_label(key),
            )
            base[:] = top
        if drawn is None:  # shown but zero everywhere: still in the legend
            drawn = ax.fill_between(xs, [0.0] * len(xs), [0.0] * len(xs), step="post",
                                    color=palette.layers[key], label=series.layer_label(key))
        handles.append(drawn)

    # The total follows what is shown, so hiding the state layer gives a
    # federal-only total.
    hidden_total = [u + d for u, d in zip(up, down)]
    if show_total and shown:
        (line,) = ax.step(
            xs,
            hidden_total + [hidden_total[-1]],
            where="post",
            color=palette.total_line,
            linewidth=1.4,
            label="Total marginal rate (shown layers)",
        )
        handles.append(line)

    ymax = max(max(hidden_total, default=0), max(up, default=0))
    ymin = min(min(down, default=0), min(hidden_total, default=0))
    ax.set_ylim(min(0, ymin * 1.1) - 0.01, max(0.10, ymax * 1.18 + 0.02))
    if ymin < -1e-9:
        ax.axhline(0, color=palette.text_secondary, linewidth=0.8)
    ax.set_xlim(0, xs[-1])
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.xaxis.set_major_formatter(FuncFormatter(_money))

    # Annotations: vertical lines with staggered labels so they do not collide.
    ann = [a for a in series.annotations if visibility.get(a.kind, False)]
    y_top = ax.get_ylim()[1]
    levels = [0.97, 0.86, 0.75]
    last_x_at_level: dict[int, float] = {}
    min_gap = xs[-1] * 0.09
    # State cliffs that come in runs (the steps of an exclusion) share one
    # label: "CT cliffs ×8, +$2,973 tax".
    labelled: list[Annotation] = []
    run = 0
    last_cliff_x = -1e18
    for a in sorted(ann, key=lambda a: a.x):
        color = {"irmaa": palette.irmaa, "state_cliff": palette.layers["state"]}.get(a.kind, palette.aca)
        style = (0, (1, 2)) if a.kind == "aca" else (0, (4, 3))
        ax.axvline(a.x, color=color, linewidth=1.2, linestyle=style)
        if a.kind == "state_cliff":
            prev = labelled[-1] if labelled else None
            if prev is not None and prev.kind == "state_cliff" and a.x - last_cliff_x < min_gap:
                run += 1
                total = (prev.annual_cost or 0.0) + (a.annual_cost or 0.0)
                code = prev.label.split(" ")[0]
                labelled[-1] = replace(prev, label=f"{code} cliffs ×{run}",
                                       sublabel=f"+${total:,.0f} tax", annual_cost=total)
                last_cliff_x = a.x
                continue
            run = 1
            last_cliff_x = a.x
        labelled.append(a)
    for a in labelled:
        level = 0
        while level < len(levels) - 1 and a.x - last_x_at_level.get(level, -1e18) < min_gap:
            level += 1
        last_x_at_level[level] = a.x
        near_right = a.x > xs[-1] * 0.88
        lines = [a.label] + ([a.sublabel] if a.sublabel else [])
        pad = lambda t: f"{t} " if near_right else f" {t}"  # noqa: E731
        ax.text(
            a.x,
            y_top * levels[level],
            _escape("\n".join(pad(t) for t in lines)),
            color=palette.text_primary,
            fontsize=8,
            va="top",
            ha="right" if near_right else "left",
            bbox=dict(boxstyle="square,pad=0.15", facecolor=palette.surface, edgecolor="none", alpha=0.85),
        )

    p = series.profile
    fs = FILING_STATUS_LABELS.get(p.filing_status, p.filing_status)
    title = (
        f"Marginal tax rate on additional {series.source.short_label}\n"
        f"{p.year} · {fs} · baseline AGI {_money(series.baseline.agi)}, "
        f"tax {_money(series.baseline.total_tax if visibility.get('state') else series.baseline.federal_tax)}"
    )
    ax.set_title(
        _escape(title),
        color=palette.text_primary,
        fontsize=11,
        loc="left",
    )
    ax.set_xlabel(f"Additional {series.source.label}", color=palette.text_secondary)
    ax.set_ylabel("Marginal tax rate", color=palette.text_secondary)
    ax.tick_params(colors=palette.text_secondary, labelsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(palette.grid)
    ax.grid(axis="y", color=palette.grid, linewidth=0.8)
    ax.set_axisbelow(True)

    if handles:
        leg = ax.legend(
            handles=handles,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.14),
            ncol=2,
            frameon=False,
            fontsize=8.5,
        )
        for t in leg.get_texts():
            t.set_color(palette.text_primary)

    notes = list(series.notes)
    if visibility.get("state") and series.state is not None:
        notes.extend(f"{series.state.code}: {n}" for n in series.state.chart_notes)
    if visibility.get("irmaa") and series.irmaa_note and any(a.kind == "irmaa" for a in ann):
        notes.append(series.irmaa_note)
    if notes:
        fig.text(0.01, 0.01, _escape("\n".join(notes)), color=palette.text_secondary, fontsize=7.5, va="bottom")
    fig.subplots_adjust(left=0.08, right=0.98, top=0.88, bottom=0.30)
