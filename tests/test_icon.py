"""The app icon ships with the package and reaches Qt and the shortcuts."""

import os

import pytest

from rtpl.app import icon, shortcut


def test_icon_files_ship():
    sizes = [int(p.stem.split("_")[1]) for p in icon.icon_png_paths()]
    assert sizes == sorted(sizes) and sizes[0] == 16 and sizes[-1] >= 256
    assert icon.ico_path().read_bytes()[:4] == b"\x00\x00\x01\x00"


def test_desktop_entry_quotes_paths():
    text = shortcut.desktop_entry("/home/me/my venv/bin/rtpl", [], "/x/rtpl_256.png")
    assert 'Exec="/home/me/my venv/bin/rtpl" %f\n' in text
    assert "MimeType=application/x-rtpl;application/x-eqta;\n" in text
    assert "Icon=/x/rtpl_256.png\n" in text
    assert f"StartupWMClass={icon.DESKTOP_FILE_NAME}\n" in text


def test_windows_script_escapes_quotes(tmp_path):
    script = shortcut.windows_shortcut_script("C:\\O'Neil\\rtpl.exe", [], tmp_path / "rtpl.ico")
    assert "'C:\\O''Neil\\rtpl.exe'" in script
    assert "IconLocation" in script and ",0'" in script


def test_linux_shortcut(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("HOME", str(tmp_path))
    ran = []
    monkeypatch.setattr(shortcut, "_run_if_present", lambda *cmd: ran.append(cmd))
    made = shortcut.make_linux()
    entry = (tmp_path / "share" / "applications" / "rtpl.desktop").read_text()
    assert made and "Icon=" in entry and "Exec=" in entry
    mime = tmp_path / "share" / "mime" / "packages" / "rtpl.xml"
    assert '<glob pattern="*.rtpl"/>' in mime.read_text()
    assert ("xdg-mime", "default", "rtpl.desktop", "application/x-rtpl") in ran


def test_qt_icon_and_about_pixmap():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    icon.install_app_icon(app)
    sizes = {(s.width(), s.height()) for s in app.windowIcon().availableSizes()}
    assert (16, 16) in sizes and (256, 256) in sizes
    pix = icon.about_pixmap(64, 2.0)
    assert pix.width() == 128 and pix.devicePixelRatio() == 2.0
