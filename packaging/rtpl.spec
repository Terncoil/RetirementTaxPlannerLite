# PyInstaller recipe for a stand-alone RetirementTaxPlannerLite app.
#
#   uv run --with pyinstaller pyinstaller packaging/rtpl.spec --noconfirm
#
# Run it on each platform you want to ship (PyInstaller does not
# cross-compile). Output goes to dist/:
#   Windows  dist/RetirementTaxPlannerLite/RetirementTaxPlannerLite.exe
#   macOS    dist/RetirementTaxPlannerLite.app
#   Linux    dist/RetirementTaxPlannerLite/RetirementTaxPlannerLite
# See docs/packaging.md for icons, shortcuts and installers.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
APP_NAME = "RetirementTaxPlannerLite"

sys.path.insert(0, str(ROOT / "src"))
from rtpl import __version__  # the one source of the version
from rtpl.app.documents import macos_plist_entries  # .rtpl opens by double-click

if sys.platform == "win32":
    # Version resource: Explorer shows it under Properties > Details.
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )

    nums = tuple(int(n) for n in __version__.split(".")) + (0,)
    win_version = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "RetirementTaxPlannerLite contributors"),
                StringStruct("FileDescription", APP_NAME),
                StringStruct("FileVersion", __version__),
                StringStruct("InternalName", APP_NAME),
                StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                StringStruct("ProductName", APP_NAME),
                StringStruct("ProductVersion", __version__),
            ])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
else:
    win_version = None

if sys.platform == "win32":
    exe_icon = str(ROOT / "src" / "rtpl" / "data" / "icons" / "rtpl.ico")
elif sys.platform == "darwin":
    exe_icon = str(ROOT / "packaging" / "rtpl.icns")
else:
    exe_icon = None  # Linux executables carry no icon; the .desktop file does

a = Analysis(
    [str(ROOT / "packaging" / "launch_rtpl.py")],
    pathex=[str(ROOT / "src")],
    datas=collect_data_files("rtpl"),  # tax law, themes and icons
    hiddenimports=["matplotlib.backends.backend_qtagg"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=exe_icon,
    version=win_version,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=exe_icon,
        bundle_identifier="io.github.magixdragon.rtpl",
        version=__version__,
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleVersion": __version__,
            # Finder opens .rtpl files with the app (see rtpl/app/documents.py)
            **macos_plist_entries(Path(exe_icon).name),
        },
    )
