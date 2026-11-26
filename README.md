# RetirementTaxPlannerLite

Free, open-source, cross-platform desktop app for **marginal tax graphs**
and **forward-looking tax projections** of the kind retirement planners
show their clients.

![Marginal tax graph for a retired couple in California](docs/images/marginal-example.png)

The marginal graph shows the real tax rate on each extra dollar of income,
split into layers: ordinary brackets, the Social Security taxation
"tax torpedo", the enhanced senior deduction phase-out, capital gains and
qualified dividends, NIIT and an optional state layer, with IRMAA and ACA
cliff lines on top.

The forward-looking graph shows each year's income by source (Social
Security, pensions, RMDs, inherited IRA withdrawals, Roth conversions,
gains, tax-free withdrawals) against the top of each tax bracket, IRMAA
tiers and the ACA cliff, with account balances on a second tab. A plan
is a profile plus accounts, income streams, Social Security claiming ages
and a short list of rules such as "convert to the top of the 22% bracket
from 63 to 70".

![Forward-looking graph for an example plan](docs/images/forward-example.png)

> **Disclaimer.** This is a planning aid, not tax, legal, investment or
> financial advice. Tax law is simplified (see [docs/design.md](docs/design.md))
> and the tax-law files can contain mistakes or be out of date. Check the
> numbers against official sources, and talk to a qualified professional
> before you act on them. The software comes with no warranty; see
> [License](#license).

## Run it

With [uv](https://docs.astral.sh/uv/) (any OS, including Windows):

```bash
uv sync --extra dev              # creates .venv and installs the app plus pytest
uv run rtpl                      # opens the desktop app
uv run pytest                    # runs the tests
uv run rtpl-chart examples/profiles/retired_couple_ca.json -o chart.png
uv run rtpl-plan examples/plans/couple_conversions_inherited.json -o income.png --balances balances.png
```

With plain pip (`.` means "this folder's pyproject.toml", `-e` makes it
editable, and `[dev]` adds the test tools):

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
rtpl                             # opens the desktop app
```

Or draw a chart without the window:

```bash
rtpl-chart examples/profiles/retired_couple_ca.json -o chart.png
rtpl-chart examples/profiles/retired_couple_ca.json -o chart.svg --source ltcg --mode dark --table
```

Run the tests with `pytest`.

For a desktop or Start-menu shortcut with the app icon, run
`uv run rtpl-shortcut`. To build a stand-alone app for Windows, macOS or
Linux, see [docs/packaging.md](docs/packaging.md). Ready-made builds are on
the Releases page for each version, and GitHub builds fresh ones after every
change to `main`.

## What's in the box

* Profile editor: tax year, filing status, state, one or two people
  (birth year, Medicare Part B enrolled, Social Security, wages, pension,
  IRA withdrawals) and household investment income.
* Graph options: income source (ordinary, qualified dividends, long-term
  gains), X range (default $0–$250K), and a checkbox per layer and
  annotation with suggested defaults from the profile.
* Light, dark or system theme; chart colors editable per layer.
* Fits small screens (down to 1024x768): the inputs and graph options are
  side panels that stay open beside the graph, or with **Auto hide** open
  over it only when needed (Ctrl+1, Ctrl+2). The window remembers its size,
  position and panel layout; View > Reset Window Layout forgets them.
* A data table that lists every rate change and threshold, for screen
  readers and for copying.
* Export a chart to PNG, SVG or PDF, or File > Export Report for one PDF
  with the profile and every chart (the forward plan's too when a plan is open).

## Forward-looking plans

In the app, use **File > Open** (try
`examples/plans/couple_conversions_inherited.json`) or **File > New Plan
from Profile**, then the **Forward plan** tab. The left panel's **Plan
items** tab lists the accounts, income, Social Security, strategies (Roth
conversions, withdrawals, qualified charitable distributions and HSA contributions) and assumptions, each with a bar for the
years it covers, under a **Plan name** for this version of the plan ("Roth
conversions until Social Security"). Select one to change it in the form below the list, add
or delete items, or untick one to see the plan without it; the graph and
totals update as you type. One profile, and at most one plan, is open at
a time. **File > Open** opens either kind of file. **File > Save** and
**Save As** save the plan when one is open (a plan file holds its profile
too), otherwise the profile; **Save Profile As** saves just the profile.
New, Open, New Plan from Profile and Exit ask first about unsaved changes.

Profiles and plans are saved as `.rtpl` files. Files saved as `.json` by
earlier versions still open, and their first **Save As** offers the
`.rtpl` name. Double-clicking a `.rtpl` file opens it in the app once the
file type is registered: the macOS app does that itself,
`sh packaging/linux/install.sh` does it for a Linux build, and on Windows
(or when running from source) use **View > Open .rtpl Files With This App** (a checked item that shows whether this copy opens `.rtpl` files; untick to undo)
or `uv run rtpl-shortcut`. The app also opens a file named on its command
line, such as `uv run rtpl "Smith plan.rtpl"`.

From the command line:

```bash
rtpl-plan plan.json -o income.png --balances balances.png --taxes taxes.png --csv years.csv
rtpl-plan plan.json -o income.svg --detail --todays-dollars --line bracket:0.32
```

See "Forward-looking plan" in [docs/design.md](docs/design.md) for every
field, the yearly steps and the simplifications.

## Tax law files

Federal law for each year is in `src/rtpl/data/federal/<year>.toml`, states
in `src/rtpl/data/states/<year>/<code>.toml`. Later federal years are
projected with inflation using `projection.toml`; a state year without a
file uses the newest earlier one. To change the law without editing the
package, put your own copy of a file in the `taxlaw/federal` or
`taxlaw/states` folder of your user config directory; it wins over the
shipped one.

Included: federal 2020 to 2027, and state files for 2020 to 2026 for all
50 states plus DC (brackets, deductions and exemptions from the Tax
Foundation's table for each year, corrected for cuts passed later in the
year that applied to all of it, plus Social Security and pension/IRA exclusions for retirees,
Washington's capital gains tax, New Hampshire's interest and dividends tax
to 2024 and Tennessee's Hall tax in 2020). Each state file lists what it leaves out
under `notes`; hover over the state checkbox in the app to read them.

See [docs/design.md](docs/design.md) for how everything works.

## License

RetirementTaxPlannerLite is free software: you can redistribute it and/or
modify it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or (at your
option) any later version. It is distributed in the hope that it will be
useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE)
for the full text.

The stand-alone builds bundle third-party libraries under their own
licenses, including Qt and PySide6 (LGPL-3.0) and matplotlib (PSF-based
matplotlib license).
