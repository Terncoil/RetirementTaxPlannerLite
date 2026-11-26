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
    assert rows["  Alex SS · Alex"] == ["$7,950", "$13,250", "$21,200", "$29,150", "$31,800"]
    assert rows["  Stock fund (estimated, 80%)"][:2] == ["$960", "$960"]
    assert rows["  Stock fund (estimated, 20%)"][:2] == ["$240", "$240"]
    dues = [cells for label, cells in row_list(win) if label == "Due date"]
    assert [d[0] for d in dues] == ["Apr 15, 2026", "May 1, 2026 †"]
    assert rows["Income through"][0] == "Apr 30 *"
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
    assert [win.years.tabText(i) for i in range(win.years.count())] == ["2026 · Actuals", "2027 · Projection"]
    clone = win.current_year()
    assert clone.year == 2027 and [s.name for s in clone.sources][:2] == ["Alex SS", "Sam wages"]
    assert all(not s.entries and not s.plan_text for s in clone.sources)
    win.new_year()
    assert win.current_year().title == "2025 · Last year" and win.current_year().sources == []
    win.rename_year()
    assert [win.years.tabText(i) for i in range(win.years.count())][0] == "2024 · Older"
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
