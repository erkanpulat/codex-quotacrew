"""Design tokens and a small icon factory.

A single source of truth for colors, spacing and typography keeps the UI
consistent. Icons are drawn with QPainter so the app needs no image assets and
stays crisp at any DPI.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QLineF, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton


@dataclass(frozen=True)
class Palette:
    bg: str
    surface: str
    surface_alt: str
    border: str
    text: str
    muted: str
    primary: str
    primary_hover: str
    on_primary: str
    success: str
    warning: str
    danger: str
    track: str


DARK = Palette(
    bg="#0f141b",
    surface="#161e28",
    surface_alt="#1e2936",
    border="#2b394a",
    text="#edf2f7",
    muted="#a7b4c7",
    primary="#2681f4",
    primary_hover="#4795fa",
    on_primary="#ffffff",
    success="#31d4a1",
    warning="#f5ad36",
    danger="#f27886",
    track="#2b405c",
)
LIGHT = Palette(
    bg="#f8fbff",
    surface="#ffffff",
    surface_alt="#f0f5fc",
    border="#dfe8f4",
    text="#0b1535",
    muted="#52668f",
    primary="#1263f5",
    primary_hover="#0756df",
    on_primary="#ffffff",
    success="#00894a",
    warning="#cf6200",
    danger="#bd3349",
    track="#e4e9f1",
)

SPACE = (0, 4, 8, 12, 16, 24, 32, 48)
RADIUS = 10


def usage_color(palette: Palette, percent: float | None) -> str:
    if percent is None:
        return palette.muted
    if percent >= 90:
        return palette.danger
    if percent >= 70:
        return palette.warning
    return palette.primary


def make_icon(name: str, color: str, size: int = 20) -> QIcon:
    """Render a named glyph into a QIcon using QPainter (no asset files)."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    pen = QPen(QColor(color))
    pen.setWidthF(size * 0.08)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)

    s = float(size)
    m = s * 0.15

    if name == "dashboard":
        w = (s - 2 * m - s * 0.1) / 2
        r = s * 0.06
        p.drawRoundedRect(QRectF(m, m, w, w), r, r)
        p.drawRoundedRect(QRectF(s - m - w, m, w, w), r, r)
        p.drawRoundedRect(QRectF(m, s - m - w, w, w), r, r)
        p.drawRoundedRect(QRectF(s - m - w, s - m - w, w, w), r, r)
    elif name == "home":
        path = QPainterPath()
        path.moveTo(m, s * 0.48)
        path.lineTo(s * 0.5, m)
        path.lineTo(s - m, s * 0.48)
        p.drawPath(path)
        p.drawRoundedRect(QRectF(s * 0.26, s * 0.46, s * 0.48, s * 0.40), s * 0.04, s * 0.04)
    elif name == "list":
        for y in (s * 0.27, s * 0.5, s * 0.73):
            p.drawPoint(QPointF(s * 0.2, y))
            p.drawLine(QLineF(s * 0.34, y, s * 0.82, y))
    elif name == "history":
        p.drawEllipse(QRectF(m, m, s - 2 * m, s - 2 * m))
        p.drawLine(QLineF(s * 0.5, s * 0.29, s * 0.5, s * 0.5))
        p.drawLine(QLineF(s * 0.5, s * 0.5, s * 0.66, s * 0.59))
    elif name == "help":
        p.drawEllipse(QRectF(m, m, s - 2 * m, s - 2 * m))
        p.drawArc(QRectF(s * 0.38, s * 0.27, s * 0.24, s * 0.27), 0, 180 * 16)
        p.drawLine(QLineF(s * 0.5, s * 0.54, s * 0.5, s * 0.62))
        p.drawPoint(QPointF(s * 0.5, s * 0.73))
    elif name == "continuity":
        path = QPainterPath()
        path.moveTo(m, s * 0.5)
        path.cubicTo(s * 0.35, m, s * 0.65, s - m, s - m, s * 0.5)
        p.drawPath(path)
    elif name == "quota":
        p.drawEllipse(QRectF(m, m, s - 2 * m, s * 0.24))
        for y in (s * 0.38, s * 0.60):
            p.drawArc(QRectF(m, y, s - 2 * m, s * 0.24), 180 * 16, 180 * 16)
        p.drawLine(QLineF(m, s * 0.27, m, s * 0.72))
        p.drawLine(QLineF(s - m, s * 0.27, s - m, s * 0.72))
    elif name == "accounts":
        p.drawEllipse(QRectF(s * 0.35, m, s * 0.3, s * 0.3))
        path = QPainterPath()
        path.moveTo(m, s - m)
        path.cubicTo(s * 0.2, s * 0.45, s * 0.8, s * 0.45, s - m, s - m)
        p.drawPath(path)
    elif name == "goals":
        p.drawEllipse(QRectF(m, m, s - 2 * m, s - 2 * m))
        inner_m = s * 0.38
        p.drawEllipse(QRectF(inner_m, inner_m, s - 2 * inner_m, s - 2 * inner_m))
    elif name == "diagnostics":
        p.drawEllipse(QRectF(m, m, s * 0.5, s * 0.5))
        p.drawLine(QLineF(s * 0.58, s * 0.58, s - m, s - m))
    elif name == "settings":
        p.drawEllipse(QRectF(s * 0.35, s * 0.35, s * 0.3, s * 0.3))
        import math

        cx = cy = s / 2.0
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            x1 = cx + math.cos(rad) * s * 0.24
            y1 = cy + math.sin(rad) * s * 0.24
            x2 = cx + math.cos(rad) * s * 0.40
            y2 = cy + math.sin(rad) * s * 0.40
            p.drawLine(QLineF(x1, y1, x2, y2))
    elif name in {"sort", "sort_asc", "sort_desc"}:
        if name in {"sort", "sort_asc"}:
            p.drawLine(QLineF(s * 0.35, s * 0.70, s * 0.35, s * 0.30))
            p.drawLine(QLineF(s * 0.22, s * 0.43, s * 0.35, s * 0.30))
            p.drawLine(QLineF(s * 0.48, s * 0.43, s * 0.35, s * 0.30))
        if name in {"sort", "sort_desc"}:
            p.drawLine(QLineF(s * 0.65, s * 0.30, s * 0.65, s * 0.70))
            p.drawLine(QLineF(s * 0.52, s * 0.57, s * 0.65, s * 0.70))
            p.drawLine(QLineF(s * 0.78, s * 0.57, s * 0.65, s * 0.70))
    elif name == "refresh":
        rect = QRectF(m, m, s - 2 * m, s - 2 * m)
        # Start at 90 deg (12 o'clock), span -270 deg (CW)
        p.drawArc(rect, 90 * 16, -270 * 16)
        # Arrowhead pointing right at 12 o'clock
        p.drawLine(QLineF(s * 0.5, m, s * 0.35, m - s * 0.08))
        p.drawLine(QLineF(s * 0.5, m, s * 0.35, m + s * 0.08))
    elif name == "briefcase":
        p.drawRoundedRect(QRectF(m, s * 0.34, s - 2 * m, s * 0.47), s * 0.07, s * 0.07)
        p.drawRoundedRect(QRectF(s * 0.36, m, s * 0.28, s * 0.20), s * 0.04, s * 0.04)
        p.drawLine(QLineF(m, s * 0.51, s - m, s * 0.51))
    elif name == "document":
        path = QPainterPath()
        path.moveTo(s * 0.24, m)
        path.lineTo(s * 0.6, m)
        path.lineTo(s * 0.78, s * 0.34)
        path.lineTo(s * 0.78, s - m)
        path.lineTo(s * 0.24, s - m)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QLineF(s * 0.34, s * 0.44, s * 0.64, s * 0.44))
        p.drawLine(QLineF(s * 0.34, s * 0.58, s * 0.64, s * 0.58))
    elif name == "mail":
        p.drawRoundedRect(QRectF(m, s * 0.24, s - 2 * m, s * 0.53), s * 0.04, s * 0.04)
        p.drawLine(QLineF(m, s * 0.27, s * 0.5, s * 0.52))
        p.drawLine(QLineF(s - m, s * 0.27, s * 0.5, s * 0.52))
    elif name == "pause":
        p.drawLine(QLineF(s * 0.36, m, s * 0.36, s - m))
        p.drawLine(QLineF(s * 0.64, m, s * 0.64, s - m))
    elif name == "play":
        path = QPainterPath()
        path.moveTo(s * 0.32, m)
        path.lineTo(s * 0.73, s * 0.5)
        path.lineTo(s * 0.32, s - m)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "more":
        for y in (s * 0.27, s * 0.5, s * 0.73):
            p.drawPoint(QPointF(s * 0.5, y))
    elif name == "chat":
        p.drawRoundedRect(QRectF(m, m, s - 2 * m, s * 0.56), s * 0.18, s * 0.18)
        p.drawLine(QLineF(s * 0.34, s * 0.71, s * 0.28, s - m))
        p.drawLine(QLineF(s * 0.28, s - m, s * 0.55, s * 0.71))
    elif name == "about":
        p.drawEllipse(QRectF(m, m, s - 2 * m, s - 2 * m))
        p.drawPoint(QPointF(s * 0.5, s * 0.32))
        p.drawLine(QLineF(s * 0.5, s * 0.45, s * 0.5, s * 0.7))
    elif name == "power":
        rect = QRectF(m, m, s - 2 * m, s - 2 * m)
        # Gap of 60 deg at the top
        p.drawArc(rect, 120 * 16, 300 * 16)
        p.drawLine(QLineF(s * 0.5, m - s * 0.05, s * 0.5, s * 0.45))
    elif name == "logo":
        p.drawRoundedRect(QRectF(m, m, s - 2 * m, s - 2 * m), s * 0.15, s * 0.15)
        p.drawLine(QLineF(s * 0.35, s * 0.5, s * 0.45, s * 0.6))
        p.drawLine(QLineF(s * 0.45, s * 0.6, s * 0.65, s * 0.4))

    p.end()
    return QIcon(pix)


def spaced_icon(name: str, color: str, size: int = 20, gap: int = 12) -> QIcon:
    pix = QPixmap(size + gap, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.drawPixmap(0, 0, make_icon(name, color, size).pixmap(size, size))
    painter.end()
    return QIcon(pix)


def set_button_icon(button: QAbstractButton, name: str, color: str, size: int = 20) -> None:
    button.setProperty("iconName", name)
    button.setProperty("iconPixelSize", size)
    button.setIcon(spaced_icon(name, color, size))
    button.setIconSize(QSize(size + 12, size))


def paint_brand(p: QPainter) -> None:
    """QuotaCrew's account orbit and continuation arrow in a 64-unit viewbox."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#101c2e"))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 16, 16)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor("#f5f8fc"), 5.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    orbit = QPainterPath()
    orbit.moveTo(37, 15.5)
    orbit.cubicTo(24, 11, 12, 22, 16, 36)
    orbit.cubicTo(19, 49, 28, 51, 35, 48)
    p.drawPath(orbit)
    arrow = QPainterPath()
    arrow.moveTo(36, 38)
    arrow.lineTo(49, 51)
    arrow.moveTo(41, 51)
    arrow.lineTo(49, 51)
    arrow.lineTo(49, 43)
    p.setPen(
        QPen(
            QColor("#f5f8fc"),
            3.5,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    p.drawPath(arrow)
    p.setPen(Qt.PenStyle.NoPen)
    for x, y, radius, color in (
        (45, 18, 3.5, "#5ee5be"),
        (50, 26, 3, "#50bea4"),
        (51, 34, 2.5, "#398674"),
    ):
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(x, y), radius, radius)


def app_icon(size: int = 64) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.scale(size / 64, size / 64)
    paint_brand(p)
    p.end()
    return QIcon(pix)


def tray_icon(*, shutdown_pending: bool = False, active_work: bool = False) -> QIcon:
    if not shutdown_pending and not active_work:
        return app_icon()
    pix = app_icon().pixmap(64, 64)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#191b20"), 3))
    painter.setBrush(QColor("#f5b94c" if shutdown_pending else "#31d4a1"))
    painter.drawEllipse(QRectF(33, 33, 29, 29))
    painter.setPen(QPen(QColor("#30230b"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    if shutdown_pending:
        painter.drawLine(47, 40, 47, 48)
        painter.drawLine(47, 48, 53, 51)
    else:
        painter.drawLine(43, 41, 43, 53)
        painter.drawLine(51, 41, 51, 53)
    painter.end()
    return QIcon(pix)
