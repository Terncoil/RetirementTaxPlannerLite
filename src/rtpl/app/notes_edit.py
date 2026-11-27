"""A small Markdown notes editor: headings 1 to 3, bold, italic and lists.

The text box is Qt's own ``QTextEdit``, which reads Markdown with
``setMarkdown``. ``document_markdown`` writes it back from the formats in
the document. Qt's ``toMarkdown`` decides bold from the font it actually
finds, so it drops bold where no bold face is installed (as on a test
machine), which is why the writer is our own.

Formatting comes from the toolbar, the shortcuts, or Markdown typed at the
start of a line: ``# `` to ``### `` for a heading, ``- `` or ``* `` for a
bulleted list and ``1. `` for a numbered one. Other Markdown typed in the
text (such as ``**word**``) stays as typed.
"""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QAction, QFont, QKeyEvent, QTextCharFormat, QTextCursor, QTextFormat, QTextListFormat
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QTextEdit, QToolButton, QVBoxLayout, QWidget

HEADING_LEVELS = 3
STYLES = ("Normal", "Heading 1", "Heading 2", "Heading 3")
BULLET = QTextListFormat.Style.ListDisc
NUMBERED = QTextListFormat.Style.ListDecimal
BULLET_STYLES = (QTextListFormat.Style.ListDisc, QTextListFormat.Style.ListCircle, QTextListFormat.Style.ListSquare)
NEST_INDENT = "    "  # four spaces nest a list item under either kind of list

_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>~|])")
_MD_LINE_START = re.compile(r"^(\s*)([#>+=-])")
_MD_NUMBERED = re.compile(r"^(\s*\d+)([.)])")


def _md_escape(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def _inline_markdown(block, heading: bool) -> str:
    """The block's text with bold and italic marks (no bold in a heading)."""
    runs: list[list] = []  # [bold, italic, text]
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        fmt = frag.charFormat()
        style = [not heading and fmt.fontWeight() >= QFont.Weight.Bold, fmt.fontItalic()]
        if runs and runs[-1][:2] == style:
            runs[-1][2] += frag.text()
        else:
            runs.append([*style, frag.text()])
        it += 1
    out = []
    for bold, italic, text in runs:
        text = "\\\n".join(_md_escape(t) for t in text.split(" "))  # Shift+Enter
        mark = ("**" if bold else "") + ("*" if italic else "")
        core = text.strip()
        if mark and core:
            lead = text[: len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            text = f"{lead}{mark}{core}{mark[::-1]}{trail}"
        out.append(text)
    text = _MD_LINE_START.sub(r"\1\\\2", "".join(out))
    return _MD_NUMBERED.sub(r"\1\\\2", text)


def document_markdown(doc) -> str:
    """The document as Markdown: headings, lists, bold and italic."""
    out = ""
    prev_item = False
    top_list = None  # the outermost list being written
    block = doc.begin()
    while block.isValid():
        level = block.blockFormat().headingLevel()
        text = _inline_markdown(block, heading=level > 0)
        lst = block.textList()
        if lst is not None:
            fmt = lst.format()
            marker = f"{lst.itemNumber(block) + 1}." if fmt.style() == NUMBERED else "-"
            line = f"{NEST_INDENT * max(0, fmt.indent() - 1)}{marker} {text}"
            # Items of one list, nested ones included, go on consecutive
            # lines; a new outermost list starts after a blank line.
            new_list = fmt.indent() <= 1 and lst is not top_list
            if fmt.indent() <= 1:
                top_list = lst
            sep = "\n" if prev_item and not new_list else "\n\n"
            prev_item = True
        else:
            line = f"{'#' * level} {text}" if 0 < level <= 6 else text
            sep, prev_item, top_list = "\n\n", False, None
        out += (sep if out else "") + line
        block = block.next()
    return out.strip()


class NotesEdit(QWidget):
    """A notes box with a formatting toolbar, kept as Markdown."""

    changed = Signal()

    def __init__(self, name: str, placeholder: str) -> None:
        super().__init__()
        self.text = QTextEdit()
        self.text.setAccessibleName(name)
        self.text.setPlaceholderText(placeholder)
        self.text.setAcceptRichText(False)  # paste as plain text
        self.text.setTabChangesFocus(True)  # except in a list, where Tab indents
        self.text.installEventFilter(self)
        self._set_indent_width()
        self.style = QComboBox()
        self.style.addItems(STYLES)
        self.style.setAccessibleName("Paragraph style")
        self.style.setToolTip("Paragraph style. At the start of a line, typing # , ## or ###\n"
                              "and a space makes a heading too.")
        self.style.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.style.activated.connect(self.set_heading)
        self.bold = self._button("B", "Bold (Ctrl+B)", "Ctrl+B", self._set_bold)
        self.italic = self._button("I", "Italic (Ctrl+I)", "Ctrl+I", self._set_italic)
        self.bullets = self._button("•", "Bulleted list (Ctrl+Shift+8). Typing - and a space at the\n"
                                    "start of a line starts one too. Tab and Shift+Tab indent.",
                                    "Ctrl+Shift+8", lambda: self.toggle_list(BULLET))
        self.numbers = self._button("1.", "Numbered list (Ctrl+Shift+7). Typing 1. and a space at the\n"
                                    "start of a line starts one too. Tab and Shift+Tab indent.",
                                    "Ctrl+Shift+7", lambda: self.toggle_list(NUMBERED))
        for button, attr in ((self.bold, "setBold"), (self.italic, "setItalic")):
            f = button.font()
            getattr(f, attr)(True)
            button.setFont(f)
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        for w in (self.style, self.bold, self.italic, self.bullets, self.numbers):
            bar.addWidget(w)
        bar.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.text, 1)
        self._loading = False
        self.text.textChanged.connect(self._changed)
        self.text.currentCharFormatChanged.connect(lambda _fmt: self._sync_toolbar())
        self.text.cursorPositionChanged.connect(self._sync_toolbar)

    def _button(self, text: str, tip: str, keys: str, slot) -> QToolButton:
        action = QAction(text, self.text, checkable=True, shortcut=keys, toolTip=tip)
        action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        action.triggered.connect(lambda _=False: slot())
        self.text.addAction(action)
        button = QToolButton()
        button.setDefaultAction(action)
        button.setAccessibleName(tip.split(" (")[0])
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep the selection in the text
        return button

    # -- character formats ------------------------------------------------
    def _set_bold(self) -> None:
        # Toggle from the text's own format, not the button's state, so a
        # stale check mark can never turn bold off when it should go on.
        on = self.text.fontWeight() < QFont.Weight.Bold
        self.text.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
        self._sync_toolbar()

    def _set_italic(self) -> None:
        self.text.setFontItalic(not self.text.fontItalic())
        self._sync_toolbar()

    # -- block formats ----------------------------------------------------
    def set_heading(self, level: int) -> None:
        """Make the paragraphs in the selection a heading (1 to 3) or Normal (0)."""
        cur = self.text.textCursor()
        cur.beginEditBlock()
        start, end = sorted((cur.anchor(), cur.position()))
        b = self.text.document().findBlock(start)
        while b.isValid() and b.position() <= end:
            c = QTextCursor(b)
            if level and b.textList() is not None:
                b.textList().remove(b)  # a heading isn't a list item
                fmt = c.blockFormat()
                fmt.setIndent(0)
                c.setBlockFormat(fmt)
            fmt = c.blockFormat()
            fmt.setHeadingLevel(level)
            c.setBlockFormat(fmt)
            c.setPosition(b.position())
            c.setPosition(b.position() + b.length() - 1, QTextCursor.MoveMode.KeepAnchor)
            c.mergeCharFormat(self._heading_chars(level))
            c.mergeBlockCharFormat(self._heading_chars(level))
            b = b.next()
        cur.endEditBlock()
        self._sync_toolbar()

    @staticmethod
    def _heading_chars(level: int) -> QTextCharFormat:
        """Qt's own look for Markdown headings: larger and bold."""
        fmt = QTextCharFormat()
        fmt.setProperty(QTextFormat.Property.FontSizeAdjustment, (4 - level) if level else 0)
        fmt.setFontWeight(QFont.Weight.Bold if level else QFont.Weight.Normal)
        return fmt

    def toggle_list(self, style) -> None:
        """Start a list of ``style``, switch the list to it, or end it."""
        cur = self.text.textCursor()
        lst = cur.currentList()
        cur.beginEditBlock()
        if lst is not None and self._same_kind(lst.format().style(), style):
            start, end = sorted((cur.anchor(), cur.position()))
            b = self.text.document().findBlock(start)
            while b.isValid() and b.position() <= end:
                if b.textList() is not None:
                    b.textList().remove(b)
                    c = QTextCursor(b)
                    fmt = c.blockFormat()
                    fmt.setIndent(0)
                    c.setBlockFormat(fmt)
                b = b.next()
        elif lst is not None:
            fmt = lst.format()
            fmt.setStyle(style)
            lst.setFormat(fmt)
        else:
            if cur.blockFormat().headingLevel():
                self.set_heading(0)
            fmt = QTextListFormat()
            fmt.setStyle(style)
            fmt.setIndent(1)
            cur.createList(fmt)
        cur.endEditBlock()
        self._sync_toolbar()

    @staticmethod
    def _same_kind(a, b) -> bool:
        return (a in BULLET_STYLES) == (b in BULLET_STYLES)

    def indent(self, step: int) -> bool:
        """Indent (+1) or outdent (-1) the current list item; False if not in a list."""
        cur = self.text.textCursor()
        lst = cur.currentList()
        if lst is None:
            return False
        fmt = QTextListFormat(lst.format())
        depth = fmt.indent() + step
        block = cur.block()
        if depth < 1:
            lst.remove(block)
            bf = cur.blockFormat()
            bf.setIndent(0)
            cur.setBlockFormat(bf)
        else:
            fmt.setIndent(depth)
            if fmt.style() in BULLET_STYLES:
                fmt.setStyle(BULLET_STYLES[(depth - 1) % len(BULLET_STYLES)])
            # Rejoin the list at that depth above or below (skipping deeper
            # items), or start one.
            target = self._list_at_depth(block, depth)
            if target is not None and target is not lst:
                lst.remove(block)
                target.add(block)
            elif target is None:
                lst.remove(block)
                cur.createList(fmt)
        self._sync_toolbar()
        return True

    @staticmethod
    def _list_at_depth(block, depth: int):
        for step in ("previous", "next"):
            b = getattr(block, step)()
            while b.isValid() and b.textList() is not None:
                d = b.textList().format().indent()
                if d == depth:
                    return b.textList()
                if d < depth:
                    break
                b = getattr(b, step)()
        return None

    # -- keys -------------------------------------------------------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.text and event.type() == QEvent.Type.KeyPress:
            return self._key(event)
        return super().eventFilter(obj, event)

    def _key(self, event: QKeyEvent) -> bool:
        key, mods = event.key(), event.modifiers()
        cur = self.text.textCursor()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and cur.currentList() is not None:
            back = key == Qt.Key.Key_Backtab or bool(mods & Qt.KeyboardModifier.ShiftModifier)
            return self.indent(-1 if back else 1)
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and cur.blockFormat().headingLevel() \
                and cur.atBlockEnd() and not mods & Qt.KeyboardModifier.ShiftModifier:
            # A new line after a heading is a normal paragraph.
            cur.beginEditBlock()
            bf = cur.blockFormat()
            bf.setHeadingLevel(0)
            cur.insertBlock(bf, self._heading_chars(0))
            cur.endEditBlock()
            self.text.setTextCursor(cur)
            return True
        if key == Qt.Key.Key_Space and not cur.hasSelection():
            return self._markdown_shortcut(cur)
        return False

    def _markdown_shortcut(self, cur: QTextCursor) -> bool:
        """``# `` to ``### ``, ``- ``, ``* `` and ``1. `` at the start of a line."""
        if cur.currentList() is not None or cur.blockFormat().headingLevel():
            return False
        typed = cur.block().text()[: cur.positionInBlock()]
        if re.fullmatch(r"#{1,%d}" % HEADING_LEVELS, typed):
            action = lambda: self.set_heading(len(typed))
        elif typed in ("-", "*"):
            action = lambda: self.toggle_list(BULLET)
        elif typed == "1.":
            action = lambda: self.toggle_list(NUMBERED)
        else:
            return False
        cur.beginEditBlock()
        cur.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        self.text.setTextCursor(cur)
        action()
        cur.endEditBlock()
        return True

    def _sync_toolbar(self) -> None:
        cur = self.text.textCursor()
        fmt = self.text.currentCharFormat()
        level = cur.blockFormat().headingLevel()
        self.style.setCurrentIndex(level if 0 <= level <= HEADING_LEVELS else 0)
        self.bold.defaultAction().setChecked(not level and fmt.fontWeight() >= QFont.Weight.Bold)
        self.italic.defaultAction().setChecked(fmt.fontItalic())
        lst = cur.currentList()
        style = lst.format().style() if lst is not None else None
        self.bullets.defaultAction().setChecked(style in BULLET_STYLES)
        self.numbers.defaultAction().setChecked(style == NUMBERED)

    # -- text -------------------------------------------------------------
    def _set_indent_width(self) -> None:
        """Each list level indents about two characters (Qt's default is 40
        pixels), leaving room for the bullet or a number such as "10."."""
        self.text.document().setIndentWidth(self.text.fontMetrics().horizontalAdvance("00") + 4)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.FontChange:
            self._set_indent_width()
        super().changeEvent(event)

    def set_markdown(self, text: str) -> None:
        self._loading = True
        self.text.setMarkdown(text)
        self._set_indent_width()
        self._loading = False
        self._sync_toolbar()

    def markdown(self) -> str:
        return document_markdown(self.text.document())

    def _changed(self) -> None:
        if not self._loading:
            self.changed.emit()
