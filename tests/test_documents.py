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
    # Room for the Estimated Quarterly Tax Assistant: its own window, not ready yet
    assert documents.window_for(Path("2026.eqta")) == documents.WINDOW_EQTA
    assert documents.WINDOW_EQTA not in documents.READY_WINDOWS
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
    (doc,) = data["CFBundleDocumentTypes"]
    (uti,) = data["UTExportedTypeDeclarations"]
    assert doc["LSItemContentTypes"] == [uti["UTTypeIdentifier"]]
    assert uti["UTTypeTagSpecification"]["public.filename-extension"] == ["rtpl"]
    assert "macos_plist_entries" in (ROOT / "packaging" / "rtpl.spec").read_text(encoding="utf-8")


def test_windows_registry_values():
    values = dict(shortcut.windows_registry_values(r"C:\Apps\RTPL\RetirementTaxPlannerLite.exe", [],
                                                    Path(r"C:\Apps\RTPL\rtpl.ico")))
    prog_id = values[".rtpl"]
    assert values[prog_id] == "RetirementTaxPlannerLite profile or plan"
    assert values[prog_id + r"\shell\open\command"] == r'"C:\Apps\RTPL\RetirementTaxPlannerLite.exe" "%1"'
    assert values[prog_id + r"\DefaultIcon"].endswith(',0')
    # From source the command runs Python with the module, then the file.
    cmd = shortcut.windows_open_command(r"C:\venv\pythonw.exe", ["-m", "rtpl.app"])
    assert cmd == r'"C:\venv\pythonw.exe" "-m" "rtpl.app" "%1"'
