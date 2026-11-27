"""The app's file types, and which window opens each one.

RTPL profiles and plans are saved as ``.rtpl`` files (JSON inside). Older
``.json`` files still open. The Estimated Quarterly Tax Assistant saves
``.eqta`` files (JSON too), which open in a window of their own;
``WINDOW_FOR_SUFFIX`` says which window opens each type.

The packaging files register the types in ``DOCUMENT_TYPES`` with each OS so
that double-clicking a file starts the app with its path on the command line
(Windows, Linux) or sends it a file-open event (macOS):

* Windows: ``rtpl-shortcut`` or View > Open .rtpl Files With This App
  (``register_windows`` in ``rtpl.app.shortcut``).
* macOS: ``CFBundleDocumentTypes`` in ``packaging/rtpl.spec``.
* Linux: ``packaging/linux/rtpl-mime.xml`` and the ``MimeType`` line of
  ``packaging/linux/rtpl.desktop``, installed by ``install.sh`` (or
  ``rtpl-shortcut`` from source).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

RTPL_SUFFIX = ".rtpl"
EQTA_SUFFIX = ".eqta"

# Which kind of window opens a file, by its extension (lower case).
WINDOW_RTPL = "rtpl"
WINDOW_EQTA = "eqta"
WINDOW_FOR_SUFFIX = {
    RTPL_SUFFIX: WINDOW_RTPL,
    ".json": WINDOW_RTPL,  # files saved before the .rtpl extension
    EQTA_SUFFIX: WINDOW_EQTA,
}
# Window kinds this version can open.
READY_WINDOWS = {WINDOW_RTPL, WINDOW_EQTA}

# File dialog filters for the RTPL window.
OPEN_FILTER = "Profiles and plans (*.rtpl *.json);;All files (*)"
SAVE_FILTER = "RetirementTaxPlannerLite files (*.rtpl);;JSON (*.json)"
# ... and for the EQTA window.
EQTA_FILTER = "Estimated tax files (*.eqta);;All files (*)"


@dataclass(frozen=True)
class DocumentType:
    """A file type registered with the OS so a double-click opens it here."""

    suffix: str  # ".rtpl"
    description: str  # shown by Explorer, Finder and file managers
    mime_type: str  # Linux shared-mime-info type
    prog_id: str  # Windows ProgID under HKCU\Software\Classes
    uti: str  # macOS Uniform Type Identifier


DOCUMENT_TYPES: tuple[DocumentType, ...] = (
    DocumentType(
        suffix=RTPL_SUFFIX,
        description="RetirementTaxPlannerLite profile or plan",
        mime_type="application/x-rtpl",
        prog_id="RetirementTaxPlannerLite.rtpl",
        uti="io.github.magixdragon.rtpl.document",
    ),
    DocumentType(
        suffix=EQTA_SUFFIX,
        description="Estimated Quarterly Tax Assistant file",
        mime_type="application/x-eqta",
        prog_id="RetirementTaxPlannerLite.eqta",
        uti="io.github.magixdragon.eqta.document",
    ),
)


def window_for(path: Path) -> str | None:
    """The kind of window that opens ``path``, or None for an unknown type."""
    return WINDOW_FOR_SUFFIX.get(Path(path).suffix.lower())


def files_from_args(args: list[str]) -> list[Path]:
    """The files named on the command line (``args`` without the program).

    Double-clicking a file passes its path here. Options (``-x``) and Qt's
    own arguments are skipped."""
    files = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-"):
            # Qt options that take a value, such as -platform offscreen
            skip_next = arg.lstrip("-") in _QT_VALUE_OPTIONS
            continue
        files.append(Path(arg))
    return files


_QT_VALUE_OPTIONS = {
    "platform", "platformpluginpath", "platformtheme", "plugin", "qmljsdebugger",
    "qwindowgeometry", "qwindowicon", "qwindowtitle", "session", "display", "style",
    "stylesheet", "geometry", "title", "name",
}


def with_rtpl_suffix(path: Path) -> Path:
    """``path`` with ``.rtpl`` added unless it already ends in .rtpl or .json."""
    path = Path(path)
    if path.suffix.lower() in (RTPL_SUFFIX, ".json"):
        return path
    return path.with_name(path.name + RTPL_SUFFIX)


def with_eqta_suffix(path: Path) -> Path:
    """``path`` with ``.eqta`` added unless it already ends in .eqta."""
    path = Path(path)
    if path.suffix.lower() == EQTA_SUFFIX:
        return path
    return path.with_name(path.name + EQTA_SUFFIX)


def save_as_start(path: Path | None, fallback: str) -> str:
    """Where Save As starts: the open file, renamed to ``.rtpl`` if it was a
    ``.json`` file, so the first Save As offers the new extension."""
    if path is None:
        return fallback
    return str(Path(path).with_suffix(RTPL_SUFFIX))


def linux_mime_xml() -> str:
    """The shared-mime-info package for ``DOCUMENT_TYPES``."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">',
    ]
    for doc in DOCUMENT_TYPES:
        lines += [
            f'  <mime-type type="{doc.mime_type}">',
            f"    <comment>{escape(doc.description)}</comment>",
            '    <sub-class-of type="application/json"/>',
            f'    <glob pattern="*{doc.suffix}"/>',
            '    <icon name="rtpl"/>',
            "  </mime-type>",
        ]
    lines.append("</mime-info>")
    return "\n".join(lines) + "\n"


def macos_plist_entries(icon_file: str = "rtpl.icns") -> dict:
    """Info.plist keys that make Finder open ``DOCUMENT_TYPES`` with the app."""
    return {
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": doc.description,
                "CFBundleTypeRole": "Editor",
                "CFBundleTypeIconFile": icon_file,
                "LSHandlerRank": "Owner",
                "LSItemContentTypes": [doc.uti],
            }
            for doc in DOCUMENT_TYPES
        ],
        "UTExportedTypeDeclarations": [
            {
                "UTTypeIdentifier": doc.uti,
                "UTTypeDescription": doc.description,
                "UTTypeConformsTo": ["public.json", "public.data"],
                "UTTypeIconFile": icon_file,
                "UTTypeTagSpecification": {
                    "public.filename-extension": [doc.suffix.lstrip(".")],
                    "public.mime-type": doc.mime_type,
                },
            }
            for doc in DOCUMENT_TYPES
        ],
    }


def desktop_mime_types() -> str:
    """The value of a .desktop file's MimeType key."""
    return "".join(f"{doc.mime_type};" for doc in DOCUMENT_TYPES)
