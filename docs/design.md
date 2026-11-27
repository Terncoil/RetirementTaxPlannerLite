# RetirementTaxPlannerLite design

Status: the marginal tax graph, and a first version of the forward-looking
plan (engine, graphs, command line, and an app page with an editor for
the plan's items). Next for the editor: per-year overrides typed into the
year table, named milestones, scenarios to compare, and a setup wizard.

## Goals

* A free, open-source desktop app (Windows, macOS, Linux) that draws the
  kinds of tax graphs retirement planners show clients: first the
  **marginal tax graph**, then the **forward-looking (year-by-year) graph**.
* Native look, keyboard and screen-reader support, not a browser app.
* Tax law lives in data files people can read and edit, with policy
  switches for things like the senior deduction's expiry.
* Reasonable defaults that hide what cannot matter for a household, while
  letting the user turn any layer back on.

## Stack

| Piece | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Familiar to the author; `tomllib` in the standard library |
| UI | PySide6 (Qt 6) | Uses UIA / NSAccessibility / AT-SPI, standard menus and shortcuts |
| Charts | matplotlib | Stacked step areas, annotations, PNG/SVG/PDF export |
| Data | TOML (tax law, themes), JSON (profiles) | Hand-editable, comment-friendly |
| Packaging (later) | PyInstaller or Briefcase in GitHub Actions | One download per OS |

## Layout of the code

```
src/rtpl/
  model/profile.py        Profile, Person, Household (JSON)
  taxlaw/loader.py        FederalLaw, StateLaw, TaxLawLibrary (TOML + projection)
  engine/tax.py           compute_tax(): one year, one household
  engine/marginal.py      build_marginal_series(), annotations, default visibility
  model/plan.py           Plan: accounts, income streams, Social Security, rules (JSON)
  engine/forward.py       PlanEngine: the year-by-year loop, threshold lines, year table
  engine/rmd.py           RMD tables (Uniform, Joint and Last Survivor, Single Life)
  engine/social_security.py  own, spousal and survivor benefit rules
  charts/marginal_chart.py  render_marginal() onto any matplotlib Figure
  charts/forward_chart.py   render_income(), render_balances()
  theme.py                light/dark palettes from TOML
  model/eqta.py           Estimated Quarterly Tax Assistant files (.eqta)
  engine/estimated.py     EQTA worksheet: tax, paid, owed and guidance by period
  app/                    Qt windows (RTPL and EQTA) and their forms
  cli.py                  headless marginal chart export (rtpl-chart)
  plan_cli.py             headless plan export: graphs and CSV (rtpl-plan)
  data/federal/*.toml     per-year federal law + projection rules
  data/states/YYYY/*.toml per-state law by tax year
  data/rmd/tables.toml    IRS life expectancy tables
  data/themes/default.toml
```

The engine never imports Qt, so it is testable on its own and reusable by
the forward-looking plan and the command-line exporter.

## The three setup areas

1. **Baseline (the profile).** Tax year, filing status, state (optional),
   one or two people with birth year, a *Medicare Part B enrolled* choice
   (Automatic / Yes / No, so SSDI recipients under 65 get IRMAA), Social
   Security, wages, pension and IRA withdrawals; household interest,
   dividends, gains, other income, adjustments, itemized deductions, cash
   gifts to charity (deducted up to a cap alongside the standard deduction
   from 2026; below the line, so AGI and every MAGI are unchanged), and
   switches for *has a taxable brokerage account* and *buys ACA coverage*.
   For the ACA premium tax credit, each person's monthly benchmark Silver
   premium and the premium of the plan they'd buy, plus the plan's metal
   tier; for education credits, the number of college students (American
   Opportunity credit), expenses per student, Lifetime Learning expenses,
   and the last year the forward plan claims them.
2. **Tax law (data files).** One TOML file per federal tax year, and one per
   state. See below.
3. **The plan (forward graph).** Accounts, income streams, Social Security
   claiming, life expectancy, assumptions and a short rule list. See
   "Forward-looking plan" below.

## Tax law data

`data/federal/<year>.toml` (2020 through 2027) holds brackets, standard deductions, the
additional 65+ deduction, the OBBBA senior deduction (amount, 6% phase-out,
first and last year), capital gains bands, NIIT, Social Security
thresholds, IRMAA tiers for that premium year, ACA poverty-line
reference points and the premium tax credit's applicable percentage table
(`[aca] applicable_percentage`: band start as a multiple of the poverty
line, initial and final share of income), and the education credits
(`[education_credits]`: AOTC and Lifetime Learning amounts and MAGI
phase-outs, fixed in statute), and from 2026 the charitable deduction for
non-itemizers (`[charitable]`: first year and cap by filing status, not
indexed; no state allows it, since every state here starts from federal
AGI). A `[policy]` table holds switches:

* `personal_exemption` (0 under TCJA, kept by OBBBA)
* `senior_deduction` (on for 2025–2028)
* `aca_enhanced_subsidies` (true for 2025; false for 2026, which brings
  back the 400% FPL cliff)

**State files** (`data/states/<year>/XX.toml`, all 50 states plus DC for
2020 to 2026) hold brackets, standard deduction, personal exemptions (as deductions or
credits), a Social Security rule (exempt, taxed, exempt from an age, or
exempt below an AGI limit with an optional phase-out), per-person
pension/IRA exclusions with minimum ages and qualifying birth years, an
age-based deduction, capital
gains exclusions or rate caps, a gains-only tax (Washington), a tax on
interest and dividends only (`[investment_income_tax]`: New Hampshire to
2024, Tennessee's Hall tax in 2020), and
nonrefundable `[[credits]]` (a flat amount, an amount per person, a share
of the federal deduction, or a share of the Social Security the state
taxes; of credits in the same `group` only the largest counts). hoh and
mfs use the single values unless a file lists them.

The standard deduction, exemption, age deduction, each retirement
exclusion, each credit and the 55-64 Social Security exemption can take a
`phaseout`, all in one format: a `start` by filing status and one shape,
`rate` (dollars lost per dollar over, e.g. Utah's 1.3% taxpayer credit
phase-out), `width` (a straight line to zero, 0 for a cliff), `step` and
`step_fraction` (e.g. 10% for each $4,000), or `tiers` (share kept up to
each income limit), plus `keep` (share left once phased out) and `income`
(`agi`, `magi` = AGI plus tax-exempt interest, `agi_ex_ss` = AGI less
taxable Social Security, or `state_income` = the state's income before its
deductions; exclusions can only use the first three).

Where a file approximates the law on purpose (Minnesota's Social Security
phase-out and Connecticut's exemption phase-out are smoothed into straight
lines rather than steps), its `chart_notes` say so in the footnotes under
the marginal graph while the state layer is shown. The note sits next to
the rule in the same file, so whoever enters the exact rule removes it.

**User overrides.** The app also reads a `taxlaw` folder in the user's
config directory; a file there replaces the shipped file for that year or
state. Nothing needs code changes.

**Older years (2020–2024).** The federal files are table additions only:
the single-year tax math needs no code for them (the senior deduction is
switched off and ARPA's ACA rules are set by `[policy]`). Three things the
forward plan does not model yet for plans that start in those years:

* 2020: the CARES Act waiver of all RMDs.
* 2020–2021: the older IRS life expectancy tables (the shipped tables took
  effect in 2022), so RMDs in those years come out a little small.
* 2021–2024: the IRS waivers of yearly inherited IRA RMDs under the
  10-year rule (owner had started RMDs, death in 2020 or later).

**State law by year.** Each state's files sit in a folder per tax year.
A year without a file uses the newest earlier one (so a state only needs
a file in the years its law changed, and a future year keeps the newest
law), or the earliest one for a year before them all; the marginal graph
then says in a footnote which year's law it used. State amounts are not
projected with inflation. A `states/XX.toml` outside a year folder in the
user's `taxlaw` folder replaces that state's shipped files for every year.

A retirement exclusion can be limited to birth years (`born_from`,
inclusive, and `born_before`), judged by the person or, with
`birth_year_of = "older_spouse"`, by the older spouse; `applies_to =
["any"]` makes it a deduction against all income; and of the exclusions
that share a `group` the return uses the one that excludes the most.
Michigan uses all three for its tiers and the 2023-2025 phase-in.
A phase-out normally scales the capped amount; `phased_share_of =
"income"` scales all the eligible income instead, with no cap, as New
Jersey's tiers above $100K take 37.5% (then 18.75%) of all pension income.

The 2020-2025 files take brackets, standard deductions and exemptions
from the Tax Foundation's table for each year (as of January 1), with
rate cuts passed later that applied to the whole year (Arkansas, Georgia,
Kansas, North Dakota, Ohio, South Carolina, Utah, West Virginia and
others) put in their place. Retiree rules follow each state's history
where it changed: Social Security exemptions (Colorado, Kansas,
Minnesota, Missouri, Nebraska, New Mexico, North Dakota, Rhode Island,
Vermont, West Virginia, Utah's credit), retirement exclusions
(Connecticut, Iowa, Michigan, New Jersey, Rhode Island, Wisconsin) and
Washington's capital gains tax from 2022. Each file's `notes` say what is
approximate.

States with no tax on wages are listed in `states/_no_income_tax.toml`.
One that taxed interest and dividends in earlier years has year files for
those years, and its `[no_tax_from]` entry gives the first year with no
tax at all (NH 2025, TN 2021), so later years don't fall back to its last
file. `StateLaw.has_any_tax` says whether the state taxes any income in
the law's year (wages, gains only or interest and dividends only), and
`has_state_layer` whether any of it is in the State Tax layer.

Such a tax is figured on the profile's interest and dividends (the kinds
the file's `income` lists) plus the household's signed
`state_investment_income_adjustment`: negative for interest the state
exempts but the profile counts (U.S. Treasury interest, the state's own
municipal bonds), positive for interest it taxes that the profile leaves
out (Tennessee's corporate bond interest). Nothing else uses it. In the
profile form the field is enabled only for a state and year with such a
tax, and its tooltip names that tax and rate from the year's file.

**Estimated payments.** Each federal year file and each state file for a
year the state taxes any income has an `[estimated_payments]` table
(`rtpl.taxlaw.estimated`), for the Estimated Quarterly Tax Assistant:

* `required`: whether individuals must make estimated payments (Idaho and
  Utah don't; Washington's capital gains tax and Tennessee's 2020 Hall
  tax are paid with the return).
* `installments`: four (periods ending Mar 31, May 31, Aug 31, Dec 31) or
  one (the whole year), each a `share` of the year's required payment and
  a `due` month and day. A due date falls in the tax year when it is after
  the period's end and otherwise in the next year. California's shares are
  30/40/0/30 with no September date; Delaware's first installment is due
  April 30, Virginia's May 1, Hawaii's on the 20th and Iowa's on the last
  day of the month. An installment's `period_end` replaces the usual end
  of its period: Virginia's first period runs to April 30 (Form 760C,
  23VAC10-112-43), so income through April 30 counts toward May 1. Every
  other state uses the usual period ends, later due dates included.
* `annual_only`: one payment with the return (Washington).
* `holidays`: the days a due date moves past, besides weekends. The
  default, `federal`, is the District of Columbia's legal holidays that
  IRC 7503 counts (Emancipation Day and Inauguration Day included), which
  the states follow too; Massachusetts and Maine add Patriots' Day, and
  Hawaii lists its own state holidays (no Juneteenth or Emancipation Day).
* `due_date`: an exact date for the file's own year only, for a
  postponement (2020's COVID-19 relief, Washington's May 1, 2026). A later
  year that falls back to the file uses the `due` rule again, and the
  projected federal years drop it.
* `notes`: annotations for EQTA, such as who must pay.
* `[estimated_payments.safe_harbor]` (federal files only): the shares of
  this year's and last year's tax that avoid the underpayment penalty
  (90%, 100%, or 110% when last year's AGI was over `high_income_agi`),
  and `penalty_floor`, the balance after withholding under which no
  penalty applies ($1,000). Fixed in statute, so projected years keep them.

`period_ends(year)` gives the last day of income each installment
covers, `due_dates(year)` each installment's date (None when nothing is
due), `differs_from(federal, year)` which ones differ from the federal
date for the same period, and `annualized_percentages()` the annualized
method's cumulative percentages (California 27/63/63/90, federal
22.5/45/67.5/90). Disaster postponements for some counties only are not
applied; a note says so where one was statewide in effect (California 2023).

**Projection.** Years without a file are built from the newest enacted
year by `federal/projection.toml`: indexed amounts (brackets, standard
deductions, capital gains bands, IRMAA thresholds and premiums, poverty
line) grow with inflation and round down like the IRS does; statutory
amounts (NIIT and Social Security thresholds, the senior deduction, the
education credits) stay fixed, and so does the newest ACA applicable
percentage table (the IRS indexes it to premium growth, which isn't
projected); `[sunsets]` turns policies off after their last year. Projected law
is labelled as projected on the chart.

## The marginal graph

The X axis is extra income of a chosen kind, $0–$250K by default:

* **Ordinary income** (treated as an IRA withdrawal or Roth conversion, the
  common retiree case: not investment income, but exempt in states that
  exempt retirement income)
* **Qualified dividends**
* **Long-term capital gains**

For each $100 step (finer steps for wider ranges), the engine recomputes the
whole return and splits the change in tax into layers using counterfactual
recomputation, so the layers always sum exactly to the real change:

| Layer (stack order, bottom up) | How it is isolated |
|---|---|
| Tax on Ordinary Income | ordinary tax change with taxable SS and the senior deduction frozen |
| Social Security Taxation Phase-In | extra federal income tax (ordinary and gains) from newly taxable SS, senior deduction frozen |
| Capital Gains and Qualified Dividends Tax | gains tax change with taxable SS and the senior deduction frozen: the 0/15/20% rate on added gains, or gains pushed out of the 0% band by added ordinary income |
| Enhanced Senior Deduction Phase-Out | the remaining federal income tax change, including gains it pushes out of the 0% band |
| Education Credit Phase-Out | education credits lost to the MAGI phase-out, nonrefundable (as far as there was tax to use it) and refundable together |
| Net Investment Income Tax | change in NIIT |
| State Tax (XX) | change in state income tax, labelled with the state code; includes a tax on interest and dividends only (NH, TN) |
| State CG&QD Tax (XX) | change in tax under the state's own capital gains rules: Washington's gains tax, Montana's gains rates, Hawaii's gains rate cap (states that tax gains as regular income show them in State Tax) |
| ACA Premium Tax Credit Phase-Out | premium tax credit lost on each extra dollar of ACA MAGI, leaving out the jumps at 100% and 400% FPL (those are lines) |
| Nonrefundable Credit Absorbing Tax (drawn below 0%) | with the credit held at its starting amount, the extra tax it now cancels: negative, so with income tax below the credit the ordinary layer shows the bracket rate and this cancels it |

**Credits.** Education credits follow Form 8863: the American Opportunity
credit is 100% of the first $2,000 and 25% of the next $2,000 of each
student's expenses, 40% refundable; the Lifetime Learning credit is 20% of
up to $10,000 a return, nonrefundable. Both phase out over MAGI $80,000 to
$90,000 ($160,000 to $180,000 joint; married filing separately can't
claim them). The nonrefundable part can only offset regular income tax
(not NIIT), so a household whose gains sit in the 0% band can't use it
until added income creates tax. The chart then goes below 0%: the
ordinary layer shows the bracket rate and the credit layer cancels it, so
the total stays at 0% until the credit is used up. Positive layers stack
up from 0% and negative ones down from it; the total line is their sum.

**The premium tax credit** is the yearly benchmark premium (the
second-lowest-cost Silver plan, summed over the people on marketplace
coverage and not on Medicare) less the applicable percentage of ACA MAGI,
but no more than the premium of the plans bought (so a cheaper Bronze plan
can be fully covered at low incomes, with no phase-out until the credit
falls below its price). There's no credit below 100% FPL, above 400% FPL
without the enhanced credits, or for married filing separately. The
expected share of income rises in a straight line inside each band, so
this layer's rate drifts rather than stepping; the data table starts a
new row when it moves a whole point. It is a premium subsidy, not a tax,
but each extra dollar of income costs it the same way, so it counts in
the total; it is kept out of federal tax everywhere else. The whole year
counts: someone turning 65 is taken off at the start of that year.

Knock-on effects are charged to their cause. Example: a married couple,
both over 65, adds long-term gains while the senior deduction is phasing
out. Each extra dollar shrinks the deduction by 12 cents, which adds 12
cents of ordinary income (12% bracket) and pushes 12 cents of existing
gains out of the 0% band (15%). The graph shows 15% in the capital gains
layer and 0.12 × (12% + 15%) = 3.24% in the senior deduction layer.

The total line is the sum of the *shown* layers, so turning off the state
layer gives a federal-only picture.

**Annotations** are cliffs, where a dollar of income costs hundreds or
thousands of dollars at once, so they are vertical lines rather than part
of the rate:

* **IRMAA.** Income in year *Y* sets Medicare premiums in *Y + 2*. The line
  sits where MAGI (AGI + tax-exempt interest) crosses each tier for the
  premium year, using projected thresholds when that year has no file. The
  label gives the added yearly premium for the people who will be enrolled.
  Projection uses the profile's *Inflation rate* (default 2.5%): the
  thresholds are CPI-indexed and rounded to $1,000, and the top tier
  ($500K / $750K) is frozen in statute until it starts indexing in 2028.
  A *IRMAA brackets* option switches to this year's published brackets
  for comparison with tools that show those.
* **ACA.** Lines at 138% FPL (Medicaid in expansion states), 250% (cost
  sharing reductions, only for a Silver plan) and, when enhanced credits
  are off, the 400% subsidy cliff, using ACA MAGI (AGI + tax-exempt
  interest + untaxed Social Security). With benchmark premiums entered,
  the cliff line is labelled with the credit lost there in one dollar.
* **State tax cliffs.** Where state tax jumps (an exemption, exclusion or
  credit ends or steps down, such as New Mexico's Social Security
  exemption above $150K or New Jersey's pension exclusion tiers), the
  jump is taken out of that step's state rate and drawn as a line
  labelled with its cost. A step whose state rate tops both neighbours by
  2 points is searched by bisection for a jump of $1 or more. Lines close
  together (Connecticut's pension deduction steps) share one label with
  the count and the total.

**Default visibility** (every item is a checkbox the user can override;
*Reset to suggested* undoes overrides):

| Item | Shown by default when |
|---|---|
| Ordinary income tax | always |
| SS phase-in, senior phase-out, education credit phase-out, ACA premium tax credit phase-out, nonrefundable credit, NIIT | the layer is non-zero somewhere on the graph |
| Capital gains / QD | brokerage account, existing gains or dividends, or a gains/dividends source |
| State | a state with an income tax is entered |
| IRMAA | someone is on Medicare by the premium year (age 63+ now, or Part B enrolled) |
| ACA | the household buys marketplace coverage and someone is under 65 and not on Medicare |
| State tax cliffs | the graph has one |

## Estimated Quarterly Tax Assistant

A second kind of window (`app/eqta_window.py`) for paying tax as you go,
in the same program and process as the RTPL window. `app/windows.py`
makes each window when first needed, routes each file by its extension
and adds a Window menu to both (Ctrl+Alt+1 RTPL, Ctrl+Alt+2 EQTA).

**File** (`model/eqta.py`, `.eqta`, JSON with `"kind": "eqta"`): New
Year Defaults (the qualified share for new ordinary-dividend sources, and
whether Clone Year keeps each source's share or sets it to a percentage),
then the years. Each year has a tax year, a description, RTPL's `Profile`
(filing status, state and people; the income fields are filled from the
registers), dependents (a tag only), last year's tax and AGI if typed,
notes, the income sources and the four payment lines. An income source has
a name, a tax type, an optional person, an annual plan amount, notes, a
qualified share (dividends to split only) and a register of date and amount
rows. Amounts are stored as typed (`3662.90*12`) and evaluated with
`evaluate_amount`: numbers, `+ - * /` and parentheses only.

Tax types and the profile field each fills: Social Security, wages,
pension and IRA distributions go to the person (the first person when
none is chosen, since state exclusions and Social Security are per
person); taxable and tax-exempt interest, qualified and non-qualified
dividends, long- and short-term gains (negative allowed, netted as
Schedule D does), other ordinary income, HSA contributions
(`hsa_deduction`), deductible IRA contributions and other above-the-line
deductions (`adjustments`), charitable gifts (the non-itemizer deduction)
and itemized deductions go to the household. Ordinary dividends to split
go to qualified and non-qualified dividends by the source's share, and
show in both groups on the worksheet, marked as estimated.

**Worksheet** (`engine/estimated.py`): each column runs `compute_tax` on
the year's profile with each source totalled through the column's date.

* Income counts when dated on or before the period end: March 31, May 31,
  August 31, December 31. The state lines use the state's own period ends
  (Virginia's first period ends April 30), so the state tax for that
  column is on income through April 30.
* Estimated tax: the tax on that income as if the year ended then.
* Paid: each payment register through the period's due date (the state's
  own date for the state lines; where the state has no installment for a
  period, as in California's September, the federal date). Owed is tax
  less paid. The Plan column uses the plan amounts throughout.
* The state section shows whenever the year's state taxes any income
  (`StateLaw.has_any_tax`), Washington's gains-only tax included. A due
  date that differs from the federal one is marked with a footnote, and an
  annual-only tax shows one due date.

Guidance, dimmed and for information only:

* Required annual payment: the smaller of 90% of this year's tax (the
  Plan column when any source has a plan amount, otherwise December 31)
  and 100% or 110% of last year's (typed in the Profile, or else last
  year's tab at December 31). Shares and limits come from the federal
  file's `[estimated_payments.safe_harbor]`.
* Annualized method (Form 2210 Schedule AI): income through each period
  end times 12 / months (4, 2.4, 1.5, 1; 3 for Virginia's April 30 state
  period), taxed with the full standard deduction, times the cumulative
  percentage (22.5/45/67.5/90). Each required installment is the smaller
  of that less earlier installments (line 24) and the regular installment
  plus what earlier periods left unused (lines 25 to 28).
* Counted as paid: estimated payments by their date, plus withholding
  spread over the due dates in the schedule's shares (IRC 6654(g)).
* State: the annualized amount with the state's own percentages
  (California 27/63/63/90). State safe-harbor rules are not modeled yet.

**Marginal chart and data table**: RTPL's marginal graph for the year,
drawn from either the Plan column or the latest YTD column (the last
period that has ended: December 31 for a past year). The profile is the
worksheet's, with each source's amount from that column, so a YTD graph
shows the rates as if the year ended then. The pieces are shared with the
RTPL window (`app/marginal_view.py`): `MarginalOptions` is the Graph
options panel (plus EQTA's Draw from choice), `MarginalChart` the chart
and its table. A year's chart is computed only while its tab shows, and
the layer choices apply to every year in the window.

**Later** (GitHub issue #45): importing the old spreadsheet, CSV import,
capital loss carryover, state safe harbors. Handing a second double-click
to the running copy is #63.

## Accessibility and CUA

* Standard menus with mnemonics and shortcuts (Ctrl+N/O/S, Ctrl+E export).
* Every form field has a label buddy and an accessible name.
* A **Data table** tab lists where the marginal rate changes and what each
  annotation means, so the chart's content is available as text.
* Layers are also told apart by legend order and labels, not color alone.

## Colors and themes

`data/themes/default.toml` defines light and dark palettes. Layer colors
come from a colorblind-checked categorical set, stepped separately for each
background. *View → Chart Colors…* edits any layer color for either mode and
saves to `theme.toml` in the user config folder. The defaults are our own
palette; if you want house colors from a planner's charts, drop their hex
values into the theme file.

## Not modelled yet

Credits other than education (child, saver's, elderly), detailed itemized
deductions (SALT cap and phase-out, medical floor, charitable rules; issue
#27), AMT, QBI, the 0.9% additional Medicare
tax, self-employment tax, local income taxes, most state credits, and the
state rules each state file lists under `notes` (cliff-style pension
exclusions, special capital gains rates), the premium tax credit's repayment rules for
advance payments (the credit is worked out on the year's actual MAGI), the qualifying surviving spouse
filing status (issue #2).

## Forward-looking plan

![Forward-looking graph for the example plan](images/forward-example.png)

A plan is the baseline profile (people, birth years, life expectancies,
filing status, state, inflation) plus accounts, income streams, Social
Security choices, assumptions and a short rule list. Each plan file keeps
its own copy of the baseline, so two scenarios can differ in who dies
when (for example the younger spouse dying first). The plan ends when the
last person dies; there is one inflation rate, the baseline's.

Plan files carry `schema_version` (now 2). Version 1 files (app 0.2.x)
still open: each person's `death_age` (or the old plan end age) becomes
their life expectancy, a plan-level inflation override becomes the
baseline's inflation, an income's `growth` becomes a "Fixed %" COLA (or
"None" at 0%), and the eligible designated beneficiary tick box becomes
the beneficiary choice. Saving writes version 2. It is saved as JSON; see
`examples/plans/couple_conversions_inherited.json`. The app's **Forward
plan** page opens plan files. `rtpl-plan` draws the same graphs and
writes the year table as CSV.

### Editing a plan in the app

On the Forward plan page the left panel has two tabs: **Baseline** (the
profile, including each person's life expectancy, default 95) and **Plan
items**. Plan items is a list grouped into Assumptions, Accounts, Income,
Social Security and Strategies; each row has a bar
across the years it covers on the plan's year axis. The selected row's
form sits under the list, and every edit is written into the plan at once
and recalculates it a quarter second after the last keystroke. The
summary above the graph says what the last change did to lifetime tax and
final balances.

* **Switching items off.** Accounts, income streams and strategies have a
  tick box. An unticked item stays in the plan file (`"enabled": false`)
  but is left out of the calculation, along with any strategy on an
  unticked account, so a what-if is one click and one click back.
* **Plan name.** A plan has its own free-form name beside the baseline's
  name; the summary, chart titles and the suggested file name use both
  ("Smith household - Roth conversions until SS.plan.json").
* **Timings** are entered as a person's age, a calendar year, a death
  ("Alex dies", "First death") or "End of plan", and the form shows the
  year next to it ("= 2031"). As a start, a death means the year after it
  ("After Alex dies"): the first year without that person.
* **Values set elsewhere** show greyed on the panel with where to change
  them: the Social Security COLA on each Social Security panel, inflation
  on Assumptions, life expectancy on Social Security.
* **Account ids** are made up by the app (`p1_traditional_ira`); deleting
  an account also deletes the strategies that use it, after asking.
* The design for the rest of the editor (milestones, year overrides,
  scenarios, setup wizard) is in the plan-editor spike,
  `spikes/plan_editor/mockup.py`, which draws mockups of those screens.

### Design decisions

* **Rules first, then a spending order.** Balances grow and drive RMDs and
  "fill to a target" conversions. Money comes out of an account where an
  RMD or a rule says so, and, when income falls short of spending and
  tax, from the account types ticked in Assumptions ▸ Pay for spending
  from, in order (cash, then brokerage, by default; traditional, inherited,
  Roth and HSA can be added).
* **Ages** are the age reached by December 31. A timing is stored as one
  person's age (or a calendar year if the user prefers) and can be shown as
  the other person's age or the year.
* **Accounts are individual.** Each spouse can hold several of each type;
  the graph groups them by type, and *Show each source separately* splits
  them out by account, person and income stream.

### What goes in

| Item | Fields |
|---|---|
| Account | id, type (traditional IRA, 401(k)/403(b), inherited IRA, Roth IRA, Roth 401(k), HSA, taxable brokerage, cash), owner, balance on Jan 1 of the plan's first year, optional return; taxable: cost basis and yields (interest, qualified and non-qualified dividends, capital gain distributions); traditional: spouse is sole beneficiary; inherited: year inherited, original owner's birth year, whether they had started RMDs, who inherited it (spouse, minor child, disabled or chronically ill, not more than 10 years younger, other person, estate or trust) |
| Income stream | wages, pension, foreign pension, annuity, interest, qualified dividends, long-term gain, other ordinary, tax-exempt; owner, yearly amount in the first year paid, start and end (age, year, a death or the end of the plan; one year for a one-off), COLA (None, Inflation, Social Security COLA or Fixed %; "yearly increase" for interest, dividends and other income), survivor %, federal taxable % |
| Social Security (per person) | benefit at full retirement age (today's dollars), claiming age (years and months), or the benefit already being paid; SSDI, with an optional early end age (a claiming age past FRA suspends SSDI's retirement benefit until then) |
| Person (on the baseline) | birth year, life expectancy (default 95) |
| Assumptions | Social Security COLA (default: inflation), default return and cash return (nominal, before inflation; every cash account earns the cash return), yearly spending (today's dollars), spending after the first death (% of the couple's), the spending order, MAGI for the two years before the plan (for IRMAA) |
| Rule | Roth conversion, withdrawal or qualified charitable distribution (QCD), from one account, over a span of years; amount: fixed, fill to a target (top of a bracket, top of the 0% gains band, under an IRMAA tier, an ACA % of the poverty line, or a MAGI), spread the balance evenly over the rule's remaining years (1/N; on a 10-year-rule inherited IRA the rule stops at the deadline and N counts to it), or the whole balance. A fixed amount can stay flat, grow with inflation (entered in today's dollars) or grow by a fixed % a year after the rule's first year (negative for a glide path down). A QCD takes a fixed amount or the year's RMD from the account. An HSA contribution goes into one person's HSA: the most allowed or a fixed amount, with the owner's coverage (self-only or family) |

**Overrides.** For each account and kind of rule, the rule with the
narrowest span wins in a year (ties go to the later rule). So "fill to 22%,
ages 63 to 70" plus "2031: convert $0" skips one year without splitting
the range. A withdrawal rule's amount is the year's total from that
account, so an RMD counts toward it; conversions come on top of RMDs.

**Qualified charitable distributions.** A QCD rule gives money straight
from an IRA to a charity (IRC 408(d)(8)):

* Only traditional and inherited IRAs qualify (a SEP or SIMPLE IRA only
  once the employer has stopped contributing, which the plan can't tell,
  so it is up to the user); not 401(k)s or 403(b)s, which have to be
  rolled into an IRA first. Roth IRAs are not offered: a QCD from one
  saves nothing.
* The owner (for an inherited IRA, the beneficiary) must be 70½ on the
  day of the gift. With only birth years, QCDs start in the year the
  person turns 71; someone born January to June could start late in the
  year they turn 70.
* Each IRA owner has a yearly limit, shared by all of their IRAs:
  $100,000 to 2023, then indexed ($105,000 in 2024, $108,000 in 2025,
  $111,000 in 2026, $114,000 in 2027), in `[qcd]` in each federal year
  file and grown with inflation (rounded down to $1,000) when projected.
  Rules are filled in list order; a capped QCD gets a note.
* A QCD is left out of income entirely (no AGI, so it also leaves Social
  Security taxation, IRMAA and the ACA alone), and can't also be deducted.
* It counts toward the RMD of the account it comes from: QCDs are taken
  first, and only the rest of that account's RMD is taxed. An "RMD" QCD
  gives exactly that account's RMD (none in a first year whose RMD was
  already taken). RMDs are still worked out per account; once a person's
  IRA RMDs are aggregated, their QCD total comes off the combined RMD
  instead, and the QCD amounts per account (`YearResult.qcds`) stay as
  they are.
* The money goes to charity, not the household: it is not part of income
  or net cash flow, and doesn't count toward a withdrawal rule's amount.
  If the spending assumption includes gifts made by QCD, lower it. On the
  graph QCDs are a tax-free segment (shown with the other tax-free
  segments), and the year table has a QCD column per account.
* Not modelled: the SECURE 2.0 one-time QCD to a charitable gift annuity
  or charitable remainder trust (from 2023; $55,000 in 2026 and $57,000 in
  2027, inside the yearly limit; stored as `split_interest_limit`), whose
  payments can be entered as an annuity income stream; and the reduction
  of the excludable QCD by deductible IRA contributions made after 70½
  (the plan has no IRA contributions).

**HSA contributions.** An HSA contribution rule pays into one person's
HSA from the year's cash flow (IRC 223). It is meant for early retirees
who keep a high-deductible health plan (from an employer, COBRA or the
ACA marketplace) until Medicare:

* No earned income is needed, unlike IRA and 401(k) contributions, so a
  retiree living on savings can contribute. The plan takes HSA-eligible
  coverage as given for the years of the rule.
* Limits by coverage: self-only or family, in `[hsa]` in each federal
  year file (2020: $3,550 / $7,100; 2024: $4,150 / $8,300; 2025: $4,300 /
  $8,550; 2026: $4,400 / $8,750; 2027: $4,500 / $9,000), grown with
  inflation (rounded down to $50) when projected. Spouses with family
  coverage share one family limit, split between their HSAs as they like:
  rules are filled in list order, so two "most allowed" rules give the
  first HSA the whole family limit; a fixed amount on the first rule splits
  it. (The tax rules treat a couple as both having family coverage if
  either does; the plan uses each rule's coverage as entered.)
* The catch-up: $1,000 a year (fixed in statute, not indexed) for each
  person 55 or older by the end of the year. It only goes into the HSA
  that person owns, so with family coverage each spouse needs their own
  HSA for their catch-up. Every HSA in the plan belongs to one person.
* Medicare ends contributions. Part A starts the month a person turns 65;
  with only birth years (issue #23 adds the month) the plan takes January,
  so nothing goes in the year they turn 65. A year with Medicare part-way
  through gets the limit (and catch-up) prorated by the months before it.
  If the baseline says the person is **not** on Medicare at 65 (still on
  an employer's plan, say), they can keep contributing until Social
  Security starts, since claiming it after 65 enrolls them in Part A. Part
  A is then backdated up to 6 months (not before 65), so the plan stops 6
  months before the claim; in real life, stop contributing 6 months
  before applying for Social Security or Medicare. Someone on Medicare
  before 65 (the baseline says so) can't contribute.
* The contribution is an above-the-line deduction: it lowers AGI, and so
  the MAGI for IRMAA and the ACA, before fill rules are solved. California
  and New Jersey don't allow it, so their state files add it back
  (`taxes_hsa_contributions`). It is paid from the year's cash flow like
  spending; a deficit is taken from the spending order as usual. The year
  table has an HSA contribution column per account, and notes show caps,
  prorated years and the year contributions stop.
* Employer contributions (and payroll contributions through a cafeteria
  plan) count toward the same limit; the "most allowed" amount doesn't
  know about them, so use a fixed amount for the rest. Payroll
  contributions also avoid Social Security and Medicare tax, which the
  plan doesn't model. Not modelled either: the last-month rule and its
  testing period, contributions for a year made by the next April, and
  the once-in-a-lifetime IRA-to-HSA transfer.

**RMD already taken.** A plan started mid-year can mark a traditional or
inherited account's RMD as already taken for the first year
(`rmd_taken_year`). The entered balance is then after the RMD, so the RMD
is worked out as balance / (period − 1), counted as that year's income,
and not taken from the balance again. Storing the year, not a yes/no,
means it stops applying once the plan starts in a later year.

### The yearly loop

1. Accounts of someone who died the year before pass to the survivor
   (spousal rollover).
2. **QCDs, then RMDs.** QCD rules first (see above), each counting toward
   its account's RMD. RMDs: traditional accounts from 73 (born 1951–59) or 75 (born
   1960+), using the Uniform Lifetime table, or the Joint and Last
   Survivor table when the spouse is the sole beneficiary and more than 10
   years younger. Inherited IRAs: yearly RMDs from the Single Life table
   (non-recalculating, from the year after death, using the longer of the
   beneficiary's and the owner's remaining expectancy) when the owner had
   started RMDs or the beneficiary is eligible to stretch, and the whole
   balance in the deadline year. The rule comes from who inherited it and
   the year of death: deaths before 2020 stretch for any person; from
   2020 a spouse, a disabled or chronically ill person, or someone not
   more than 10 years younger stretches; a minor child stretches until 21
   and then has 10 years; anyone else has the 10-year rule. An estate or
   trust has the 5-year rule if the owner died before their RMDs started,
   otherwise yearly RMDs over the owner's remaining life expectancy. When
   a stretching beneficiary dies in the plan, the spouse who takes the
   account over must empty it within 10 years.
3. **Income.** Streams, taxable-account yields (reinvested) and cash
   interest.
4. **Social Security.** Own benefit with early-claiming reductions or
   delayed credits; a spousal top-up (half the other's full benefit, less
   one's own, reduced if started early) once both have filed, which makes
   "claim my own at 62, switch to spousal when my spouse files" happen on
   its own; deemed filing means "spousal first, own later" is not offered.
   Either person can get the top-up. The person form shows it as a
   read-only line ("Spousal top-up: $3,000 a year from 2034 (age 68)"),
   says when a $0 own benefit means spousal only, and warns when the
   person files before the spouse, since the top-up waits for the spouse.
   After a death, the survivor gets the larger of their own benefit and the
   deceased's (at least 82.5% of the deceased's full benefit if they had
   claimed). SSDI is paid at the full benefit and becomes a retirement
   benefit at full retirement age. With a claiming age past FRA the benefit
   is suspended from FRA and restarts then with delayed credits (so "SSDI
   until FRA, then let it grow to 70" works). An optional SSDI end age
   covers a return to work: nothing is paid until the claiming age, which
   then works as an ordinary claim. While a benefit is suspended, the
   spouse's spousal benefit on that record stops too.
   **Earnings test.** Before full retirement age, a person's wages (the
   whole Wages amount, as gross pay) withhold $1 of benefits for every $2
   over SSA's lower limit; in the year FRA is reached, $1 for every $3 over
   the higher limit, counting only the months before FRA. The limits are in
   each federal year file (`earnings_test`) and grow with inflation after
   the last one, standing in for wage growth. A person's wages withhold
   their own check (own, spousal or survivor benefit) and the spouse's
   spousal top-up, which is paid on their record, in the same months, as
   SSA withholds whole months of the family's checks together. The spouse's
   own benefit is not touched by them. Whole months withheld are credited
   back at each person's own FRA: the own benefit (and spousal top-up) is
   recomputed as if claimed that many months later. The year table's *SS withheld* column and the notes show
   both. SSDI is left out (it has its own work rules), and pensions, IRA
   withdrawals and investment income never count.
5. **Rules.** HSA contributions first (see above), then fixed, spread and whole-balance rules, then fill rules
   in list order; each fill amount is solved by bisection on the whole
   return.
6. **Tax.** `compute_tax()` with that year's law (projected past the newest
   data file), the year's filing status (joint through the year of death,
   then single), and inflation-indexed adjustments and itemized deductions.
   Education credits from the baseline are claimed from the first year
   through its *Claimed through* year (expenses grow with inflation), and
   lower federal tax; the year table's *Education credits* column shows
   them, and the Taxes by source graph takes them off federal ordinary
   income tax first.
7. **Cash flow.** Income minus spending, tax, IRMAA surcharges, net ACA
   premiums and HSA contributions. **ACA premiums:** each person's
   baseline benchmark and plan premiums grow at the *ACA premium growth*
   assumption (default 5% a year, usually above inflation) until they are
   on Medicare; the premium tax credit is worked out on that year's own
   ACA MAGI, and the plan pays the premium less the credit, so leave
   marketplace premiums out of spending. The year table shows the
   premiums, the credit and the net cost. After
   the first death a couple's spending is the survivor's share. A surplus
   goes into the taxable account (or cash); a deficit is taken from the
   account types in the spending order (by default cash, then the taxable
   account), realizing gains in proportion to the basis; withdrawals from
   traditional accounts are ordinary income. Both feed back into the tax
   (iterated, so a withdrawal also pays its own tax). Anything still short is
   reported as a shortfall in the graph's notes and the year table.
8. **IRMAA.** Premiums in year *Y* use MAGI from *Y − 2* (the plan's own
   years, or the MAGI entered for the years before the plan) and that
   year's filing status.
9. **Balances** grow by their return.

### The graphs

**Income and brackets.** Bars are income by source, in this stack order:
Social Security, wages, pensions and annuities (including foreign
pensions), interest and other ordinary income, RMDs, inherited IRA
withdrawals, other IRA withdrawals, Roth conversions, qualified dividends
and fund distributions (income a taxable account pays whether you want it
or not), realized capital gains (sales you choose, sales that cover a
shortfall, and one-off gains such as a property sale), then tax-free money (tax-exempt interest, Roth and
HSA withdrawals, brokerage and cash principal) in light hatched grays that
can be switched off.

Lines, each on or off: the top of each ordinary bracket, the top of the 0%
capital gains band, IRMAA tiers, the ACA 400% line and the deductions line
(where taxable income starts). **Lines are in the same dollars as the
bars**: each is the total income at which that year's return would reach
the threshold, found by adding or removing ordinary income and recomputing
the return. That accounts for Social Security taxation, the standard and
senior deductions and the year's filing status, so where a bar crosses a
line is where the income really reaches it. Bracket tops are read against
the top of the ordinary segments, because gains stack above ordinary
income on the return; the other lines are read against the whole taxable
bar. IRMAA lines use the tiers for the premium year two years later.
When the income that can't be removed (Social Security, RMDs, dividends,
gains) is already over a threshold, the line is still drawn where the
threshold sits (inside the bar), carrying the last dollar-for-dollar step on
below the removable income, rather than at the removable-income limit (which
would stack several lines on one value). Every line runs the full width;
which lines show is up to the user. IRMAA tiers are tight dots in shades of
one colour (light for tier 1), the 0% gains band dots in another colour.

Suggested lines: the brackets the bars reach plus the next one up (not
10%), the 0% gains band if there are gains, IRMAA tiers the bars come
near if anyone will be on Medicare, and ACA if the household buys
marketplace coverage.

**Taxes by source.** Each year's taxes as stacked bars, bottom to top:
federal tax on ordinary income, federal tax on gains and qualified
dividends, NIIT, state income tax, the state's own capital gains tax (such
as Washington's) and Medicare IRMAA surcharges (premiums rather than tax,
so on top). Colors match the marginal graph's layers, and IRMAA takes the
income graph's IRMAA line color. It follows the today's-dollars toggle,
and the notes give lifetime totals by source. FICA payroll taxes and local
income taxes are not modeled, so they are not shown.
`examples/plans/couple_rmd_tax_problem.json` shows the typical shape: a
low-tax stretch, a jump when RMDs start at 75, and another when the
survivor files single.

**Balances.** Year-end balances by account type (or by account), colored
to match the income each feeds. **Bar details.** Clicking a bar on any of
the three graphs pops up that year's segments (top of the bar first) with
exact amounts and the total, then, on the income graph, each shown
threshold line that crosses the bar and its value. A click off the bars,
Escape, a redraw, a resize or a tab change closes it. **Year table.** For screen readers and
copying. The suggested columns are ages, filing status, income by
category, total income, AGI, taxable income, IRMAA MAGI, federal and state
tax, IRMAA, spending, net cash flow, total balances and notes, plus
Social Security withheld under the earnings test (per person) and QCDs
when the plan has any. The
**Columns…** button (also the column headers' right-click menu) adds or
removes: qualified dividends and gains by the rate they are taxed at (0%,
15%, 20%; any under the deductions count as 0%), the marginal rate on the
next $1,000 of ordinary income (federal and state), total tax, the
shortfall, and per-account RMDs, Roth conversions, other withdrawals and
balances. The choice is saved in the plan file (`table_columns`; empty
means the suggested set), and *Show each source separately* splits the
income columns the way it splits the bars. Net cash flow is income,
Social Security and rule withdrawals less spending, tax and IRMAA: a
surplus is saved, a deficit comes from the spending order. **File ▸ Export
Year Table…** saves the table as shown (the chosen columns, today's
dollars, each source) as an OpenDocument spreadsheet (.ods, with money
and rates as numbers) or as CSV.

A *today's dollars* switch divides every amount by inflation since the
first year.

### Data sources and checks

* `data/rmd/tables.toml` holds the Uniform Lifetime and Single Life tables
  (IRS Publication 590-B, 2022 and later). The Joint and Last Survivor
  table is derived from the Single Life table's mortality; that
  reproduces every Uniform Lifetime entry (a 10-year age gap) to within
  0.1 year. A user copy can add the published joint table.
* Social Security factors follow SSA's published reduction and credit
  rules (5/9% and 5/12% a month early, 2/3% a month delayed; spousal
  25/36% and 5/12%).

### Simplifications in this version

* No birth month: a benefit claimed at age *A* starts in January of the
  year the person reaches *A* (a claim at *A* years and *M* months pays
  12 − *M* months that year).
* After SSDI ends early, a later early claim takes the full early-claiming
  reduction (the credit for months already on SSDI is not applied).
* Earnings test: wages count evenly across the year (no birth month, so
  birthdays are January); the first-year monthly test (a month with low
  wages pays in full) is not applied; the higher benefit for withheld
  months starts the year after full retirement age is reached (that year
  if FRA is in January); months withheld from a survivor benefit are not
  credited, since the early-survivor reduction isn't modelled either.
* No early-survivor reduction; a survivor steps up the year after the
  death.
* Spousal and survivor benefits assume the couple stays married to each
  other; divorced-spouse benefits are not modelled.
* State taxes treat foreign pensions as other ordinary income (no state
  pension exclusion), and use the same taxable share as the federal return.
* An inherited IRA left by a spouse who dies during the plan is rolled over
  into the survivor's own account. Inherited Roth IRAs are not modelled.
* Withdrawals from Roth accounts and HSAs are treated as qualified (tax-free).
* Returns are a flat rate per account; no Monte Carlo trials yet.

### Next steps

* An in-app editor for accounts, income streams, Social Security and rules.
* A spending tab: a spending target line on the income graph, and
  optionally withdrawals that follow a spending need and an account order.
* A tax-dollars view (tax paid per year by layer).
* Qualifying surviving spouse filing (issue #2).
