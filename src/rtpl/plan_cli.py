"""Command-line entry point for the forward-looking plan.

Example::

    rtpl-plan examples/plans/couple_conversions_inherited.json -o income.png --balances balances.png --taxes taxes.png
    rtpl-plan plan.json -o income.svg --detail --todays-dollars --csv years.csv
"""

from __future__ import annotations

import argparse
import csv
import sys

from rtpl.model.plan import Plan
from rtpl.taxlaw.loader import TaxLawLibrary
from rtpl.theme import Theme


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render forward-looking plan graphs.")
    ap.add_argument("plan", help="Plan JSON file")
    ap.add_argument("-o", "--output", help="Income graph (.png, .svg or .pdf)")
    ap.add_argument("--balances", help="Also draw account balances to this file")
    ap.add_argument("--taxes", help="Also draw taxes by source to this file")
    ap.add_argument("--csv", help="Write the year-by-year table to this CSV file")
    ap.add_argument("--detail", action="store_true", help="Show each income source and account separately")
    ap.add_argument("--todays-dollars", action="store_true", help="Show amounts in the plan's first-year dollars")
    ap.add_argument("--no-tax-free", action="store_true", help="Hide tax-free withdrawals from the bars")
    ap.add_argument("--line", action="append", default=[],
                    help="Force a line on, e.g. bracket:0.32, irmaa:2, aca:4, cg0, deduction (repeatable)")
    ap.add_argument("--hide-line", action="append", default=[], help="Force a line off (repeatable)")
    ap.add_argument("--all-columns", action="store_true",
                    help="Year table with every column (otherwise the plan's chosen columns)")
    ap.add_argument("--mode", choices=["light", "dark"], default="light")
    ap.add_argument("--theme", help="Theme TOML overriding the default colors")
    ap.add_argument("--tax-law-dir", action="append", default=[], help="Extra tax-law folder")
    args = ap.parse_args(argv)

    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    from rtpl.charts.forward_chart import render_balances, render_income, render_taxes
    from rtpl.engine.forward import (
        TABLE_COLUMN_KEYS,
        default_line_visibility,
        run_plan,
        year_table,
    )

    plan = Plan.load(args.plan)
    problems = plan.validate()
    if problems:
        print("Plan problems: " + "; ".join(problems), file=sys.stderr)
        return 2
    result = run_plan(plan, TaxLawLibrary(user_dirs=args.tax_law_dir))
    palette = Theme.load(args.theme).palette(args.mode)
    vis = default_line_visibility(result)
    vis.update({k: True for k in args.line})
    vis.update({k: False for k in args.hide_line})
    vis["tax_free"] = not args.no_tax_free
    if args.output:
        fig = Figure(figsize=(12, 7.5), dpi=150)
        render_income(fig, result, vis, palette, detail=args.detail, todays_dollars=args.todays_dollars)
        fig.savefig(args.output, facecolor=fig.get_facecolor())
    if args.balances:
        fig = Figure(figsize=(12, 6.5), dpi=150)
        render_balances(fig, result, palette, detail=args.detail, todays_dollars=args.todays_dollars)
        fig.savefig(args.balances, facecolor=fig.get_facecolor())
    if args.taxes:
        fig = Figure(figsize=(12, 6.5), dpi=150)
        render_taxes(fig, result, palette, todays_dollars=args.todays_dollars)
        fig.savefig(args.taxes, facecolor=fig.get_facecolor())
    columns = list(TABLE_COLUMN_KEYS) if args.all_columns else plan.table_columns
    header, rows = year_table(result, todays_dollars=args.todays_dollars, columns=columns, detail=args.detail)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
    if not (args.output or args.balances or args.taxes or args.csv):
        for row in rows:
            print(" | ".join(row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
