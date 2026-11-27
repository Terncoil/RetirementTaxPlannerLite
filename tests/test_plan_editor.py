"""The Plan items tab: list, forms, add/duplicate/delete, switching items off."""

import json
import os
from pathlib import Path

import pytest

from rtpl.engine.forward import run_plan
from rtpl.model.plan import Account, Plan

PLAN = Path("examples/plans/couple_conversions_inherited.rtpl")


def test_active_plan_leaves_out_switched_off_items():
    plan = Plan.load(PLAN)
    plan.incomes[0].enabled = False
    plan.accounts[2].enabled = False  # the inherited IRA; its 1/N rule goes too
    active = plan.active()
    assert plan.incomes[0] not in active.incomes and len(active.incomes) == len(plan.incomes) - 1
    assert all(a.enabled for a in active.accounts)
    assert all(r.account != plan.accounts[2].id for r in active.rules)
    assert len(plan.accounts) == 6  # the plan itself is unchanged


def test_switching_off_wages_changes_the_result(lib):
    plan = Plan.load(PLAN)
    before = run_plan(plan, lib)
    plan.incomes[0].enabled = False
    after = run_plan(plan, lib)
    assert before.years[0].category_totals()["wages"] > 0
    assert after.years[0].category_totals()["wages"] == 0


def test_new_account_id_is_unique():
    plan = Plan.load(PLAN)
    a = plan.new_account_id("roth_ira", 1)
    assert a == "p2_roth_ira"
    from rtpl.model.plan import Account

    plan.accounts.append(Account(id=a, kind="roth_ira", owner=1))
    assert plan.new_account_id("roth_ira", 1) == "p2_roth_ira_2"
    assert plan.new_account_id("taxable", None) == "joint_taxable"


def test_enabled_round_trips_and_old_files_load(tmp_path):
    plan = Plan.load(PLAN)
    assert all(a.enabled for a in plan.accounts)  # files without the field load as on
    plan.incomes[1].enabled = False
    path = tmp_path / "p.json"
    plan.save(path)
    assert Plan.load(path).incomes[1].enabled is False


# -- the app ------------------------------------------------------------------

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(tmp_path, monkeypatch):
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    QtCore = pytest.importorskip("PySide6.QtCore")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from rtpl.app.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow()
    w.load_plan(Plan.load(PLAN), PLAN)
    yield w
    w.setWindowModified(False)
    w.close()
    w.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def find_item(editor, obj):
    from rtpl.app.plan_editor import ROLE

    for g in editor.groups.values():
        for i in range(g.childCount()):
            if g.child(i).data(0, ROLE)[1] is obj:
                return g.child(i)
    raise AssertionError("not listed")


def pick(editor, obj):
    item = find_item(editor, obj)
    editor.tree.setCurrentItem(item)
    return item


def test_plan_items_tab_lists_the_plan(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    # Baseline comes first, then Plan items (shown, and picked, with a plan open).
    assert window.input_tabs.widget(0) is window.form and window.input_tabs.widget(1) is ed
    assert window.input_tabs.isTabVisible(1) and window.input_tabs.currentWidget() is ed
    assert "Plan" in window.inputs_panel.title.text()
    counts = {k: g.childCount() for k, g in ed.groups.items()}
    assert counts == {"accounts": 6, "incomes": 4, "people": 2, "rules": 4, "assumptions": 1}
    assert ed.tree.topLevelItem(0).text(0) == "Assumptions"  # at the top of the list
    assert find_item(ed, plan.rules[1]).text(0) == "Convert Alex Traditional IRA: fill to top of the 22% bracket"
    # The marginal graph page shows only the profile.
    window.pages.setCurrentIndex(0)
    assert not window.input_tabs.isTabVisible(1) and window.input_tabs.currentWidget() is window.form
    assert "Inputs" in window.inputs_panel.title.text()


def test_editing_a_balance_recalculates(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    tax_before = sum(y.tax.total_tax for y in window.plan_view.result.years)
    pick(ed, plan.accounts[0])
    form = ed.forms["accounts"]
    assert ed.stack.currentWidget() is form and form.balance.value() == 1_100_000
    form.balance.setValue(2_000_000)
    assert plan.accounts[0].balance == 2_000_000
    assert "$2.00M" in find_item(ed, plan.accounts[0]).text(0)
    assert window.isWindowModified()
    window._plan_timer.timeout.emit()  # the debounce timer firing
    tax_after = sum(y.tax.total_tax for y in window.plan_view.result.years)
    assert tax_after > tax_before
    assert "Last change: tax" in window.plan_view.summary.text()


def test_conversion_form_edits_the_rule(window):
    ed = window.plan_editor
    rule = window.plan_view.plan.rules[1]
    pick(ed, rule)
    form = ed.forms["rules"]
    assert form.mode.currentData() == "fill" and form.target.currentData() == "bracket:0.22"
    assert form.start.mode.currentText() == "Alex's age" and form.start.spin.value() == 63
    form.end.spin.setValue(72)
    assert rule.end.age == 72 and rule.end.person == 0
    form.target.setCurrentIndex(form.target.findData("bracket:0.24"))
    assert rule.target == "bracket:0.24"
    form.target.setCurrentIndex(form.target.findData("magi"))
    form.magi.setValue(150000)
    assert rule.target == "magi:150000"
    # Switching the end to a year keeps the same calendar year.
    form.end.mode.setCurrentIndex(form.end.mode.findData("year"))
    assert rule.end.year == 1964 + 72


def test_untick_an_income_switches_it_off(window):
    from PySide6.QtCore import Qt

    ed = window.plan_editor
    wages = window.plan_view.plan.incomes[0]
    item = find_item(ed, wages)
    item.setCheckState(0, Qt.CheckState.Unchecked)
    assert wages.enabled is False
    window._plan_timer.timeout.emit()
    assert window.plan_view.result.years[0].category_totals()["wages"] == 0


def test_add_duplicate_and_delete(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    ed.add_actions["roth_conversion"].trigger()  # the Add item menu
    new = plan.rules[-1]
    assert new.kind == "roth_conversion" and ed.current_object() is new
    ed.duplicate_item()
    assert len(plan.rules) == 6 and plan.rules[-1] is not new
    ed.delete_item()
    ed.delete_item()
    assert len(plan.rules) == 4
    ed.add_item("account")
    acct = plan.accounts[-1]
    assert acct.id == "p1_traditional_ira" and ed.current_object() is acct
    ed.duplicate_item()
    assert plan.accounts[-1].id == "p1_traditional_ira_2"
    window._plan_timer.timeout.emit()
    assert window.plan_view.result is not None


def test_deleting_an_account_asks_about_its_strategies(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    ira = plan.accounts[0]
    pick(ed, ira)
    ed.delete_item(confirm=lambda _text: False)
    assert ira in plan.accounts
    ed.delete_item(confirm=lambda _text: True)
    assert ira not in plan.accounts and all(r.account != ira.id for r in plan.rules)


def test_save_plan_writes_edits(window, tmp_path):
    plan = window.plan_view.plan
    plan.assumptions.spending = 95000
    window.plan_path = tmp_path / "edited.plan.json"
    assert window.save()  # Save saves the open plan
    assert Plan.load(window.plan_path).assumptions.spending == 95000
    assert not window.isWindowModified()
    assert window.windowTitle().startswith("edited.plan.json")


def test_assumptions_and_person_forms(window):
    from PySide6.QtCore import Qt

    from rtpl.app.plan_editor import ROLE

    ed = window.plan_editor
    plan = window.plan_view.plan
    pick(ed, plan.assumptions)
    form = ed.forms["assumptions"]
    form.spending.setValue(90000)
    assert plan.assumptions.spending == 90000
    # One inflation rate, on the Baseline: shown here, read only.
    assert form.inflation.text() == f"{100 * plan.profile.inflation:.2f}%\nChange it on the Baseline tab"
    assert form.cola.is_default() and form.cola.field.value() == pytest.approx(100 * plan.profile.inflation)
    assert not hasattr(form, "end_age")
    form.survivor.setValue(70)
    assert plan.assumptions.survivor_spending_pct == pytest.approx(0.7)
    # Spending order: tick the traditional accounts and move them up.
    assert form.order.value() == ["cash", "taxable"]
    trad = next(i for i in range(form.order.list.count()) if form.order.list.item(i).data(ROLE) == "traditional")
    form.order.list.item(trad).setCheckState(Qt.CheckState.Checked)
    assert plan.assumptions.spending_order == ["cash", "taxable", "traditional"]
    form.order.list.setCurrentRow(trad)
    form.order.move(-1)
    assert plan.assumptions.spending_order == ["cash", "traditional", "taxable"]
    assert form.form.labelForField(form.magi1).text() == f"IRMAA MAGI for {plan.start_year - 2}"
    form.magi1.setValue(0)
    assert form.magi1.text() == "Not entered" and str(plan.start_year - 2) not in plan.assumptions.prior_magi
    pick(ed, plan.people[1])
    pf = ed.forms["people"]
    assert pf.claim.value() == 62
    pf.claim.years.setValue(67)
    pf.claim.months.setValue(5)
    assert plan.people[1].social_security.claim_age == pytest.approx(67 + 5 / 12)
    assert "claims at 67 and 5 months" in find_item(ed, plan.people[1]).text(0)
    pf.claim.years.setValue(70)
    assert pf.claim.months.maximum() == 0 and plan.people[1].social_security.claim_age == 70
    # Life expectancy is on the Baseline now; the panel says so.
    assert not hasattr(pf, "death")
    assert pf.life_note.text().startswith(str(plan.profile.people[1].life_expectancy))
    # The SS COLA shows on the panel, with where to change it.
    assert "same as inflation" in pf.cola_note.text()
    assert "Change it in Assumptions ▸ Social Security COLA" in pf.cola_note.text()
    pf.fra.setValue(0)
    assert pf.fra.text() == "No Social Security"
    pf.ssdi.setChecked(True)
    assert pf.ssdi_end.is_default() and plan.people[1].social_security.ssdi_end_age is None
    pf.ssdi_end.check.setChecked(False)
    pf.ssdi_end.field.setValue(60.5)
    assert plan.people[1].social_security.ssdi_end_age == pytest.approx(60.5)


def test_account_form_cash_basis_and_inherited_deadline(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    form = ed.forms["accounts"]
    taxable = next(a for a in plan.accounts if a.kind == "taxable")
    pick(ed, taxable)
    assert form.interest.decimals() == 4
    form.interest.setValue(0.0175)
    assert form.interest.text() == "0.0175%"
    form.interest.setValue(1.5)
    assert form.interest.text() == "1.50%"
    form.interest.setValue(0.0175)
    assert taxable.interest_yield == pytest.approx(0.000175)
    form.basis.check.setChecked(False)
    form.basis.field.setValue(123_000)
    assert taxable.cost_basis == 123_000
    form.basis.check.setChecked(True)
    assert taxable.cost_basis is None and form.basis.field.value() == taxable.balance
    # A cash account hides the return and yield it would ignore.
    form.kind.setCurrentIndex(form.kind.findData("cash"))
    assert not form.form.isRowVisible(form.return_rate) and not form.form.isRowVisible(form.interest)
    assert form.form.isRowVisible(form.cash_note) and "Cash return" in form.cash_note.text()
    inherited = next(a for a in plan.accounts if a.kind == "inherited_ira")
    pick(ed, inherited)
    deadline = inherited.deadline_year(plan.profile)
    assert form.deadline.text() == f"10-year rule: empty by {deadline}"
    # Who inherited it decides the rule.
    form.beneficiary.setCurrentIndex(form.beneficiary.findData("disabled"))
    assert inherited.beneficiary == "disabled" and form.deadline.text().startswith("Lifetime withdrawals")
    form.beneficiary.setCurrentIndex(form.beneficiary.findData("other"))
    form.inherited_year.setValue(2018)
    assert form.deadline.text() == "Lifetime withdrawals: inherited before 2020"
    form.inherited_year.setValue(2024)
    # A rule on it can't end after the deadline.
    rule = next(r for r in plan.rules if r.account == inherited.id)
    pick(ed, rule)
    rf = ed.forms["rules"]
    assert rf.form.isRowVisible(rf.deadline)
    rf.end.mode.setCurrentIndex(rf.end.mode.findData("year"))
    rf.end.spin.setValue(deadline + 5)
    assert rf.end.spin.maximum() == deadline
    assert rule.end.resolve(plan.profile) == deadline


def test_new_plan_from_profile_can_be_built_in_the_app(window):
    window.new_plan()
    ed = window.plan_editor
    plan = window.plan_view.plan
    assert ed.plan is plan and ed.groups["accounts"].childCount() == 0
    ed.add_item("account")
    ed.forms["accounts"].balance.setValue(500000)
    ed.add_item("roth_conversion")
    window._plan_timer.timeout.emit()
    assert window.plan_view.result.years[0].category_totals()["roth_conversion"] > 0


def test_plan_name_round_trips_and_titles(tmp_path):
    plan = Plan.load(PLAN)
    assert plan.name == "" and plan.title == plan.profile.name
    plan.name = "Roth conversions until SS"
    assert plan.title == "Couple with conversions and an inherited IRA - Roth conversions until SS"
    plan.save(tmp_path / "p.json")
    assert Plan.load(tmp_path / "p.json").name == "Roth conversions until SS"


def test_plan_name_field_and_suggested_file_name(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    window.setWindowModified(False)
    ed.name.setText("David retires early")
    ed.name.textEdited.emit("David retires early")  # as if typed
    assert plan.name == "David retires early" and window.isWindowModified()
    window.plan_path = None
    window.form.plan_name.setText("Smith: household")
    window.form.changed.emit()
    path = Path(window._suggested_plan_path(".plan.json"))
    assert path.name == "Smith_ household - David retires early.plan.json"
    window._plan_timer.timeout.emit()
    assert "David retires early" in window.plan_view.summary.text()


def test_baseline_tab_gets_its_full_height(window):
    """Switching to the Baseline tab on the plan page must not squash the
    profile form: the panel measures only the page on show, right away."""
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    window.resize(1200, 700)
    window.show()
    # Saved panel settings can leak between tests on Windows and macOS
    # (QSettings ignores XDG_CONFIG_HOME there), so open the panel explicitly.
    window.panels.set_pinned(window.inputs_panel, True)
    window.panels.set_open(window.inputs_panel, True)
    QtWidgets.QApplication.processEvents()
    for page in (window.form, window.plan_editor, window.form):
        window.input_tabs.setCurrentWidget(page)
        assert page.isVisible() and page.height() >= page.minimumSizeHint().height()
        others = [p for p in (window.form, window.plan_editor) if p is not page]
        assert all(not p.isVisible() for p in others)


def test_timings_cannot_start_before_the_plan_or_end_before_the_start(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    rule = plan.rules[0]
    pick(ed, rule)
    rf = ed.forms["rules"]
    rf.start.mode.setCurrentIndex(rf.start.mode.findData("year"))
    assert rf.start.spin.minimum() == plan.start_year
    rf.start.spin.setValue(plan.start_year - 5)
    assert rule.start.resolve(plan.profile) == plan.start_year
    rf.end.mode.setCurrentIndex(rf.end.mode.findData("year"))
    rf.start.spin.setValue(plan.start_year + 3)
    assert rf.end.spin.minimum() == plan.start_year + 3
    rf.end.spin.setValue(plan.start_year)
    assert rule.end.resolve(plan.profile) == plan.start_year + 3
    income = plan.incomes[1]
    pick(ed, income)
    inf = ed.forms["incomes"]
    inf.start.mode.setCurrentIndex(inf.start.mode.findData("year"))
    assert inf.start.spin.minimum() == plan.start_year



def test_death_timings_cola_and_growing_rule_amounts(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    alex, sam = plan.profile.people
    # An income can start after a death and end at one.
    stream = plan.incomes[1]  # Alex's pension
    pick(ed, stream)
    form = ed.forms["incomes"]
    form.end.mode.setCurrentIndex(form.end.mode.findData("event:first_death"))
    assert stream.end.event == "first_death" and not form.end.spin.isVisible()
    assert form.end.result.text() == f"= {plan.death_year(0)}"
    assert form.start.mode.findData("event:last_death") < 0  # "End of plan" is not a start
    form.start.mode.setCurrentIndex(form.start.mode.findData("event:death:0"))
    assert stream.start.event == "death:0" and form.start.result.text() == f"= {plan.death_year(0) + 1}"
    assert form.start.mode.itemText(form.start.mode.currentIndex()) == f"After {alex.name} dies"
    # COLA choices; the fixed rate shows only for Fixed %, with the floor/cap note for pensions.
    assert form.form.labelForField(form.cola).text() == "COLA"
    assert form.form.isRowVisible(form.cola_note) and "floor or cap" in form.cola_note.text()
    form.cola.setCurrentIndex(form.cola.findData("ss_cola"))
    assert stream.cola == "ss_cola" and not form.form.isRowVisible(form.growth)
    form.cola.setCurrentIndex(form.cola.findData("fixed"))
    assert form.form.isRowVisible(form.growth)
    form.kind.setCurrentIndex(form.kind.findData("interest"))
    assert form.form.labelForField(form.cola).text() == "Yearly increase"
    assert not form.form.isRowVisible(form.cola_note)
    # A fixed rule amount can grow with inflation.
    rule = next(r for r in plan.rules if r.mode == "fixed" and r.kind == "withdrawal")
    pick(ed, rule)
    rf = ed.forms["rules"]
    assert rf.form.isRowVisible(rf.growth) and not rf.form.isRowVisible(rf.growth_rate)
    rf.growth.setCurrentIndex(rf.growth.findData("fixed"))
    rf.growth_rate.setValue(2.0)
    assert rule.growth == "fixed" and rule.growth_rate == pytest.approx(0.02)
    assert rf.end.mode.itemText(rf.end.mode.currentIndex()) == "End of plan"


def test_life_expectancy_is_on_the_baseline(window):
    plan = window.plan_view.plan
    box = window.form.person_boxes[1]
    assert box.life.value() == plan.profile.people[1].life_expectancy == 94
    box.life.setValue(80)
    window._timer.timeout.emit()
    plan = window.plan_view.plan
    assert plan.profile.people[1].life_expectancy == 80
    assert plan.end_year == max(1964 + 88, 1966 + 80)  # the plan ends when the last person dies
    assert window.plan_view.result.years[-1].year == plan.end_year


def test_year_table_column_picker(window):
    from rtpl.engine.forward import DEFAULT_TABLE_COLUMNS

    view = window.plan_view
    plan = view.plan
    headers = lambda: [view.table.horizontalHeaderItem(c).text() for c in range(view.table.columnCount())]
    assert "Spending" in headers() and "Net cash flow" in headers()
    assert view.column_actions["notes"].isChecked() and not view.column_actions["balances"].isChecked()
    window.setWindowModified(False)
    view.column_actions["balances"].setChecked(True)
    assert "Alex Traditional IRA balance" in headers()
    assert "balances" in plan.table_columns and window.isWindowModified()
    view.column_actions["notes"].setChecked(False)
    assert "Notes" not in headers()
    # Header tooltips explain the column.
    col = headers().index("Net cash flow")
    assert "spending order" in view.table.horizontalHeaderItem(col).toolTip()
    # Show each source separately splits the income columns too.
    n = view.table.columnCount()
    view.detail.setChecked(True)
    assert view.table.columnCount() > n
    view.detail.setChecked(False)
    view._reset_columns()
    assert plan.table_columns == [] and view.table_columns() == list(DEFAULT_TABLE_COLUMNS)
    assert "Notes" in headers()


def test_person_form_shows_the_spousal_top_up(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    pf = ed.forms["people"]
    pick(ed, plan.people[0])
    assert pf.spousal.text().startswith("None (own benefit is at least half of Sam's")
    pick(ed, plan.people[1])
    # Sam (16,000) files at 62; half of Alex's 38,000 is 19,000, so a 3,000 top-up from Alex's claim at 70.
    assert pf.spousal.text() == "$3,000 a year from 2034 (age 68), in today's dollars"
    assert "waits until Alex files in 2034" in pf.spousal_hint.text()
    assert "Either of you" in pf.spousal_hint.text()
    pf.fra.setValue(0)
    assert pf.spousal.text().startswith("$19,000 a year from 2034")
    assert "spousal benefit only" in pf.spousal_hint.text()


def test_account_form_rmd_already_taken(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    form = ed.forms["accounts"]
    pick(ed, plan.account("alex_ira"))  # Alex is 62: no RMD yet
    assert form.form.isRowVisible(form.rmd_taken) is False
    inh = plan.account("alex_inh")
    pick(ed, inh)
    assert form.form.isRowVisible(form.rmd_taken)
    before = window.plan_view.result.years[0].balances_end["alex_inh"]
    form.rmd_taken.setChecked(True)
    assert inh.rmd_taken_year == plan.start_year
    window.plan_view.recompute()
    assert window.plan_view.result.years[0].balances_end["alex_inh"] > before
    form.rmd_taken.setChecked(False)
    assert inh.rmd_taken_year is None


def test_export_year_table(window, tmp_path, monkeypatch):
    import zipfile

    from PySide6.QtWidgets import QFileDialog

    assert window.export_table_action.isEnabled()
    window.plan_view.set_table_columns(["spending", "notes"])
    for name in ("table.ods", "table.csv"):
        target = tmp_path / name
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
        window.export_year_table()
        assert target.exists()
    assert (tmp_path / "table.csv").read_text(encoding="utf-8").splitlines()[0] == "Year,Spending,Notes"
    assert b"Spending" in zipfile.ZipFile(tmp_path / "table.ods").read("content.xml")
    window.setWindowModified(False)  # no save prompt
    window.new_profile()  # no plan: nothing to export
    assert not window.export_table_action.isEnabled()


def test_add_a_qcd_strategy(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    ed.add_item("qcd")
    rule = plan.rules[-1]
    assert rule.kind == "qcd" and rule.mode == "rmd" and plan.account(rule.account).kind in ("traditional_ira",
                                                                                               "inherited_ira")
    form = ed.forms["rules"]
    assert ed.stack.currentWidget() is form
    # Only IRAs, and only the fixed and RMD amounts, are offered.
    kinds = {plan.account(form.account.itemData(i)).kind for i in range(form.account.count())}
    assert kinds <= {"traditional_ira", "inherited_ira"}
    assert [form.mode.itemData(i) for i in range(form.mode.count())] == ["fixed", "rmd"]
    assert "Limit $" in form.qcd_hint.text() and "age 71" in form.qcd_hint.text()
    assert find_item(ed, rule).text(0).startswith("QCD from") and "the RMD" in find_item(ed, rule).text(0)
    form.mode.setCurrentIndex(form.mode.findData("fixed"))
    form.amount.setValue(20_000)
    assert rule.mode == "fixed" and rule.amount == 20_000
    window._plan_timer.timeout.emit()
    assert any(y.qcds for y in window.plan_view.result.years)
    # Switching to a withdrawal offers every amount mode again.
    form.kind.setCurrentIndex(form.kind.findData("withdrawal"))
    assert rule.kind == "withdrawal" and form.mode.count() == 4


def test_add_an_hsa_contribution(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    plan.accounts.append(Account(id="p1_hsa", kind="hsa", owner=0, balance=5_000))
    ed.rebuild()
    ed.add_item("hsa_contribution")
    rule = plan.rules[-1]
    assert rule.kind == "hsa_contribution" and rule.account == "p1_hsa" and rule.mode == "max"
    assert rule.coverage == "family"  # a couple
    last = plan.hsa_last_year(0)
    assert last is not None and rule.end.resolve(plan.profile) == last  # the year before Medicare
    form = ed.forms["rules"]
    assert form.form.labelForField(form.account).text() == "Into account"
    assert [form.account.itemData(i) for i in range(form.account.count())] == ["p1_hsa"]
    assert [form.mode.itemData(i) for i in range(form.mode.count())] == ["max", "fixed"]
    assert "self-only or $" in form.hsa_hint.text() and "into this HSA only" in form.hsa_hint.text()
    assert find_item(ed, rule).text(0).startswith("Contribute to") and "the most allowed" in find_item(ed, rule).text(0)
    form.coverage.setCurrentIndex(form.coverage.findData("self"))
    assert rule.coverage == "self"
    form.mode.setCurrentIndex(form.mode.findData("fixed"))
    form.amount.setValue(3_000)
    assert rule.mode == "fixed" and rule.amount == 3_000
    form.kind.setCurrentIndex(form.kind.findData("withdrawal"))
    assert form.form.labelForField(form.account).text() == "From account"


def test_file_menu_has_one_open_and_save(window):
    menu = window.menuBar().actions()[0].menu()
    names = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert names == ["&New", "&Open…", "&Save", "Save &As…", "Save &Profile As…",
                     "New P&lan from Profile", "&Export Chart…", "Export &Year Table…",
                     "Export &Report…", "E&xit"]


def test_new_plan_from_profile_asks_about_unsaved_changes(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    plan = window.plan_view.plan
    asked = []

    def answer(button):
        def warning(*_a, **_k):
            asked.append(True)
            return button
        return warning

    monkeypatch.setattr(QMessageBox, "warning", answer(QMessageBox.StandardButton.Cancel))
    window.new_plan()  # nothing unsaved: no question
    assert not asked and window.plan_view.plan is not plan
    assert window.isWindowModified()  # the new plan is not on disk yet
    new = window.plan_view.plan
    window.new_plan()  # Cancel keeps the open plan
    assert asked and window.plan_view.plan is new
    monkeypatch.setattr(QMessageBox, "warning", answer(QMessageBox.StandardButton.Discard))
    window.new_plan()
    assert window.plan_view.plan is not new


def test_save_as_saves_the_plan_under_the_new_name(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    window.plan_path = tmp_path / "old.json"
    target = tmp_path / "new.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert window.save_as()
    assert target.exists() and not window.plan_path.with_name("old.json").exists()
    assert window.plan_path == target and "profile" in json.loads(target.read_text(encoding="utf-8"))


def test_save_profile_as_with_a_plan_open(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from rtpl.model.profile import Profile

    window.plan_path = tmp_path / "plan.json"
    window.setWindowModified(True)
    target = tmp_path / "profile.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    assert window.save_profile_as()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "profile" not in data and Profile.from_dict(data)
    # The plan is still the open file, with its changes still unsaved.
    assert window.plan_view.plan is not None and window.isWindowModified()
    assert not window.plan_path.exists()
    window.open_file(target)  # opens as a profile, with no plan
    assert window.plan_view.plan is None and window.current_path == target


def test_balance_label_follows_the_baseline_year(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    pick(ed, plan.accounts[0])
    form = ed.forms["accounts"]
    label = form.form.labelForField(form.balance)
    year = plan.start_year
    assert label.text() == f"Balance on Jan 1, {year}"
    window.form.year.setValue(year + 1)  # change the year on the Baseline tab
    window._timer.timeout.emit()
    assert plan.start_year == year + 1
    assert label.text() == f"Balance on Jan 1, {year + 1}"


def test_only_capital_gain_incomes_can_be_negative(window):
    ed = window.plan_editor
    plan = window.plan_view.plan
    stream = plan.incomes[1]
    pick(ed, stream)
    form = ed.forms["incomes"]
    assert form.amount.minimum() == 0
    form.kind.setCurrentIndex(form.kind.findData("stcg"))
    assert stream.kind == "stcg" and form.amount.minimum() < 0
    form.amount.setValue(-5_000)
    assert stream.amount == -5_000
    form.kind.setCurrentIndex(form.kind.findData("ltcg"))
    assert stream.amount == -5_000
    form.kind.setCurrentIndex(form.kind.findData("wages"))
    assert form.amount.minimum() == 0 and stream.amount == 0


def test_baseline_allows_negative_short_term_gains(window):
    box = window.form.household_money["short_term_capital_gains"]
    assert box.minimum() < 0
    box.setValue(-2_000)
    assert window.form.household_money["long_term_capital_gains"].minimum() < 0
