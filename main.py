"""Shrinkit — pick videos on your phone, compress them with ffmpeg, push them back.

UI v3: sidebar navigation, multi-select library, live selection inspector and a
batch queue view. All widgets are painted / styled in code (no image assets).
Backend modules (adb, ffmpeg, cache, presets, thumbs, workers) are unchanged.
"""

from __future__ import annotations

import datetime
import logging
import math
import os
import sys

from PyQt6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QPoint, QPointF, QRect, QRectF,
    QSize, Qt, QTimer, QUrl, QVariantAnimation, pyqtSignal,
)
from PyQt6.QtGui import (
    QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon,
    QKeySequence, QPainter, QPainterPath, QPalette, QPen, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QDialog, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListView, QMainWindow, QMenu, QPlainTextEdit,
    QPushButton, QScrollArea, QSizePolicy, QSlider, QStackedWidget, QStyle,
    QStyledItemDelegate, QVBoxLayout, QWidget,
)

import adb
import ffmpeg
from cache import ThumbCache
from presets import DEFAULT_PRESET_ID, PRESETS
from thumbs import ThumbnailWorker
from workers import PipelineWorker, VideoLoadWorker

__version__ = "0.3.0"

PAGE_LIBRARY, PAGE_QUEUE = range(2)
ROLE_VIDEO = Qt.ItemDataRole.UserRole + 1
PREVIEW_BATCH = 120          # max previews requested per click
SCROLLBAR_W = 10


# ======================================================================
# logging (unchanged behaviour)
# ======================================================================

def _setup_logging() -> str:
    from PyQt6.QtCore import QStandardPaths
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    logdir = os.path.join(base or os.path.expanduser("~"), "Shrinkit", "logs")
    try:
        os.makedirs(logdir, exist_ok=True)
        from logging.handlers import RotatingFileHandler
        h = RotatingFileHandler(os.path.join(logdir, "shrinkit.log"),
                                maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, handlers=[h],
                            format="%(asctime)s %(levelname)s %(message)s")
    except OSError:
        logging.basicConfig(level=logging.INFO)
    return logdir


LOG_DIR = _setup_logging()
log = logging.getLogger("shrinkit")


# ======================================================================
# formatting helpers
# ======================================================================

def fmt_size(n: int) -> str:
    n = max(0, int(n or 0))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_dur(ms: int) -> str:
    s = int((ms or 0) // 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def short_date(ts: int) -> str:
    if not ts:
        return ""
    try:
        d = datetime.datetime.fromtimestamp(ts)
    except (OSError, ValueError, OverflowError):
        return ""
    txt = d.strftime("%b %d").replace(" 0", " ")
    return txt if d.year == datetime.datetime.now().year else f"{txt}, {d.year}"


def long_date(ts: int) -> str:
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):
        return ""


def res_label(v) -> str:
    s = min(v.width or 0, v.height or 0)
    if not s:
        return ""
    if s >= 2100:
        return "4K"
    if s >= 1400:
        return "1440p"
    if s >= 1060:
        return "1080p"
    if s >= 700:
        return "720p"
    return f"{s}p"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def base_name(path: str) -> str:
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1]


# ======================================================================
# design tokens + stylesheet
# ======================================================================

class C:
    BG = "#111318"
    BG_DEEP = "#0D0F13"
    SURFACE = "#171A20"
    SURFACE_HI = "#1D212A"
    RAISED = "#1F242D"
    RAISED_HI = "#282E39"
    BORDER = "#272C36"
    BORDER_HI = "#384050"
    TEXT = "#E8EBF1"
    MUTED = "#9BA3B2"
    DIM = "#6B7486"
    ACCENT = "#6A97FF"
    ACCENT_HI = "#89ACFF"
    ACCENT_BG = "#1C2842"
    ON_ACCENT = "#08102A"
    OK = "#43D69C"
    ERR = "#FF7070"
    WARN = "#F3B94D"


_QSS = """
* { outline: none; }
QMainWindow, QDialog { background: @BG; }
QToolTip { background: @RAISED; color: @TEXT; border: 1px solid @BORDER_HI;
           padding: 6px 8px; border-radius: 6px; }

QLabel { background: transparent; color: @TEXT; }
QLabel#H1 { font-size: 24px; font-weight: 600; }
QLabel#H2 { font-size: 15px; font-weight: 600; }
QLabel#Brand { font-size: 17px; font-weight: 600; }
QLabel#Section { color: @DIM; font-size: 12px; font-weight: 600; padding-left: 6px; }
QLabel#Field { color: @MUTED; font-size: 12px; font-weight: 600; }
QLabel#Muted { color: @MUTED; }
QLabel#Dim { color: @DIM; font-size: 12px; }
QLabel#PresetName { font-size: 14px; font-weight: 600; }
QLabel#Count { background: @ACCENT_BG; color: @ACCENT_HI; border-radius: 9px;
               padding: 1px 8px; font-size: 12px; font-weight: 600; }
QLabel#Chip { background: @RAISED; color: @MUTED; border: 1px solid @BORDER;
              border-radius: 6px; padding: 1px 7px; font-size: 11px; }
QLabel#ChipAccent { background: @ACCENT_BG; color: @ACCENT_HI; border: 1px solid #2A3A5E;
                    border-radius: 6px; padding: 1px 7px; font-size: 11px; font-weight: 600; }
QLabel#Hint { color: @DIM; border: 1px dashed @BORDER_HI; border-radius: 12px;
              padding: 18px 16px; }
QLabel#BannerText { color: #F1D9A2; }
QLabel#RowName { font-size: 13px; font-weight: 600; }
QLabel#RowDetail { color: @MUTED; font-size: 12px; }
QLabel#RowDetail[tone="ok"] { color: @OK; }
QLabel#RowDetail[tone="err"] { color: @ERR; }
QLabel#RowDetail[tone="accent"] { color: @ACCENT_HI; }

QFrame#Sidebar { background: @BG_DEEP; border: none; border-right: 1px solid @BORDER; }
QFrame#Inspector { background: @BG_DEEP; border: none; border-left: 1px solid @BORDER; }
QFrame#InspectorFooter { background: @BG_DEEP; border: none; border-top: 1px solid @BORDER; }
QFrame#DeviceCard { background: @SURFACE; border: 1px solid @BORDER; border-radius: 12px; }
QFrame#Banner { background: #2A2313; border: 1px solid #4A3E1C; border-radius: 10px; }
QFrame#SettingsCard { background: @SURFACE; border: 1px solid @BORDER; border-radius: 12px; }
QFrame#Sep { background: @BORDER; border: none; max-height: 1px; min-height: 1px; }

QFrame#PresetCard, QFrame#CustomCard {
    background: @SURFACE; border: 1px solid @BORDER; border-radius: 12px; }
QFrame#PresetCard:hover { background: @SURFACE_HI; border-color: @BORDER_HI; }
QFrame#PresetCard[active="true"] { background: @ACCENT_BG; border-color: @ACCENT; }

QFrame#QueueRow { background: @SURFACE; border: 1px solid @BORDER; border-radius: 12px; }
QFrame#QueueRow[state="active"] { background: @SURFACE_HI; border-color: #3A4C78; }

QFrame#Segmented { background: @BG; border: 1px solid @BORDER; border-radius: 9px; }
QPushButton[seg="true"] { background: transparent; border: none; border-radius: 7px;
                          padding: 0 4px; min-height: 28px; color: @MUTED; font-weight: 500; }
QPushButton[seg="true"]:hover { color: @TEXT; background: transparent; }
QPushButton[seg="true"]:checked { background: @RAISED_HI; color: @TEXT; }
QPushButton[seg="true"]:disabled { color: @DIM; background: transparent; }

QPushButton { background: @RAISED; color: @TEXT; border: 1px solid @BORDER;
              border-radius: 9px; padding: 0 14px; min-height: 34px; font-weight: 500; }
QPushButton:hover { background: @RAISED_HI; border-color: @BORDER_HI; }
QPushButton:pressed { background: @SURFACE; }
QPushButton:focus { border-color: @ACCENT; }
QPushButton:disabled { background: @SURFACE; color: @DIM; border-color: @BORDER; }
QPushButton[variant="primary"] { background: @ACCENT; border: 1px solid @ACCENT;
                                 color: @ON_ACCENT; font-weight: 600; }
QPushButton[variant="primary"]:hover { background: @ACCENT_HI; border-color: @ACCENT_HI; }
QPushButton[variant="primary"]:pressed { background: @ACCENT; }
QPushButton[variant="primary"]:disabled { background: @RAISED; border-color: @BORDER; color: @DIM; }
QPushButton[variant="ghost"] { background: transparent; border: 1px solid transparent; color: @MUTED; }
QPushButton[variant="ghost"]:hover { background: @RAISED; color: @TEXT; }
QPushButton[variant="ghost"]:disabled { color: @DIM; background: transparent; }
QPushButton[variant="danger"] { background: transparent; border: 1px solid #5A2E33; color: @ERR; }
QPushButton[variant="danger"]:hover { background: #2A1A1E; border-color: #7A3B42; }
QPushButton[variant="danger"]:disabled { color: @DIM; border-color: @BORDER; background: transparent; }
QPushButton[variant="link"] { background: transparent; border: none; color: @ACCENT;
                              padding: 0 4px; min-height: 24px; }
QPushButton[variant="link"]:hover { color: @ACCENT_HI; background: transparent; }
QPushButton#SettingsBtn { text-align: left; padding-left: 12px; }

QLineEdit { background: @BG; color: @TEXT; border: 1px solid @BORDER; border-radius: 9px;
            padding: 0 8px; min-height: 34px; selection-background-color: @ACCENT;
            selection-color: @ON_ACCENT; }
QLineEdit:hover { border-color: @BORDER_HI; }
QLineEdit:focus { border-color: @ACCENT; }

QSlider { min-height: 22px; }
QSlider::groove:horizontal { height: 4px; background: @BORDER_HI; border-radius: 2px; }
QSlider::sub-page:horizontal { background: @ACCENT; border-radius: 2px; }
QSlider::handle:horizontal { width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
                             background: @TEXT; border: none; }
QSlider::handle:horizontal:hover { background: #FFFFFF; }

QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: @BORDER_HI; border-radius: 3px; min-height: 40px;
                              margin: 0 2px; }
QScrollBar::handle:vertical:hover { background: @DIM; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { height: 0; }

QListView { background: transparent; border: none; }
QListView#Tray { background: @SURFACE; border: 1px solid @BORDER; border-radius: 12px; padding: 4px; }

QPlainTextEdit#Log { background: @BG_DEEP; color: @MUTED; border: 1px solid @BORDER;
                     border-radius: 10px; padding: 10px;
                     font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace;
                     font-size: 12px; selection-background-color: @ACCENT;
                     selection-color: @ON_ACCENT; }

QMenu { background: @SURFACE_HI; border: 1px solid @BORDER_HI; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 7px 26px 7px 8px; border-radius: 6px; color: @TEXT; }
QMenu::item:selected { background: @RAISED_HI; }
"""


def build_qss() -> str:
    names = sorted((n for n in vars(C) if n.isupper()), key=len, reverse=True)
    qss = _QSS
    for n in names:
        qss = qss.replace("@" + n, getattr(C, n))
    return qss


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    f = QFont()
    f.setFamilies(["Inter", "Segoe UI Variable Text", "Segoe UI", "SF Pro Text",
                   "Helvetica Neue", "Ubuntu", "Noto Sans", "DejaVu Sans"])
    f.setPixelSize(13)
    f.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(f)

    pal = QPalette()
    R = QPalette.ColorRole
    for role, col in (
        (R.Window, C.BG), (R.WindowText, C.TEXT), (R.Base, C.BG), (R.AlternateBase, C.SURFACE),
        (R.Text, C.TEXT), (R.Button, C.RAISED), (R.ButtonText, C.TEXT),
        (R.ToolTipBase, C.RAISED), (R.ToolTipText, C.TEXT), (R.Highlight, C.ACCENT),
        (R.HighlightedText, C.ON_ACCENT), (R.PlaceholderText, C.DIM), (R.Link, C.ACCENT),
    ):
        pal.setColor(role, QColor(col))
    for role in (R.Text, R.ButtonText, R.WindowText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(C.DIM))
    app.setPalette(pal)
    app.setStyleSheet(build_qss())


# ======================================================================
# vector glyphs → crisp icons without image files
# ======================================================================

def _poly(p: QPainter, pts, close=False):
    path = QPainterPath(QPointF(*pts[0]))
    for x, y in pts[1:]:
        path.lineTo(x, y)
    if close:
        path.closeSubpath()
    p.drawPath(path)


def _g_search(p):
    p.drawEllipse(QPointF(10.5, 10.5), 6.0, 6.0)
    p.drawLine(QPointF(15.0, 15.0), QPointF(20.0, 20.0))


def _g_refresh(p):
    rect = QRectF(5, 5, 14, 14)
    path = QPainterPath()
    path.arcMoveTo(rect, 60)
    path.arcTo(rect, 60, 280)
    p.drawPath(path)
    a = math.radians(340)
    ex, ey = 12 + 7 * math.cos(a), 12 - 7 * math.sin(a)
    tx, ty = -math.sin(a), -math.cos(a)
    for s in (1, -1):
        ang = math.atan2(-ty, -tx) + s * math.radians(42)
        p.drawLine(QPointF(ex, ey), QPointF(ex + 4.2 * math.cos(ang), ey + 4.2 * math.sin(ang)))


def _g_sliders(p):
    for y, kx in ((7, 9.0), (12, 15.0), (17, 8.0)):
        p.drawLine(QPointF(4, y), QPointF(kx - 2.9, y))
        p.drawLine(QPointF(kx + 2.9, y), QPointF(20, y))
        p.drawEllipse(QPointF(kx, y), 2.1, 2.1)


def _g_folder(p):
    path = QPainterPath()
    path.moveTo(3.5, 7.5)
    path.quadTo(3.5, 5.5, 5.5, 5.5)
    path.lineTo(9.3, 5.5)
    path.lineTo(11.3, 7.8)
    path.lineTo(18.5, 7.8)
    path.quadTo(20.5, 7.8, 20.5, 9.8)
    path.lineTo(20.5, 17.5)
    path.quadTo(20.5, 19.5, 18.5, 19.5)
    path.lineTo(5.5, 19.5)
    path.quadTo(3.5, 19.5, 3.5, 17.5)
    path.closeSubpath()
    p.drawPath(path)


def _g_grid(p):
    for x in (4.0, 13.5):
        for y in (4.0, 13.5):
            p.drawRoundedRect(QRectF(x, y, 6.5, 6.5), 1.8, 1.8)


def _g_film(p):
    p.drawRoundedRect(QRectF(3.5, 5.5, 17, 13), 3, 3)
    _poly(p, [(10.2, 9.3), (10.2, 14.7), (14.6, 12.0)], close=True)


def _g_check(p):
    _poly(p, [(5.5, 12.5), (10, 17), (18.5, 7.5)])


def _g_x(p):
    p.drawLine(QPointF(6.5, 6.5), QPointF(17.5, 17.5))
    p.drawLine(QPointF(17.5, 6.5), QPointF(6.5, 17.5))


def _g_chev_down(p):
    _poly(p, [(7, 9.5), (12, 14.5), (17, 9.5)])


def _g_chev_right(p):
    _poly(p, [(9.5, 7), (14.5, 12), (9.5, 17)])


def _g_chev_left(p):
    _poly(p, [(14.5, 7), (9.5, 12), (14.5, 17)])


def _g_image(p):
    p.drawRoundedRect(QRectF(3.5, 4.5, 17, 15), 3, 3)
    p.drawEllipse(QPointF(9, 10), 1.5, 1.5)
    _poly(p, [(4.5, 17.5), (10, 13), (13, 15.8), (15.5, 13.5), (19.5, 17.5)])


def _g_alert(p):
    _poly(p, [(12, 4.5), (20.5, 19), (3.5, 19)], close=True)
    p.drawLine(QPointF(12, 10.5), QPointF(12, 14.2))
    p.drawLine(QPointF(12, 16.7), QPointF(12, 16.8))


def _g_phone(p):
    p.drawRoundedRect(QRectF(7, 3.5, 10, 17), 2.4, 2.4)
    p.drawLine(QPointF(10.5, 17.6), QPointF(13.5, 17.6))


def _g_logo(p):
    _poly(p, [(7.5, 4.5), (12, 8.5), (16.5, 4.5)])
    _poly(p, [(7.5, 19.5), (12, 15.5), (16.5, 19.5)])
    p.drawLine(QPointF(6, 12), QPointF(18, 12))


def _g_dash(p):
    p.drawLine(QPointF(7, 12), QPointF(17, 12))


_GLYPHS = {
    "search": _g_search, "refresh": _g_refresh, "sliders": _g_sliders,
    "folder": _g_folder, "grid": _g_grid, "film": _g_film, "check": _g_check,
    "x": _g_x, "chevron-down": _g_chev_down, "chevron-right": _g_chev_right,
    "chevron-left": _g_chev_left, "image": _g_image, "alert": _g_alert,
    "phone": _g_phone, "logo": _g_logo, "dash": _g_dash,
}


def draw_glyph(p: QPainter, name: str, rect: QRectF, color, stroke: float = 1.8) -> None:
    fn = _GLYPHS.get(name)
    if fn is None:
        return
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.translate(rect.x(), rect.y())
    s = rect.width() / 24.0
    p.scale(s, s)
    p.setPen(QPen(QColor(color), stroke, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    fn(p)
    p.restore()


def _dpr() -> float:
    scr = QGuiApplication.primaryScreen()
    return max(2.0, math.ceil(scr.devicePixelRatio())) if scr else 2.0


_ICONS: dict = {}


def icon(name: str, color: str = C.MUTED, size: int = 18) -> QIcon:
    key = (name, color, size)
    ic = _ICONS.get(key)
    if ic is not None:
        return ic
    scale = _dpr()
    ic = QIcon()
    for mode, col in ((QIcon.Mode.Normal, color), (QIcon.Mode.Disabled, C.DIM)):
        pm = QPixmap(int(size * scale), int(size * scale))
        pm.fill(Qt.GlobalColor.transparent)
        pm.setDevicePixelRatio(scale)
        p = QPainter(pm)
        draw_glyph(p, name, QRectF(0, 0, size, size), QColor(col))
        p.end()
        ic.addPixmap(pm, mode)
    _ICONS[key] = ic
    return ic


def blank_icon(size: int = 16) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    return QIcon(pm)


# ======================================================================
# small helpers
# ======================================================================

def mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def make_button(text="", variant="", glyph=None, glyph_color=None) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("variant", variant)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    if glyph:
        col = glyph_color or (C.ON_ACCENT if variant == "primary"
                              else C.ERR if variant == "danger" else C.MUTED)
        b.setIcon(icon(glyph, col, 16))
        b.setIconSize(QSize(16, 16))
    return b


def make_scroll(content: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sa.setWidget(content)
    content.setAutoFillBackground(False)
    sa.viewport().setAutoFillBackground(False)
    return sa


def cover_src(pw: int, ph: int, target: QRectF) -> QRectF:
    """Source rect that center-crops a pw×ph pixmap to fill `target`."""
    tr = target.width() / max(1.0, target.height())
    if pw / max(1, ph) > tr:
        w = ph * tr
        return QRectF((pw - w) / 2, 0, w, ph)
    h = pw / tr
    return QRectF(0, (ph - h) / 2, pw, h)


def rounded_path(rect, r) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), r, r)
    return path


def cached_pixmap(cache: ThumbCache, v):
    if cache.has(v.thumb_key):
        pm = cache.get(v.thumb_key)
        if pm is not None and not pm.isNull():
            return pm
    return None


def rounded_thumb(cache: ThumbCache, v, w: int, h: int, r: int) -> QPixmap:
    dpr = _dpr()
    out = QPixmap(int(w * dpr), int(h * dpr))
    out.fill(Qt.GlobalColor.transparent)
    out.setDevicePixelRatio(dpr)
    p = QPainter(out)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    p.setClipPath(rounded_path(QRectF(0, 0, w, h), r))
    pm = cached_pixmap(cache, v)
    target = QRectF(0, 0, w, h)
    if pm is not None:
        p.drawPixmap(target, pm, cover_src(pm.width(), pm.height(), target))
    else:
        p.fillRect(target, QColor(C.RAISED))
        draw_glyph(p, "film", QRectF(w / 2 - 11, h / 2 - 11, 22, 22), QColor(C.DIM))
    p.end()
    return out


def draw_text(p: QPainter, rect, text: str, color, font: QFont,
              align=Qt.AlignmentFlag.AlignLeft) -> None:
    p.setFont(font)
    p.setPen(QColor(color))
    text = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width()))
    p.drawText(QRectF(rect), align | Qt.AlignmentFlag.AlignVCenter, text)


def draw_spinner(p: QPainter, center: QPointF, r: float, angle: float, color=C.ACCENT, w=2.2):
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r)
    p.setPen(QPen(QColor(C.BORDER_HI), w))
    p.drawEllipse(rect)
    p.setPen(QPen(QColor(color), w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawArc(rect, int((90 - angle) * 16), -100 * 16)
    p.restore()


# ======================================================================
# reusable widgets
# ======================================================================

class ElideLabel(QLabel):
    """Single-line label that elides to the width the layout gives it."""

    def __init__(self, text="", parent=None, mode=Qt.TextElideMode.ElideRight):
        super().__init__(parent)
        self._full = text or ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self._apply()

    def setText(self, text):  # noqa: N802
        self._full = text or ""
        self._apply()

    def fullText(self):  # noqa: N802
        return self._full

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._apply()

    def _apply(self):
        w = self.width()
        shown = self.fontMetrics().elidedText(self._full, self._mode, w) if w > 8 else self._full
        if shown != self.text():
            super().setText(shown)


class LogoMark(QWidget):
    def __init__(self, size=30, parent=None):
        super().__init__(parent)
        self._s = size
        self.setFixedSize(size, size)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C.ACCENT))
        p.drawRoundedRect(QRectF(self.rect()), self._s * 0.28, self._s * 0.28)
        m = self._s * 0.16
        draw_glyph(p, "logo", QRectF(m, m, self._s - 2 * m, self._s - 2 * m),
                   QColor(C.ON_ACCENT), stroke=2.3)


class StateIcon(QWidget):
    """Tiny status glyph: waiting / active(spinner) / done / failed / skipped / online / offline."""

    def __init__(self, size=20, parent=None):
        super().__init__(parent)
        self._s = size
        self._state = "waiting"
        self._angle = 0.0
        self.setFixedSize(size, size)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
        self._anim.setDuration(900)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._tick)

    def set_state(self, state: str):
        self._state = state
        if state == "active" and self.isVisible():
            self._anim.start()
        else:
            self._anim.stop()
        self.update()

    def _tick(self, v):
        self._angle = float(v)
        self.update()

    def showEvent(self, e):  # noqa: N802
        if self._state == "active":
            self._anim.start()
        super().showEvent(e)

    def hideEvent(self, e):  # noqa: N802
        self._anim.stop()
        super().hideEvent(e)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s, st = self._s, self._state
        r = QRectF(1.5, 1.5, s - 3, s - 3)
        inner = QRectF(s * 0.24, s * 0.24, s * 0.52, s * 0.52)
        if st == "waiting":
            p.setPen(QPen(QColor(C.BORDER_HI), 1.6))
            p.drawEllipse(r)
        elif st == "active":
            draw_spinner(p, QPointF(s / 2, s / 2), s / 2 - 2, self._angle)
        elif st in ("done", "failed"):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.OK if st == "done" else C.ERR))
            p.drawEllipse(r)
            draw_glyph(p, "check" if st == "done" else "x", inner, QColor(C.ON_ACCENT), 2.6)
        elif st == "skipped":
            p.setPen(QPen(QColor(C.DIM), 1.6))
            p.drawEllipse(r)
            draw_glyph(p, "dash", inner, QColor(C.DIM), 2.4)
        elif st in ("online", "offline"):
            draw_glyph(p, "phone", QRectF(0, 0, s, s),
                       QColor(C.OK if st == "online" else C.ERR), 1.8)


class Badge(QWidget):
    """Large round icon used in empty states (glyph or spinner)."""

    def __init__(self, size=76, parent=None):
        super().__init__(parent)
        self._s = size
        self._glyph = "film"
        self._color = C.MUTED
        self._busy = False
        self._angle = 0.0
        self.setFixedSize(size, size)
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
        self._anim.setDuration(900)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._tick)

    def set_content(self, glyph: str, color=C.MUTED, busy=False):
        self._glyph, self._color, self._busy = glyph, color, busy
        if busy and self.isVisible():
            self._anim.start()
        else:
            self._anim.stop()
        self.update()

    def _tick(self, v):
        self._angle = float(v)
        self.update()

    def showEvent(self, e):  # noqa: N802
        if self._busy:
            self._anim.start()
        super().showEvent(e)

    def hideEvent(self, e):  # noqa: N802
        self._anim.stop()
        super().hideEvent(e)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self._s
        p.setPen(QPen(QColor(C.BORDER), 1))
        p.setBrush(QColor(C.SURFACE))
        p.drawEllipse(QRectF(0.5, 0.5, s - 1, s - 1))
        if self._busy:
            draw_spinner(p, QPointF(s / 2, s / 2), s * 0.22, self._angle, C.ACCENT, 3)
        else:
            g = s * 0.42
            draw_glyph(p, self._glyph, QRectF((s - g) / 2, (s - g) / 2, g, g),
                       QColor(self._color), 1.7)


class SlimProgress(QWidget):
    def __init__(self, height=4, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self._v = 0.0
        self._mode = "idle"
        self._color = C.ACCENT
        self._t = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(1300)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._tick)

    def _tick(self, v):
        self._t = float(v)
        self.update()

    def set_color(self, color: str):
        self._color = color
        self.update()

    def set_idle(self):
        self._mode, self._v = "idle", 0.0
        self._anim.stop()
        self.update()

    def set_value(self, pct: float):
        self._mode = "value"
        self._v = max(0.0, min(100.0, float(pct)))
        self._anim.stop()
        self.update()

    def set_busy(self):
        self._mode = "busy"
        self._anim.start()
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        rad = r.height() / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C.BORDER))
        p.drawRoundedRect(r, rad, rad)
        p.setBrush(QColor(self._color))
        if self._mode == "value" and self._v > 0:
            p.drawRoundedRect(QRectF(0, 0, max(r.height(), r.width() * self._v / 100.0), r.height()),
                              rad, rad)
        elif self._mode == "busy":
            p.setClipPath(rounded_path(r, rad))
            seg = r.width() * 0.3
            x = -seg + (r.width() + seg) * self._t
            p.drawRoundedRect(QRectF(x, 0, seg, r.height()), rad, rad)


class Switch(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(40, 24)
        self._t = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(140)
        self._anim.valueChanged.connect(self._tick)
        self.toggled.connect(self._animate)

    def _tick(self, v):
        self._t = float(v)
        self.update()

    def _animate(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        track = mix(QColor(C.BORDER_HI), QColor(C.ACCENT), self._t)
        if not self.isEnabled():
            track.setAlpha(110)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.x() + 3 + self._t * (r.width() - d - 6)
        p.setBrush(QColor("#FFFFFF" if self.isEnabled() else C.MUTED))
        p.drawEllipse(QRectF(x, r.y() + 3, d, d))
        if self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(C.ACCENT_HI), 1.5))
            p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 13, 13)


class Segmented(QFrame):
    changed = pyqtSignal(int)

    def __init__(self, labels, parent=None):
        super().__init__(parent)
        self.setObjectName("Segmented")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for i, text in enumerate(labels):
            b = QPushButton(text)
            b.setProperty("seg", True)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            self._group.addButton(b, i)
            lay.addWidget(b, 1)
        self._group.button(0).setChecked(True)
        self._group.idClicked.connect(self.changed.emit)

    def index(self) -> int:
        return max(0, self._group.checkedId())

    def set_index(self, i: int):
        b = self._group.button(i)
        if b:
            b.setChecked(True)


class RadioMark(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False
        self.setFixedSize(18, 18)

    def set_active(self, on: bool):
        self._on = on
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1.5, 1.5, 15, 15)
        p.setPen(QPen(QColor(C.ACCENT if self._on else C.BORDER_HI), 1.6))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(r)
        if self._on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.ACCENT))
            p.drawEllipse(QRectF(5.5, 5.5, 7, 7))


class EmptyState(QWidget):
    actionClicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 40, 40, 80)
        outer.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.badge = Badge(76)
        self.title = QLabel(objectName="H2")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text = QLabel(objectName="Muted")
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setWordWrap(True)
        self.text.setMaximumWidth(400)
        self.button = make_button("", "")
        self.button.clicked.connect(self.actionClicked)
        col.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addSpacing(18)
        col.addWidget(self.title)
        col.addSpacing(6)
        col.addWidget(self.text, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addSpacing(18)
        col.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addLayout(col)
        outer.addStretch(2)

    def show_state(self, glyph, title, text, action=None, busy=False, color=C.MUTED):
        self.badge.set_content(glyph, color, busy)
        self.title.setText(title)
        self.text.setText(text)
        self.text.setVisible(bool(text))
        self.button.setVisible(bool(action))
        if action:
            self.button.setText(action)


class Banner(QFrame):
    actionClicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(icon("alert", C.WARN, 18).pixmap(QSize(18, 18)))
        self.label = QLabel(objectName="BannerText")
        self.label.setWordWrap(True)
        self.button = make_button("Check again", "ghost")
        self.button.clicked.connect(self.actionClicked)
        lay.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.label, 1)
        lay.addWidget(self.button)

    def set_text(self, text: str):
        self.label.setText(text)


# ======================================================================
# selection + models
# ======================================================================

class SelectionStore(QObject):
    """Keeps the user's selection across folders and searches."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: dict[str, adb.Video] = {}

    def __contains__(self, v) -> bool:
        return v.phone_path in self._items

    def toggle(self, v):
        if v.phone_path in self._items:
            del self._items[v.phone_path]
        else:
            self._items[v.phone_path] = v
        self.changed.emit()

    def set_many(self, videos, on: bool):
        before = len(self._items)
        for v in videos:
            if on:
                self._items[v.phone_path] = v
            else:
                self._items.pop(v.phone_path, None)
        self.changed.emit()
        return len(self._items) != before

    def remove(self, v):
        if self._items.pop(v.phone_path, None) is not None:
            self.changed.emit()

    def clear(self):
        if self._items:
            self._items.clear()
            self.changed.emit()

    def videos(self) -> list:
        return list(self._items.values())

    def count(self) -> int:
        return len(self._items)

    def total_bytes(self) -> int:
        return sum((v.size or 0) for v in self._items.values())

    def reconcile(self, fresh) -> None:
        """After a reload: keep only still-existing videos, using the new objects."""
        by_path = {v.phone_path: v for v in fresh}
        new = {k: by_path[k] for k in self._items if k in by_path}
        if list(new) != list(self._items):
            self._items = new
            self.changed.emit()
        else:
            self._items = new


class VideoModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[adb.Video] = []

    def set_items(self, items):
        self.beginResetModel()
        self.items = list(items)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.items):
            return None
        v = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return v.name
        if role == ROLE_VIDEO:
            return v
        if role == Qt.ItemDataRole.ToolTipRole:
            bits = [fmt_size(v.size)]
            if v.duration_ms:
                bits.append(fmt_dur(v.duration_ms))
            if v.width:
                bits.append(f"{v.width}×{v.height}")
            return f"{v.name}\n{v.folder}\n{'  ·  '.join(bits)}\n{long_date(v.modified)}"
        return None


def thumb_state(cache: ThumbCache, states: dict, v) -> str:
    if cache.has(v.thumb_key):
        return "ready"
    return states.get(v.thumb_key, "empty")


# ======================================================================
# library grid
# ======================================================================

class VideoCardDelegate(QStyledItemDelegate):
    INSET = 6
    PAD = 8
    TEXT_BLOCK = 58

    def __init__(self, store: SelectionStore, cache: ThumbCache, states: dict, parent=None):
        super().__init__(parent)
        self.store, self.cache, self.states = store, cache, states
        self.tile = QSize(232, 210)
        self.phase = 0
        self.show_folder = False

    @classmethod
    def tile_height(cls, w: int) -> int:
        thumb_h = round((w - 2 * cls.INSET - 2 * cls.PAD) * 9 / 16)
        return 2 * cls.INSET + cls.PAD + thumb_h + cls.TEXT_BLOCK

    def sizeHint(self, option, index):  # noqa: N802
        return self.tile

    def geometry(self, rect: QRect):
        card = rect.adjusted(self.INSET, self.INSET, -self.INSET, -self.INSET)
        tw = card.width() - 2 * self.PAD
        thumb = QRect(card.x() + self.PAD, card.y() + self.PAD, tw, round(tw * 9 / 16))
        check = QRect(thumb.x() + 8, thumb.y() + 8, 22, 22)
        action = QRect(0, 0, 104, 26)
        action.moveCenter(QPoint(thumb.center().x(), thumb.center().y() + 20))
        return card, thumb, check, action

    def hit_preview(self, rect: QRect, pos: QPoint, v) -> bool:
        if thumb_state(self.cache, self.states, v) not in ("empty", "failed"):
            return False
        return self.geometry(rect)[3].contains(pos)

    def paint(self, p, option, index):
        v = index.data(ROLE_VIDEO)
        if v is None:
            return
        p.save()
        p.setRenderHints(QPainter.RenderHint.Antialiasing |
                         QPainter.RenderHint.SmoothPixmapTransform |
                         QPainter.RenderHint.TextAntialiasing)
        card, thumb, check, action = self.geometry(option.rect)
        selected = v in self.store
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        if selected:
            bg, border = C.ACCENT_BG, C.ACCENT
        elif hover:
            bg, border = C.SURFACE_HI, C.BORDER_HI
        else:
            bg, border = C.SURFACE, C.BORDER
        p.setPen(QPen(QColor(border), 1.4 if selected else 1))
        p.setBrush(QColor(bg))
        p.drawRoundedRect(QRectF(card).adjusted(.5, .5, -.5, -.5), 12, 12)

        # ---- thumbnail
        state = thumb_state(self.cache, self.states, v)
        p.save()
        p.setClipPath(rounded_path(thumb, 8))
        pm = cached_pixmap(self.cache, v) if state == "ready" else None
        if pm is not None:
            tgt = QRectF(thumb)
            p.drawPixmap(tgt, pm, cover_src(pm.width(), pm.height(), tgt))
        else:
            p.fillRect(thumb, QColor(C.RAISED))
            cx, cy = thumb.center().x(), thumb.center().y()
            small = QFont(option.font)
            small.setPixelSize(12)
            if state == "loading":
                draw_spinner(p, QPointF(cx, cy - 6), 10, self.phase * 30)
                draw_text(p, QRectF(thumb.x(), cy + 10, thumb.width(), 18), "Loading preview",
                          C.MUTED, small, Qt.AlignmentFlag.AlignHCenter)
            else:
                draw_glyph(p, "film", QRectF(cx - 12, cy - 34, 24, 24), QColor(C.DIM), 1.6)
                failed = state == "failed"
                p.setPen(QPen(QColor("#5A2E33" if failed else C.BORDER_HI), 1))
                p.setBrush(QColor(C.RAISED_HI))
                p.drawRoundedRect(QRectF(action), 13, 13)
                draw_text(p, QRectF(action), "Retry" if failed else "Load preview",
                          C.ERR if failed else C.MUTED, small, Qt.AlignmentFlag.AlignHCenter)
        p.restore()

        # ---- chips
        chip_font = QFont(option.font)
        chip_font.setPixelSize(11)
        chip_font.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetrics(chip_font)

        def chip(text, right_edge=None, left_edge=None):
            w = fm.horizontalAdvance(text) + 12
            x = (right_edge - w) if right_edge is not None else left_edge
            rect = QRectF(x, thumb.bottom() - 21, w, 17)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(8, 10, 14, 185))
            p.drawRoundedRect(rect, 5, 5)
            draw_text(p, rect, text, "#FFFFFF", chip_font, Qt.AlignmentFlag.AlignHCenter)
            return w

        rx = thumb.right() - 6 + 1
        if v.duration_ms:
            w = chip(fmt_dur(v.duration_ms), right_edge=rx)
            rx -= w + 4
        rl = res_label(v)
        if rl:
            chip(rl, left_edge=thumb.x() + 6)

        # ---- selection checkbox
        cr = QRectF(check)
        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.ACCENT))
            p.drawEllipse(cr)
            draw_glyph(p, "check", cr.adjusted(4.5, 4.5, -4.5, -4.5), QColor(C.ON_ACCENT), 2.6)
        else:
            p.setPen(QPen(QColor(255, 255, 255, 235 if hover else 170), 1.6))
            p.setBrush(QColor(8, 10, 14, 150 if hover else 110))
            p.drawEllipse(cr.adjusted(.8, .8, -.8, -.8))

        # ---- text
        name_f = QFont(option.font)
        name_f.setPixelSize(13)
        name_f.setWeight(QFont.Weight.DemiBold)
        meta_f = QFont(option.font)
        meta_f.setPixelSize(12)
        tx, tw = card.x() + self.PAD + 2, card.width() - 2 * self.PAD - 4
        y = thumb.bottom() + 10
        draw_text(p, QRectF(tx, y, tw, 18), v.name, C.TEXT, name_f)
        size_txt = fmt_size(v.size)
        right_txt = v.folder if self.show_folder else short_date(v.modified)
        sw = QFontMetrics(meta_f).horizontalAdvance(size_txt) + 10
        draw_text(p, QRectF(tx, y + 21, sw, 16), size_txt, C.MUTED, meta_f)
        draw_text(p, QRectF(tx + sw, y + 21, tw - sw, 16), right_txt, C.DIM, meta_f,
                  Qt.AlignmentFlag.AlignRight)
        p.restore()


class VideoGrid(QListView):
    previewRequested = pyqtSignal(object)
    MIN_TILE = 214

    def __init__(self, store: SelectionStore, cache: ThumbCache, states: dict, parent=None):
        super().__init__(parent)
        self.store = store
        self.card_delegate = VideoCardDelegate(store, cache, states, self)
        self.setItemDelegate(self.card_delegate)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setSpacing(0)
        self.setUniformItemSizes(True)
        self.setWrapping(True)
        self.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._anchor: int | None = None
        self._tile = QSize(0, 0)
        self.verticalScrollBar().setSingleStep(40)
        self._relayout()

    def set_items(self, items):
        self._anchor = None
        self.model().set_items(items)

    def _relayout(self):
        avail = self.width() - 2 * self.frameWidth() - SCROLLBAR_W
        if avail < 100:
            return
        cols = max(1, avail // self.MIN_TILE)
        w = avail // cols
        size = QSize(w, VideoCardDelegate.tile_height(w))
        if size != self._tile:
            self._tile = size
            self.card_delegate.tile = size
            self.setGridSize(size)
            self.scheduleDelayedItemsLayout()

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._relayout()

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        self.setFocus()
        if not idx.isValid():
            return
        v = idx.data(ROLE_VIDEO)
        if self.card_delegate.hit_preview(self.visualRect(idx), pos, v):
            self.previewRequested.emit(v)
            return
        items = self.model().items
        if (e.modifiers() & Qt.KeyboardModifier.ShiftModifier) and self._anchor is not None:
            lo, hi = sorted((self._anchor, idx.row()))
            self.store.set_many(items[lo:hi + 1], True)
        else:
            self.store.toggle(v)
            self._anchor = idx.row()

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position().toPoint()
        over_card = self.indexAt(pos).isValid()
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over_card
                                  else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(e)


# ======================================================================
# sidebar
# ======================================================================

class NavItem(QAbstractButton):
    def __init__(self, glyph: str, text: str, count: int | None = None, key=None, parent=None):
        super().__init__(parent)
        self.glyph, self._label, self._count, self.key = glyph, text, count, key
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(38)
        self.setToolTip(text)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def set_count(self, n):
        self._count = n
        self.update()

    def sizeHint(self):  # noqa: N802
        return QSize(200, 38)

    def enterEvent(self, e):  # noqa: N802
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):  # noqa: N802
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.45)
        r = QRectF(self.rect()).adjusted(0, 1, 0, -1)
        checked, hover = self.isChecked(), self.underMouse()
        if checked or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.RAISED if checked else C.SURFACE))
            p.drawRoundedRect(r, 9, 9)
        if checked:
            p.setBrush(QColor(C.ACCENT))
            p.drawRoundedRect(QRectF(0, r.center().y() - 8, 3, 16), 1.5, 1.5)
        col = C.ACCENT if checked else (C.TEXT if hover else C.MUTED)
        draw_glyph(p, self.glyph, QRectF(14, r.center().y() - 9, 18, 18), QColor(col), 1.7)
        f = QFont(self.font())
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.DemiBold if checked else QFont.Weight.Medium)
        right = r.right() - 12
        if self._count is not None:
            cf = QFont(self.font())
            cf.setPixelSize(12)
            txt = str(self._count)
            cw = QFontMetrics(cf).horizontalAdvance(txt) + 6
            draw_text(p, QRectF(right - cw, r.y(), cw, r.height()), txt, C.DIM, cf,
                      Qt.AlignmentFlag.AlignRight)
            right -= cw + 6
        draw_text(p, QRectF(42, r.y(), max(0, right - 42), r.height()), self._label,
                  C.TEXT if (checked or hover) else "#C3C9D4", f)


class DeviceCard(QFrame):
    refreshClicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DeviceCard")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 8, 10)
        lay.setSpacing(10)
        self.state_icon = StateIcon(22)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.title = ElideLabel()
        self.title.setStyleSheet("font-weight:600;")
        self.sub = ElideLabel()
        self.sub.setObjectName("Dim")
        col.addWidget(self.title)
        col.addWidget(self.sub)
        self.refresh_btn = make_button("", "ghost", "refresh")
        self.refresh_btn.setFixedSize(32, 32)
        self.refresh_btn.setStyleSheet("padding:0; min-height:32px;")
        self.refresh_btn.setToolTip("Refresh  (F5)")
        self.refresh_btn.clicked.connect(self.refreshClicked)
        lay.addWidget(self.state_icon)
        lay.addLayout(col, 1)
        lay.addWidget(self.refresh_btn)

    def set_state(self, state: str, title: str, sub: str, tip: str = ""):
        self.state_icon.set_state(state)
        self.title.setText(title)
        self.sub.setText(sub)
        self.setToolTip(tip or sub)


# ======================================================================
# inspector (right panel): selection tray + presets + custom settings
# ======================================================================

class TrayDelegate(QStyledItemDelegate):
    ROW_H = 52

    def __init__(self, cache: ThumbCache, parent=None):
        super().__init__(parent)
        self.cache = cache

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(100, self.ROW_H)

    @staticmethod
    def remove_rect(rect: QRect) -> QRect:
        return QRect(rect.right() - 28, rect.center().y() - 11, 22, 22)

    def paint(self, p, option, index):
        v = index.data(ROLE_VIDEO)
        if v is None:
            return
        p.save()
        p.setRenderHints(QPainter.RenderHint.Antialiasing |
                         QPainter.RenderHint.SmoothPixmapTransform |
                         QPainter.RenderHint.TextAntialiasing)
        r = option.rect.adjusted(0, 2, 0, -2)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.SURFACE_HI))
            p.drawRoundedRect(QRectF(r), 8, 8)
        thumb = QRectF(r.x() + 6, r.center().y() - 16, 56, 32)
        p.drawPixmap(thumb.toRect().topLeft(), rounded_thumb(self.cache, v, 56, 32, 6))
        nf = QFont(option.font)
        nf.setPixelSize(13)
        nf.setWeight(QFont.Weight.Medium)
        mf = QFont(option.font)
        mf.setPixelSize(12)
        x0 = r.x() + 72
        w = r.right() - 34 - x0
        draw_text(p, QRectF(x0, r.y() + 6, w, 18), v.name, C.TEXT, nf)
        meta = fmt_size(v.size) + (f"   {fmt_dur(v.duration_ms)}" if v.duration_ms else "")
        draw_text(p, QRectF(x0, r.y() + 24, w, 16), meta, C.MUTED, mf)
        rr = QRectF(self.remove_rect(r))
        if hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C.RAISED_HI))
            p.drawEllipse(rr)
        draw_glyph(p, "x", rr.adjusted(5, 5, -5, -5), QColor(C.TEXT if hover else C.DIM), 2.0)
        p.restore()


class SelectionTray(QListView):
    removeRequested = pyqtSignal(object)

    def __init__(self, cache: ThumbCache, parent=None):
        super().__init__(parent)
        self.setObjectName("Tray")
        self.setModel(VideoModel(self))
        self.tray_delegate = TrayDelegate(cache, self)
        self.setItemDelegate(self.tray_delegate)
        self.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        if not idx.isValid():
            return
        rect = self.visualRect(idx).adjusted(0, 2, 0, -2)
        if TrayDelegate.remove_rect(rect).adjusted(-8, -8, 8, 8).contains(pos):
            self.removeRequested.emit(idx.data(ROLE_VIDEO))

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        over = False
        if idx.isValid():
            rect = self.visualRect(idx).adjusted(0, 2, 0, -2)
            over = TrayDelegate.remove_rect(rect).adjusted(-8, -8, 8, 8).contains(pos)
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over
                                  else Qt.CursorShape.ArrowCursor)
        super().mouseMoveEvent(e)


class PresetCard(QFrame):
    clicked = pyqtSignal(str)

    def __init__(self, pid: str, preset, parent=None):
        super().__init__(parent)
        self.pid = pid
        self.setObjectName("PresetCard")
        self.setProperty("active", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(12)
        self.mark = RadioMark()
        outer.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        name = QLabel(preset.name, objectName="PresetName")
        tag = QLabel(preset.tagline, objectName="Muted")
        tag.setWordWrap(True)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.setContentsMargins(0, 4, 0, 0)
        if getattr(preset, "hint", ""):
            chips.addWidget(QLabel(preset.hint, objectName="ChipAccent"))
        chips.addWidget(QLabel(preset.codec_label, objectName="Chip"))
        chips.addWidget(QLabel(preset.res_label, objectName="Chip"))
        chips.addStretch(1)
        col.addWidget(name)
        col.addWidget(tag)
        col.addLayout(chips)
        outer.addLayout(col, 1)

    def set_active(self, on: bool):
        self.setProperty("active", on)
        self.mark.set_active(on)
        repolish(self)

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.pid)


RES_LABELS = ["Original", "1440p", "1080p", "720p"]
RES_VALUES = [0, 1440, 1080, 720]
AUDIO_LABELS = ["192k", "160k", "128k", "96k"]
AUDIO_VALUES = [192, 160, 128, 96]


class Inspector(QFrame):
    startRequested = pyqtSignal()

    def __init__(self, store: SelectionStore, cache: ThumbCache, parent=None):
        super().__init__(parent)
        self.setObjectName("Inspector")
        self.setFixedWidth(344)
        self.store = store
        self.preset_id = DEFAULT_PRESET_ID

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # header
        head = QHBoxLayout()
        head.setContentsMargins(20, 22, 20, 12)
        head.setSpacing(8)
        head.addWidget(QLabel("Selected videos", objectName="H2"))
        self.count = QLabel("0", objectName="Count")
        head.addWidget(self.count)
        head.addStretch(1)
        self.clear_btn = make_button("Clear all", "link")
        self.clear_btn.clicked.connect(store.clear)
        head.addWidget(self.clear_btn)
        root.addLayout(head)

        # scrolling body
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(20, 0, 20, 18)
        bl.setSpacing(10)

        self.hint = QLabel("Click videos to select them. Hold Shift to select a range, "
                           "or use Select all.", objectName="Hint")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bl.addWidget(self.hint)
        self.tray = SelectionTray(cache)
        self.tray.removeRequested.connect(store.remove)
        bl.addWidget(self.tray)
        bl.addSpacing(10)

        bl.addWidget(QLabel("Quality preset", objectName="H2"))
        self.cards: dict[str, PresetCard] = {}
        for pid, preset in PRESETS.items():
            card = PresetCard(pid, preset)
            card.clicked.connect(self.set_preset)
            self.cards[pid] = card
            bl.addWidget(card)

        bl.addSpacing(6)
        bl.addWidget(self._build_custom())
        bl.addStretch(1)
        root.addWidget(make_scroll(body), 1)

        # footer
        foot = QFrame(objectName="InspectorFooter")
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(20, 14, 20, 18)
        fl.setSpacing(10)
        row = QHBoxLayout()
        self.n_lbl = QLabel("No videos selected", objectName="Muted")
        self.size_lbl = QLabel("", objectName="Muted")
        row.addWidget(self.n_lbl)
        row.addStretch(1)
        row.addWidget(self.size_lbl)
        fl.addLayout(row)
        self.go_btn = make_button("Compress", "primary")
        self.go_btn.setMinimumHeight(42)
        self.go_btn.clicked.connect(self.startRequested)
        fl.addWidget(self.go_btn)
        root.addWidget(foot)

        self.set_preset(self.preset_id)
        self.refresh()

    # ----- custom settings card
    def _build_custom(self) -> QFrame:
        card = QFrame(objectName="CustomCard")
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(0)
        top = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(QLabel("Custom settings", objectName="PresetName"))
        col.addWidget(QLabel("Override the preset for this run", objectName="Muted"))
        top.addLayout(col, 1)
        self.custom_switch = Switch()
        top.addWidget(self.custom_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(top)

        self.custom_body = QWidget()
        b = QVBoxLayout(self.custom_body)
        b.setContentsMargins(0, 14, 0, 0)
        b.setSpacing(6)

        qrow = QHBoxLayout()
        qrow.addWidget(QLabel("Quality", objectName="Field"))
        qrow.addStretch(1)
        self.crf_lbl = QLabel("", objectName="Muted")
        qrow.addWidget(self.crf_lbl)
        b.addLayout(qrow)
        self.crf_slider = QSlider(Qt.Orientation.Horizontal)
        self.crf_slider.setRange(16, 28)
        self.crf_slider.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.crf_slider.valueChanged.connect(
            lambda x: self.crf_lbl.setText(f"CRF {x}"))
        b.addWidget(self.crf_slider)
        ends = QHBoxLayout()
        ends.addWidget(QLabel("Best quality", objectName="Dim"))
        ends.addStretch(1)
        ends.addWidget(QLabel("Smallest file", objectName="Dim"))
        b.addLayout(ends)
        b.addSpacing(8)

        b.addWidget(QLabel("Resolution", objectName="Field"))
        self.res_seg = Segmented(RES_LABELS)
        b.addWidget(self.res_seg)
        b.addSpacing(8)

        b.addWidget(QLabel("Audio bitrate", objectName="Field"))
        self.audio_seg = Segmented(AUDIO_LABELS)
        b.addWidget(self.audio_seg)
        b.addSpacing(6)
        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("Remove audio"))
        mrow.addStretch(1)
        self.mute_switch = Switch()
        mrow.addWidget(self.mute_switch)
        b.addLayout(mrow)
        self.mute_switch.toggled.connect(lambda on: self.audio_seg.setEnabled(not on))

        self.custom_body.setVisible(False)
        self.custom_switch.toggled.connect(self.custom_body.setVisible)
        v.addWidget(self.custom_body)
        return card

    # ----- presets
    def set_preset(self, pid: str):
        self.preset_id = pid
        for k, c in self.cards.items():
            c.set_active(k == pid)
        p = PRESETS[pid]
        self.crf_slider.setValue(p.crf)
        self.crf_lbl.setText(f"CRF {self.crf_slider.value()}")
        self.res_seg.set_index(RES_VALUES.index(p.max_height) if p.max_height in RES_VALUES else 0)
        nearest = min(range(len(AUDIO_VALUES)), key=lambda i: abs(AUDIO_VALUES[i] - p.audio_kbps))
        self.audio_seg.set_index(nearest)

    def adv_values(self):
        if not self.custom_switch.isChecked():
            return None, None, None, False
        return (self.crf_slider.value(), RES_VALUES[self.res_seg.index()],
                AUDIO_VALUES[self.audio_seg.index()], self.mute_switch.isChecked())

    # ----- selection state
    def refresh(self):
        vids = self.store.videos()
        n = len(vids)
        self.count.setVisible(n > 0)
        self.count.setText(str(n))
        self.clear_btn.setVisible(n > 0)
        sb = self.tray.verticalScrollBar()
        pos = sb.value()
        self.tray.model().set_items(vids)
        sb.setValue(pos)
        self.tray.setVisible(n > 0)
        self.hint.setVisible(n == 0)
        self.tray.setFixedHeight(min(n, 4) * TrayDelegate.ROW_H + 12)
        self.n_lbl.setText(f"{plural(n, 'video')} selected" if n else "No videos selected")
        self.size_lbl.setText(fmt_size(self.store.total_bytes()) if n else "")
        self.go_btn.setEnabled(n > 0)
        self.go_btn.setText(f"Compress {plural(n, 'video')}" if n else "Compress")


# ======================================================================
# queue page
# ======================================================================

class QueueRow(QFrame):
    def __init__(self, cache: ThumbCache, video, parent=None):
        super().__init__(parent)
        self.video = video
        self.state = "waiting"
        self.setObjectName("QueueRow")
        self.setProperty("state", "waiting")
        self.setFixedHeight(88)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 12, 16, 12)
        lay.setSpacing(14)
        thumb = QLabel()
        thumb.setFixedSize(96, 54)
        thumb.setPixmap(rounded_thumb(cache, video, 96, 54, 8))
        lay.addWidget(thumb)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.setContentsMargins(0, 0, 0, 0)
        self.name = ElideLabel(video.name)
        self.name.setObjectName("RowName")
        self.detail = ElideLabel()
        self.detail.setObjectName("RowDetail")
        self.bar = SlimProgress(4)
        col.addWidget(self.name)
        col.addWidget(self.detail)
        col.addSpacing(4)
        col.addWidget(self.bar)
        lay.addLayout(col, 1)
        self.icon = StateIcon(22)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.set_state("waiting", f"Waiting  ·  {fmt_size(video.size)}")

    def set_state(self, state: str, detail: str = "", tone: str = ""):
        self.state = state
        self.setProperty("state", state)
        repolish(self)
        self.icon.set_state(state)
        self.detail.setProperty("tone", tone)
        repolish(self.detail)
        self.detail.setText(detail)
        if state in ("waiting", "skipped", "failed"):
            self.bar.set_idle()
            self.bar.set_color(C.ACCENT)

    def set_stage(self, text: str, pct: int | None = None):
        self.state = "active"
        self.setProperty("state", "active")
        repolish(self)
        self.icon.set_state("active")
        self.bar.set_color(C.ACCENT)
        self.detail.setProperty("tone", "accent")
        repolish(self.detail)
        self.detail.setText(text if pct is None else f"{text}  {pct}%")
        self._stage = text
        if pct is None:
            self.bar.set_busy()
        else:
            self.bar.set_value(pct)

    def set_progress(self, pct: int):
        self.bar.set_value(pct)
        self.detail.setText(f"{getattr(self, '_stage', 'Compressing')}  {pct}%")

    def set_done(self, before: int, after: int, dest: str):
        saved = (1 - after / before) * 100 if before else 0
        self.set_state("done", f"{fmt_size(before)} → {fmt_size(after)}   {saved:.0f}% smaller", "ok")
        self.bar.set_color(C.OK)
        self.bar.set_value(100)
        if dest:
            self.setToolTip(f"Saved to phone as {base_name(dest)}\n{dest}")

    def set_failed(self, msg: str):
        first = (msg or "Failed").strip().splitlines()[0]
        self.set_state("failed", first, "err")
        self.setToolTip(msg)


class QueuePage(QWidget):
    cancelRequested = pyqtSignal()
    backRequested = pyqtSignal()

    def __init__(self, cache: ThumbCache, parent=None):
        super().__init__(parent)
        self.cache = cache
        self.rows: list[QueueRow] = []

        outer = QHBoxLayout(self)
        outer.setContentsMargins(24, 0, 24, 0)
        outer.addStretch(1)
        wrap = QWidget()
        wrap.setMaximumWidth(920)
        wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        outer.addWidget(wrap, 100)
        outer.addStretch(1)

        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 36, 0, 24)
        v.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(16)
        col = QVBoxLayout()
        col.setSpacing(6)
        self.title = QLabel("", objectName="H1")
        self.subtitle = QLabel("", objectName="Muted")
        self.subtitle.setWordWrap(True)
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        head.addLayout(col, 1)
        self.cancel_btn = make_button("Cancel", "danger")
        self.cancel_btn.clicked.connect(self.cancelRequested)
        self.back_btn = make_button("Back to library", "primary", "chevron-left")
        self.back_btn.clicked.connect(self.backRequested)
        head.addWidget(self.cancel_btn, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.back_btn, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(head)
        v.addSpacing(20)
        self.overall = SlimProgress(6)
        v.addWidget(self.overall)
        v.addSpacing(20)

        host = QWidget()
        self.list_l = QVBoxLayout(host)
        self.list_l.setContentsMargins(0, 0, 4, 0)
        self.list_l.setSpacing(10)
        self.list_l.addStretch(1)
        self.scroll = make_scroll(host)
        v.addWidget(self.scroll, 1)
        v.addSpacing(12)

        self.details_btn = make_button("Show details", "ghost", "chevron-right")
        self.details_btn.clicked.connect(self._toggle_log)
        v.addWidget(self.details_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.log_view = QPlainTextEdit(objectName="Log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(4000)
        self.log_view.setFixedHeight(170)
        self.log_view.hide()
        v.addSpacing(6)
        v.addWidget(self.log_view)

    def _toggle_log(self):
        show = not self.log_view.isVisible()
        self.log_view.setVisible(show)
        self.details_btn.setText("Hide details" if show else "Show details")
        self.details_btn.setIcon(icon("chevron-down" if show else "chevron-right", C.MUTED, 16))

    def append_log(self, msg: str):
        self.log_view.appendPlainText(str(msg))

    def begin(self, videos):
        for r in self.rows:
            self.list_l.removeWidget(r)
            r.deleteLater()
        self.rows = []
        for i, v in enumerate(videos):
            row = QueueRow(self.cache, v)
            self.list_l.insertWidget(i, row)
            self.rows.append(row)
        self.log_view.clear()
        self.overall.set_color(C.ACCENT)
        self.overall.set_value(0)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")
        self.cancel_btn.show()
        self.back_btn.hide()
        n = len(videos)
        self.title.setText(f"Compressing {plural(n, 'video')}")
        self.subtitle.setText("Each video is copied to this PC, compressed, then copied back to your "
                              "phone. Keep the phone connected.")

    def activate(self, i: int, total: int):
        self.title.setText("Compressing video" if total == 1 else f"Compressing {i + 1} of {total}")
        row = self.rows[i]
        row.set_stage("Starting", None)
        self.scroll.ensureWidgetVisible(row, 0, 40)

    def set_overall(self, frac: float):
        self.overall.set_value(frac * 100)

    def finish(self, stats: dict, total: int):
        ok, fail = stats["ok"], stats["fail"]
        before, after = stats["before"], stats["after"]
        self.cancel_btn.hide()
        self.back_btn.show()
        if stats["cancelled"]:
            self.title.setText("Cancelled")
            msg = (f"{ok} of {total} finished before you cancelled. "
                   "Temporary files on this PC were removed.")
            self.overall.set_color(C.WARN)
        elif fail == 0:
            self.title.setText(f"{plural(ok, 'video')} compressed")
            msg = ""
            self.overall.set_color(C.OK)
        elif ok == 0:
            self.title.setText("Compression failed")
            msg = "None of the videos could be compressed."
            self.overall.set_color(C.ERR)
        else:
            self.title.setText(f"{ok} of {total} videos compressed")
            msg = ""
            self.overall.set_color(C.WARN)
        if ok and not stats["cancelled"]:
            pct = (1 - after / before) * 100 if before else 0
            msg = (f"{fmt_size(before)} → {fmt_size(after)}, {pct:.0f}% smaller. "
                   "Temporary files on this PC were deleted.")
        if fail and not stats["cancelled"]:
            msg = (msg + " " if msg else "") + f"{plural(fail, 'video')} failed — see Show details or the log folder."
        self.subtitle.setText(msg)
        self.overall.set_value(100)
        self.scroll.verticalScrollBar().setValue(0)


# ======================================================================
# settings dialog
# ======================================================================

class SettingRow(QWidget):
    def __init__(self, title: str, desc: str, control: QWidget, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 16, 14)
        lay.setSpacing(14)
        col = QVBoxLayout()
        col.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet("font-weight:600;")
        self.desc = ElideLabel(desc, mode=Qt.TextElideMode.ElideMiddle)
        self.desc.setObjectName("Dim")
        self.desc.setToolTip(desc)
        col.addWidget(t)
        col.addWidget(self.desc)
        lay.addLayout(col, 1)
        lay.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_desc(self, text: str):
        self.desc.setText(text)
        self.desc.setToolTip(text)


class SettingsDialog(QDialog):
    def __init__(self, cache: ThumbCache, tools: dict, parent=None):
        super().__init__(parent)
        self.cache = cache
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setFixedWidth(500)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 20)
        v.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(LogoMark(38))
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(QLabel("Shrinkit", objectName="H2"))
        col.addWidget(QLabel(f"Version {__version__}", objectName="Muted"))
        head.addLayout(col)
        head.addStretch(1)
        v.addLayout(head)
        v.addSpacing(18)

        card = QFrame(objectName="SettingsCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        clear_btn = make_button("Clear cache")
        clear_btn.clicked.connect(self._clear)
        self.cache_row = SettingRow("Preview cache",
                                    f"{fmt_size(cache.disk_usage())} stored on this PC", clear_btn)
        open_btn = make_button("Open folder")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(LOG_DIR)))
        rows = [self.cache_row, SettingRow("Log files", LOG_DIR, open_btn)]
        for name in ("ADB", "FFmpeg"):
            ok, msg = tools.get(name, (False, "Not checked"))
            ic = StateIcon(22)
            ic.set_state("done" if ok else "failed")
            rows.append(SettingRow(name, msg or ("Ready" if ok else "Not found"), ic))
        for i, r in enumerate(rows):
            if i:
                cl.addWidget(QFrame(objectName="Sep"))
            cl.addWidget(r)
        v.addWidget(card)
        v.addSpacing(18)

        close = make_button("Done", "primary")
        close.setMinimumWidth(96)
        close.clicked.connect(self.accept)
        v.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

    def _clear(self):
        self.cache.clear()
        self.cache_row.set_desc(f"{fmt_size(0)} stored on this PC")


# ======================================================================
# main window
# ======================================================================

SORTS = [
    ("Newest first", lambda v: v.modified or 0, True),
    ("Oldest first", lambda v: v.modified or 0, False),
    ("Largest first", lambda v: v.size or 0, True),
    ("Longest first", lambda v: v.duration_ms or 0, True),
    ("Name A–Z", lambda v: v.name.lower(), False),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Shrinkit")
        self.resize(1340, 840)
        self.setMinimumSize(1180, 680)

        self.videos: list[adb.Video] = []
        self.folders: list[adb.Folder] = []
        self._folders_by_name: dict[str, adb.Folder] = {}
        self.visible: list[adb.Video] = []
        self.scope: str | None = None
        self.sort_idx = 0
        self.lib_state = "loading"          # loading | ready | error | setup
        self.error_msg = ""
        self.deps_ok = True
        self.tools: dict = {}
        self.folder_items: list[NavItem] = []
        self._empty_kind = ""

        self.store = SelectionStore(self)
        self.cache = ThumbCache()
        self.thumb_states: dict[str, str] = {}
        self.loader: VideoLoadWorker | None = None

        # queue state
        self.pipe: PipelineWorker | None = None
        self.qvideos: list[adb.Video] = []
        self.qi = -1
        self.stage = ""
        self.pct = 0
        self.cancel_requested = False
        self.run_cfg = None
        self.qstats: dict = {}

        self.thumbs = ThumbnailWorker(self.cache, self)
        self.thumbs.started_job.connect(self._on_thumb_started)
        self.thumbs.ready.connect(self._on_thumb_ready)
        self.thumbs.failed.connect(self._on_thumb_failed)
        self.thumbs.start()
        self.ticker = QTimer(self)
        self.ticker.setInterval(60)
        self.ticker.timeout.connect(self._tick)

        self._build()
        self._shortcuts()
        self.store.changed.connect(self._on_selection_changed)
        self._check_deps()
        self.load_videos()

    # ------------------------------------------------------------ layout
    def _build(self):
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._build_sidebar())

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)
        self.banner = Banner()
        self.banner.actionClicked.connect(self._recheck)
        self.banner_wrap = QWidget()
        bw = QVBoxLayout(self.banner_wrap)
        bw.setContentsMargins(24, 16, 24, 0)
        bw.addWidget(self.banner)
        self.banner_wrap.hide()
        rv.addWidget(self.banner_wrap)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_library())
        self.queue = QueuePage(self.cache)
        self.queue.cancelRequested.connect(self._cancel)
        self.queue.backRequested.connect(self._leave_queue)
        self.pages.addWidget(self.queue)
        rv.addWidget(self.pages, 1)
        h.addWidget(right, 1)
        self.setCentralWidget(root)

    def _build_sidebar(self) -> QFrame:
        sb = QFrame(objectName="Sidebar")
        sb.setFixedWidth(248)
        v = QVBoxLayout(sb)
        v.setContentsMargins(16, 20, 16, 16)
        v.setSpacing(0)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        brand.setContentsMargins(4, 0, 0, 0)
        brand.addWidget(LogoMark(30))
        brand.addWidget(QLabel("Shrinkit", objectName="Brand"))
        brand.addStretch(1)
        v.addLayout(brand)
        v.addSpacing(20)

        self.device_card = DeviceCard()
        self.device_card.refreshClicked.connect(self.load_videos)
        v.addWidget(self.device_card)
        v.addSpacing(22)

        v.addWidget(QLabel("Library", objectName="Section"))
        v.addSpacing(6)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_all = NavItem("grid", "All videos", 0, key=None)
        self.nav_group.addButton(self.nav_all)
        self.nav_all.setChecked(True)
        v.addWidget(self.nav_all)
        v.addSpacing(16)
        v.addWidget(QLabel("Folders", objectName="Section"))
        v.addSpacing(6)

        host = QWidget()
        self.folder_layout = QVBoxLayout(host)
        self.folder_layout.setContentsMargins(0, 0, 0, 0)
        self.folder_layout.setSpacing(2)
        self.folder_layout.addStretch(1)
        v.addWidget(make_scroll(host), 1)
        self.nav_group.buttonClicked.connect(self._on_nav)

        v.addSpacing(10)
        self.settings_btn = make_button("Settings", "ghost", "sliders")
        self.settings_btn.setObjectName("SettingsBtn")
        self.settings_btn.clicked.connect(self._open_settings)
        v.addWidget(self.settings_btn)
        self.sidebar = sb
        return sb

    def _build_library(self) -> QWidget:
        page = QWidget()
        h = QHBoxLayout(page)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(28, 26, 28, 0)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.title = QLabel("All videos", objectName="H1")
        self.subtitle = QLabel("", objectName="Muted")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        self.select_btn = make_button("Select all", "ghost")
        self.select_btn.clicked.connect(self._toggle_select_all)
        head.addWidget(self.select_btn, 0, Qt.AlignmentFlag.AlignBottom)
        cv.addLayout(head)

        bar = QHBoxLayout()
        bar.setContentsMargins(28, 18, 28, 14)
        bar.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search by name or folder")
        self.search.setMinimumWidth(170)
        self.search.setMaximumWidth(340)
        self.search.addAction(icon("search", C.DIM, 16), QLineEdit.ActionPosition.LeadingPosition)
        self._clear_action = self.search.addAction(icon("x", C.MUTED, 16),
                                                   QLineEdit.ActionPosition.TrailingPosition)
        self._clear_action.setVisible(False)
        self._clear_action.triggered.connect(self.search.clear)
        self.search.textChanged.connect(self._on_search)
        self.sort_btn = make_button(SORTS[0][0], "", "chevron-down")
        self.sort_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.sort_btn.clicked.connect(self._open_sort_menu)
        self.preview_btn = make_button("Load previews", "", "image")
        self.preview_btn.clicked.connect(self._load_previews)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.sort_btn)
        bar.addWidget(self.preview_btn)
        bar.addStretch(2)
        cv.addLayout(bar)

        self.lib_stack = QStackedWidget()
        gridwrap = QWidget()
        gl = QHBoxLayout(gridwrap)
        gl.setContentsMargins(22, 0, 6, 0)
        self.model = VideoModel(self)
        self.grid = VideoGrid(self.store, self.cache, self.thumb_states)
        self.grid.setModel(self.model)
        self.grid.previewRequested.connect(self._request_thumb)
        gl.addWidget(self.grid)
        self.lib_stack.addWidget(gridwrap)
        self.empty = EmptyState()
        self.empty.actionClicked.connect(self._on_empty_action)
        self.lib_stack.addWidget(self.empty)
        cv.addWidget(self.lib_stack, 1)

        h.addWidget(content, 1)
        self.inspector = Inspector(self.store, self.cache)
        self.inspector.startRequested.connect(self._start_queue)
        h.addWidget(self.inspector)
        return page

    def _shortcuts(self):
        def sc(seq, fn, widget=None):
            s = QShortcut(QKeySequence(seq), widget or self)
            if widget is not None:
                s.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            s.activated.connect(fn)
            return s
        sc("Ctrl+F", lambda: (self.search.setFocus(), self.search.selectAll()))
        sc("F5", self.load_videos)
        sc("Ctrl+A", self._select_all_visible, self.grid)
        sc("Escape", self.store.clear, self.grid)

    # ------------------------------------------------------------ deps / loading
    def _check_deps(self):
        ok_adb, adb_msg = adb.check_adb()
        ok_ff, ff_msg = ffmpeg.check_ffmpeg()
        self.tools = {"ADB": (ok_adb, adb_msg), "FFmpeg": (ok_ff, ff_msg)}
        self.deps_ok = ok_adb and ok_ff
        problems = [f"{n}: {m}" for n, (ok, m) in self.tools.items() if not ok]
        if problems:
            self.banner.set_text(" · ".join(problems) +
                                 " — install the missing tool and add it to PATH.")
            self.banner_wrap.show()
        else:
            self.banner_wrap.hide()

    def _recheck(self):
        self._check_deps()
        if self.deps_ok:
            self.load_videos()

    def load_videos(self):
        if self.pipe is not None or (self.loader is not None and self.loader.isRunning()):
            return
        if not self.deps_ok:
            self._check_deps()
            if not self.deps_ok:
                self.lib_state = "setup"
                self.device_card.set_state("offline", "Setup needed", "ADB or FFmpeg is missing")
                self._refresh_grid()
                return
        self.device_card.set_state("active", "Looking for phone", "Checking USB connection")
        if not self.videos:
            self.lib_state = "loading"
            self._refresh_grid()
        self.loader = VideoLoadWorker(self)
        self.loader.loaded.connect(self._on_videos)
        self.loader.failed.connect(self._on_load_fail)
        self.loader.start()

    def _on_videos(self, videos):
        self.videos = list(videos)
        self.folders = adb.group_by_folder(self.videos)
        self._folders_by_name = {f.name: f for f in self.folders}
        self.store.reconcile(self.videos)
        self.lib_state = "ready"
        total = sum((v.size or 0) for v in self.videos)
        self.device_card.set_state("online", "Phone connected",
                                   f"{plural(len(self.videos), 'video')}, {fmt_size(total)}")
        log.info("loaded %d videos in %d folders", len(self.videos), len(self.folders))
        self._rebuild_nav()
        self._refresh_grid()

    def _on_load_fail(self, msg):
        log.warning("video load failed: %s", msg)
        self.lib_state = "error"
        self.error_msg = str(msg)
        self.videos, self.folders, self._folders_by_name = [], [], {}
        self.store.reconcile([])
        self.device_card.set_state("offline", "No phone found", "Connect it with USB", tip=str(msg))
        self._rebuild_nav()
        self._refresh_grid()

    # ------------------------------------------------------------ sidebar nav
    def _rebuild_nav(self):
        for it in self.folder_items:
            self.nav_group.removeButton(it)
            self.folder_layout.removeWidget(it)
            it.deleteLater()
        self.folder_items = []
        for f in self.folders:
            it = NavItem("folder", f.name, f.count, key=f.name)
            self.nav_group.addButton(it)
            self.folder_layout.insertWidget(self.folder_layout.count() - 1, it)
            self.folder_items.append(it)
        self.nav_all.set_count(len(self.videos))
        if self.scope is not None and self.scope not in self._folders_by_name:
            self.scope = None
        target = self.nav_all
        for it in self.folder_items:
            if it.key == self.scope:
                target = it
        target.setChecked(True)

    def _on_nav(self, btn):
        self.scope = btn.key
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._clear_action.setVisible(False)
        self._refresh_grid()
        self.grid.verticalScrollBar().setValue(0)

    # ------------------------------------------------------------ library view
    def _query(self) -> str:
        return self.search.text().strip()

    def _on_search(self, text):
        self._clear_action.setVisible(bool(text))
        self._refresh_grid()
        self.grid.verticalScrollBar().setValue(0)

    def _scope_videos(self) -> list:
        if self.scope is None:
            return self.videos
        f = self._folders_by_name.get(self.scope)
        return f.videos if f else []

    def _compute_visible(self) -> list:
        base = self._scope_videos()
        q = self._query().lower()
        if q:
            base = [v for v in base if q in v.name.lower() or q in v.folder.lower()]
        _label, key, rev = SORTS[self.sort_idx]
        try:
            return sorted(base, key=key, reverse=rev)
        except TypeError:
            return list(base)

    def _refresh_grid(self):
        items = self._compute_visible() if self.lib_state == "ready" else []
        self.visible = items
        self.grid.card_delegate.show_folder = self.scope is None
        self.grid.set_items(items)

        q = self._query()
        scope_total = len(self._scope_videos())
        if q:
            self.title.setText("Search results")
            self.subtitle.setText(f"{len(items)} of {plural(scope_total, 'video')} match “{q}”")
        else:
            self.title.setText(self.scope or "All videos")
            size = sum((v.size or 0) for v in items)
            self.subtitle.setText(f"{plural(len(items), 'video')}, {fmt_size(size)}"
                                  if self.lib_state == "ready" else "")

        st = self.lib_state
        if st == "loading":
            self._show_empty("loading", "film", "Looking for your phone",
                             "Make sure it is unlocked and connected by USB.", None, busy=True)
        elif st == "setup":
            self._show_empty("setup", "alert", "Setup needed",
                             "Shrinkit needs ADB and FFmpeg. Install them, add both to PATH, "
                             "then check again.", "Check again", color=C.WARN)
        elif st == "error":
            self._show_empty("error", "phone", "No phone found",
                             (self.error_msg or "Connect your phone with USB and allow USB debugging."),
                             "Try again", color=C.ERR)
        elif not self.videos:
            self._show_empty("empty", "film", "No videos on this phone",
                             "Record or copy a video to your phone, then refresh.", "Refresh")
        elif not items:
            if q:
                self._show_empty("nomatch", "search", "No matches",
                                 f"Nothing matches “{q}”. Try a different name or folder.",
                                 "Clear search")
            else:
                self._show_empty("nomatch_folder", "folder", "This folder is empty", "", None)
        else:
            self.lib_stack.setCurrentIndex(0)
        self._sync_toolbar()

    def _show_empty(self, kind, glyph, title, text, action, busy=False, color=C.MUTED):
        self._empty_kind = kind
        self.empty.show_state(glyph, title, text, action, busy, color)
        self.lib_stack.setCurrentIndex(1)

    def _on_empty_action(self):
        k = self._empty_kind
        if k == "setup":
            self._recheck()
        elif k in ("error", "empty"):
            self.load_videos()
        elif k == "nomatch":
            self.search.clear()

    def _sync_toolbar(self):
        has = bool(self.visible)
        self.sort_btn.setEnabled(self.lib_state == "ready")
        self.search.setEnabled(self.lib_state == "ready" or bool(self._query()))
        self._sync_select_button()
        self._refresh_thumb_button()
        self.sort_btn.setText(SORTS[self.sort_idx][0])
        self.select_btn.setVisible(has)
        self.preview_btn.setVisible(has)

    def _open_sort_menu(self):
        menu = QMenu(self)
        menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.NoDropShadowWindowHint)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        for i, (label, _k, _r) in enumerate(SORTS):
            act = menu.addAction(icon("check", C.ACCENT, 16) if i == self.sort_idx
                                 else blank_icon(16), label)
            act.triggered.connect(lambda _=False, i=i: self._set_sort(i))
        menu.exec(self.sort_btn.mapToGlobal(QPoint(0, self.sort_btn.height() + 6)))

    def _set_sort(self, i: int):
        self.sort_idx = i
        self._refresh_grid()
        self.grid.verticalScrollBar().setValue(0)

    # ------------------------------------------------------------ selection
    def _select_all_visible(self):
        self.store.set_many(self.visible, True)

    def _toggle_select_all(self):
        if not self.visible:
            return
        all_on = all(v in self.store for v in self.visible)
        self.store.set_many(self.visible, not all_on)

    def _sync_select_button(self):
        if not self.visible:
            return
        all_on = all(v in self.store for v in self.visible)
        self.select_btn.setText("Deselect all" if all_on else f"Select all {len(self.visible)}")

    def _on_selection_changed(self):
        self.grid.viewport().update()
        self.inspector.refresh()
        self._sync_select_button()

    # ------------------------------------------------------------ thumbnails
    def _repaint(self):
        self.grid.viewport().update()
        self.inspector.tray.viewport().update()

    def _ensure_ticker(self):
        if any(s == "loading" for s in self.thumb_states.values()) and not self.ticker.isActive():
            self.ticker.start()

    def _tick(self):
        if not any(s == "loading" for s in self.thumb_states.values()):
            self.ticker.stop()
            return
        self.grid.card_delegate.phase += 1
        self.grid.viewport().update()

    def _request_thumb(self, video):
        if self.cache.has(video.thumb_key):
            self._repaint()
            return
        self.thumb_states[video.thumb_key] = "loading"
        n = self.thumbs.enqueue([video])
        log.info("thumb requested: %s (queued=%d)", video.name, n)
        self._ensure_ticker()
        self._repaint()
        self._refresh_thumb_button()

    def _load_previews(self):
        missing = [v for v in self.visible
                   if not self.cache.has(v.thumb_key)
                   and self.thumb_states.get(v.thumb_key) != "loading"][:PREVIEW_BATCH]
        if not missing:
            return
        for v in missing:
            self.thumb_states[v.thumb_key] = "loading"
        n = self.thumbs.enqueue(missing)
        log.info("bulk previews: %d queued", n)
        self._ensure_ticker()
        self._repaint()
        self._refresh_thumb_button()

    def _on_thumb_started(self, key: str):
        self.thumb_states[key] = "loading"
        self._ensure_ticker()
        self._repaint()

    def _on_thumb_ready(self, key: str):
        self.thumb_states.pop(key, None)
        self._repaint()
        self._refresh_thumb_button()

    def _on_thumb_failed(self, key: str, msg: str):
        self.thumb_states[key] = "failed"
        log.warning("thumb failed %s: %s", key[:8], msg)
        self._repaint()
        self._refresh_thumb_button()

    def _refresh_thumb_button(self):
        b = self.preview_btn
        if not self.visible:
            b.setEnabled(False)
            return
        pending = self.thumbs.pending_count()
        loading = any(s == "loading" for s in self.thumb_states.values())
        missing = sum(1 for v in self.visible if not self.cache.has(v.thumb_key))
        if pending or (missing and loading):
            b.setText(f"Loading previews ({pending})" if pending else "Loading previews")
            b.setEnabled(False)
        elif missing:
            b.setText(f"Load previews ({min(missing, PREVIEW_BATCH)})")
            b.setEnabled(True)
        else:
            b.setText("Previews loaded")
            b.setEnabled(False)

    # ------------------------------------------------------------ settings
    def _open_settings(self):
        SettingsDialog(self.cache, self.tools, self).exec()

    # ------------------------------------------------------------ batch queue
    def _start_queue(self):
        vids = self.store.videos()
        if not vids or self.pipe is not None:
            return
        preset = PRESETS[self.inspector.preset_id]
        crf, maxh, ab, mute = self.inspector.adv_values()
        self.run_cfg = (preset, crf, maxh, ab, mute)
        self.qvideos = vids
        self.qi = -1
        self.cancel_requested = False
        self.qstats = {"ok": 0, "fail": 0, "before": 0, "after": 0,
                       "cancelled": False, "done": []}
        log.info("batch start: %d videos preset=%s", len(vids), self.inspector.preset_id)
        self.queue.begin(vids)
        self.sidebar.setEnabled(False)
        self.pages.setCurrentIndex(PAGE_QUEUE)
        self._run_next()

    def _run_next(self):
        self.qi += 1
        if self.cancel_requested or self.qi >= len(self.qvideos):
            self._finish_queue()
            return
        v = self.qvideos[self.qi]
        self.stage, self.pct = "", 0
        self.queue.activate(self.qi, len(self.qvideos))
        self._update_overall()
        preset, crf, maxh, ab, mute = self.run_cfg
        log.info("compress start: %s", v.phone_path)
        self.queue.append_log(f"— {v.name}")
        pipe = PipelineWorker(v, preset, crf, maxh, ab, mute, self)
        pipe.step.connect(self._on_step)
        pipe.compress_pct.connect(self._on_pct)
        pipe.log.connect(self.queue.append_log)
        pipe.finished.connect(self._on_item_done)
        pipe.failed.connect(self._on_item_failed)
        self.pipe = pipe
        pipe.start()

    def _row(self):
        return self.queue.rows[self.qi] if 0 <= self.qi < len(self.queue.rows) else None

    def _on_step(self, s):
        row = self._row()
        if row is None:
            return
        if s == "pull":
            self.stage = "pull"
            row.set_stage("Copying from your phone")
        elif s == "compress":
            self.stage, self.pct = "compress", 0
            row.set_stage("Compressing", 0)
        elif s == "push":
            self.stage = "push"
            row.set_stage("Copying back to your phone")
        self._update_overall()

    def _on_pct(self, pct):
        row = self._row()
        if row is None:
            return
        self.pct = int(pct)
        row.set_progress(self.pct)
        self._update_overall()

    def _update_overall(self):
        frac = {"pull": 0.03, "compress": 0.08 + 0.84 * self.pct / 100.0,
                "push": 0.95}.get(self.stage, 0.0)
        self.queue.set_overall((self.qi + frac) / max(1, len(self.qvideos)))

    def _on_item_done(self, res):
        row = self._row()
        before, after = res.get("in_bytes", 0), res.get("out_bytes", 0)
        if row is not None:
            row.set_done(before, after, res.get("dest", ""))
        st = self.qstats
        st["ok"] += 1
        st["before"] += before
        st["after"] += after
        st["done"].append(self.qvideos[self.qi])
        log.info("compress done: %s %d -> %d", res.get("dest"), before, after)
        self._advance()

    def _on_item_failed(self, msg):
        log.warning("compress failed: %s", msg)
        row = self._row()
        if self.cancel_requested or "Cancelled" in str(msg):
            self.cancel_requested = True
            self.qstats["cancelled"] = True
            if row is not None:
                row.set_state("skipped", "Cancelled")
        else:
            self.qstats["fail"] += 1
            if row is not None:
                row.set_failed(str(msg))
            self.queue.append_log(f"Failed: {msg}")
        self._advance()

    def _advance(self):
        QTimer.singleShot(0, self._run_next)

    def _finish_queue(self):
        self.pipe = None
        if self.cancel_requested:
            self.qstats["cancelled"] = True
            for row in self.queue.rows:
                if row.state == "waiting":
                    row.set_state("skipped", "Skipped")
        self.queue.finish(self.qstats, len(self.qvideos))

    def _cancel(self):
        self.cancel_requested = True
        self.queue.cancel_btn.setEnabled(False)
        self.queue.cancel_btn.setText("Cancelling…")
        if self.pipe is not None:
            self.pipe.cancel()

    def _leave_queue(self):
        done = self.qstats.get("done", [])
        self.sidebar.setEnabled(True)
        self.pages.setCurrentIndex(PAGE_LIBRARY)
        if done:
            self.store.set_many(done, False)
            self.load_videos()

    # ------------------------------------------------------------ shutdown
    def closeEvent(self, event):  # noqa: N802
        try:
            self.cancel_requested = True
            if self.pipe:
                self.pipe.cancel()
                self.pipe.wait(2000)
            if self.loader and self.loader.isRunning():
                self.loader.wait(2000)
            self.thumbs.clear_pending()
            self.thumbs.stop()
            self.ticker.stop()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Shrinkit")
    apply_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()