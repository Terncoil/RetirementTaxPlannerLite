"""The EQTA window, offscreen. Skipped without Qt."""

import datetime as dt
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QtCore = pytest.importorskip("PySide6.QtCore")

from rtpl.model.eqta import EqtaFile, Entry, IncomeSource  # noqa: E402
from rtpl.model.profile import Person  # noqa: E402


@pytest.fixture
def win(tmp_path, monkeypatch, lib):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from rtpl.app.eqta_window import EqtaWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    w = EqtaWindow(lib, settings_path=tmp_path / "eqta.ini")
    yield w
    w.setWindowModified(False)
    w.close()
    w.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def sample(state="VA") -> EqtaFile:
    eq = EqtaFile()
    ty = eq.new_year(2026, "Actuals")
    ty.profile.filing_status = "mfj"
    ty.profile.state = state
    ty.profile.people = [Person(name="Alex", birth_year=1958), Person(name="Sam", birth_year=1961)]
    ty.sources = [
        IncomeSource("Alex SS", "social_security", person=0, plan_text="2650*12",
                     entries=[Entry(dt.date(2026, m, 3), "2650") for m in range(1, 12)]),
        IncomeSource("Sam wages", "wages", person=1, notes="Paid on the 28th",
                     entries=[Entry(dt.date(2026, m, 28), "3662.90") for m in range(1, 12)]),
        IncomeSource("Stock fund", "dividends_to_split", qualified_share=0.8,
                     entries=[Entry(dt.date(2026, 3, 25), "1200"), Entry(dt.date(2026, 6, 25), "1300")]),
        IncomeSource("Gains", "ltcg", entries=[Entry(dt.date(2026, 10, 2), "600000")]),
    ]
    ty.payments["state_estimated"].entries = [Entry(dt.date(2026, 5, 1), "400")]
    return eq


def row_list(win):
    grid = win.years.currentWidget().worksheet
    return [(grid.item(r, 0).text(), [grid.item(r, c).text() for c in range(1, grid.columnCount())])
            for r in range(grid.rowCount())]


def cell_rows(win):
    return dict(row_list(win))


def test_new_file_has_this_years_tab(win):
    assert win.years.count() == 1
    assert win.years.tabText(0) == str(dt.date.today().year)
    assert win.windowTitle().startswith("Untitled[*]")
    tree = win.editor.tree
    assert [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())] == [
        "Profile", "Notes", "Payments", "Income and deductions"]


def test_worksheet_rows(win):
    win.load(sample(), None)
    rows = cell_rows(win)
    assert rows["  Alex SS - Alex"] == ["$7,950", "$13,250", "$21,200", "$29,150", "$31,800"]
    assert rows["  Stock fund (estimated, 80%)"][:2] == ["$960", "$960"]
    assert rows["  Stock fund (estimated, 20%)"][:2] == ["$240", "$240"]
    dues = [cells for label, cells in row_list(win) if label == "  Due date"]
    assert [d[0] for d in dues] == ["Apr 15, 2026", "May 1, 2026 †"]
    assert rows["  Income through"][0] == "Apr 30 *"
    assert "Virginia estimated payments" in " ".join(rows)
    assert rows["  Virginia estimated payments"][0] == "$400"
    notes = win.years.currentWidget().notes.text()
    assert "income through Apr 30" in notes and "Virginia due May 1, 2026" in notes
    detail = win.years.currentWidget().detail
    labels = [detail.item(r, 0).text() for r in range(detail.rowCount())]
    assert "  Ordinary income in the 12% bracket" in labels and "Virginia" in labels


def test_editing_a_register_updates_the_worksheet(win):
    ed = win.editor
    ed.add_source("taxable_interest")
    page = ed.item_page
    page.name.setText("Savings")
    page.name.textEdited.emit("Savings")
    table = page.register.table
    table.item(0, 0).setText("3/31")
    table.item(0, 1).setText("100*2")
    table.item(1, 0).setText("4/1")
    table.item(1, 1).setText("50")
    table.item(2, 1).setText("2+")  # rejected: not stored
    year = win.current_year()
    src = year.sources[0]
    assert [(e.date, e.amount) for e in src.entries] == [
        (dt.date(year.year, 3, 31), 200), (dt.date(year.year, 4, 1), 50)]
    assert "Not a number" in page.register.warning.text() or "Only numbers" in page.register.warning.text()
    assert win.isWindowModified()
    win.recompute()
    assert cell_rows(win)["  Savings"][:2] == ["$200", "$250"]
    assert ed.tree.currentItem().text(0) == "Savings"


def test_year_menu(win, monkeypatch):
    win.load(sample(), None)
    answers = iter([(2027, "Projection"), (2025, "Last year"), (2024, "Older")])
    monkeypatch.setattr(win, "_ask_year", lambda *a: next(answers))
    win.clone_year()
    assert [win.years.tabText(i) for i in range(win.years.count())] == ["2027 · Projection", "2026 · Actuals"]
    clone = win.current_year()
    assert clone.year == 2027 and [s.name for s in clone.sources][:2] == ["Alex SS", "Sam wages"]
    assert all(not s.entries and not s.plan_text for s in clone.sources)
    win.new_year()
    assert win.current_year().title == "2025 · Last year" and win.current_year().sources == []
    win.rename_year()
    assert [win.years.tabText(i) for i in range(win.years.count())][-1] == "2024 · Older"
    win.delete_year(confirm=False)
    assert win.years.count() == 2


def test_save_and_open(win, tmp_path, monkeypatch):
    from rtpl.app import eqta_window as ew

    win.load(sample(), None)
    win._mark_modified()
    monkeypatch.setattr(ew.QFileDialog, "getSaveFileName", lambda *a: (str(tmp_path / "taxes"), ""))
    assert win.save()
    path = tmp_path / "taxes.eqta"
    assert path.exists() and win.path == path and not win.isWindowModified()
    assert win.windowTitle().startswith("taxes.eqta[*]")
    win.new_file(ask=False)
    win.open_file(path)
    assert win.current_year().sources[1].entries[0].text == "3662.90"


def test_close_asks_about_its_own_changes(win, monkeypatch):
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a: asked.append(a[2]) or QtWidgets.QMessageBox.StandardButton.Cancel)
    win._mark_modified()
    assert not win.close()
    assert "estimated tax file" in asked[0]


def test_new_year_defaults_dialog(win):
    from rtpl.app.eqta_window import NewYearDefaultsDialog

    dlg = NewYearDefaultsDialog(win.eq.defaults, win)
    dlg.share.setValue(60)
    dlg.retain.setChecked(True)
    d = dlg.defaults()
    assert (d.qualified_share, d.clone_mode) == (0.6, "retain")
    dlg.deleteLater()


def test_fits_the_smallest_screen(win):
    win.load(sample(), None)
    assert win.minimumSizeHint().width() <= 1024


def test_tooltip_lines_fit(win):
    from rtpl.app import TIP_WIDTH

    tips = set()
    for state in ("VA", "CA", "WA", "NY"):
        win.load(sample(state), None)
        tree = win.editor.tree
        it = QtWidgets.QTreeWidgetItemIterator(tree)
        while it.value():
            tree.setCurrentItem(it.value())
            for w in win.findChildren(QtWidgets.QWidget):
                tips.add(w.toolTip())
                if isinstance(w, QtWidgets.QTableWidget):
                    for r in range(w.rowCount()):
                        for c in range(w.columnCount()):
                            if w.item(r, c):
                                tips.add(w.item(r, c).toolTip())
            it += 1
    for menu in win.menuBar().findChildren(QtWidgets.QMenu):
        tips.update(a.toolTip() for a in menu.actions())
    long = sorted({line for t in tips for line in t.split("\n") if len(line) > TIP_WIDTH})
    assert not long, "\n".join(long)


def test_whole_dollars_round_half_up_like_the_irs():
    from rtpl.app.eqta_worksheet import money

    assert [money(v) for v in (2.5, 3.5, 2.49, -2.5, 1234.5, 0.1 * 25)] == [
        "$3", "$4", "$2", "-$3", "$1,235", "$3"]


def test_default_names_show_the_tax_type_and_person(win):
    win.load(sample(), None)
    ty = win.current_year()
    ty.sources[0].name = ""
    win.editor.set_year(win.eq, ty)
    win.recompute()
    labels = [label for label, _ in row_list(win)]
    assert "  Social Security - Alex" in labels and "  Sam wages - Sam" in labels
    assert "  Gains" in labels  # household sources have no person
    node = win.editor._find(("source", 0))
    assert node.text(0) == "Social Security - Alex"
    win.editor.add_source("pension")
    assert win.editor.tree.currentItem().text(0) == "Pension - Alex"
    assert win.editor.item_page.name.text() == "" and win.editor.item_page.name.placeholderText() == "Pension"


def test_mistakes_show_dark_text_on_red(win):
    from PySide6.QtGui import QColor

    from rtpl.app.eqta_editor import ERROR_SHEET

    ed = win.editor
    ed.add_source("wages")
    page = ed.item_page
    page.plan.setText("12*")
    page.plan.textEdited.emit("12*")
    assert page.plan.styleSheet() == ERROR_SHEET and "color: #000000" in ERROR_SHEET
    page.plan.setText("12")
    page.plan.textEdited.emit("12")
    assert page.plan.styleSheet() == ""
    item = page.register.table.item(0, 1)
    item.setText("abc")
    assert item.foreground().color() == QColor("#000000")
    item.setText("5")
    assert item.foreground().style() == QtCore.Qt.BrushStyle.NoBrush


def test_profile_shows_age_at_end_of_year(win):
    win.load(sample(), None)
    page = win.editor.profile_page
    assert page.ages[0].text() == "Age 68 at end of 2026"
    page.people_boxes[0][2].setValue(1960)
    assert page.ages[0].text() == "Age 66 at end of 2026"


def test_buttons_sit_above_the_tree_and_the_register(win):
    ed = win.editor
    ed.add_source("wages")
    pos = lambda w: w.mapTo(win, QtCore.QPoint(0, 0)).y()  # noqa: E731
    win.resize(1100, 700)
    win.show()
    QtWidgets.QApplication.processEvents()
    assert pos(ed.add_btn) < pos(ed.tree)
    reg = ed.item_page.register
    assert pos(reg.delete_btn) < pos(reg.table)
    win.hide()


def test_register_date_picker(win):
    from rtpl.app.eqta_editor import DateEdit

    ed = win.editor
    ed.add_source("wages")
    reg = ed.item_page.register
    year = win.current_year().year
    assert reg.calendar_range == (dt.date(year, 1, 1), dt.date(year, 12, 31))
    editor = reg.table.itemDelegateForColumn(0).createEditor(reg.table.viewport(), None, None)
    assert isinstance(editor, DateEdit)
    editor.setText("3/15")
    editor.show_calendar()
    cal = editor.calendar.actions()[0].defaultWidget()
    assert cal.selectedDate().toPython() == dt.date(year, 3, 15)
    assert cal.maximumDate().toPython() == dt.date(year, 12, 31)
    cal.clicked.emit(cal.selectedDate().addDays(1))
    assert editor.text() == f"03/16/{year}"
    editor.deleteLater()
    # Estimated payments: the next January too.
    node = ed._find(("payment", "federal_estimated"))
    ed.tree.setCurrentItem(node)
    assert reg.calendar_range[1] == dt.date(year + 1, 1, 31)


def test_panel_fits_each_form(win):
    win.load(sample(), None)
    ed = win.editor
    widths = []
    for key in (("profile", None), ("source", 0), ("payment", "state_estimated"), ("source", 2)):
        ed.tree.setCurrentItem(ed._find(key))
        need = ed.minimumSizeHint().width()
        assert win.inputs_panel.minimumWidth() >= need
        assert win.inputs_panel.scroll.horizontalScrollBar().value() == 0
        widths.append(win.inputs_panel.minimumWidth())
    assert max(widths) <= 420  # the long tax type names don't widen it


def test_saved_theme_applies_when_only_the_eqta_window_opens(tmp_path, monkeypatch, lib):
    """A double-clicked .eqta file opens only the EQTA window; it still
    gets the theme chosen in the View menu last time."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from PySide6.QtGui import QPalette

    from rtpl.app.main_window import apply_app_theme, save_theme_mode, saved_theme_mode
    from rtpl.app.windows import AppWindows

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    save_theme_mode("dark")
    windows = AppWindows(lib)
    eqta = windows.eqta_window()
    rtpl = None
    try:
        assert windows.effective_mode == "dark"
        assert app.palette().color(QPalette.ColorRole.Window).lightness() < 80
        assert eqta.theme_actions["dark"].isChecked()
        rtpl = windows.rtpl_window()
        assert rtpl.theme_actions["dark"].isChecked() and rtpl.effective_mode == "dark"
        eqta.theme_actions["light"].trigger()  # either window's View menu sets both
        assert saved_theme_mode() == "light"
        assert rtpl.theme_actions["light"].isChecked() and rtpl.effective_mode == "light"
        assert app.palette().color(QPalette.ColorRole.Window).lightness() > 200
    finally:
        apply_app_theme(app, "light")
        for w in (eqta, rtpl):
            if w is not None:
                w.setWindowModified(False)
                w.close()
                w.deleteLater()
        app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def test_paid_totals_head_their_payment_lines(win):
    win.load(sample(), None)
    labels = [label for label, _ in row_list(win)]
    for total, first in (("Federal paid", "  Federal withholding"), ("Virginia paid", "  Virginia withholding")):
        assert labels.index(first) == labels.index(total) + 1
    grid = win.years.currentWidget().worksheet
    r = labels.index("Federal paid")
    assert grid.item(r, 0).font().bold()
    guidance = labels[labels.index("Guidance (for information)") + 1:]
    assert guidance and all(label.startswith("  ") for label in guidance)


def test_file_opens_at_this_years_tab():
    from rtpl.app.eqta_window import default_year

    eq = EqtaFile()
    for year in (2025, 2027, 2026):
        eq.new_year(year)
    assert [y.year for y in eq.years] == [2027, 2026, 2025]  # tabs newest first
    assert default_year(eq, dt.date(2026, 11, 26)).year == 2026
    assert default_year(eq, dt.date(2030, 1, 5)).year == 2027  # no tab for this year: the newest
    assert default_year(EqtaFile()) is None


def test_file_and_year_notes_keep_bold_and_italic_as_markdown(win, tmp_path):
    from PySide6.QtGui import QTextCursor

    ed = win.editor
    ed.tree.setCurrentItem(ed._find(("notes", None)))
    tabs = ed.notes_page
    assert ed.stack.currentWidget() is tabs
    assert [tabs.tabText(i) for i in range(2)] == ["File", f"Year ({win.current_year().year})"]
    page = tabs.file_page
    page.text.setPlainText("Keep the HSA receipts")
    cur = page.text.textCursor()
    cur.movePosition(QTextCursor.MoveOperation.Start)
    cur.movePosition(QTextCursor.MoveOperation.EndOfWord, QTextCursor.MoveMode.KeepAnchor)
    page.text.setTextCursor(cur)
    page.edit.bold.defaultAction().trigger()
    assert page.text.textCursor().charFormat().fontWeight() >= 700
    assert page.edit.bold.defaultAction().isChecked()
    assert win.eq.notes == "**Keep** the HSA receipts"
    year_page = tabs.year_page
    year_page.text.setPlainText("Convert in *December*")
    ty = win.current_year()
    saved = ty.notes
    assert saved.startswith("Convert in \\*")  # typed asterisks stay literal
    assert win.isWindowModified()
    year_page.set_target(ty)  # opening the page changes nothing
    assert ty.notes == saved and year_page.text.toPlainText() == "Convert in *December*"
    path = tmp_path / "n.eqta"
    win.eq.save(path)
    back = EqtaFile.load(path)
    assert back.notes == "**Keep** the HSA receipts"
    # The file notes show the same text under another year's tree.
    ed.tree.setCurrentItem(ed._find(("profile", None)))
    ed.tree.setCurrentItem(ed._find(("notes", None)))
    assert page.text.toPlainText() == "Keep the HSA receipts"


@pytest.mark.parametrize("text", [
    "a **bold** and *italic* x", "***both*** end", "**Keep**the", "a\\*b\\_c 5\\*3 \\[x\\]",
    "\\- not a list", "\\# not a heading\n\n2\\. not a list", "line1\n\nline2", "back\\\\slash",
])
def test_notes_markdown_round_trips(win, text):
    from PySide6.QtWidgets import QTextEdit

    from rtpl.app.notes_edit import document_markdown

    a, b = QTextEdit(), QTextEdit()
    a.setMarkdown(text)
    b.setMarkdown(document_markdown(a.document()))
    assert b.toHtml() == a.toHtml()


def test_notes_toolbar_and_typing_shortcuts(win):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from rtpl.app.notes_edit import NUMBERED, NotesEdit

    ed = NotesEdit("Notes", "")
    ed.show()
    t = ed.text
    QTest.keyClicks(t, "## Plan")
    QTest.keyClick(t, Qt.Key.Key_Return)
    QTest.keyClicks(t, "- first")
    QTest.keyClick(t, Qt.Key.Key_Return)
    QTest.keyClicks(t, "second")
    QTest.keyClick(t, Qt.Key.Key_Tab)
    assert ed.style.currentText() == "Normal" and ed.bullets.defaultAction().isChecked()
    QTest.keyClick(t, Qt.Key.Key_Return)
    QTest.keyClick(t, Qt.Key.Key_Backtab)
    QTest.keyClicks(t, "third")
    QTest.keyClick(t, Qt.Key.Key_Return)
    QTest.keyClick(t, Qt.Key.Key_Return)  # an empty item ends the list
    QTest.keyClicks(t, "1. one")
    QTest.keyClick(t, Qt.Key.Key_Return)
    QTest.keyClicks(t, "two")
    assert ed.numbers.defaultAction().isChecked()
    assert ed.markdown() == "## Plan\n\n- first\n    - second\n- third\n\n1. one\n2. two"
    ed.toggle_list(NUMBERED)  # the same kind again ends the list
    assert ed.markdown().endswith("- third\n\n1. one\n\ntwo")
    ed.set_heading(1)
    assert ed.markdown().endswith("\n\n# two") and ed.style.currentText() == "Heading 1"
    ed.set_heading(0)
    assert ed.markdown().endswith("\n\ntwo")
    # A # typed mid-line is just text.
    ed.set_markdown("")
    QTest.keyClicks(t, "Item #1 ok")
    assert ed.markdown() == "Item #1 ok"
    ed.deleteLater()
