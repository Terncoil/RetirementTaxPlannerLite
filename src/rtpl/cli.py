"""Command-line entry point: render a marginal graph without the GUI.

Example::

    rtpl-chart examples/profiles/retired_couple_ca.rtpl -o chart.png --source ltcg
"""

from __future__ import annotations

import argparse
import sys

from rtpl.engine.marginal import ANNOTATIONS, LAYERS, build_marginal_series, default_visibility
from rtpl.engine.tax import IncomeSource
from rtpl.model.profile import Profile
from rtpl.taxlaw.loader import TaxLawLibrary
from rtpl.theme import Theme


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render a marginal tax graph.")
    ap.add_argument("profile", help="Profile JSON file")
    ap.add_argument("-o", "--output", required=True, help="Output image (.png, .svg or .pdf)")
    ap.add_argument("--source", choices=[s.value for s in IncomeSource], default="ordinary")
    ap.add_argument("--x-max", type=float, default=250_000)
    ap.add_argument("--step", type=float, default=100)
    ap.add_argument("--inflation", type=float, help="Override the profile's inflation rate, e.g. 0.03")
    ap.add_argument(
        "--irmaa-basis", choices=["premium_year", "current_year"], default="premium_year",
        help="IRMAA brackets for the premium year two years out (default) or this year's",
    )
    ap.add_argument("--mode", choices=["light", "dark"], default="light")
    ap.add_argument("--theme", help="Theme TOML overriding the default colors")
    ap.add_argument("--tax-law-dir", action="append", default=[], help="Extra tax-law folder")
    ap.add_argument(
        "--show", action="append", default=[], choices=[*LAYERS, *ANNOTATIONS],
        help="Force a layer or annotation on (repeatable)",
    )
    ap.add_argument(
        "--hide", action="append", default=[], choices=[*LAYERS, *ANNOTATIONS],
        help="Force a layer or annotation off (repeatable)",
    )
    ap.add_argument("--table", action="store_true", help="Also print where the rate changes")
    args = ap.parse_args(argv)

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    from rtpl.charts.marginal_chart import render_marginal

    profile = Profile.load(args.profile)
    problems = profile.validate()
    if problems:
        print("Profile problems: " + "; ".join(problems), file=sys.stderr)
        return 2
    if args.inflation is not None:
        profile.inflation = args.inflation
    lib = TaxLawLibrary(user_dirs=args.tax_law_dir)
    series = build_marginal_series(
        profile, lib, source=IncomeSource(args.source), x_max=args.x_max, step=args.step,
        irmaa_basis=args.irmaa_basis,
    )
    vis = default_visibility(series)
    vis.update({k: True for k in args.show})
    vis.update({k: False for k in args.hide})
    fig = Figure(figsize=(10, 6.2), dpi=150)
    render_marginal(fig, series, vis, Theme.load(args.theme).palette(args.mode))
    fig.savefig(args.output, facecolor=fig.get_facecolor())
    if args.table:
        for x, r in series.rate_changes():
            print(f"${x:>10,.0f}  {r:6.1%}")
        for a in series.annotations:
            if vis.get(a.kind):
                print(a.detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
