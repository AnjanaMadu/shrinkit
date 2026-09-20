"""Shrinkit — browse phone videos by folder, compress via ffmpeg, push back."""

from __future__ import annotations

import datetime
import logging
import os
import sys

from PyQt6.QtCore import (
    QAbstractListModel, QEvent, QModelIndex, QPointF, QRect, QRectF, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDesktopServices, QFont, QFontMetrics, QPainter,
    QPainterPath, QPen, QPixmap, QMouseEvent,
)
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListView, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QRadioButton, QSlider, QStackedWidget,
    QStyle, QStyledItemDelegate, QTextEdit, QVBoxLayout, QWidget,
)

import adb
import ffmpeg
from cache import ThumbCache
from presets import DEFAULT_PRESET_ID, PRESETS
from thumbs import ThumbnailWorker
from workers import PipelineWorker, VideoLoadWorker

__version__ = "0.2.0"

# pages
P_HOME, P_FOLDER, P_PRESET, P_WORK, P_DONE = range(5)

ROLE_ITEM = Qt.ItemDataRole.UserRole + 1


# ---------- logging ----------

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


# ---------- helpers ----------

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


def fmt_date(ts: int) -> str:
    if not ts:
        return "—"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "—"


def cover_pixmap(pm: QPixmap, w: int, h: int) -> QPixmap:
    """Center-crop pixmap to exactly w×h."""
    if pm.isNull():
        return pm
    scaled = pm.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                       Qt.TransformationMode.SmoothTransformation)
    x = max(0, (scaled.width() - w) // 2)
    y = max(0, (scaled.height() - h) // 2)
    return scaled.copy(x, y, w, h)


# ---------- stylesheet (pure QSS, no external images) ----------
# Checked states use solid colors only (no url(image) references), so the
# app works from source, from a PyInstaller exe, read-only / locked-down
# temp dirs, and on any OS without writing PNGs at runtime.

def build_stylesheet() -> str:
    return STYLESHEET


STYLESHEET = """
* { font-size: 13px; }
QMainWindow, QWidget { background: #131316; color: #e8e8ea; }
#Header { background: #1a1a1f; border-bottom: 1px solid #2a2a31; }
#Header QLabel { background: transparent; }
#Title { font-size: 16px; font-weight: 700; color: #ffffff; background: transparent; }
#Subtitle { color: #8e8e96; background: transparent; }
#Muted { color: #8e8e96; background: transparent; }
#Crumb { font-size: 14px; font-weight: 600; color: #ffffff; }
QPushButton { background: #26262c; color: #e8e8ea; border: 1px solid #3a3a42;
              border-radius: 8px; padding: 8px 14px; }
QPushButton:hover { background: #2e2e35; }
QPushButton[primary="true"] { background: #f2f2f4; color: #131316; border: none;
                              border-radius: 8px; padding: 8px 18px; font-weight: 600; }
QPushButton[primary="true"]:hover { background: #ffffff; }
QPushButton[primary="true"]:disabled { background: #3a3a42; color: #77777f; }
QPushButton:disabled { background: #232328; color: #66666e; border-color: #2c2c32; }
QLineEdit { padding: 7px 10px; border: 1px solid #34343c; border-radius: 8px;
            background: #1e1e24; color: #e8e8ea; selection-background-color: #4a4a55; }
QComboBox { padding: 7px 10px; border: 1px solid #34343c; border-radius: 8px;
            background: #1e1e24; color: #e8e8ea; }
QComboBox:hover, QComboBox:focus { border-color: #4a4a55; }
QComboBox::drop-down { border: none; background: transparent; width: 24px; }
QComboBox QAbstractItemView { background: #1e1e24; color: #e8e8ea;
                             selection-background-color: #4ade80; selection-color: #131316;
                             border: 1px solid #34343c; outline: 0; }
QComboBox QAbstractItemView::item { padding: 6px 10px; background: #1e1e24; color: #e8e8ea; }
QComboBox QAbstractItemView::item:selected { background: #4ade80; color: #131316; }
QComboBox QAbstractItemView::item:hover:!selected { background: #2a2a31; color: #ffffff; }
QListView { background: transparent; border: none; outline: none; }
QListView::item { background: transparent; border: none; }
QListView::item:selected { background: transparent; }
QRadioButton, QCheckBox { color: #e8e8ea; spacing: 8px; background: transparent; }
QRadioButton::indicator { width: 16px; height: 16px; border-radius: 8px;
                         border: 1px solid #55555f; background: #1e1e24; }
QRadioButton::indicator:hover { border-color: #4ade80; }
QRadioButton::indicator:checked { background: #4ade80; border: 2px solid #1e1e24;
                                outline: 1px solid #4ade80; }
QRadioButton::indicator:checked:hover { background: #4ade80; border: 2px solid #1e1e24;
                                      outline: 1px solid #4ade80; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
                      border: 1px solid #55555f; background: #1e1e24; }
QCheckBox::indicator:hover { border-color: #4ade80; }
QCheckBox::indicator:checked { background: #4ade80; border: 1px solid #4ade80; }
QCheckBox::indicator:checked:hover { background: #4ade80; border: 1px solid #4ade80; }
QProgressBar { background: #232329; border: 1px solid #2e2e35; border-radius: 6px;
               text-align: center; color: #b9b9c1; height: 18px; }
QProgressBar::chunk { background: #4ade80; border-radius: 5px; }
QTextEdit { background: #0c0c0f; color: #c9c9d1; border: 1px solid #26262c; border-radius: 8px;
            font-family: Consolas, monospace; font-size: 12px; }
QSlider::groove:horizontal { background: #2c2c33; height: 6px; border-radius: 3px; }
QSlider::handle:horizontal { background: #f2f2f4; width: 14px; height: 14px;
                             margin: -4px 0; border-radius: 7px; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #34343c; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #26262c; color: #e8e8ea; border: 1px solid #3a3a42; }
"""


# ---------- models ----------

class FolderListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.folders: list[adb.Folder] = []

    def set_folders(self, folders: list[adb.Folder]):
        self.beginResetModel()
        self.folders = list(folders)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.folders)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.folders):
            return None
        f = self.folders[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return f.name
        if role == ROLE_ITEM:
            return f
        return None


class VideoListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.videos: list[adb.Video] = []
        self.sort_key = "modified"
        self.reverse = True

    def set_videos(self, videos: list[adb.Video]):
        self.beginResetModel()
        self.videos = list(videos)
        self._apply_sort()
        self.endResetModel()

    def _apply_sort(self):
        key = {
            "name": lambda v: v.name.lower(),
            "size": lambda v: v.size,
            "duration": lambda v: v.duration_ms,
            "modified": lambda v: v.modified,
        }.get(self.sort_key, lambda v: v.modified)
        try:
            self.videos.sort(key=key, reverse=self.reverse)
        except TypeError:
            pass

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.videos)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.videos):
            return None
        v = self.videos[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return v.name
        if role == ROLE_ITEM:
            return v
        return None


# ---------- painting shared ----------

TILE_W, TILE_H = 216, 208
THUMB_W, THUMB_H = 192, 108
RADIUS = 10


def _rounded_clip(p: QPainter, rect: QRect, r: int) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), r, r)
    return path


def _draw_refresh_glyph(p: QPainter, center, r: int, color: QColor):
    """Circular-arrow glyph (no font dependency)."""
    p.save()
    pen = QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRect(center.x() - r, center.y() - r, r * 2, r * 2)
    p.drawArc(rect, 40 * 16, 290 * 16)
    import math
    ang = math.radians(40)
    tip = QPointF(center.x() + r * math.cos(ang), center.y() - r * math.sin(ang))
    d = r * 0.45
    a1 = ang + math.radians(35) + math.pi
    a2 = ang - math.radians(35) + math.pi
    p.drawLine(tip, tip + QPointF(d * math.cos(a1), -d * math.sin(a1)))
    p.drawLine(tip, tip + QPointF(d * math.cos(a2), -d * math.sin(a2)))
    p.restore()


def _draw_folder_glyph(p: QPainter, rect: QRect):
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#33333c")))
    body = QRect(rect.x(), rect.y() + 8, rect.width(), rect.height() - 8)
    path = QPainterPath()
    path.addRoundedRect(QRectF(body), 6, 6)
    p.drawPath(path)
    tab = QRect(rect.x(), rect.y(), rect.width() // 2, 14)
    tpath = QPainterPath()
    tpath.addRoundedRect(QRectF(tab), 5, 5)
    p.drawPath(tpath)
    p.setBrush(QBrush(QColor("#4ade80")))
    dot = QRect(rect.x() + rect.width() - 16, rect.y() + rect.height() - 16, 8, 8)
    p.setClipPath(path)
    p.drawEllipse(dot)
    p.restore()


# ---------- delegates ----------

class FolderDelegate(QStyledItemDelegate):
    folderOpened = pyqtSignal(object)

    def __init__(self, cache: ThumbCache, parent=None):
        super().__init__(parent)
        self.cache = cache

    def sizeHint(self, option, index):
        return QSize(TILE_W, 196)

    def paint(self, painter, option, index):
        f: adb.Folder | None = index.data(ROLE_ITEM)
        if f is None:
            return
        p = painter
        p.save()
        p.setRenderHints(QPainter.RenderHint.Antialiasing |
                         QPainter.RenderHint.SmoothPixmapTransform)
        card = option.rect.adjusted(6, 6, -6, -6)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.setPen(QPen(QColor("#3a3a45" if hover else "#2a2a31"), 1))
        p.setBrush(QBrush(QColor("#202027" if hover else "#1a1a1f")))
        p.drawRoundedRect(card, RADIUS, RADIUS)

        cover = QRect(card.x() + 10, card.y() + 10, card.width() - 20, THUMB_H)
        pm = None
        for v in f.videos:
            if self.cache.has(v.thumb_key):
                pm = self.cache.get(v.thumb_key)
                if pm is not None and not pm.isNull():
                    break
        if pm is not None and not pm.isNull():
            crop = cover_pixmap(pm, cover.width(), cover.height())
            p.save()
            p.setClipPath(_rounded_clip(p, cover, 8))
            p.drawPixmap(cover, crop)
            p.restore()
        else:
            p.save()
            p.setClipPath(_rounded_clip(p, cover, 8))
            p.fillRect(cover, QColor("#232329"))
            iw = 44
            _draw_folder_glyph(p, QRect(cover.center().x() - iw // 2,
                                        cover.center().y() - 20, iw, 36))
            p.restore()
        p.setPen(QPen(QColor("#3a3a42"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(cover, 8, 8)

        fm = QFontMetrics(p.font())
        name = fm.elidedText(f.name, Qt.TextElideMode.ElideRight, card.width() - 24)
        p.setPen(QPen(QColor("#ffffff")))
        font = QFont(p.font())
        font.setBold(True)
        p.setFont(font)
        p.drawText(QRect(card.x() + 12, cover.bottom() + 6, card.width() - 24, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        font.setBold(False)
        p.setFont(font)
        p.setPen(QPen(QColor("#8e8e96")))
        sub = f"{f.count} video{'s' if f.count != 1 else ''} · {fmt_size(f.total_bytes)}"
        p.drawText(QRect(card.x() + 12, cover.bottom() + 26, card.width() - 24, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)
        p.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease:
            f = index.data(ROLE_ITEM)
            if f is not None:
                self.folderOpened.emit(f)
                return True
        return False


class VideoDelegate(QStyledItemDelegate):
    loadRequested = pyqtSignal(object)

    def __init__(self, cache: ThumbCache, states: dict, parent=None):
        super().__init__(parent)
        self.cache = cache
        self.states = states  # thumb_key -> 'loading' | 'failed'
        self.frame = 0

    def sizeHint(self, option, index):
        return QSize(TILE_W, TILE_H)

    # geometry shared by paint + hit test
    def _geom(self, rect: QRect):
        card = rect.adjusted(6, 6, -6, -6)
        thumb = QRect(card.x() + 10, card.y() + 10, card.width() - 20, THUMB_H)
        load_btn = QRect(thumb.right() - 34, thumb.top() + 6, 28, 28)
        return card, thumb, load_btn

    def paint(self, painter, option, index):
        v: adb.Video | None = index.data(ROLE_ITEM)
        if v is None:
            return
        p = painter
        p.save()
        p.setRenderHints(QPainter.RenderHint.Antialiasing |
                         QPainter.RenderHint.SmoothPixmapTransform)
        card, thumb, load_btn = self._geom(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.setPen(QPen(QColor("#4ade80" if selected else ("#3a3a45" if hover else "#2a2a31")),
                      2 if selected else 1))
        p.setBrush(QBrush(QColor("#222229" if selected else ("#202027" if hover else "#1a1a1f"))))
        p.drawRoundedRect(card, RADIUS, RADIUS)

        state = "ready" if self.cache.has(v.thumb_key) else self.states.get(v.thumb_key, "empty")
        pm = self.cache.get(v.thumb_key) if state == "ready" else None
        p.save()
        p.setClipPath(_rounded_clip(p, thumb, 8))
        if pm is not None and not pm.isNull():
            p.drawPixmap(thumb, cover_pixmap(pm, thumb.width(), thumb.height()))
        else:
            p.fillRect(thumb, QColor("#232329"))
            if state == "loading":
                import math
                p.setPen(QPen(QColor("#4ade80"), 3, Qt.PenStyle.SolidLine,
                             Qt.PenCapStyle.RoundCap))
                ang = (self.frame * 30) % 360
                r = 12
                sq = QRect(thumb.center().x() - r, thumb.center().y() - 14 - r, r * 2, r * 2)
                p.drawArc(sq, (90 - ang) * 16, 270 * 16)
                p.setPen(QPen(QColor("#8e8e96")))
                p.drawText(QRect(thumb.x(), thumb.center().y() + 4, thumb.width(), 18),
                           Qt.AlignmentFlag.AlignHCenter, "Loading…")
            else:
                iw = 40
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor("#3a3a45")))
                tri = QRect(thumb.center().x() - 11, thumb.center().y() - 22, 26, 26)
                path = QPainterPath()
                path.moveTo(tri.x() + 6, tri.y())
                path.lineTo(tri.x() + 6, tri.y() + tri.height())
                path.lineTo(tri.x() + tri.width(), tri.y() + tri.height() // 2)
                path.closeSubpath()
                p.drawPath(path)
                if state == "failed":
                    p.setPen(QPen(QColor("#f87171")))
                    p.drawText(QRect(thumb.x(), thumb.center().y() + 6, thumb.width(), 18),
                               Qt.AlignmentFlag.AlignHCenter, "Tap ↻ to retry")
        p.restore()
        p.setPen(QPen(QColor("#4ade80" if selected else "#3a3a42"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(thumb, 8, 8)

        if state in ("empty", "failed"):
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(0, 0, 0, 150)))
            p.drawEllipse(load_btn)
            _draw_refresh_glyph(p, load_btn.center(), 8, QColor("#ffffff"))
            p.restore()

        if v.duration_ms > 0:
            txt = fmt_dur(v.duration_ms)
            fm = QFontMetrics(p.font())
            tw = fm.horizontalAdvance(txt) + 14
            badge = QRect(thumb.right() - tw - 6, thumb.bottom() - 22, tw, 17)
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(0, 0, 0, 170)))
            bpath = QPainterPath()
            bpath.addRoundedRect(QRectF(badge), 8, 8)
            p.drawPath(bpath)
            p.setPen(QPen(QColor("#ffffff")))
            small = QFont(p.font())
            small.setPointSize(max(8, small.pointSize() - 2))
            p.setFont(small)
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, txt)
            p.restore()

        fm = QFontMetrics(p.font())
        name = fm.elidedText(v.name, Qt.TextElideMode.ElideRight, card.width() - 24)
        p.setPen(QPen(QColor("#ffffff")))
        p.drawText(QRect(card.x() + 12, thumb.bottom() + 6, card.width() - 24, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        p.setPen(QPen(QColor("#8e8e96")))
        sub = f"{fmt_size(v.size)}"
        if v.width:
            sub += f" · {v.width}×{v.height}"
        p.drawText(QRect(card.x() + 12, thumb.bottom() + 26, card.width() - 24, 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sub)
        p.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
            v = index.data(ROLE_ITEM)
            if v is None:
                return False
            _, _, load_btn = self._geom(option.rect)
            pos = event.position().toPoint()
            state = "ready" if self.cache.has(v.thumb_key) else self.states.get(v.thumb_key, "empty")
            if load_btn.contains(pos) and state in ("empty", "failed"):
                self.loadRequested.emit(v)
                return True
        return False


# ---------- settings dialog ----------

class SettingsDialog(QDialog):
    def __init__(self, cache: ThumbCache, parent=None):
        super().__init__(parent)
        self.cache = cache
        self.setWindowTitle("Settings")
        self.setMinimumWidth(380)
        l = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("Version:", QLabel(f"Shrinkit {__version__}"))
        self.cache_lbl = QLabel(fmt_size(cache.disk_usage()))
        form.addRow("Thumbnail cache:", self.cache_lbl)
        self.log_lbl = QLabel(LOG_DIR)
        self.log_lbl.setWordWrap(True)
        form.addRow("Logs:", self.log_lbl)
        l.addLayout(form)
        row = QHBoxLayout()
        clear_btn = QPushButton("Clear thumbnail cache")
        clear_btn.clicked.connect(self._clear)
        logs_btn = QPushButton("Open logs folder")
        logs_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(LOG_DIR)))
        row.addWidget(clear_btn)
        row.addWidget(logs_btn)
        l.addLayout(row)
        close_btn = QPushButton("Close")
        close_btn.setProperty("primary", True)
        close_btn.clicked.connect(self.accept)
        l.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

    def _clear(self):
        self.cache.clear()
        self.cache_lbl.setText(fmt_size(0))


# ---------- main window ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Shrinkit")
        self.resize(900, 660)
        self.setMinimumSize(680, 520)
        self.videos: list[adb.Video] = []
        self.folders: list[adb.Folder] = []
        self.current_folder: adb.Folder | None = None
        self.selected: adb.Video | None = None
        self.return_page = P_HOME
        self.preset_id = DEFAULT_PRESET_ID
        self.cache = ThumbCache()
        self.thumb_states: dict[str, str] = {}
        self.loader: VideoLoadWorker | None = None
        self.pipe: PipelineWorker | None = None
        self.thumbs = ThumbnailWorker(self.cache, self)
        self.thumbs.started_job.connect(self._on_thumb_started)
        self.thumbs.ready.connect(self._on_thumb_ready)
        self.thumbs.failed.connect(self._on_thumb_failed)
        self.thumbs.start()
        self._spin = QTimer(self)
        self._spin.setInterval(120)
        self._spin.timeout.connect(self._tick_spin)
        self._build()

    # ----- layout -----
    def _build(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget(objectName="Header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 10, 16, 10)
        hl.setSpacing(8)
        title = QLabel("Shrinkit", objectName="Title")
        sub = QLabel("phone → pc → phone", objectName="Subtitle")
        self.device_lbl = QLabel("Checking device…", objectName="Muted")
        refresh = QPushButton("Refresh")
        refresh.setFixedWidth(90)
        refresh.clicked.connect(self.load_videos)
        settings_btn = QPushButton("Settings")
        settings_btn.setFixedWidth(90)
        settings_btn.clicked.connect(self._open_settings)
        hl.addWidget(title)
        hl.addWidget(sub)
        hl.addStretch(1)
        hl.addWidget(self.device_lbl)
        hl.addWidget(refresh)
        hl.addWidget(settings_btn)
        layout.addWidget(header)

        self.dep_banner = QLabel()
        self.dep_banner.setWordWrap(True)
        self.dep_banner.setStyleSheet("background:#3a2f10;padding:8px 16px;color:#fbbf24")
        self.dep_banner.hide()
        layout.addWidget(self.dep_banner)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        self.stack.addWidget(self._page_home())
        self.stack.addWidget(self._page_folder())
        self.stack.addWidget(self._page_presets())
        self.stack.addWidget(self._page_progress())
        self.stack.addWidget(self._page_done())

        footer = QWidget()
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(16, 10, 16, 14)
        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(self.go_back)
        self.next_btn = QPushButton("Continue")
        self.next_btn.setProperty("primary", True)
        self.next_btn.clicked.connect(self.go_next)
        fl.addWidget(self.back_btn)
        fl.addStretch()
        fl.addWidget(self.next_btn)
        layout.addWidget(footer)

        self.setCentralWidget(root)
        self._check_deps()
        self._update_nav()
        self.load_videos()

    def _grid_view(self, delegate) -> QListView:
        view = QListView()
        view.setViewMode(QListView.ViewMode.IconMode)
        view.setResizeMode(QListView.ResizeMode.Adjust)
        view.setMovement(QListView.Movement.Static)
        view.setSpacing(8)
        view.setGridSize(QSize(TILE_W + 8, TILE_H + 8))
        view.setUniformItemSizes(True)
        view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        view.setItemDelegate(delegate)
        view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return view

    # ----- page: home (folders / global results) -----
    def _page_home(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(16, 14, 16, 8)
        l.setSpacing(8)
        self.home_search = QLineEdit(placeholderText="Search all videos…")
        self.home_search.textChanged.connect(self._apply_home_filter)
        l.addWidget(self.home_search)
        self.home_title = QLabel("Folders", objectName="Crumb")
        l.addWidget(self.home_title)

        self.folder_model = FolderListModel(self)
        self.folder_delegate = FolderDelegate(self.cache, self)
        self.folder_delegate.folderOpened.connect(self.open_folder)
        self.folder_view = self._grid_view(self.folder_delegate)
        self.folder_view.setModel(self.folder_model)
        self.folder_view.setSelectionMode(QListView.SelectionMode.NoSelection)
        l.addWidget(self.folder_view, 1)

        self.video_model_home = VideoListModel(self)
        self.video_delegate_home = VideoDelegate(self.cache, self.thumb_states, self)
        self.video_delegate_home.loadRequested.connect(self._request_thumb)
        self.home_results = self._grid_view(self.video_delegate_home)
        self.home_results.setModel(self.video_model_home)
        self.home_results.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.home_results.selectionModel().selectionChanged.connect(self._on_home_select)
        self.home_results.doubleClicked.connect(lambda *_: self.go_next())
        self.home_results.hide()
        l.addWidget(self.home_results, 1)

        self.home_status = QLabel(objectName="Muted")
        l.addWidget(self.home_status)
        return w

    # ----- page: folder videos -----
    def _page_folder(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(16, 14, 16, 8)
        l.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(8)
        back = QPushButton("‹ Folders")
        back.setFixedWidth(100)
        back.clicked.connect(lambda: (self.stack.setCurrentIndex(P_HOME), self._update_nav()))
        self.folder_title = QLabel("", objectName="Crumb")
        self.sort_box = QComboBox()
        self.sort_box.addItems(["Newest", "Largest", "Longest", "Name A–Z"])
        self.sort_box.setFixedWidth(130)
        self.sort_box.currentIndexChanged.connect(self._resort_folder)
        self.load_all_btn = QPushButton("Load previews")
        self.load_all_btn.setFixedWidth(140)
        self.load_all_btn.clicked.connect(self._load_all_thumbs)
        top.addWidget(back)
        top.addWidget(self.folder_title)
        top.addStretch(1)
        top.addWidget(self.sort_box)
        top.addWidget(self.load_all_btn)
        l.addLayout(top)

        self.video_model = VideoListModel(self)
        self.video_delegate = VideoDelegate(self.cache, self.thumb_states, self)
        self.video_delegate.loadRequested.connect(self._request_thumb)
        self.folder_grid = self._grid_view(self.video_delegate)
        self.folder_grid.setModel(self.video_model)
        self.folder_grid.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.folder_grid.selectionModel().selectionChanged.connect(self._on_folder_select)
        self.folder_grid.doubleClicked.connect(lambda *_: self.go_next())
        l.addWidget(self.folder_grid, 1)
        self.folder_status = QLabel(objectName="Muted")
        l.addWidget(self.folder_status)
        return w

    # ----- page: presets -----
    def _page_presets(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(16, 14, 16, 8)
        head = QHBoxLayout()
        head.setSpacing(12)
        self.preset_thumb = QLabel()
        self.preset_thumb.setFixedSize(176, 99)
        self.preset_thumb.setStyleSheet(
            "background:#232329;border:1px solid #2e2e35;border-radius:8px;")
        self.preset_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sel_info = QLabel()
        self.sel_info.setStyleSheet("color:#a9a9b2")
        self.sel_info.setWordWrap(True)
        head.addWidget(self.preset_thumb)
        head.addWidget(self.sel_info, 1)
        l.addLayout(head)
        self.preset_group = QButtonGroup(self)
        self.preset_radios: dict[str, QRadioButton] = {}
        for pid, p in PRESETS.items():
            r = QRadioButton(
                f"{p.name}   —   {p.tagline}   ({p.hint})   "
                f"[{p.codec_label} · {p.res_label}]")
            r.setChecked(pid == self.preset_id)
            r.toggled.connect(lambda on, _pid=pid: self._set_preset(_pid) if on else None)
            self.preset_group.addButton(r)
            self.preset_radios[pid] = r
            l.addWidget(r)
        self.adv_toggle = QCheckBox("Advanced overrides")
        self.adv_box = QWidget()
        form = QFormLayout(self.adv_box)
        self.crf_slider = QSlider(Qt.Orientation.Horizontal)
        self.crf_slider.setRange(16, 28)
        self.crf_slider.setValue(PRESETS[self.preset_id].crf)
        self.crf_lbl = QLabel()
        self.crf_slider.valueChanged.connect(
            lambda v: self.crf_lbl.setText(f"CRF {v} (lower = better)"))
        self.crf_lbl.setText(f"CRF {self.crf_slider.value()} (lower = better)")
        self.maxh_box = QComboBox()
        self.maxh_box.addItems(["Original", "1440p max", "1080p max", "720p max"])
        self.audio_box = QComboBox()
        self.audio_box.addItems(["192k", "160k", "128k", "96k"])
        self.audio_box.setCurrentText("128k")
        self.mute_box = QCheckBox("Remove audio")
        self._sync_adv_boxes(PRESETS[self.preset_id])
        form.addRow("Quality", self.crf_slider)
        form.addRow("", self.crf_lbl)
        form.addRow("Resolution", self.maxh_box)
        form.addRow("Audio", self.audio_box)
        form.addRow("", self.mute_box)
        self.adv_box.hide()
        self.adv_toggle.toggled.connect(self.adv_box.setVisible)
        l.addWidget(self.adv_toggle)
        l.addWidget(self.adv_box)
        l.addStretch()
        return w

    # ----- page: progress -----
    def _page_progress(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(16, 14, 16, 8)
        self.step_pull = QLabel("○ 1. Copying from phone")
        self.step_comp = QLabel("○ 2. Compressing")
        self.step_push = QLabel("○ 3. Copying back to phone")
        for s in (self.step_pull, self.step_comp, self.step_push):
            s.setStyleSheet("font-size:14px;color:#e8e8ea")
            l.addWidget(s)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        l.addWidget(self.bar)
        self.log_view = QTextEdit(readOnly=True)
        l.addWidget(self.log_view, 1)
        row = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        row.addStretch()
        row.addWidget(self.cancel_btn)
        l.addLayout(row)
        return w

    # ----- page: done -----
    def _page_done(self):
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(16, 14, 16, 8)
        self.done_title = QLabel("✓ Done")
        self.done_title.setStyleSheet("font-size:22px;font-weight:700;color:#4ade80")
        self.done_detail = QLabel()
        self.done_detail.setWordWrap(True)
        l.addWidget(self.done_title)
        l.addWidget(self.done_detail)
        l.addStretch()
        again = QPushButton("Compress another")
        again.clicked.connect(self._restart)
        again.setProperty("primary", True)
        l.addWidget(again, alignment=Qt.AlignmentFlag.AlignLeft)
        return w

    # ----- deps / loading -----
    def _check_deps(self):
        msgs = []
        ok_adb, adb_msg = adb.check_adb()
        ok_ff, ff_msg = ffmpeg.check_ffmpeg()
        if not ok_adb:
            msgs.append(f"ADB: {adb_msg}")
        if not ok_ff:
            msgs.append(f"FFmpeg: {ff_msg}")
        if msgs:
            self.dep_banner.setText(" · ".join(msgs) + " — install, add to PATH, restart Shrinkit.")
            self.dep_banner.show()
        self.deps_ok = ok_adb and ok_ff

    def load_videos(self):
        if getattr(self, "deps_ok", True) is False:
            self._check_deps()
            if not self.deps_ok:
                return
        self.device_lbl.setText("Looking for device…")
        self.home_status.setText("Loading…")
        self.loader = VideoLoadWorker(self)
        self.loader.loaded.connect(self._on_videos)
        self.loader.failed.connect(self._on_load_fail)
        self.loader.start()

    def _on_videos(self, videos):
        self.videos = videos
        self.folders = adb.group_by_folder(videos)
        self.device_lbl.setText("● Device connected")
        self.device_lbl.setStyleSheet("color:#4ade80; background:transparent")
        log.info("loaded %d videos in %d folders", len(videos), len(self.folders))
        self._apply_home_filter()
        self._update_nav()

    def _on_load_fail(self, msg):
        self.device_lbl.setText("○ No device")
        self.device_lbl.setStyleSheet("color:#f87171; background:transparent")
        self.home_status.setText(msg)
        self.folder_model.set_folders([])
        self.video_model_home.set_videos([])
        log.warning("video load failed: %s", msg)
        self._update_nav()

    # ----- home filtering -----
    def _apply_home_filter(self):
        q = self.home_search.text().strip().lower()
        if q:
            rows = [v for v in self.videos
                    if q in v.name.lower() or q in v.folder.lower()]
            self.video_model_home.set_videos(rows)
            self.folder_view.hide()
            self.home_results.show()
            self.home_title.setText(f"Results ({len(rows)})")
            self.home_status.setText(
                f"{len(rows)} / {len(self.videos)} shown" if self.videos else "")
        else:
            self.home_results.hide()
            self.folder_view.show()
            self.home_title.setText("Folders")
            self.folder_model.set_folders(self.folders)
            total = len(self.videos)
            self.home_status.setText(
                f"{len(self.folders)} folders · {total} videos"
                if total else ("No videos found on phone." if self.videos is not None else ""))

    def _on_home_select(self):
        rows = self.home_results.selectionModel().selectedIndexes()
        self.selected = self.video_model_home.videos[rows[0].row()] if rows else None
        if self.selected:
            self.return_page = P_HOME
        self._update_nav()

    # ----- folder page -----
    def open_folder(self, folder: adb.Folder):
        self.current_folder = folder
        self.folder_title.setText(f"{folder.name} ({folder.count})")
        self.video_model.set_videos(folder.videos)
        self.folder_status.setText(
            f"{folder.count} videos · {fmt_size(folder.total_bytes)}")
        self.folder_grid.selectionModel().clearSelection()
        self._refresh_thumb_button()
        self.stack.setCurrentIndex(P_FOLDER)
        self._update_nav()

    def _resort_folder(self):
        idx = self.sort_box.currentIndex()
        self.video_model.sort_key = ["modified", "size", "duration", "name"][idx]
        self.video_model.reverse = idx != 3
        self.video_model.set_videos(self.video_model.videos)

    def _on_folder_select(self):
        rows = self.folder_grid.selectionModel().selectedIndexes()
        self.selected = self.video_model.videos[rows[0].row()] if rows else None
        if self.selected:
            self.return_page = P_FOLDER
        self._update_nav()

    # ----- thumbnails -----
    def _request_thumb(self, video: adb.Video):
        if self.cache.has(video.thumb_key):
            self._repaint_grids()
            return
        self.thumb_states[video.thumb_key] = "loading"
        n = self.thumbs.enqueue([video])
        log.info("thumb requested: %s (queued=%d)", video.name, n)
        self._repaint_grids()
        self._refresh_thumb_button()

    def _load_all_thumbs(self):
        if not self.current_folder:
            return
        missing = [v for v in self.current_folder.videos
                   if not self.cache.has(v.thumb_key)]
        for v in missing:
            self.thumb_states[v.thumb_key] = "loading"
        n = self.thumbs.enqueue(missing)
        log.info("load-all thumbs in %s: %d queued", self.current_folder.name, n)
        self._repaint_grids()
        self._refresh_thumb_button()

    def _on_thumb_started(self, key: str):
        self.thumb_states[key] = "loading"
        self._repaint_grids()

    def _on_thumb_ready(self, key: str):
        self.thumb_states.pop(key, None)
        self._repaint_grids()
        self._refresh_thumb_button()
        self._maybe_refresh_preset_thumb(key)

    def _on_thumb_failed(self, key: str, msg: str):
        self.thumb_states[key] = "failed"
        log.warning("thumb failed %s: %s", key[:8], msg)
        self._repaint_grids()
        self._refresh_thumb_button()

    def _repaint_grids(self):
        for view in (self.folder_view, self.home_results, self.folder_grid):
            view.viewport().update()

    def _tick_spin(self):
        active = any(s == "loading" for s in self.thumb_states.values())
        if not active:
            self._spin.stop()
            return
        for d in (self.video_delegate, self.video_delegate_home):
            d.frame += 1
        self.folder_grid.viewport().update()
        self.home_results.viewport().update()

    def _refresh_thumb_button(self):
        if not self.current_folder or self.stack.currentIndex() != P_FOLDER:
            return
        pending = self.thumbs.pending_count()
        missing = sum(1 for v in self.current_folder.videos
                      if not self.cache.has(v.thumb_key))
        if pending or (missing and any(s == "loading" for s in self.thumb_states.values())):
            if not self._spin.isActive():
                self._spin.start()
            self.load_all_btn.setText(f"Loading… ({pending})")
            self.load_all_btn.setEnabled(False)
        elif missing:
            self.load_all_btn.setText(f"Load previews ({missing})")
            self.load_all_btn.setEnabled(True)
        else:
            self.load_all_btn.setText("Previews ready ✓")
            self.load_all_btn.setEnabled(False)

    def _maybe_refresh_preset_thumb(self, key: str):
        if self.selected and self.selected.thumb_key == key \
                and self.stack.currentIndex() == P_PRESET:
            self._set_preset_thumb()

    def _set_preset_thumb(self):
        v = self.selected
        if v is None:
            self.preset_thumb.setText("—")
            return
        pm = self.cache.get(v.thumb_key) if self.cache.has(v.thumb_key) else None
        if pm is not None and not pm.isNull():
            self.preset_thumb.setPixmap(cover_pixmap(pm, 176, 99))
        else:
            self.preset_thumb.setText("No preview")

    # ----- misc -----
    def _open_settings(self):
        SettingsDialog(self.cache, self).exec()

    def _set_preset(self, pid):
        self.preset_id = pid
        p = PRESETS[pid]
        self.crf_slider.setValue(p.crf)
        self._sync_adv_boxes(p)

    def _sync_adv_boxes(self, p):
        res_labels = {0: "Original", 1440: "1440p max",
                      1080: "1080p max", 720: "720p max"}
        self.maxh_box.setCurrentText(res_labels.get(p.max_height, "Original"))
        self.audio_box.setCurrentText(f"{p.audio_kbps}k")

    def _restart(self):
        self.selected = None
        self.current_folder = None
        self.home_search.clear()
        self.stack.setCurrentIndex(P_HOME)
        self._apply_home_filter()
        self._update_nav()

    # ----- nav -----
    def _update_nav(self):
        i = self.stack.currentIndex()
        self.back_btn.setVisible(i in (P_FOLDER, P_PRESET, P_WORK))
        self.back_btn.setEnabled(i in (P_FOLDER, P_PRESET))
        if i in (P_HOME, P_FOLDER):
            self.next_btn.setText("Continue")
            self.next_btn.setEnabled(self.selected is not None)
            self.next_btn.show()
        elif i == P_PRESET:
            self.next_btn.setText("Compress")
            self.next_btn.setEnabled(self.selected is not None)
            self.next_btn.show()
        elif i == P_WORK:
            self.next_btn.setText("Working…")
            self.next_btn.setEnabled(False)
            self.next_btn.show()
        else:
            self.next_btn.hide()
        if i == P_FOLDER:
            self._refresh_thumb_button()

    def go_back(self):
        i = self.stack.currentIndex()
        if i == P_FOLDER:
            self.stack.setCurrentIndex(P_HOME)
        elif i == P_PRESET:
            self.stack.setCurrentIndex(self.return_page)
        self._update_nav()

    def go_next(self):
        i = self.stack.currentIndex()
        if i in (P_HOME, P_FOLDER) and self.selected:
            self.return_page = i
            p = PRESETS[self.preset_id]
            self.sel_info.setText(
                f"<b>{self.selected.name}</b><br>"
                f"{fmt_size(self.selected.size)} · {fmt_dur(self.selected.duration_ms)}"
                + (f" · {self.selected.width}×{self.selected.height}"
                   if self.selected.width else "")
                + f" · {self.selected.folder}<br>Preset: <b>{p.name}</b> "
                f"({p.codec_label} CRF {p.crf} · {p.res_label})")
            self._set_preset_thumb()
            self.stack.setCurrentIndex(P_PRESET)
            self._update_nav()
        elif i == P_PRESET and self.selected:
            self._start_pipeline()

    # ----- pipeline -----
    def _adv_values(self):
        if not self.adv_toggle.isChecked():
            return None, None, None, False
        maxh = {"Original": 0, "1440p max": 1440, "1080p max": 1080,
                "720p max": 720}[self.maxh_box.currentText()]
        ab = int(self.audio_box.currentText().replace("k", ""))
        return self.crf_slider.value(), maxh, ab, self.mute_box.isChecked()

    def _start_pipeline(self):
        crf, maxh, ab, mute = self._adv_values()
        self.stack.setCurrentIndex(P_WORK)
        self._update_nav()
        self.bar.setValue(0)
        self.log_view.clear()
        for s in (self.step_pull, self.step_comp, self.step_push):
            s.setText(s.text().replace("●", "○").replace("✓", "○"))
        log.info("compress start: %s preset=%s", self.selected.phone_path, self.preset_id)
        self.pipe = PipelineWorker(self.selected, PRESETS[self.preset_id],
                                   crf, maxh, ab, mute, self)
        self.pipe.step.connect(self._on_step)
        self.pipe.compress_pct.connect(self.bar.setValue)
        self.pipe.log.connect(lambda m: self.log_view.append(m))
        self.pipe.finished.connect(self._on_done)
        self.pipe.failed.connect(self._on_fail)
        self.pipe.start()

    def _on_step(self, s):
        mapping = {"pull": self.step_pull, "compress": self.step_comp, "push": self.step_push}
        if s in mapping:
            for k, lbl in mapping.items():
                base = lbl.text().split(". ", 1)[-1].replace("✓ ", "").replace("● ", "")
                num = {"pull": "1", "compress": "2", "push": "3"}[k]
                if k == s:
                    lbl.setText(f"● {num}. {base} — working…")
                elif list(mapping).index(k) < list(mapping).index(s):
                    lbl.setText(f"✓ {num}. {base}")
        if s == "compress":
            self.step_pull.setText("✓ 1. Copying from phone")
        elif s == "push":
            self.step_comp.setText("✓ 2. Compressing")
            self.bar.setValue(100)
        elif s == "done":
            self.step_push.setText("✓ 3. Copying back to phone")

    def _on_done(self, res):
        before = res.get("in_bytes", 0)
        after = res.get("out_bytes", 0)
        saved = (1 - after / before) * 100 if before else 0
        log.info("compress done: %s %d -> %d", res.get("dest"), before, after)
        self.done_detail.setText(
            f"Saved to phone as:\n{res.get('dest', '')}\n\n"
            f"Before {fmt_size(before)} → After {fmt_size(after)} ({saved:.0f}% smaller).\n"
            f"Temp files on PC were deleted.")
        self.stack.setCurrentIndex(P_DONE)
        self._update_nav()

    def _on_fail(self, msg):
        log.warning("compress failed: %s", msg)
        if "Cancelled" in msg:
            QMessageBox.information(self, "Cancelled", "Cancelled. Temp files cleaned up.")
        else:
            QMessageBox.critical(self, "Failed", f"{msg}\n\nSee logs: {LOG_DIR}")
        self.stack.setCurrentIndex(P_PRESET)
        self._update_nav()

    def _cancel(self):
        if self.pipe:
            self.pipe.cancel()

    def closeEvent(self, event):
        try:
            if self.pipe:
                self.pipe.cancel()
                self.pipe.wait(2000)
            if self.loader and self.loader.isRunning():
                self.loader.wait(2000)
            self.thumbs.clear_pending()
            self.thumbs.stop()
            self._spin.stop()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Shrinkit")
    app.setStyleSheet(build_stylesheet())
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
