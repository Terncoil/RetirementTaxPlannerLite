"""Click a bar on a forward plan graph to see what it is made of.

The popup lists the bar's segments from the top of the bar down, then the
total, then any threshold lines that cross the bar (highest first). It
closes on a click anywhere off the bars, on Escape, and whenever the graph
is redrawn or resized.
"""

from __future__ import annotations

from html import escape

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel

from rtpl.charts.forward_chart import BarDetail, BarIndex
from rtpl.theme import Palette

GAP = 14  # pixels between the click and the popup


def _dollars(v: float) -> str:
    return f"-${-v:,.0f}" if v < 0 else f"${v:,.0f}"


def popup_html(bar: BarDetail, total_label: str, palette: Palette) -> str:
    def row(color: str, label: str, value: float, *, bold: bool = False, swatch: str = "■") -> str:
        b, nb = ("<b>", "</b>") if bold else ("", "")
        mark = f'<span style="color:{color}">{swatch}</span>' if color else ""
        return (f"<tr><td>{mark}</td><td>{b}{escape(label)}{nb}</td>"
                f'<td align="right">&nbsp;&nbsp;&nbsp;{b}{_dollars(value)}{nb}</td></tr>')

    parts = [f"<b>{escape(bar.heading)}</b>", '<table cellspacing="0" cellpadding="1">']
    parts += [row(color, label, amount) for label, color, amount in bar.segments]
    parts.append(row("", total_label, bar.total, bold=True))
    parts.append("</table>")
    if bar.lines:
        parts.append(f'<div style="color:{palette.text_secondary}; margin-top:6px">Lines crossing this bar</div>')
        parts.append('<table cellspacing="0" cellpadding="1">')
        parts += [row(color, label, value, swatch="▬") for label, color, value in bar.lines]
        parts.append("</table>")
    return "".join(parts)


class BarPopup(QLabel):
    """A small panel over a chart canvas, driven by clicks on that canvas."""

    def __init__(self, canvas: FigureCanvasQTAgg, figure: Figure, palette_of) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.figure = figure
        self.palette_of = palette_of  # callable returning the current Palette (or None)
        self.bar: BarDetail | None = None
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAccessibleName("Bar details")
        self.hide()
        # A click gives the chart focus, so Escape reaches it.
        canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        canvas.mpl_connect("button_press_event", self._clicked)
        canvas.mpl_connect("key_press_event", self._key)
        canvas.mpl_connect("resize_event", lambda _e: self.dismiss())

    def dismiss(self) -> None:
        self.bar = None
        self.hide()

    def _key(self, event) -> None:
        if event.key == "escape":
            self.dismiss()

    def _clicked(self, event) -> None:
        index: BarIndex | None = getattr(self.figure, "rtpl_bars", None)
        palette = self.palette_of()
        bar = index.hit(event.inaxes, event.xdata, event.ydata) if index and palette and event.button == 1 else None
        if bar is None:
            self.dismiss()
            return
        self.bar = bar
        self.setStyleSheet(
            f"QLabel {{ background: {palette.surface}; color: {palette.text_primary};"
            f" border: 1px solid {palette.grid}; border-radius: 4px; padding: 6px; }}")
        self.setText(popup_html(bar, index.total_label, palette))
        self.adjustSize()
        # matplotlib counts physical pixels up from the bottom; Qt counts
        # logical pixels down from the top.
        ratio = self.canvas.device_pixel_ratio or 1
        at = QPoint(round(event.x / ratio), round(self.canvas.height() - event.y / ratio))
        self.move(self._place(at))
        self.show()
        self.raise_()

    def _place(self, at: QPoint) -> QPoint:
        """Beside the click, flipped left or up where it would leave the chart."""
        w, h = self.width(), self.height()
        cw, ch = self.canvas.width(), self.canvas.height()
        x = at.x() + GAP if at.x() + GAP + w <= cw else at.x() - GAP - w
        y = at.y() + GAP if at.y() + GAP + h <= ch else at.y() - GAP - h
        return QPoint(max(0, min(x, cw - w)), max(0, min(y, ch - h)))
