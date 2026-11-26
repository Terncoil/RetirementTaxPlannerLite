"""Opens the main window offscreen and exercises it. Skipped without Qt."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QtCore = pytest.importorskip("PySide6.QtCore")


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from rtpl.app.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = MainWindow()
    yield w
    w.setWindowModified(False)  # never block on the save prompt in tests
    w.close()
    # Destroy the window (and its child dialogs) now, while QApplication is
    # alive; Qt objects left for interpreter shutdown can crash the process.
    w.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def test_main_window_draws(window, tmp_path):
    from rtpl.model.profile import Profile

    path = Path("examples/profiles/retired_couple_ca.json")
    window._load_profile(Profile.load(path), path)
    window.recompute()
    assert window.series is not None and window.series.state.code == "CA"
    assert window.checks["state"].text() == "State Tax (CA)"
    assert window.table.rowCount() > 0
    window.checks["state"].click()  # user turns the state layer off
    assert window.visibility()["state"] is False
    window.set_theme_mode("dark")
    out = tmp_path / "chart.svg"
    window.figure.savefig(out)
    assert out.stat().st_size > 0


def test_title_tracks_file_and_changes(window):
    from rtpl.model.profile import Profile

    assert window.windowTitle().startswith("Untitled[*]")
    assert not window.isWindowModified()
    path = Path("examples/profiles/retired_couple_ca.json")
    window._load_profile(Profile.load(path), path)
    assert window.windowTitle().startswith("retired_couple_ca.json[*]")
    window.form.plan_name.setText("Edited")
    assert window.isWindowModified()
    window.setWindowModified(False)
    window.new_profile()
    assert window.windowTitle().startswith("Untitled[*]")


def test_age_shown_next_to_birth_year(window):
    box = window.form.person_boxes[0]
    window.form.year.setValue(2026)
    box.birth_year.setValue(1958)
    assert box.age.text() == "Age 68 at end of 2026"
    window.form.year.setValue(2030)
    assert box.age.text() == "Age 72 at end of 2030"


def test_safe_file_stem():
    from rtpl.app.main_window import safe_file_stem

    assert safe_file_stem('Plan: A/B "v2"?') == "Plan_ A_B _v2__"
    assert safe_file_stem("  trailing dots... ") == "trailing dots"
    assert safe_file_stem("CON") == "_CON"
    assert safe_file_stem("nul.backup") == "_nul.backup"
    assert safe_file_stem("") == "profile"
    assert safe_file_stem("a\\b|c<d>e*f") == "a_b_c_d_e_f"


def test_color_dialog_link_and_reset(window):
    from rtpl.app.main_window import ColorDialog
    from rtpl.theme import Theme

    theme = Theme.load(None)
    labels = {k: k for k in theme.raw["light"]["layers"]}
    dlg = ColorDialog(theme, labels, window)  # parented, so it is cleaned up with the window
    theme.set_layer_color("light", "ordinary", "#123456")
    dlg.same.setChecked(True)
    assert theme.linked
    assert theme.palette("dark").layers["ordinary"] == "#123456"
    assert dlg.buttons[("dark", "ordinary")].isHidden()
    dlg._reset()
    assert theme.raw["light"]["layers"]["ordinary"] == Theme.default_raw()["light"]["layers"]["ordinary"]
    assert theme.palette("dark").layers == theme.palette("light").layers
    dlg.same.setChecked(False)
    assert theme.palette("dark").surface != theme.palette("light").surface
    assert "same_layer_colors = false" in theme.to_toml()


def test_inflation_and_irmaa_basis_controls(window):
    window.form.inflation.setValue(3.0)
    assert window.form.profile().inflation == 0.03
    window.irmaa_basis.setCurrentIndex(1)
    window.recompute()
    irmaa = [a for a in window.series.annotations if a.kind == "irmaa"]
    assert irmaa and "published thresholds" in irmaa[0].detail


def test_forward_plan_page(window):
    from rtpl.model.plan import Plan

    path = Path("examples/plans/couple_conversions_inherited.json")
    window.load_plan(Plan.load(path), path)
    view = window.plan_view
    assert view.result is not None and view.result.years
    assert window.form.profile().state == "NC"
    assert view.table.rowCount() == len(view.result.years)
    assert view.line_checks["bracket:0.22"].isChecked()
    view.line_checks["bracket:0.22"].click()  # user hides the 22% line
    assert view.visibility()["bracket:0.22"] is False
    view.detail.setChecked(True)
    view.todays.setChecked(True)
    # Each chart tab exports its own figure.
    assert [view.tabs.tabText(i) for i in range(view.tabs.count())] == [
        "&Income and brackets", "Tax&es by source", "&Balances", "&Year table"]
    for canvas, fig in ((view.income_canvas, view.income_fig), (view.tax_canvas, view.tax_fig),
                        (view.bal_canvas, view.bal_fig)):
        view.tabs.setCurrentWidget(canvas)
        assert view.current_figure() is fig
    assert "Taxes by source" in view.tax_fig.axes[0].get_title(loc="left")
    window.new_plan()
    assert view.plan is not None and view.result is not None


def test_graph_options_follow_active_page(window):
    """The Graph options panel shows the options of whichever page is active."""
    assert window.options_stack.currentIndex() == 0
    assert "Marginal graph options" in window.options_panel.title.text()
    window.pages.setCurrentWidget(window.plan_view)
    assert window.options_stack.currentWidget() is window.plan_view.options_widget
    assert "Forward plan options" in window.options_panel.title.text()
    window.pages.setCurrentIndex(0)
    assert "Marginal graph options" in window.options_panel.title.text()


def test_empty_plan_page_uses_theme_background(window):
    """Before any plan is loaded the blank charts take the theme's background."""
    from matplotlib.colors import to_hex

    window.recompute()  # the default profile's first draw passes the palette on
    view = window.plan_view
    assert view.result is None
    dark = window.theme.palette("dark")
    view.set_palette(dark)
    assert to_hex(view.income_fig.get_facecolor()) == to_hex(dark.surface)
    assert to_hex(view.bal_fig.get_facecolor()) == to_hex(dark.surface)
    assert to_hex(view.tax_fig.get_facecolor()) == to_hex(dark.surface)


def test_window_fits_small_screens(window):
    """With both panels pinned, only the panels and the chart set the width."""
    for panel in (window.inputs_panel, window.options_panel):
        window.panels.set_pinned(panel, True)
        window.panels.set_open(panel, True)
    # Pixel widths follow the font, which differs a lot between CI runners,
    # so check the layout instead: nothing but the two panels and the
    # chart's own minimum may set the window's minimum width (before, the
    # graph options row above the chart forced about 1,440 px).
    parts = (
        window.inputs_panel.minimumWidth()
        + window.options_panel.minimumWidth()
        + window.tabs.minimumSizeHint().width()
    )
    assert window.minimumSizeHint().width() <= parts + 40


def test_panels_pin_and_pop_open(window):
    panels, inputs = window.panels, window.inputs_panel
    window.resize(1024, 740)
    window.show()
    panels.set_pinned(inputs, False)
    panels.set_open(inputs, False)
    assert inputs.isHidden() and not window.inputs_action.isChecked()
    window.inputs_action.trigger()  # pops open over the graph
    assert inputs.isVisible() and inputs.parent() is window.centralWidget()
    assert window.inputs_action.isChecked()
    panels.close_floating()
    assert inputs.isHidden()
    inputs.auto_hide.click()  # turning Auto hide off pins it beside the graph
    assert inputs.parent() is panels.splitter
    assert window.settings.value("panels/inputs_pinned") in (True, "true")


def test_window_geometry_and_panel_width_restored(window):
    from rtpl.app.main_window import MainWindow

    window.resize(700, 500)  # the offscreen test screen is 800 x 600
    wide = window.inputs_panel.minimumWidth() + 40
    window.inputs_panel.saved_width = wide
    window.save_window_state()
    again = MainWindow()
    try:
        again.resize_to_screen()
        assert (again.width(), again.height()) == (700, 500)
        assert again.inputs_panel.preferred_width() == wide
        again.reset_layout()
        assert again.settings.value("window/geometry") is None
    finally:
        again.setWindowModified(False)
        again.deleteLater()


def test_auto_hide_check_box_shows_state(window):
    panels, inputs = window.panels, window.inputs_panel
    panels.set_pinned(inputs, True)
    assert not inputs.auto_hide.isChecked() and inputs.auto_hide.text() == "Auto hide"
    inputs.auto_hide.click()
    assert not panels.is_pinned(inputs) and inputs.parent() is window.centralWidget()


@pytest.mark.parametrize("which", ["inputs_panel", "options_panel"])
def test_switching_to_auto_hide_keeps_the_panel_open_until_dismissed(window, which):
    """Ticking Auto hide on a pinned panel moves it over the graph; the move
    must not count as focus leaving it, or the panel is marked closed while
    it stays on screen, and the graph or the X can't hide it."""
    from PySide6.QtTest import QTest

    app = QtWidgets.QApplication.instance()
    panels, panel = window.panels, getattr(window, which)
    window.resize(1024, 740)
    window.show()
    panels.set_pinned(panel, True)
    app.processEvents()
    QTest.mouseClick(panel.auto_hide, QtCore.Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not panels.is_pinned(panel)
    assert panels.is_open(panel) and panel.isVisible()
    QTest.mouseClick(panel.close_btn, QtCore.Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not panels.is_open(panel) and panel.isHidden()
    # Pin it again, switch to Auto hide, then click the graph.
    panels.set_pinned(panel, True)
    app.processEvents()
    QTest.mouseClick(panel.auto_hide, QtCore.Qt.MouseButton.LeftButton)
    app.processEvents()
    graph = window.pages.currentWidget()
    QTest.mouseClick(graph, QtCore.Qt.MouseButton.LeftButton, pos=graph.rect().center())
    app.processEvents()
    assert not panels.is_open(panel) and panel.isHidden()


def test_forward_plan_page_does_not_widen_window(window):
    from rtpl.model.plan import Plan

    path = Path("examples/plans/couple_conversions_inherited.json")
    window.load_plan(Plan.load(path), path)
    # The plan page's options scroll instead of setting a minimum width.
    assert window.plan_view.minimumSizeHint().width() <= window.tabs.minimumSizeHint().width() + 40


def test_file_open_recognizes_plan_files(window):
    window.open_file(Path("examples/plans/couple_conversions_inherited.json"))
    assert window.plan_view.result is not None
    assert window.pages.currentWidget() is window.plan_view
    window.open_file(Path("examples/profiles/retired_couple_ca.json"))
    assert window.plan_view.plan is None and window.form.profile().state == "CA"


def _all_tooltips(window) -> set[str]:
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QTableWidget, QTreeWidget

    tips = set()
    for w in window.findChildren(QtWidgets.QWidget):
        tips.add(w.toolTip())
        if isinstance(w, QTableWidget):
            for c in range(w.columnCount()):
                head = w.horizontalHeaderItem(c)
                tips.add(head.toolTip() if head else "")
        if isinstance(w, QTreeWidget):
            it = QtWidgets.QTreeWidgetItemIterator(w)
            while it.value():
                tips.update(it.value().toolTip(c) for c in range(w.columnCount()))
                it += 1
    tips.update(a.toolTip() for a in window.findChildren(QAction))
    return tips


def test_tooltip_lines_fit_the_smallest_screen(window):
    """Tooltips are plain text and Qt doesn't wrap them: break lines by hand
    at TIP_WIDTH characters (see rtpl/app/__init__.py)."""
    from rtpl.app import TIP_WIDTH
    from rtpl.app.plan_editor import ROLE
    from rtpl.engine.forward import TABLE_COLUMN_KEYS
    from rtpl.model.plan import Plan
    from rtpl.model.profile import Profile

    tips = set()
    for state in ("CA", "NC", "WA", "MT"):  # state notes and sources from the data files
        prof = Profile.load(Path("examples/profiles/retired_couple_ca.json"))
        prof.state = state
        window._load_profile(prof, None)
        window.recompute()
        tips |= _all_tooltips(window)
    path = Path("examples/plans/couple_conversions_inherited.json")
    window.load_plan(Plan.load(path), path)
    view = window.plan_view
    view.set_table_columns(list(TABLE_COLUMN_KEYS))
    for pinned in (True, False):
        window.inputs_panel.auto_hide.setChecked(not pinned)
        tips |= _all_tooltips(window)
    ed = window.plan_editor
    it = QtWidgets.QTreeWidgetItemIterator(ed.tree)
    while it.value():  # fill each form, for tooltips set as it loads
        if it.value().data(0, ROLE):
            ed.tree.setCurrentItem(it.value())
            tips |= _all_tooltips(window)
        it += 1
    long = sorted({line for t in tips for line in t.split("\n") if len(line) > TIP_WIDTH})
    assert not long, f"Tooltip lines over {TIP_WIDTH} characters:\n" + "\n".join(long)


def test_year_table_hides_a_tooltip_over_a_cell_without_one(window):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QToolTip

    from rtpl.model.plan import Plan

    path = Path("examples/plans/couple_conversions_inherited.json")
    window.load_plan(Plan.load(path), path)
    table = window.plan_view.table
    for c in range(table.columnCount()):  # every header explains itself or names itself
        assert table.horizontalHeaderItem(c).toolTip()
    QToolTip.showText(table.mapToGlobal(QPoint(5, 5)), "stale tooltip", table)
    event = QHelpEvent(QtCore.QEvent.Type.ToolTip, QPoint(5, 5), table.viewport().mapToGlobal(QPoint(5, 5)))
    assert window.plan_view.eventFilter(table.viewport(), event) is True
    from PySide6.QtTest import QTest

    QTest.qWait(600)  # Qt hides a tooltip after a short fade
    assert not QToolTip.isVisible()


def test_file_menu_groups_and_report_item(window):
    menu = window.menuBar().actions()[0].menu()
    groups, current = [], []
    for act in menu.actions():
        if act.isSeparator():
            groups.append(current)
            current = []
        else:
            current.append(act.text().replace("&", ""))
    groups.append(current)
    assert [len(g) for g in groups] == [5, 1, 3, 1]
    assert groups[2][-1] == "Export Report…"


def test_export_report_with_and_without_plan(window, tmp_path):
    from rtpl.model.plan import Plan
    from rtpl.model.profile import Profile
    from rtpl.report import write_report

    path = Path("examples/profiles/retired_couple_ca.json")
    window._load_profile(Profile.load(path), path)
    window.recompute()
    window.set_theme_mode("dark")  # the report is still drawn in light mode
    title, sections, charts = window.report_contents()
    assert [c.title for c in charts] == ["Marginal tax graph"]
    assert sections[0].rows[0] == ("Name", window.form.profile().name)
    out = tmp_path / "profile.pdf"
    assert write_report(out, title, sections, charts) == 2
    assert out.read_bytes().startswith(b"%PDF")

    plan_path = Path("examples/plans/couple_conversions_inherited.json")
    window.load_plan(Plan.load(plan_path), plan_path)
    window.recompute()
    title, sections, charts = window.report_contents()
    assert [c.title for c in charts] == ["Marginal tax graph", "Income and brackets", "Taxes by source", "Balances"]
    assert sections[-1].heading == "Forward plan"
    out = tmp_path / "plan.pdf"
    assert write_report(out, title, sections, charts) == 5


def test_light_mode_uses_light_widget_colors_whatever_the_system_scheme(window):
    from PySide6.QtGui import QPalette

    app = QtWidgets.QApplication.instance()
    window.set_theme_mode("dark")
    assert app.palette().color(QPalette.ColorRole.Window).lightness() < 128
    window.set_theme_mode("light")
    pal = app.palette()
    assert pal.color(QPalette.ColorRole.Window).lightness() > 200
    assert pal.color(QPalette.ColorRole.Base).lightness() > 200
    assert pal.color(QPalette.ColorRole.Mid) != pal.color(QPalette.ColorRole.Window)  # separators show
    window.set_theme_mode("system")


@pytest.mark.parametrize("pinned", [True, False])
def test_side_panels_follow_each_theme_change(window, pinned):
    window.resize(1024, 740)
    window.show()
    panel = window.inputs_panel
    window.panels.set_pinned(panel, pinned)
    window.panels.set_open(panel, True)
    for mode, light in (("light", True), ("dark", False), ("light", True), ("dark", False)):
        window.set_theme_mode(mode)
        QtWidgets.QApplication.instance().processEvents()
        img = panel.grab().toImage()
        color = img.pixelColor(img.width() // 2, img.height() - 6)
        assert (color.lightness() > 128) is light, (mode, color.name())
    window.set_theme_mode("system")


def test_clicking_a_bar_shows_its_breakdown(window):
    """A click on a bar lists its segments, the total, then the lines crossing it;
    a click off the bars or Escape closes the list."""
    from matplotlib.backend_bases import KeyEvent, MouseEvent

    from rtpl.model.plan import Plan

    path = Path("examples/plans/couple_conversions_inherited.json")
    window.load_plan(Plan.load(path), path)
    view = window.plan_view
    view.tabs.setCurrentWidget(view.income_canvas)
    canvas, fig, popup = view.income_canvas, view.income_fig, view.popups[0]
    canvas.draw()
    index = fig.rtpl_bars
    year, bar = next((y, b) for y, b in index.bars.items() if b.lines)
    assert abs(bar.total - sum(s[2] for s in bar.segments)) < 1
    assert all(0 <= v <= bar.total for _l, _c, v in bar.lines)

    def click(x, y, button=1):
        px, py = index.ax.transData.transform((x, y))
        MouseEvent("button_press_event", canvas, px, py, button=button)._process()

    click(year, bar.total / 2)
    assert (not popup.isHidden()) and popup.bar is bar
    text = popup.text()
    assert "Total income" in text and "Lines crossing this bar" in text
    # Segments come first, then the total, then the lines.
    assert text.index(bar.segments[0][0]) < text.index("Total income") < text.index(bar.lines[0][0])
    assert popup.geometry().right() <= canvas.width() and popup.geometry().bottom() <= canvas.height()

    click(year, bar.total * 1.03)  # above the bar
    assert not (not popup.isHidden())
    click(year, bar.total / 2)
    KeyEvent("key_press_event", canvas, "escape")._process()
    assert not (not popup.isHidden())
    click(year, bar.total / 2)
    view.todays.setChecked(True)  # a redraw closes it, since the bars moved
    assert not (not popup.isHidden())

    view.tabs.setCurrentWidget(view.tax_canvas)
    view.tax_canvas.draw()
    tax_index = view.tax_fig.rtpl_bars
    ty, tbar = next(iter(tax_index.bars.items()))
    px, py = tax_index.ax.transData.transform((ty, tbar.total / 2))
    MouseEvent("button_press_event", view.tax_canvas, px, py, button=1)._process()
    assert (not view.popups[1].isHidden()) and "Total tax" in view.popups[1].text()
    view.tabs.setCurrentWidget(view.bal_canvas)
    assert not (not view.popups[1].isHidden())


def test_aca_and_education_fields_round_trip(window, tmp_path):
    from rtpl.model.profile import Profile

    path = Path("examples/profiles/early_retirees_college_aca.json")
    original = Profile.load(path)
    window._load_profile(original, path)
    window.recompute()
    p = window.form.profile()
    assert p.people[0].aca_benchmark_premium == 1100 and p.people[0].aca_plan_premium == 800
    h = p.household
    assert (h.aca_plan_tier, h.aotc_students, h.aotc_expenses_per_student, h.education_last_year) == (
        "bronze", 2, 4000, 2028)
    for key in ("edu", "nonref", "ptc"):
        assert window.checks[key].isChecked()
    assert window.figure.axes[0].get_ylim()[0] < 0
    # "This year only" is the lowest value and saves as None.
    window.form.education_last.setValue(window.form.education_last.minimum())
    assert window.form.profile().household.education_last_year is None
    assert window.form.education_last.text() == "This year only"



def test_charitable_gifts_field(window):
    form = window.form
    box = form.household_money["charitable_cash_gifts"]
    box.setValue(1_500)
    assert form.profile().household.charitable_cash_gifts == 1_500
    # The tooltip quotes the cap from the year's tax law and filing status.
    form.year.setValue(2026)
    form.filing.setCurrentIndex(form.filing.findData("single"))
    assert "$1,000" in box.toolTip() and "2026" in box.toolTip()
    form.filing.setCurrentIndex(form.filing.findData("mfj"))
    assert "$2,000" in box.toolTip()
    form.year.setValue(2025)
    assert "no deduction" in box.toolTip() and "$" not in box.toolTip()
    stcg = form.household_money["short_term_capital_gains"]
    form.filing.setCurrentIndex(form.filing.findData("mfs"))
    assert "$1,500" in stcg.toolTip()
    form.filing.setCurrentIndex(form.filing.findData("single"))
    assert "$3,000" in stcg.toolTip()



def test_check_boxes_have_their_label_on_the_left(window):
    from PySide6.QtWidgets import QFormLayout

    form = window.form
    for box, text in ((form.person_boxes[0].blind, "Legally blind"),
                      (form.brokerage, "Taxable brokerage account"),
                      (form.aca, "ACA marketplace coverage")):
        assert box.text() == ""
        layout = box.parentWidget().layout()
        assert isinstance(layout, QFormLayout)
        label = layout.labelForField(box)
        assert label is not None and label.text() == text
        assert box.accessibleName() == text

def test_law_tips_read_the_tax_law_files():
    from dataclasses import replace

    from rtpl.app.profile_form import charity_tip, stcg_tip
    from rtpl.taxlaw.loader import TaxLawLibrary

    law = TaxLawLibrary().federal(2030)
    changed = replace(law, charitable_nonitemizer_limit={"single": 1500, "mfj": 3000, "mfs": 1500, "hoh": 1500},
                      capital_loss_limit={"single": 4000, "mfj": 4000, "mfs": 2000, "hoh": 4000})
    assert "$1,500" in charity_tip(changed, "single") and "2030" in charity_tip(changed, "single")
    assert "$3,000" in charity_tip(changed, "mfj")
    assert "$2,000" in stcg_tip(changed, "mfs")
    for tip in (charity_tip(changed, "mfj"), stcg_tip(changed, "mfs")):
        assert all(len(line) <= 70 for line in tip.split("\n"))

def test_new_profile_defaults(window, monkeypatch):
    """View > New Profile Defaults sets what File > New starts from, kept
    in the settings folder rather than in any profile file."""
    from rtpl.app import main_window as mw

    builtin = window._starter_profile()
    assert window._new_profile_defaults().to_dict() == builtin.to_dict()

    def single_ny(dlg):
        f = dlg.form
        f.filing.setCurrentIndex(f.filing.findData("single"))
        f.state.setCurrentIndex(f.state.findData("NY"))
        f.inflation.setValue(3.0)
        f.person_boxes[0].money["social_security"].setValue(0)
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw.NewProfileDefaultsDialog, "exec", single_ny)
    window.edit_new_profile_defaults()
    assert window.new_defaults_path.exists()
    window.new_profile()
    p = window.form.profile()
    assert (p.filing_status, p.state, p.inflation) == ("single", "NY", 0.03)
    assert len(p.people) == 1 and p.people[0].social_security == 0

    # The open profile is untouched by later edits to the defaults, and
    # resetting to the built-in example removes the settings file.
    def reset(dlg):
        dlg.form.set_profile(dlg._builtin)
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(mw.NewProfileDefaultsDialog, "exec", reset)
    window.edit_new_profile_defaults()
    assert not window.new_defaults_path.exists()
    assert window.form.profile().state == "NY"

    # The tax year follows the calendar, even in saved defaults.
    import datetime
    window.new_defaults_path.write_text('{"year": 2020, "state": "NY"}', encoding="utf-8")
    assert window._new_profile_defaults().year == datetime.date.today().year
    assert builtin.year == datetime.date.today().year

    # A damaged file falls back to the built-in example.
    window.new_defaults_path.write_text("not json", encoding="utf-8")
    assert window._new_profile_defaults().to_dict() == builtin.to_dict()


def test_help_project_home_opens_browser(window, monkeypatch):
    from rtpl.app import main_window as mw

    opened = []
    monkeypatch.setattr(mw.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    help_menu = next(a.menu() for a in window.menuBar().actions() if a.text() == "&Help")
    next(a for a in help_menu.actions() if a.text() == "&Project Home…").trigger()
    assert opened == [mw.PROJECT_URL]


def test_save_as_offers_rtpl_and_opens_it(window, tmp_path, monkeypatch):
    """Save As names the file .rtpl (even when the old file was .json), and
    the saved file opens again, as a double-click passes it to the app."""
    import shutil

    from rtpl.app import main_window as mw

    old = tmp_path / "couple.json"
    shutil.copy("examples/profiles/retired_couple_ca.json", old)
    window.open_file(old)
    offered = {}

    def fake_save(parent, caption, start, filters):
        offered["start"], offered["filters"] = start, filters
        return str(tmp_path / "couple"), ""  # no extension typed

    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", fake_save)
    assert window.save_as()
    assert offered["start"] == str(tmp_path / "couple.rtpl")
    assert offered["filters"].startswith("RetirementTaxPlannerLite files (*.rtpl)")
    saved = tmp_path / "couple.rtpl"
    assert window.current_path == saved and saved.exists()
    assert window.windowTitle().startswith("couple.rtpl[*]")

    window.new_profile()
    from rtpl.app.__main__ import DocumentRouter

    DocumentRouter(window).open(saved, ask=False)
    assert window.current_path == saved
    assert window.form.profile().state == "CA"


def test_eqta_file_waits_for_its_window(window, monkeypatch):
    from rtpl.app import __main__ as entry

    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a: shown.append(a[2]))
    opened = []
    monkeypatch.setattr(window, "open_file", opened.append)
    router = entry.DocumentRouter(window)
    router.open(Path("2026.eqta"))
    assert opened == [] and "later version" in shown[0]
    router.open(Path("plan.rtpl"), ask=False)
    assert opened == [Path("plan.rtpl")]


def test_macos_file_open_event_reaches_the_window(window, monkeypatch):
    """Finder sends a file-open event to the running app instead of argv."""
    from PySide6.QtGui import QFileOpenEvent

    from rtpl.app.__main__ import DocumentRouter, application_class

    cls = application_class()
    opened = []
    monkeypatch.setattr(window, "open_from_os", opened.append)
    # The running QApplication is the test's own, so call the handler directly.
    fake = type("App", (), {"router": DocumentRouter(window), "pending": []})()
    cls.event(fake, QFileOpenEvent("/tmp/x.rtpl"))
    assert opened == [Path("/tmp/x.rtpl")]
