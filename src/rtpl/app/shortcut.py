"""Make a desktop and Start-menu shortcut, with the app icon, for the app
as installed in this Python environment: ``uv run rtpl-shortcut``.

It also registers the ``.rtpl`` file type, so double-clicking a file opens
it in the app (see ``rtpl.app.documents``). View > Open .rtpl Files With
This App does only that, for the downloaded Windows build too.

Packaged builds get their shortcuts from the installer instead (see
``docs/packaging.md``).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from rtpl.app.documents import DOCUMENT_TYPES, desktop_mime_types, linux_mime_xml
from rtpl.app.icon import DESKTOP_FILE_NAME, ico_path, largest_png

APP_NAME = "RetirementTaxPlannerLite"
COMMENT = "Marginal and forward-looking tax graphs"


def launch_command() -> tuple[str, list[str]]:
    """The program (and its arguments) that opens the app without a console."""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return str(exe), []
    scripts = exe.parent
    for name in ("rtpl.exe", "rtpl"):
        if (scripts / name).exists():
            return str(scripts / name), []
    found = shutil.which("rtpl")
    if found:
        return found, []
    pythonw = scripts / "pythonw.exe"
    return str(pythonw if pythonw.exists() else exe), ["-m", "rtpl.app"]


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def windows_shortcut_script(target: str, args: list[str], icon: Path) -> str:
    """PowerShell that writes the .lnk files on the Desktop and in the Start menu."""
    arguments = " ".join(f'"{a}"' for a in args)
    return "\n".join(
        [
            "$shell = New-Object -ComObject WScript.Shell",
            "$folders = @([Environment]::GetFolderPath('Desktop'), "
            "[Environment]::GetFolderPath('Programs'))",
            "foreach ($folder in $folders) {",
            f"  $lnk = $shell.CreateShortcut((Join-Path $folder {_ps_quote(APP_NAME + '.lnk')}))",
            f"  $lnk.TargetPath = {_ps_quote(target)}",
            f"  $lnk.Arguments = {_ps_quote(arguments)}",
            f"  $lnk.WorkingDirectory = {_ps_quote(str(Path.home()))}",
            f"  $lnk.IconLocation = {_ps_quote(str(icon) + ',0')}",
            f"  $lnk.Description = {_ps_quote(COMMENT)}",
            "  $lnk.Save()",
            "  Write-Output (Join-Path $folder " + _ps_quote(APP_NAME + ".lnk") + ")",
            "}",
        ]
    )


def desktop_exec_line(target: str, args: list[str]) -> str:
    # %f: the file manager puts the double-clicked file's path here.
    return " ".join(_desktop_quote(a) for a in [target, *args]) + " %f"


def desktop_entry(target: str, args: list[str], icon: Path | str) -> str:
    exec_line = desktop_exec_line(target, args)
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Comment={COMMENT}\n"
        f"Exec={exec_line}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Office;Finance;\n"
        f"MimeType={desktop_mime_types()}\n"
        f"StartupWMClass={DESKTOP_FILE_NAME}\n"
    )


def _desktop_quote(arg: str) -> str:
    if any(c in arg for c in ' \t"\'\\$`'):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'
    return arg


def windows_open_command(target: str, args: list[str]) -> str:
    """The shell\\open\\command value: the app, then the double-clicked file."""
    return " ".join(f'"{a}"' for a in [target, *args, "%1"])


def windows_registry_values(target: str, args: list[str], icon: Path) -> list[tuple[str, str]]:
    """(key under HKCU\\Software\\Classes, default value) for each document type.

    Per-user keys need no administrator rights, which suits a build unzipped
    anywhere as well as a Python environment."""
    values = []
    for doc in DOCUMENT_TYPES:
        values += [
            (doc.suffix, doc.prog_id),
            (doc.prog_id, doc.description),
            (rf"{doc.prog_id}\DefaultIcon", f'"{icon}",0'),
            (rf"{doc.prog_id}\shell\open\command", windows_open_command(target, args)),
        ]
    return values


CLASSES = r"Software\Classes"
# Where Explorer keeps the app a user picked with "Open with > Always". It
# wins over Software\Classes and only the user can change it.
USER_CHOICE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{suffix}\UserChoice"


def _notify_explorer() -> None:
    """Tell Explorer the associations changed (SHCNE_ASSOCCHANGED, SHCNF_IDLIST)."""
    import ctypes

    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def _winreg():
    import winreg

    return winreg


def _read_default(reg, key: str) -> str | None:
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, key) as handle:
            return reg.QueryValueEx(handle, "")[0]
    except OSError:
        return None


def _read_value(reg, key: str, name: str) -> str | None:
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, key) as handle:
            return reg.QueryValueEx(handle, name)[0]
    except OSError:
        return None


def windows_user_choice(reg=None) -> dict[str, str]:
    """{suffix: ProgID} for document types the user tied to some app in Explorer."""
    reg = reg or _winreg()
    found = {}
    for doc in DOCUMENT_TYPES:
        prog = _read_value(reg, USER_CHOICE.format(suffix=doc.suffix), "ProgId")
        if prog:
            found[doc.suffix] = prog
    return found


def windows_registered(reg=None) -> bool:
    """True when double-clicking each document type opens this copy of the app."""
    reg = reg or _winreg()
    target, args = launch_command()
    command = windows_open_command(target, args)
    for doc in DOCUMENT_TYPES:
        if _read_default(reg, rf"{CLASSES}\{doc.suffix}") != doc.prog_id:
            return False
        if _read_default(reg, rf"{CLASSES}\{doc.prog_id}\shell\open\command") != command:
            return False
    choices = windows_user_choice(reg)
    return all(choices.get(doc.suffix, doc.prog_id) == doc.prog_id for doc in DOCUMENT_TYPES)


def register_windows(reg=None) -> list[str]:
    """Register the document types for the current user (Windows only).

    ``reg`` stands in for ``winreg`` in tests; Explorer is told only then."""
    real = reg is None
    reg = reg or _winreg()
    target, args = launch_command()
    made = []
    for key, value in windows_registry_values(target, args, ico_path()):
        with reg.CreateKey(reg.HKEY_CURRENT_USER, rf"{CLASSES}\{key}") as handle:
            reg.SetValueEx(handle, "", 0, reg.REG_SZ, value)
        made.append(rf"HKCU\{CLASSES}\{key}")
    if real:
        _notify_explorer()
    return made


def unregister_windows(reg=None) -> None:
    """Remove the keys ``register_windows`` wrote. A ``.rtpl`` key that now
    names another app's ProgID is left alone."""
    real = reg is None
    reg = reg or _winreg()
    for doc in DOCUMENT_TYPES:
        for sub in (r"\shell\open\command", r"\shell\open", r"\shell", r"\DefaultIcon", ""):
            try:
                reg.DeleteKey(reg.HKEY_CURRENT_USER, rf"{CLASSES}\{doc.prog_id}{sub}")
            except OSError:
                pass
        if _read_default(reg, rf"{CLASSES}\{doc.suffix}") == doc.prog_id:
            try:
                reg.DeleteKey(reg.HKEY_CURRENT_USER, rf"{CLASSES}\{doc.suffix}")
            except OSError:  # it has subkeys of its own (OpenWithProgids...)
                with reg.OpenKey(reg.HKEY_CURRENT_USER, rf"{CLASSES}\{doc.suffix}", 0,
                                 reg.KEY_SET_VALUE) as handle:
                    reg.DeleteValue(handle, "")
    if real:
        _notify_explorer()


def make_windows() -> list[str]:
    target, args = launch_command()
    script = windows_shortcut_script(target, args, ico_path())
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
    )
    made = [line for line in out.stdout.splitlines() if line.strip()]
    return made + register_windows()


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _run_if_present(*command: str) -> None:
    if shutil.which(command[0]):
        subprocess.run(command, check=False, capture_output=True)


def register_linux_mime() -> list[str]:
    """Install the shared-mime-info package and make the app the default for it."""
    data_home = _data_home()
    packages = data_home / "mime" / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    xml = packages / f"{DESKTOP_FILE_NAME}.xml"
    xml.write_text(linux_mime_xml(), encoding="utf-8")
    _run_if_present("update-mime-database", str(data_home / "mime"))
    _run_if_present("update-desktop-database", str(data_home / "applications"))
    for doc in DOCUMENT_TYPES:
        _run_if_present("xdg-mime", "default", f"{DESKTOP_FILE_NAME}.desktop", doc.mime_type)
    return [str(xml)]


def make_linux(on_desktop: bool = True) -> list[str]:
    target, args = launch_command()
    apps = _data_home() / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = desktop_entry(target, args, largest_png())
    written = [apps / f"{DESKTOP_FILE_NAME}.desktop"]
    desktop = Path.home() / "Desktop"
    if on_desktop and desktop.is_dir():
        written.append(desktop / f"{DESKTOP_FILE_NAME}.desktop")
    for path in written:
        path.write_text(entry, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return [str(p) for p in written] + register_linux_mime()


def _mime_package() -> Path:
    return _data_home() / "mime" / "packages" / f"{DESKTOP_FILE_NAME}.xml"


def _xdg_default(mime_type: str) -> str | None:
    """The .desktop file that opens ``mime_type``, or None if xdg-mime is missing."""
    if not shutil.which("xdg-mime"):
        return None
    out = subprocess.run(["xdg-mime", "query", "default", mime_type],
                         check=False, capture_output=True, text=True)
    return out.stdout.strip()


def linux_registered() -> bool:
    """True when the file manager opens each document type with this copy."""
    entry_path = _data_home() / "applications" / f"{DESKTOP_FILE_NAME}.desktop"
    if not _mime_package().exists() or not entry_path.exists():
        return False
    target, args = launch_command()
    entry = entry_path.read_text(encoding="utf-8")
    if f"Exec={desktop_exec_line(target, args)}\n" not in entry:
        return False
    for doc in DOCUMENT_TYPES:
        default = _xdg_default(doc.mime_type)
        if default is not None and default != f"{DESKTOP_FILE_NAME}.desktop":
            return False
    return True


def unregister_linux() -> None:
    """Remove the MIME package, so .rtpl files no longer open here. The menu
    entry stays."""
    _mime_package().unlink(missing_ok=True)
    _run_if_present("update-mime-database", str(_data_home() / "mime"))


def can_register_file_types() -> bool:
    """Windows and Linux. On macOS the app bundle registers its own types."""
    return sys.platform == "win32" or sys.platform.startswith("linux")


def file_types_registered() -> bool:
    """Whether the OS now opens this app's documents with this copy."""
    if sys.platform == "win32":
        return windows_registered()
    if sys.platform.startswith("linux"):
        return linux_registered()
    return True


def register_file_types() -> list[str]:
    """Make double-clicking a document open it in this copy of the app.

    On Linux this also writes the menu entry, which names the app's file types."""
    if sys.platform == "win32":
        return register_windows()
    if sys.platform.startswith("linux"):
        return make_linux(on_desktop=False)
    raise RuntimeError("On macOS the app bundle registers its file types itself.")


def unregister_file_types() -> None:
    """Undo ``register_file_types``."""
    if sys.platform == "win32":
        unregister_windows()
    elif sys.platform.startswith("linux"):
        unregister_linux()
    else:
        raise RuntimeError("On macOS the app bundle registers its file types itself.")


def main() -> int:
    if sys.platform == "win32":
        made = make_windows()
    elif sys.platform.startswith("linux"):
        made = make_linux()
    else:
        print(
            "On macOS, build the app bundle (see docs/packaging.md) and drag "
            f"{APP_NAME}.app to Applications or the Dock."
        )
        return 1
    for path in made:
        print(f"Created {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
