"""The .rtpl file type: routing, command-line files and OS registration."""

import configparser
import plistlib
from pathlib import Path

from rtpl.app import documents, shortcut

ROOT = Path(__file__).resolve().parents[1]


def test_window_for_each_extension():
    assert documents.window_for(Path("a.rtpl")) == documents.WINDOW_RTPL
    assert documents.window_for(Path("A.RTPL")) == documents.WINDOW_RTPL
    assert documents.window_for(Path("old.json")) == documents.WINDOW_RTPL
    # The Estimated Quarterly Tax Assistant has a window of its own.
    assert documents.window_for(Path("2026.eqta")) == documents.WINDOW_EQTA
    assert documents.READY_WINDOWS == {documents.WINDOW_RTPL, documents.WINDOW_EQTA}
    assert documents.window_for(Path("notes.txt")) is None


def test_files_from_args_skips_options():
    args = ["-platform", "offscreen", "--style=fusion", r"C:\Plans\Smith plan.rtpl"]
    assert documents.files_from_args(args) == [Path(r"C:\Plans\Smith plan.rtpl")]
    assert documents.files_from_args([]) == []


def test_rtpl_suffix_added_and_offered():
    assert documents.with_rtpl_suffix(Path("x/Smith")) == Path("x/Smith.rtpl")
    assert documents.with_rtpl_suffix(Path("x/Smith v1.2")) == Path("x/Smith v1.2.rtpl")
    assert documents.with_rtpl_suffix(Path("x/old.json")) == Path("x/old.json")
    # The first Save As of an old .json file offers the .rtpl name.
    assert documents.save_as_start(Path("x/old.json"), "y") == str(Path("x/old.rtpl"))
    assert documents.save_as_start(None, "fallback.rtpl") == "fallback.rtpl"


def test_linux_packaging_matches_the_document_types():
    shipped = (ROOT / "packaging" / "linux" / "rtpl-mime.xml").read_text(encoding="utf-8")
    assert shipped == documents.linux_mime_xml()
    desktop = configparser.ConfigParser(interpolation=None)
    desktop.optionxform = str
    desktop.read(ROOT / "packaging" / "linux" / "rtpl.desktop", encoding="utf-8")
    entry = desktop["Desktop Entry"]
    assert entry["MimeType"] == documents.desktop_mime_types()
    assert entry["Exec"].endswith(" %f")


def test_macos_document_types_are_valid_plist():
    entries = documents.macos_plist_entries()
    data = plistlib.loads(plistlib.dumps(entries))
    docs = data["CFBundleDocumentTypes"]
    utis = data["UTExportedTypeDeclarations"]
    assert [d["LSItemContentTypes"] for d in docs] == [[u["UTTypeIdentifier"]] for u in utis]
    assert [u["UTTypeTagSpecification"]["public.filename-extension"] for u in utis] == [["rtpl"], ["eqta"]]
    assert "macos_plist_entries" in (ROOT / "packaging" / "rtpl.spec").read_text(encoding="utf-8")


def test_windows_registry_values():
    values = dict(shortcut.windows_registry_values(r"C:\Apps\RTPL\RetirementTaxPlannerLite.exe", [],
                                                    Path(r"C:\Apps\RTPL\rtpl.ico")))
    prog_id = values[".rtpl"]
    assert values[prog_id] == "RetirementTaxPlannerLite profile or plan"
    assert values[prog_id + r"\shell\open\command"] == r'"C:\Apps\RTPL\RetirementTaxPlannerLite.exe" "%1"'
    assert values[prog_id + r"\DefaultIcon"].endswith(',0')
    assert values[values[".eqta"]] == "Estimated Quarterly Tax Assistant file"
    # From source the command runs Python with the module, then the file.
    cmd = shortcut.windows_open_command(r"C:\venv\pythonw.exe", ["-m", "rtpl.app"])
    assert cmd == r'"C:\venv\pythonw.exe" "-m" "rtpl.app" "%1"'


class FakeWinreg:
    """Just enough of winreg, keyed by path under HKEY_CURRENT_USER."""

    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.keys: dict[str, dict[str, str]] = {}

    class _Handle:
        def __init__(self, values):
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def CreateKey(self, root, key):
        parts = key.split("\\")
        for i in range(1, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:i]).lower(), {})
        return self._Handle(self.keys[key.lower()])

    def OpenKey(self, root, key, *_):
        if key.lower() not in self.keys:
            raise FileNotFoundError(key)
        return self._Handle(self.keys[key.lower()])

    def SetValueEx(self, handle, name, _reserved, _type, value):
        handle.values[name] = value

    def QueryValueEx(self, handle, name):
        if name not in handle.values:
            raise FileNotFoundError(name)
        return handle.values[name], self.REG_SZ

    def DeleteValue(self, handle, name):
        handle.values.pop(name, None)

    def DeleteKey(self, root, key):
        key = key.lower()
        if key not in self.keys:
            raise FileNotFoundError(key)
        if any(k.startswith(key + "\\") for k in self.keys):
            raise PermissionError("has subkeys")
        del self.keys[key]


def test_windows_register_reads_state_and_unregisters():
    reg = FakeWinreg()
    assert not shortcut.windows_registered(reg)
    shortcut.register_windows(reg)
    assert shortcut.windows_registered(reg)
    # Another copy of the app (moved folder) registered: this copy is not ticked.
    command_key = r"Software\Classes\RetirementTaxPlannerLite.rtpl\shell\open\command"
    with reg.OpenKey(reg.HKEY_CURRENT_USER, command_key) as handle:
        real = handle.values[""]
        handle.values[""] = r'"D:\Old\RetirementTaxPlannerLite.exe" "%1"'
    assert not shortcut.windows_registered(reg)
    with reg.OpenKey(reg.HKEY_CURRENT_USER, command_key) as handle:
        handle.values[""] = real
    # The user picked another app with Open with > Always: Explorer uses that.
    choice = shortcut.USER_CHOICE.format(suffix=".rtpl")
    with reg.CreateKey(reg.HKEY_CURRENT_USER, choice) as handle:
        handle.values["ProgId"] = "Applications\\notepad.exe"
    assert not shortcut.windows_registered(reg)
    reg.keys = {k: v for k, v in reg.keys.items() if "fileexts" not in k}
    shortcut.unregister_windows(reg)
    assert not shortcut.windows_registered(reg)
    assert not any("retirementtaxplannerlite.rtpl" in k for k in reg.keys)
    assert r"software\classes\.rtpl" not in reg.keys


def test_unregister_windows_leaves_another_apps_rtpl_key():
    reg = FakeWinreg()
    with reg.CreateKey(reg.HKEY_CURRENT_USER, r"Software\Classes\.rtpl") as handle:
        handle.values[""] = "SomeOtherApp.rtpl"
    shortcut.unregister_windows(reg)
    assert reg.keys[r"software\classes\.rtpl"][""] == "SomeOtherApp.rtpl"


def test_linux_register_reads_state_and_unregisters(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(shortcut, "_run_if_present", lambda *cmd: None)
    default = {"app": "rtpl.desktop"}
    monkeypatch.setattr(shortcut, "_xdg_default", lambda mime: default["app"])
    assert not shortcut.linux_registered()
    shortcut.make_linux(on_desktop=False)
    assert shortcut.linux_registered()
    default["app"] = "org.gnome.TextEditor.desktop"  # user chose another app
    assert not shortcut.linux_registered()
    default["app"] = "rtpl.desktop"
    shortcut.unregister_linux()
    assert not shortcut.linux_registered()
    assert (tmp_path / "share" / "applications" / "rtpl.desktop").exists()  # menu entry stays
