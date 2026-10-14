"""Native Qt Widgets frontend. Network and process work never runs on the UI thread."""

from __future__ import annotations

import sys
import math
import time
import json
import os
from pathlib import Path

from PySide6.QtCore import (
    QEvent, QObject, QPoint, QPointF, QRunnable, QRect, QRectF, QSize, Qt,
    QThreadPool, QTimer, Signal, Slot, QVariantAnimation, QEasingCurve, QPropertyAnimation,
    QSaveFile, QIODevice,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QIcon, QLinearGradient, QPainter,
    QPen, QPixmap, QPolygonF, QPainterPath, QRadialGradient, QImageReader, QRegion, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel,
    QMainWindow, QMenu, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget, QGridLayout,
)

from .client import APP_VERSION, ensure_service, request
from .theme import APP_NAME, COLORS, STYLESHEET, stylesheet_for, is_dark_theme
from .vendor_host import VendorHost, finish_pending_discovery
from .whitelist import normalize_target, WhitelistError
from .whitelist_ui import WhitelistDialog
from .engine import recent_destinations, service_root, initialize_storage


BACKGROUND_PRESETS = (
    ("night-lake", "静谧夜湖", "风景", "night-lake.png"),
    ("sunset-coast", "海岸日落", "风景", "sunset-coast.png"),
    ("anime-rain-city", "雨夜霓虹", "二次元", "anime-rain-city.png"),
    ("anime-seaside", "夏日海边", "二次元", "anime-seaside.png"),
    ("cosmic-nebula", "宇宙星云", "宇宙", "cosmic-nebula.png"),
    ("abstract-aurora", "极简流光", "抽象", "abstract-aurora.png"),
)
BACKGROUND_FILES = {item[0]: item[3] for item in BACKGROUND_PRESETS}
DEFAULT_BACKGROUND = "night-lake"


def _appearance_file():
    return service_root() / "appearance.json"


def _read_appearance(path):
    """Only fixed IDs and two theme names cross the preference-file boundary."""
    result = {"background": DEFAULT_BACKGROUND, "theme": "dark"}
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return result
        saved = json.loads(raw.decode("utf-8-sig"))
        if isinstance(saved, dict):
            if saved.get("background") in BACKGROUND_FILES:
                result["background"] = saved["background"]
            if saved.get("theme") in ("dark", "light"):
                result["theme"] = saved["theme"]
    except (OSError, UnicodeError, ValueError, TypeError):
        pass
    return result


def _label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    value = QLabel(text)
    if name:
        value.setObjectName(name)
    value.setWordWrap(wrap)
    value.setTextFormat(Qt.TextFormat.PlainText)
    return value


def _card(name: str = "Card") -> QFrame:
    value = QFrame()
    value.setObjectName(name)
    return value


def _button(text: str, name: str = "", tip: str = "") -> QPushButton:
    value = MotionButton(text)
    value.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        value.setObjectName(name)
    if tip:
        value.setToolTip(tip)
    return value


class MotionButton(QPushButton):
    """Finite hover glow, immediate press and a small release spring."""
    def __init__(self, text):
        super().__init__(text)
        self._hover = 0.0
        self._pulse = 1.0
        self._press_at = QPointF()
        self._compression = 0.0
        self._press_active = False
        self._keyboard_focus = False
        self.hover_motion = QVariantAnimation(self)
        self.hover_motion.setDuration(180)
        self.hover_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.hover_motion.valueChanged.connect(self._hover_changed)
        self.press_motion = QVariantAnimation(self)
        self.press_motion.setDuration(360)
        self.press_motion.setStartValue(0.0)
        self.press_motion.setEndValue(1.0)
        self.press_motion.setEasingCurve(QEasingCurve.Type.Linear)
        self.press_motion.valueChanged.connect(self._pulse_changed)
        self.spring_motion = QVariantAnimation(self)
        self.spring_motion.valueChanged.connect(self._compression_changed)
        self.clicked.connect(self._click_feedback)

    def _hover_changed(self, value):
        self._hover = float(value)
        self.update()

    def _pulse_changed(self, value):
        self._pulse = float(value)
        self.update()

    def _compression_changed(self, value):
        self._compression = float(value)
        self.update()

    def _compress_to(self, target, release=False):
        current = self._compression
        self.spring_motion.stop()
        self.spring_motion.setDuration(320 if release else 85)
        curve = QEasingCurve(QEasingCurve.Type.OutBack if release else QEasingCurve.Type.OutCubic)
        if release:
            curve.setOvershoot(1.25)
        self.spring_motion.setEasingCurve(curve)
        self.spring_motion.setStartValue(current)
        self.spring_motion.setEndValue(target)
        self.spring_motion.start()

    def _begin_press(self, position):
        self._press_active = True
        self._press_at = position
        self._pulse = 0.0
        self.press_motion.stop()
        self.press_motion.start()
        # The first rendered frame must already acknowledge the input.
        self._compression = max(.45, self._compression)
        self._compress_to(1.0)
        self.update()

    def _hover_to(self, target):
        current = self._hover
        self.hover_motion.stop()
        self.hover_motion.setStartValue(current)
        self.hover_motion.setEndValue(target)
        self.hover_motion.start()

    def enterEvent(self, event):
        if self.isEnabled():
            self._hover_to(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_to(0.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._begin_press(event.position())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        # Keep _press_active set during clicked emission to avoid a second pulse.
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and self._press_active:
            self._press_active = False
            self._compress_to(0.0, release=True)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.isAutoRepeat() and self.isEnabled():
            self._begin_press(QPointF(self.width() / 2, self.height() / 2))
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        super().keyReleaseEvent(event)
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.isAutoRepeat() and self._press_active:
            self._press_active = False
            self._compress_to(0.0, release=True)

    def focusInEvent(self, event):
        self._keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._keyboard_focus = False
        if self._press_active:
            self._press_active = False
            self._compress_to(0.0, release=True)
        super().focusOutEvent(event)

    def _click_feedback(self):
        if not self._press_active and self.isEnabled():
            self._begin_press(QPointF(self.width() / 2, self.height() / 2))
            self._press_active = False
            self._compress_to(0.0, release=True)

    def _reset_motion(self):
        self.hover_motion.stop()
        self.press_motion.stop()
        self.spring_motion.stop()
        self._hover, self._pulse, self._compression = 0.0, 1.0, 0.0
        self._press_active = False
        self.update()

    def hideEvent(self, event):
        self._reset_motion()
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._reset_motion()

    @staticmethod
    def _mix(first, second, amount):
        a, b = QColor(first), QColor(second)
        amount = max(0.0, min(1.0, amount))
        return QColor(*(round(x + (y-x)*amount) for x, y in zip(a.getRgb(), b.getRgb())))

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(2, 2, self.width() - 4, self.height() - 4)
        name = self.objectName()
        scale = 1.0 - (.023 if name == "Nav" else .035) * self._compression
        p.translate(bounds.center() + QPointF(0, 1.5 * self._compression))
        p.scale(scale, scale)
        p.translate(-bounds.center())
        disabled = not self.isEnabled()
        dark = is_dark_theme()
        path = QPainterPath()
        radius = 9 if name == "Nav" else 11
        path.addRoundedRect(bounds, radius, radius)
        primary = name == "Primary"
        nav = name == "Nav"
        ghost = name in ("Ghost", "TextButton")
        if primary:
            gradient = QLinearGradient(bounds.topLeft(), bounds.bottomRight())
            gradient.setColorAt(0, self._mix("#4ba7ff", "#6bcdff", self._hover))
            gradient.setColorAt(1, self._mix("#256ae5", "#347dec", self._hover))
            p.fillPath(path, QColor("#3b5879") if disabled else gradient)
            border, ink = "#8bcfff", "#ffffff"
        elif nav:
            if dark:
                p.fillPath(path, self._mix("#273d59", "#344e6c", self._hover) if self.isChecked() else QColor(122,180,233,int(36*self._hover)))
                border, ink = ("#58789c", "#eef7ff") if self.isChecked() else ("#476480", "#b3c6da")
            else:
                p.fillPath(path, self._mix("#f4faff", "#ffffff", self._hover) if self.isChecked() else QColor(255,255,255,int(125*self._hover)))
                border, ink = ("#bad0e5", "#234469") if self.isChecked() else ("#c3d6e8", "#37516b")
        elif name == "WindowControl":
            p.fillPath(path, QColor(224,241,255,int(35*self._hover)))
            border, ink = "#00000000", "#e7f3ff"
        elif ghost:
            p.fillPath(path, self._mix("#172332", "#2a3c50", self._hover) if dark else self._mix("#eaf3fb", "#ffffff", self._hover))
            border, ink = ("#41586e", "#d4e5f6") if dark else ("#abc3da", "#284a6b")
        else:
            p.fillPath(path, self._mix("#233346", "#354e68", self._hover) if dark else self._mix("#dfedf9", "#f8fcff", self._hover))
            border, ink = ("#4a647e", "#e4effb") if dark else ("#a9c2dc", "#274d72")
        if name not in ("TextButton", "WindowControl") and (not nav or self.isChecked()):
            p.setPen(QPen(QColor(border), 1))
            p.drawPath(path)
        if nav and self.isChecked():
            p.setPen(QPen(QColor("#87c7ff" if dark else "#548ee0"), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(bounds.width()*.3, bounds.bottom()-2), QPointF(bounds.width()*.7, bounds.bottom()-2))
        if self._hover > 0 and not disabled:
            p.setClipPath(path)
            shine = QLinearGradient(bounds.left(), 0, bounds.right(), 0)
            shine.setColorAt(0, QColor(140, 221, 255, 0))
            shine.setColorAt(.35 + .4 * self._hover, QColor(180, 235, 255, int(38 * self._hover)))
            shine.setColorAt(1, QColor(140, 221, 255, 0))
            p.fillRect(bounds, shine)
        if self._pulse < 1 and not disabled:
            p.setClipPath(path)
            bloom = QRadialGradient(self._press_at, max(10, bounds.width() * (.12 + 1.1 * self._pulse)))
            bloom.setColorAt(0, QColor(220, 247, 255, int(115 * (1-self._pulse))))
            bloom.setColorAt(.6, QColor(111, 203, 255, int(72 * (1-self._pulse))))
            bloom.setColorAt(1, QColor(84, 164, 255, 0))
            p.fillRect(bounds, bloom)
        p.setClipping(False)
        if (self._hover > 0 or self._compression > 0) and not disabled:
            halo = QColor("#93d7ff" if dark or primary else "#5698d1")
            halo.setAlpha(round(110 * self._hover + 90 * max(0.0, self._compression)))
            p.setPen(QPen(halo, 1.3))
            p.drawPath(path)
        p.setPen(QColor("#9cabb9" if disabled and dark else "#778b9d" if disabled else ink))
        font = QFont("Microsoft YaHei UI", 11 if primary else 10)
        font.setWeight(QFont.Weight.DemiBold if primary or self.isChecked() else QFont.Weight.Medium)
        p.setFont(font)
        pad = 4 if name == "WindowControl" else 10 if name == "Ghost" else 14
        text_rect = bounds.adjusted(pad, 0, -pad, 0)
        align = Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if nav or name == "TextButton" else Qt.AlignmentFlag.AlignHCenter)
        if not self.icon().isNull():
            size = self.iconSize()
            self.icon().paint(p, int(text_rect.left()), int((self.height()-size.height())/2), size.width(), size.height())
            text_rect.adjust(size.width()+10, 0, 0, 0)
        p.drawText(text_rect, align, self.text())
        if self.hasFocus() and self._keyboard_focus:
            p.setPen(QPen(QColor("#f2b073"), 1, Qt.PenStyle.DotLine))
            p.drawRoundedRect(bounds.adjusted(4, 4, -4, -4), 7, 7)


class EnergyHero(QFrame):
    """Painted launcher artwork with finite, state-driven motion; idle is static."""
    def __init__(self):
        super().__init__()
        self.energy = .45
        self.phase = 0.0
        self._target_state = None
        self.energy_motion = QVariantAnimation(self)
        self.energy_motion.setDuration(1100)
        self.energy_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.energy_motion.valueChanged.connect(self._energy_changed)
        self.stage_motion = QVariantAnimation(self)
        self.stage_motion.setDuration(2600)
        self.stage_motion.setStartValue(0.0)
        self.stage_motion.setEndValue(1.0)
        self.stage_motion.setLoopCount(-1)
        self.stage_motion.valueChanged.connect(self._phase_changed)

    def _energy_changed(self, value):
        self.energy = float(value)
        self.phase = float(value) * .6
        self.update()

    def _phase_changed(self, value):
        self.phase = float(value)
        self.update()

    def set_connection(self, state, connected):
        key = (state, connected)
        if key == self._target_state:
            return
        self._target_state = key
        self.energy_motion.stop()
        self.energy_motion.setStartValue(self.energy)
        self.energy_motion.setEndValue(1.0 if connected and state == "running" else .7 if state in ("starting", "running") else .45)
        self.energy_motion.start()
        if state in ("starting", "stopping") and self.isVisible():
            self.stage_motion.start()
        else:
            self.stage_motion.stop()

    def pulse(self):
        if self.stage_motion.state() == QVariantAnimation.State.Running:
            return
        self.energy_motion.stop()
        self.energy_motion.setStartValue(self.energy + .22)
        self.energy_motion.setEndValue(self.energy)
        self.energy_motion.start()

    def hideEvent(self, event):
        self.stage_motion.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._target_state and self._target_state[0] in ("starting", "stopping"):
            self.stage_motion.start()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        shape = QPainterPath(); shape.addRoundedRect(bounds, 24, 24)
        p.setClipPath(shape)
        bg = QLinearGradient(0, 0, self.width(), self.height())
        bg.setColorAt(0, QColor("#24201d")); bg.setColorAt(.43, QColor("#1a1717")); bg.setColorAt(1, QColor("#25170f"))
        p.fillPath(shape, bg)
        w, h = self.width(), self.height()
        center = QPointF(w*.79, h*.52)
        radius = min(h*.36, w*.17)
        # Wide cinematic glow and static scan lines frame the energy core.
        glow = QRadialGradient(center, radius*2.25)
        glow.setColorAt(0, QColor(255, 131, 34, int(100 + self.energy*60)))
        glow.setColorAt(.5, QColor(217, 69, 9, 65)); glow.setColorAt(1, QColor(255, 106, 10, 0))
        p.fillRect(bounds, glow)
        p.setPen(QPen(QColor(255, 179, 102, 10), 1))
        for y in range(20, int(h), 22):
            p.drawLine(QPointF(w*.5, y), QPointF(w, y))
        # Orbital paths use perspective rather than a flat dashboard badge.
        for index in range(3):
            p.save(); p.translate(center); p.rotate(-24 + index*37 + self.phase*18)
            rect = QRectF(-radius*(1.45+index*.16), -radius*(.47+index*.16), radius*(2.9+index*.32), radius*(.94+index*.32))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 154, 72, 35 + index*19), 1.2))
            p.drawEllipse(rect)
            p.setPen(QPen(QColor(255, 187, 114, 165), 2 if index==0 else 1.2))
            p.drawArc(rect, int((self.phase*100 + index*85)*16), 56*16)
            p.restore()
        # Brighter rim and sphere shading make the artwork feel physical.
        sphere = QRadialGradient(center - QPointF(radius*.35, radius*.45), radius*1.6)
        sphere.setColorAt(0, QColor("#ffe7b2")); sphere.setColorAt(.14, QColor("#ffbd63"))
        sphere.setColorAt(.4, QColor("#ff852e")); sphere.setColorAt(.7, QColor("#b83407")); sphere.setColorAt(1, QColor("#341607"))
        p.setPen(QPen(QColor(255, 182, 92, 190), 1.4)); p.setBrush(sphere)
        p.drawEllipse(center, radius*.69, radius*.69)
        p.save(); p.translate(center); p.rotate(-15)
        p.setPen(QPen(QColor(255, 240, 190, 135), 1)); p.setBrush(Qt.BrushStyle.NoBrush)
        for k in (-.47, -.22, .1, .36):
            p.drawArc(QRectF(-radius*.65, radius*k-radius*.25, radius*1.3, radius*.5), 15*16, 150*16)
        p.restore()
        # Small satellites are fixed at idle and move only during interaction.
        for index, angle in enumerate((15, 147, 251)):
            a = math.radians(angle + self.phase*65)
            point = center + QPointF(math.cos(a)*radius*1.45, math.sin(a)*radius*.87)
            p.setBrush(QColor("#ffd199")); p.setPen(Qt.PenStyle.NoPen); p.drawEllipse(point, 3, 3)
            p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(QColor(255, 154, 73, 80), 1)); p.drawEllipse(point, 9, 9)
        p.setPen(QColor("#eeac77")); p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        p.drawText(QRectF(w*.59, h*.84, w*.38, 20), Qt.AlignmentFlag.AlignCenter, "FASTORANGE  /  CONNECTION ENGINE")
        # Decorative left edge creates a cinematic launcher composition.
        p.setPen(QPen(QColor("#ff9b48"), 3)); p.drawLine(QPointF(0, h*.23), QPointF(0, h*.7))
        p.setClipping(False)
        p.setPen(QPen(QColor("#4e3324"), 1)); p.setBrush(Qt.BrushStyle.NoBrush); p.drawPath(shape)


class PageTransition(QWidget):
    """Fade through the scenic backdrop; only one page's text is drawn at a time."""
    def __init__(self, parent):
        super().__init__(parent)
        # Navigation remains outside this layer. Page actions wait until visible.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.progress = 1.0
        self.old = self.new = self.backdrop = None
        self.direction = 1
        self.motion = QVariantAnimation(self)
        self.motion.setDuration(330)
        self.motion.setStartValue(0.0); self.motion.setEndValue(1.0)
        self.motion.setEasingCurve(QEasingCurve.Type.Linear)
        self.motion.valueChanged.connect(self._advance)
        self.motion.finished.connect(self.finish)
        parent.installEventFilter(self)
        self.hide()

    @staticmethod
    def snapshot(page):
        ratio = page.devicePixelRatioF()
        pixmap = QPixmap(page.size() * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        # QWidget.grab() fills otherwise transparent page roots with the palette
        # background. Keep just child content, including their own card fills.
        page.render(pixmap, renderFlags=QWidget.RenderFlag.DrawChildren)
        return pixmap

    def current_content(self):
        if not self.isVisible() or self.old is None:
            return self.snapshot(self.parentWidget().currentWidget())
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(self.size() * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self._paint_content(painter)
        painter.end()
        return pixmap

    def capture_backdrop(self):
        pages = self.parentWidget()
        page = pages.currentWidget()
        root = pages.parentWidget()
        self.hide()
        page.hide()
        try:
            return root.grab(QRect(pages.mapTo(root, QPoint()), pages.size()))
        finally:
            page.show()

    def begin(self, old, new, direction=1, backdrop=None):
        self.old, self.new, self.direction = old, new, direction
        self.backdrop = backdrop
        self.motion.stop(); self.progress = 0.0
        self.setGeometry(self.parentWidget().rect()); self.show(); self.raise_(); self.motion.start()

    def finish(self):
        self.motion.stop()
        self.progress = 1.0
        self.hide()
        self.old = self.new = self.backdrop = None

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() in (QEvent.Type.Resize, QEvent.Type.Hide):
            self.finish()
        return super().eventFilter(watched, event)

    def _advance(self, value):
        self.progress = float(value); self.update()

    def _paint_content(self, painter):
        if self.progress < .42:
            amount = self.progress / .42
            painter.setOpacity((1-amount) ** 2)
            painter.drawPixmap(QPointF(0, -self.direction * 8 * amount), self.old)
        else:
            amount = (self.progress-.42) / .58
            arrival = (1-amount) ** 3
            painter.setOpacity(1-arrival)
            painter.drawPixmap(QPointF(0, self.direction * 14 * arrival), self.new)

    def paintEvent(self, event):
        if self.old is None or self.new is None:
            return
        p = QPainter(self)
        # A full scene masks the live target beneath translucent snapshots.
        if self.backdrop is not None:
            p.drawPixmap(0, 0, self.backdrop)
        self._paint_content(p)


class BackgroundTile(MotionButton):
    def __init__(self, background_id, title, category, available):
        super().__init__(title)
        self.background_id, self.title, self.category = background_id, title, category
        self.thumbnail = QPixmap()
        self.available = available
        self.setCheckable(True)
        self.setEnabled(available)
        self.setFixedHeight(112)
        self.setMinimumWidth(160)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{title}，{category}" + ("" if available else "，待准备"))
        self.setToolTip(f"{category} · {title}" if available else "背景尚未准备好")

    def set_thumbnail(self, image):
        self.thumbnail = QPixmap.fromImage(image)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        compression = float(getattr(self, "_compression", 0.0))
        p.translate(bounds.center() + QPointF(0, 1.5*compression))
        p.scale(1-.028*compression, 1-.028*compression)
        p.translate(-bounds.center())
        path = QPainterPath(); path.addRoundedRect(bounds, 11, 11)
        p.setClipPath(path)
        p.fillPath(path, QColor("#1b2e43" if is_dark_theme() else "#a8bdd0"))
        if not self.thumbnail.isNull():
            # A small thumbnail is the only per-tile retained bitmap.
            source = QRectF(self.thumbnail.rect())
            ratio = max(bounds.width()/source.width(), bounds.height()/source.height())
            crop_width, crop_height = bounds.width()/ratio, bounds.height()/ratio
            crop = QRectF((source.width()-crop_width)/2, (source.height()-crop_height)/2, crop_width, crop_height)
            p.drawPixmap(bounds, self.thumbnail, crop)
        band = QLinearGradient(0, bounds.top(), 0, bounds.bottom())
        band.setColorAt(0, QColor(5, 15, 28, 8))
        band.setColorAt(.45, QColor(5, 15, 28, 5))
        band.setColorAt(1, QColor(5, 15, 28, 225))
        p.fillRect(bounds, band)
        hover = float(getattr(self, "_hover", 0.0))
        pulse = float(getattr(self, "_pulse", 1.0))
        if hover > 0:
            p.fillRect(bounds, QColor(120, 214, 255, int(28*hover)))
        if pulse < 1:
            bloom = QRadialGradient(getattr(self, "_press_at", bounds.center()), max(15, bounds.width()*1.4*pulse))
            bloom.setColorAt(0, QColor(219, 248, 255, int(155*(1-pulse))))
            bloom.setColorAt(1, QColor(121, 218, 255, 0))
            p.fillRect(bounds, bloom)
        p.setPen(QColor("#f0f7ff")); p.setFont(QFont("Microsoft YaHei UI", 9, QFont.Weight.DemiBold))
        p.drawText(bounds.adjusted(11, 0, -11, -9), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, self.title)
        p.setFont(QFont("Microsoft YaHei UI", 8))
        p.setPen(QColor("#c0d9ef"))
        p.drawText(bounds.adjusted(11, 8, -11, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, self.category)
        if not self.available:
            p.setFont(QFont("Microsoft YaHei UI", 10))
            p.drawText(bounds, Qt.AlignmentFlag.AlignCenter, "待准备")
        if self.isChecked():
            circle = QPointF(bounds.right()-17, bounds.top()+17)
            p.setBrush(QColor("#4ba2f8")); p.setPen(Qt.PenStyle.NoPen); p.drawEllipse(circle, 10, 10)
            p.setPen(QPen(QColor("#ffffff"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(circle+QPointF(-4,0),circle+QPointF(-1,3)); p.drawLine(circle+QPointF(-1,3),circle+QPointF(5,-4))
        p.setClipping(False)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#80caff" if self.isChecked() else "#7896b0"), 2.3 if self.isChecked() else 1))
        p.drawPath(path)


class ArtworkSignals(QObject):
    ready = Signal(str, object)


class ThumbnailTask(QRunnable):
    def __init__(self, background_id, path):
        super().__init__()
        self.background_id, self.path = background_id, str(path)
        self.signals = ArtworkSignals()

    @Slot()
    def run(self):
        reader = QImageReader(self.path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(QSize(480, 280), Qt.AspectRatioMode.KeepAspectRatio))
        self.signals.ready.emit(self.background_id, reader.read())


class AppearanceSignals(QObject):
    failed = Signal(str)
    finished = Signal(object)


class AppearanceTask(QRunnable):
    def __init__(self, path, preferences):
        super().__init__()
        self.path = Path(path)
        self.preferences = {"background": preferences.get("background"), "theme": preferences.get("theme")}
        self.signals = AppearanceSignals()

    @Slot()
    def run(self):
        try:
            if self.preferences.get("background") not in BACKGROUND_FILES or self.preferences.get("theme") not in ("light", "dark"):
                raise ValueError("不支持的外观选项")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = QSaveFile(str(self.path))
            if not target.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(target.errorString())
            data = json.dumps(self.preferences, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if target.write(data) != len(data) or not target.commit():
                raise OSError(target.errorString())
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            self.signals.finished.emit(self)


class LandscapeCanvas(QWidget):
    """Cached photographic backdrop with finite, interaction-driven light sweeps."""
    def __init__(self, background_id=DEFAULT_BACKGROUND):
        super().__init__()
        self.setObjectName("Root")
        # The scene fills every pixel. Native window clipping owns the corners;
        # child animation frames can safely reuse this opaque background.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.phase = 1.0
        self.energy = .4
        self._target_state = None
        # Decode once. Cover cropping and the readability overlay are cached by
        # logical size + DPR, so action animation never repeatedly scales a photo.
        self.background_id = DEFAULT_BACKGROUND
        requested = background_id if background_id in BACKGROUND_FILES else DEFAULT_BACKGROUND
        if (Path(__file__).parent / "assets" / BACKGROUND_FILES[requested]).is_file():
            self.background_id = requested
        self._background_source = QPixmap(str(Path(__file__).parent / "assets" / BACKGROUND_FILES[self.background_id]))
        self._background_source.setDevicePixelRatio(1.0)
        self._background_cache = QPixmap()
        self._background_key = None
        self._background_builds = 0
        self.motion = QVariantAnimation(self)
        self.motion.setDuration(850)
        self.motion.setStartValue(0.0); self.motion.setEndValue(1.0)
        self.motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.motion.valueChanged.connect(self._advance)
        self.stage_motion = QVariantAnimation(self)
        self.stage_motion.setDuration(2300)
        self.stage_motion.setStartValue(0.0); self.stage_motion.setEndValue(1.0)
        self.stage_motion.setLoopCount(-1)
        self.stage_motion.valueChanged.connect(self._advance)

    def set_background(self, background_id):
        if background_id not in BACKGROUND_FILES:
            return False
        if background_id == self.background_id:
            return True
        path = Path(__file__).parent / "assets" / BACKGROUND_FILES[background_id]
        if not path.is_file():
            return False
        source = QPixmap(str(path))
        if source.isNull():
            return False
        source.setDevicePixelRatio(1.0)
        self._background_source = source
        self.background_id = background_id
        self._background_cache = QPixmap()
        self._background_key = None
        self.pulse()
        self.update()
        return True

    def _advance(self, value):
        self.phase = float(value); self.update()

    def pulse(self):
        if self.stage_motion.state() != QVariantAnimation.State.Running:
            self.motion.stop(); self.motion.start()

    def set_connection(self, state, connected):
        key = (state, connected)
        if key == self._target_state:
            return
        self._target_state = key
        self.energy = 1.0 if connected else .4
        if state in ("starting", "stopping") and self.isVisible():
            self.stage_motion.start()
        else:
            self.stage_motion.stop(); self.pulse()

    def hideEvent(self, event):
        self.stage_motion.stop(); super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._target_state and self._target_state[0] in ("starting", "stopping"):
            self.stage_motion.start()

    def _background_for_size(self, width, height, dpr):
        key = (max(1, int(width)), max(1, int(height)), round(float(dpr), 4))
        if key == self._background_key and not self._background_cache.isNull():
            return self._background_cache
        pixel_width = max(1, int(math.ceil(key[0] * key[2])))
        pixel_height = max(1, int(math.ceil(key[1] * key[2])))
        if not self._background_source.isNull():
            covered = self._background_source.scaled(
                QSize(pixel_width, pixel_height),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            backdrop = covered.copy(
                max(0, (covered.width() - pixel_width) // 2),
                max(0, (covered.height() - pixel_height) // 2),
                pixel_width, pixel_height,
            )
        else:
            # A missing resource leaves a quiet gradient, never the old polygon
            # mountains or synthetic circular star trails.
            backdrop = QPixmap(pixel_width, pixel_height)
            backdrop.fill(QColor("#112c47"))
        painter = QPainter(backdrop)
        if self._background_source.isNull():
            fallback = QLinearGradient(0, 0, pixel_width*.25, pixel_height)
            fallback.setColorAt(0, QColor("#08182c"))
            fallback.setColorAt(.55, QColor("#1d4260"))
            fallback.setColorAt(1, QColor("#183045"))
            painter.fillRect(backdrop.rect(), fallback)
        # Shade only the text edge and the bottom bar; the snow peaks and lake
        # through the center/right retain the original photograph's detail.
        left = QLinearGradient(0, 0, pixel_width*.7, 0)
        left.setColorAt(0, QColor(3, 12, 27, 62))
        left.setColorAt(.48, QColor(3, 12, 27, 23))
        left.setColorAt(1, QColor(3, 12, 27, 0))
        painter.fillRect(backdrop.rect(), left)
        bottom = QLinearGradient(0, pixel_height*.55, 0, pixel_height)
        bottom.setColorAt(0, QColor(3, 12, 27, 0))
        bottom.setColorAt(.55, QColor(3, 12, 27, 19))
        bottom.setColorAt(1, QColor(3, 12, 27, 92))
        painter.fillRect(backdrop.rect(), bottom)
        painter.end()
        backdrop.setDevicePixelRatio(key[2])
        self._background_cache, self._background_key = backdrop, key
        self._background_builds += 1
        return backdrop

    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#08182c"))
        p.drawPixmap(QPointF(0, 0), self._background_for_size(w, h, self.devicePixelRatioF()))
        if self.phase < .995:
            # One luminous sweep per action; no permanently animated wallpaper.
            x = w*(.28+.58*self.phase); y = h*(.14+.2*self.phase)
            trail = QLinearGradient(x-w*.2,y-h*.05,x,y)
            trail.setColorAt(0,QColor(139,224,255,0)); trail.setColorAt(1,QColor(206,245,255,int(210*(1-self.phase))))
            p.setPen(QPen(trail,2.5,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap)); p.drawLine(QPointF(x-w*.2,y-h*.05),QPointF(x,y))
        radius = 0 if self.window().isMaximized() or self.window().isFullScreen() else 18
        p.setPen(QPen(QColor(173,214,255,70),1)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(.5, .5, w-1, h-1), radius, radius)


class DragBar(QWidget):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.window().windowHandle():
            self.window().windowHandle().startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.window().showNormal() if self.window().isMaximized() else self.window().showMaximized()
        super().mouseDoubleClickEvent(event)


def _paint_symbol(painter: QPainter, rect: QRectF, kind: str, color: str) -> None:
    """Small vector symbols avoid font-glyph and platform-icon inconsistencies."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / 24, rect.height() / 24)
    painter.setPen(QPen(QColor(color), 1.65, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "overview":
        for x, y in ((3, 3), (14, 3), (3, 14), (14, 14)):
            painter.drawRoundedRect(QRectF(x, y, 7, 7), 1.7, 1.7)
    elif kind == "activity":
        painter.drawPolyline(QPolygonF([QPointF(2, 13), QPointF(6, 13),
            QPointF(9, 5), QPointF(13, 20), QPointF(17, 10), QPointF(22, 10)]))
    elif kind == "settings":
        for x, y in ((5, 7), (12, 16), (19, 10)):
            painter.drawLine(QPointF(x, 3), QPointF(x, y - 3))
            painter.drawLine(QPointF(x, y + 3), QPointF(x, 21))
            painter.drawEllipse(QPointF(x, y), 2.5, 2.5)
    elif kind == "about":
        painter.drawEllipse(QRectF(3, 3, 18, 18))
        painter.drawLine(QPointF(12, 11), QPointF(12, 17))
        painter.drawPoint(QPointF(12, 7))
    elif kind == "direct":
        painter.drawRoundedRect(QRectF(3, 4, 18, 12), 2, 2)
        painter.drawLine(QPointF(12, 16), QPointF(12, 20))
        painter.drawLine(QPointF(7, 20), QPointF(17, 20))
        painter.drawLine(QPointF(8, 10), QPointF(16, 10))
        painter.drawLine(QPointF(13, 7), QPointF(16, 10))
        painter.drawLine(QPointF(13, 13), QPointF(16, 10))
    elif kind == "vpn":
        painter.drawPath(_shield_path())
        painter.drawLine(QPointF(8, 12), QPointF(11, 15))
        painter.drawLine(QPointF(11, 15), QPointF(17, 9))
    elif kind == "active":
        painter.drawEllipse(QRectF(3, 3, 18, 18))
        painter.drawLine(QPointF(12, 7), QPointF(12, 12))
        painter.drawLine(QPointF(12, 12), QPointF(16, 15))
    elif kind == "errors":
        painter.drawPolygon(QPolygonF([QPointF(12, 3), QPointF(22, 21), QPointF(2, 21)]))
        painter.drawLine(QPointF(12, 9), QPointF(12, 14))
        painter.drawPoint(QPointF(12, 18))
    painter.restore()


def _shield_path():
    from PySide6.QtGui import QPainterPath
    path = QPainterPath(QPointF(12, 2))
    path.lineTo(21, 6)
    path.lineTo(20, 14)
    path.cubicTo(19, 18, 15, 21, 12, 22)
    path.cubicTo(9, 21, 5, 18, 4, 14)
    path.lineTo(3, 6)
    path.closeSubpath()
    return path


def _icon(kind: str, color: str = "#8d9cb3") -> QIcon:
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    _paint_symbol(painter, QRectF(5, 5, 38, 38), kind, color)
    painter.end()
    return QIcon(pixmap)


class Logo(QWidget):
    def __init__(self, size: int = 46, hero: bool = False):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(size, size)
        self.hero = hero
        self.connected = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        if self.hero:
            painter.setPen(QPen(QColor("#455063"), 1))
            painter.setBrush(QColor("#1c2431"))
            painter.drawEllipse(QRectF(3, 3, width - 6, width - 6))
            painter.setPen(QPen(QColor("#7c6149"), 1.7))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(QRectF(3, 3, width - 6, width - 6), 40 * 16, 98 * 16)
            painter.drawArc(QRectF(3, 3, width - 6, width - 6), 220 * 16, 72 * 16)
            painter.translate(width * .22, width * .22)
            painter.scale(width * .56 / 24, width * .56 / 24)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#2e2927"))
            painter.drawRoundedRect(QRectF(0, 0, width, width), 12, 12)
            painter.translate(width * .19, width * .16)
            painter.scale(width * .65 / 24, width * .65 / 24)
        gradient = QLinearGradient(3, 2, 21, 22)
        gradient.setColorAt(0, QColor("#ffc279"))
        gradient.setColorAt(1, QColor("#ff8145"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawPolygon(QPolygonF([QPointF(14, 1), QPointF(4, 13),
            QPointF(11, 13), QPointF(9, 24), QPointF(21, 10), QPointF(14, 10)]))


class Metric(QFrame):
    def __init__(self, title: str, hint: str, kind: str, color: str):
        super().__init__()
        self.setObjectName("Metric")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(17, 14, 17, 13)
        layout.setSpacing(3)
        row = QHBoxLayout()
        row.addWidget(_label(title, "MetricName"))
        icon = QLabel()
        self._metric_kind, self._metric_icon = kind, icon
        icon.setPixmap(_icon(kind, color).pixmap(19, 19))
        row.addStretch()
        row.addWidget(icon)
        layout.addLayout(row)
        self.value = _label("—", "MetricValue")
        layout.addWidget(self.value)
        layout.addWidget(_label(hint, "MetricHint"))
        self.setMinimumHeight(105)
        self._displayed = None
        self.count_motion = QVariantAnimation(self)
        self.count_motion.setDuration(550)
        self.count_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.count_motion.valueChanged.connect(self._count_changed)

    def _count_changed(self, value):
        self.value.setText(f"{int(round(float(value))):,}")

    def update_value(self, value):
        target = int(value) if isinstance(value, (int, float)) else None
        if target == self._displayed:
            return
        self.count_motion.stop()
        if target is not None and self._displayed is not None:
            self.count_motion.setStartValue(float(self._displayed))
            self.count_motion.setEndValue(float(target))
            self.count_motion.start()
        else:
            self.value.setText(f"{target:,}" if target is not None else "—")
        self._displayed = target

    def update_theme(self):
        color = "#9fc9f4" if is_dark_theme() else "#48779f"
        self._metric_icon.setPixmap(_icon(self._metric_kind, color).pixmap(19, 19))


class WorkerSignals(QObject):
    succeeded = Signal(str, object)
    failed = Signal(str, str)
    finished = Signal(str)


class ServiceTask(QRunnable):
    def __init__(self, action: str, payload=None):
        super().__init__()
        self.action, self.payload = action, payload
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            if self.action == "initialize":
                ensure_service()
                result = request("status", timeout=5)
            else:
                timeout = 20 if self.action in ("start", "stop", "restore", "install_vpn") else 5
                result = request(self.action, self.payload, timeout=timeout)
            self.signals.succeeded.emit(self.action, result)
        except Exception as exc:
            self.signals.failed.emit(self.action, str(exc))
        finally:
            self.signals.finished.emit(self.action)


class CompanionWindow(QMainWindow):
    def __init__(self, preview: bool = False, appearance_path=None):
        super().__init__()
        self.preview = preview
        self.appearance_path = Path(appearance_path) if appearance_path is not None else _appearance_file()
        self.appearance = _read_appearance(self.appearance_path)
        QApplication.instance().setProperty("appearanceTheme", self.appearance["theme"])
        QApplication.instance().setStyleSheet(stylesheet_for(self.appearance["theme"]))
        self._art_tasks = {}
        self._appearance_tasks = []
        self.appearance_pool = QThreadPool(self)
        self.appearance_pool.setMaxThreadCount(1)
        self._owned = False
        self._closing = False
        self._closed = False
        self._busy_action = ""
        self._tasks = {}
        self._status = {}
        self._last_logs = []
        self._errors = 0
        self._tray_hint_shown = False
        self._vendor_auto_embed = False
        self._vendor_launch_pending = False
        self._vendor_probe_at = 0.0
        self._vendor_probe_attempts = 0
        self._whitelist_dialog = None
        self._whitelist_vendor_hidden = False
        self._whitelist_mutation_epoch = 0
        self._whitelist_list_epoch = 0
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        # Avoid a per-pixel translucent top-level surface on Windows. Preserve
        # rounded corners with the native region instead of alpha compositing.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.resize(1100, 690)
        self.setMinimumSize(920, 600)
        self.setWindowIcon(self._app_icon())
        self._build_ui()
        self._window_shape_ready = True
        self._update_window_mask()
        self._apply_appearance_theme(self.appearance["theme"])
        self._load_background_thumbnails()
        self._build_tray()
        self._quick_whitelist_shortcuts = []
        for keys, callback in (("Ctrl+L", self._open_whitelist),
                               ("Ctrl+Shift+D", lambda: self._clipboard_whitelist("direct")),
                               ("Ctrl+Shift+P", lambda: self._clipboard_whitelist("vpn"))):
            shortcut = QShortcut(keys, self)
            shortcut.activated.connect(callback)
            self._quick_whitelist_shortcuts.append(shortcut)
        self.window_motion = QPropertyAnimation(self, b"windowOpacity", self)
        self.window_motion.setDuration(280)
        self.window_motion.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.window_motion.finished.connect(self._window_motion_finished)
        self._hiding = False
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self._poll)
        if preview:
            self._apply_status({"state": "stopped", "connected": False,
                "vpn_running": False, "paused": False, "version": APP_VERSION,
                "direct": None, "vpn": None, "active": None, "errors": None,
                "physical": "", "dns": "DoH", "vendor_installed": True,
                "message": "界面预览 · 未启动网络服务", "logs": [], "session_path": ""})
            self.service_chip.setText("预览模式 · 不改变网络")
            self.primary.setText("启动伴航")
            self.footer.setText("界面预览不会启动快橙、保活或分流，也不会改变系统代理。")
        else:
            self._submit("initialize")

    def _update_window_mask(self):
        if self.isMaximized() or self.isFullScreen():
            self.clearMask()
        else:
            shape = QPainterPath()
            shape.addRoundedRect(QRectF(self.rect()), 18, 18)
            self.setMask(QRegion(shape.toFillPolygon().toPolygon()))

    def event(self, event):
        result = super().event(event)
        if getattr(self, "_window_shape_ready", False) and event.type() in (
            QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.WindowStateChange,
            QEvent.Type.DevicePixelRatioChange,
        ):
            self._update_window_mask()
        return result

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor("#08182c"))
        painter.end()
        super().paintEvent(event)

    def _app_icon(self) -> QIcon:
        logo = Logo(64)
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        logo.render(pixmap)
        return QIcon(pixmap)

    def _build_ui(self):
        root = LandscapeCanvas(self.appearance["background"])
        self.appearance["background"] = root.background_id
        self.energy_hero = root
        self.setCentralWidget(root)
        body = QVBoxLayout(root)
        body.setContentsMargins(24, 15, 24, 17)
        body.setSpacing(12)
        titlebar = DragBar()
        header = QHBoxLayout(titlebar)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)
        header.addWidget(Logo(30))
        header.addWidget(_label(APP_NAME, "WindowBrand"))
        header.addSpacing(10)
        self.page_title = _label("主页", "HeaderContext")
        header.addWidget(self.page_title)
        self.page_subtitle = _label("", "PageSubtitle")
        self.page_subtitle.hide()
        header.addStretch()
        self.service_chip = _label("本地服务 · 正在连接", "Chip")
        header.addWidget(self.service_chip)
        self.whitelist_button = _button("白名单", "Ghost", "Ctrl+L 打开；Ctrl+Shift+D 剪贴板直连；Ctrl+Shift+P 剪贴板 VPN")
        self.whitelist_button.setMinimumHeight(31)
        self.whitelist_button.clicked.connect(self._open_whitelist)
        header.addWidget(self.whitelist_button)
        self.open_vpn = _button("打开快橙", "Ghost")
        self.open_vpn.setMinimumHeight(31)
        self.open_vpn.clicked.connect(self._open_vendor)
        header.addWidget(self.open_vpn)
        self.background_button = _button("后台", "Ghost", "继续保活与分流，将窗口收起至通知区")
        self.background_button.setMinimumWidth(57)
        self.background_button.clicked.connect(self._hide_to_tray)
        header.addWidget(self.background_button)
        header.addSpacing(5)
        for text, callback in (("−", self.showMinimized), ("□", self._maximize), ("×", self.close)):
            control = _button(text, "WindowControl")
            control.setFixedSize(31, 30)
            control.clicked.connect(callback)
            header.addWidget(control)
        body.addWidget(titlebar)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._overview_page())
        self.pages.addWidget(self._activity_page())
        self.pages.addWidget(self._settings_page())
        self.pages.addWidget(self._about_page())
        self.pages.addWidget(self._vendor_page())
        self.page_transition = PageTransition(self.pages)
        body.addWidget(self.pages, 1)
        self.footer = _label("开启伴航后，关闭窗口会停止保活与分流，并恢复启动前的代理设置。", "Footer", True)
        body.addWidget(self.footer)
        bottom = _card("BottomBar")
        bar = QHBoxLayout(bottom)
        bar.setContentsMargins(9, 8, 10, 8)
        bar.setSpacing(5)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for index, (text, kind) in enumerate((("主页", "overview"), ("运行记录", "activity"), ("设置", "settings"), ("关于", "about"), ("快橙", "vpn"))):
            button = _button(text, "Nav")
            button.setCheckable(True)
            button.setIcon(_icon(kind, "#345271"))
            button.setIconSize(QSize(17, 17))
            button.setFixedHeight(40)
            button.setMinimumWidth(100 if index != 1 else 119)
            self.nav.addButton(button, index)
            bar.addWidget(button)
        self.nav.button(0).setChecked(True)
        self.nav.idClicked.connect(self._navigate)
        bar.addStretch()
        bar.addWidget(_label(f"v{APP_VERSION}", "BottomVersion"))
        bar.addSpacing(12)
        self.primary = _button("启动伴航", "Primary")
        self.primary.setMinimumWidth(148)
        self.primary.setFixedHeight(43)
        self.primary.clicked.connect(self._toggle)
        bar.addWidget(self.primary)
        body.addWidget(bottom)

    def _maximize(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()
        self.energy_hero.pulse()

    def _overview_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 16, 8, 6)
        layout.setSpacing(10)
        hintrow = QHBoxLayout()
        hintrow.addWidget(_label("FASTORANGE  /  CONNECTION COMPANION", "SkyEyebrow"))
        hintrow.addStretch()
        self.route_chip = _label("等待启动", "Chip")
        hintrow.addWidget(self.route_chip)
        layout.addLayout(hintrow)
        layout.addStretch(3)
        self.hero_title = _label("准备就绪", "ConnectionHeadline")
        layout.addWidget(self.hero_title)
        self.hero_description = _label("启动后，国内访问走本地网络，海外访问通过快橙。", "SkyDescription", True)
        self.hero_description.setMaximumWidth(640)
        layout.addWidget(self.hero_description)
        layout.addSpacing(10)
        info = _card("FrostInfo")
        box = QHBoxLayout(info)
        box.setContentsMargins(17, 12, 17, 12)
        box.setSpacing(18)
        self.metrics = {}
        self.metrics_panel = info
        self._metrics_visible = None
        self.metrics_motion = QPropertyAnimation(info, b"maximumHeight", self)
        self.metrics_motion.setDuration(480)
        self.metrics_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.metrics_motion.finished.connect(self._metrics_motion_finished)
        for key, title, hint, kind, color in (
            ("direct", "国内直连", "累计连接", "direct", "#37688c"),
            ("vpn", "快橙代理", "累计连接", "vpn", "#4679b2"),
            ("active", "当前连接", "正在转发", "active", "#5684b1"),
            ("errors", "连接异常", "累计记录", "errors", "#85749b"),
        ):
            metric = Metric(title, hint, kind, color)
            metric.setMinimumHeight(79)
            self.metrics[key] = metric
            box.addWidget(metric, 1)
        layout.addWidget(info)
        route = QHBoxLayout()
        route.setSpacing(20)
        self.physical_label = _label("国内 · 启动后直连", "SkyDescription")
        self.foreign_label = _label("海外 · 快橙未连接", "SkyDescription")
        route.addWidget(self.physical_label)
        route.addWidget(self.foreign_label)
        route.addStretch()
        details = _button("查看记录 →", "TextButton")
        details.clicked.connect(lambda: self._navigate(1))
        route.addWidget(details)
        layout.addLayout(route)
        self.message_label = _label("服务就绪后，点击「启动伴航」即可开始。", "SkyHint", True)
        layout.addWidget(self.message_label)
        return page

    def _set_metrics_visible(self, visible):
        if visible == self._metrics_visible:
            return
        previous = self._metrics_visible
        self._metrics_visible = visible
        self.metrics_motion.stop()
        if previous is None or not self.isVisible():
            self.metrics_panel.setVisible(visible)
            self.metrics_panel.setMaximumHeight(16777215 if visible else 0)
            return
        self.metrics_panel.show()
        self.metrics_motion.setStartValue(0 if visible else self.metrics_panel.height())
        self.metrics_motion.setEndValue(125 if visible else 0)
        self.metrics_motion.start()

    def _metrics_motion_finished(self):
        self.metrics_panel.setVisible(bool(self._metrics_visible))
        self.metrics_panel.setMaximumHeight(16777215 if self._metrics_visible else 0)

    def _activity_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(14)
        header = QHBoxLayout()
        header.addWidget(_label("会话日志", "SectionTitle"))
        header.addStretch()
        self.logs_count = _label("0 条", "Chip")
        header.addWidget(self.logs_count)
        box.addLayout(header)
        box.addWidget(_label("查看连接、分流与恢复记录。只显示最近的运行事件。", "Muted", True))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("暂无运行记录。启动伴航后，事件会显示在这里。")
        self.log_view.setMaximumBlockCount(300)
        self.log_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.log_view.customContextMenuRequested.connect(self._log_whitelist_menu)
        box.addWidget(self.log_view, 1)
        self.session_label = _label("当前没有会话", "Muted", True)
        self.session_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.session_label)
        return page

    def _settings_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(14)
        appearance = _card()
        design = QVBoxLayout(appearance)
        design.setContentsMargins(18, 16, 18, 16)
        design.setSpacing(12)
        theme_row = QHBoxLayout()
        theme_copy = QVBoxLayout()
        theme_copy.setSpacing(5)
        theme_copy.addWidget(_label("外观与背景", "CardTitle"))
        theme_copy.addWidget(_label("只改变伴航的界面；主题与背景会自动记住。", "Muted", True))
        theme_row.addLayout(theme_copy, 1)
        self.theme_choices = QButtonGroup(self)
        self.theme_choices.setExclusive(True)
        for index, (mode, title) in enumerate((("dark", "黑夜"), ("light", "浅色"))):
            choice = _button(title, "Nav")
            choice.setCheckable(True)
            choice.setFixedSize(80, 36)
            choice.setChecked(mode == self.appearance["theme"])
            self.theme_choices.addButton(choice, index)
            choice.clicked.connect(lambda checked=False, selected=mode: self._select_theme(selected))
            theme_row.addWidget(choice)
        design.addLayout(theme_row)
        self.background_caption = _label("", "Muted")
        design.addWidget(self.background_caption)
        gallery = QGridLayout()
        gallery.setSpacing(9)
        self.background_choices = QButtonGroup(self)
        self.background_choices.setExclusive(True)
        self.background_tiles = {}
        asset_folder = Path(__file__).parent / "assets"
        for index, (background_id, title, category, filename) in enumerate(BACKGROUND_PRESETS):
            available = (asset_folder / filename).is_file()
            tile = BackgroundTile(background_id, title, category, available)
            tile.setChecked(background_id == self.appearance["background"])
            tile.clicked.connect(lambda checked=False, selected=background_id: self._select_background(selected))
            self.background_choices.addButton(tile, index)
            self.background_tiles[background_id] = tile
            gallery.addWidget(tile, index//3, index%3)
        design.addLayout(gallery)
        self.appearance_notice = _label("", "Muted", True)
        design.addWidget(self.appearance_notice)
        self._update_background_caption()
        box.addWidget(appearance)
        heading = _label("连接管理", "SectionTitle")
        box.addWidget(heading)
        keepalive = _card()
        row = QHBoxLayout(keepalive)
        row.setContentsMargins(20, 19, 20, 19)
        text = QVBoxLayout()
        text.setSpacing(7)
        text.addWidget(_label("断线自动重连", "CardTitle"))
        text.addWidget(_label("检测到快橙断线后重试。暂停保活时，国内分流继续生效。", "Muted", True))
        row.addLayout(text, 1)
        row.addSpacing(14)
        self.pause_button = _button("暂停保活")
        self.pause_button.clicked.connect(self._pause)
        row.addWidget(self.pause_button)
        box.addWidget(keepalive)
        routing = _card()
        routing_box = QVBoxLayout(routing)
        routing_box.setContentsMargins(20, 18, 20, 18)
        routing_box.setSpacing(10)
        routing_box.addWidget(_label("临时国内分流", "CardTitle"))
        routing_box.addWidget(_label("国内域名与国内 IP 从物理网卡直连；海外访问保持快橙出口。国内 DNS 经物理网络使用 HTTPS 查询，并按有效期缓存。", "Muted", True))
        routing_box.addWidget(_label("适用于遵循 Windows 系统代理的网页与应用。独立代理、SSH 与 UDP 应用由各自设置决定。", "Muted", True))
        box.addWidget(routing)
        whitelist = _card()
        whitelist_box = QHBoxLayout(whitelist)
        whitelist_box.setContentsMargins(20, 18, 20, 18)
        whitelist_copy = QVBoxLayout()
        whitelist_copy.addWidget(_label("常用网址白名单", "CardTitle"))
        whitelist_copy.addWidget(_label("固定直连或 VPN 后，命中域名跳过自动分流判断。支持粘贴网址、批量添加和最近连接一键设置。", "Muted", True))
        whitelist_box.addLayout(whitelist_copy, 1)
        manage = _button("管理白名单", "Ghost")
        manage.clicked.connect(self._open_whitelist)
        whitelist_box.addWidget(manage)
        box.addWidget(whitelist)
        behavior = _card()
        behavior_box = QVBoxLayout(behavior)
        behavior_box.setContentsMargins(20, 18, 20, 18)
        behavior_box.setSpacing(10)
        behavior_box.addWidget(_label("关闭与后台运行", "CardTitle"))
        behavior_box.addWidget(_label("关闭窗口：停止本窗口启动的会话，恢复启动前设置，快橙继续运行。选择「后台运行」：保活与分流继续工作，可从通知区重新打开。", "Muted", True))
        box.addWidget(behavior)
        recovery = _card()
        recovery_row = QHBoxLayout(recovery)
        recovery_row.setContentsMargins(20, 18, 20, 18)
        recovery_text = QVBoxLayout()
        recovery_text.setSpacing(7)
        recovery_text.addWidget(_label("恢复残留设置", "CardTitle"))
        recovery_text.addWidget(_label("意外关机后可尝试恢复伴航留下的临时代理。", "Muted", True))
        recovery_row.addLayout(recovery_text, 1)
        recovery_row.addSpacing(14)
        self.restore_button = _button("恢复设置", "Ghost")
        self.restore_button.clicked.connect(lambda: self._submit("restore"))
        recovery_row.addWidget(self.restore_button)
        box.addWidget(recovery)
        box.addStretch()
        scroll.setWidget(page)
        return scroll

    def _load_background_thumbnails(self):
        for background_id, _, _, filename in BACKGROUND_PRESETS:
            tile = self.background_tiles[background_id]
            if not tile.available:
                continue
            task = ThumbnailTask(background_id, Path(__file__).parent / "assets" / filename)
            self._art_tasks[background_id] = task
            task.signals.ready.connect(self._thumbnail_ready)
            self.appearance_pool.start(task)

    @Slot(str, object)
    def _thumbnail_ready(self, background_id, image):
        tile = self.background_tiles.get(background_id)
        if tile is not None:
            if not image.isNull():
                tile.set_thumbnail(image)
            else:
                tile.available = False
                tile.setEnabled(False)
                tile.setToolTip("背景暂时无法读取")
        self._art_tasks.pop(background_id, None)

    def _update_background_caption(self):
        title = next((item[1] for item in BACKGROUND_PRESETS if item[0] == self.appearance["background"]), "静谧夜湖")
        self.background_caption.setText(f"当前背景：{title}")

    def _select_background(self, background_id):
        if background_id not in BACKGROUND_FILES or not self.energy_hero.set_background(background_id):
            self.appearance_notice.setText("这张背景暂时未准备好。")
            current = self.background_tiles.get(self.appearance["background"])
            if current:
                current.setChecked(True)
            return False
        self.appearance["background"] = background_id
        self.background_tiles[background_id].setChecked(True)
        self._update_background_caption()
        self.appearance_notice.setText("背景已应用到所有页面。")
        self._save_appearance()
        return True

    def _apply_appearance_theme(self, mode):
        QApplication.instance().setProperty("appearanceTheme", mode)
        QApplication.instance().setStyleSheet(stylesheet_for(mode))
        for index, kind in enumerate(("overview", "activity", "settings", "about", "vpn")):
            self.nav.button(index).setIcon(_icon(kind, "#a1ccf4" if mode == "dark" else "#345271"))
            self.nav.button(index).update()
        for metric in self.metrics.values():
            metric.update_theme()
        self.theme_choices.button(0 if mode == "dark" else 1).setChecked(True)
        for tile in self.background_tiles.values():
            tile.update()
        self.update()

    def _select_theme(self, mode):
        if mode not in ("light", "dark"):
            return False
        self.appearance["theme"] = mode
        self._apply_appearance_theme(mode)
        self.energy_hero.pulse()
        self.appearance_notice.setText("黑夜模式已开启。" if mode == "dark" else "浅色模式已开启。")
        self._save_appearance()
        return True

    def _save_appearance(self):
        # One worker serializes saves, preserving the last choice on rapid clicks.
        task = AppearanceTask(self.appearance_path, self.appearance)
        self._appearance_tasks.append(task)
        task.signals.failed.connect(self._appearance_save_failed)
        task.signals.finished.connect(self._appearance_saved)
        self.appearance_pool.start(task)

    @Slot(str)
    def _appearance_save_failed(self, message):
        self.appearance_notice.setText(f"外观已应用，但暂未保存：{message}")

    @Slot(object)
    def _appearance_saved(self, task):
        if task in self._appearance_tasks:
            self._appearance_tasks.remove(task)

    def _about_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(17)
        brand = _card("Hero")
        row = QHBoxLayout(brand)
        row.setContentsMargins(27, 26, 27, 26)
        row.setSpacing(21)
        row.addWidget(Logo(80))
        text = QVBoxLayout()
        text.setSpacing(8)
        text.addWidget(_label(APP_NAME, "HeroTitle"))
        text.addWidget(_label("让连接更稳，让国内访问更顺畅。", "HeroDescription"))
        text.addWidget(_label(f"Windows 桌面版  ·  {APP_VERSION}", "Muted"))
        row.addLayout(text, 1)
        box.addWidget(brand)
        for title, body in (
            ("原生界面，轻量运行", "界面使用 Qt Widgets。网络与进程操作在后台执行；窗口可见时每 2 秒刷新状态，后台时每 5 秒刷新。"),
            ("GUI 与 CLI 共用一个会话", "桌面窗口和命令行共享本地协调服务。仅打开界面不会启用代理；启动伴航后才应用临时保活与分流。"),
            ("快橙的账号仍由快橙管理", "首次安装与登录通过快橙官方客户端完成。伴航不接收或保存你的快橙账号密码。"),
        ):
            card = _card()
            card_box = QVBoxLayout(card)
            card_box.setContentsMargins(20, 18, 20, 18)
            card_box.setSpacing(10)
            card_box.addWidget(_label(title, "CardTitle"))
            card_box.addWidget(_label(body, "Muted", True))
            box.addWidget(card)
        box.addStretch()
        return page

    def _vendor_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(20)

        info = QWidget()
        info.setMaximumWidth(246)
        details = QVBoxLayout(info)
        details.setContentsMargins(0, 12, 0, 0)
        details.setSpacing(15)
        details.addWidget(_label("FASTORANGE", "SkyEyebrow"))
        details.addWidget(_label("快橙", "ConnectionHeadline"))
        details.addWidget(_label("登录、选择线路和连接，\n都在这里完成。", "SkyDescription", True))
        self.vendor_notice = _label("点击「在此打开」，显示快橙原界面。", "SkyHint", True)
        details.addWidget(self.vendor_notice)
        self.vendor_embed_button = _button("在此打开", "Primary")
        self.vendor_embed_button.clicked.connect(self._request_vendor_embed)
        details.addWidget(self.vendor_embed_button)
        self.vendor_detach_button = _button("独立窗口", "Ghost")
        self.vendor_detach_button.setEnabled(False)
        self.vendor_detach_button.clicked.connect(self._detach_vendor)
        details.addWidget(self.vendor_detach_button)
        expand = _button("展开界面", "Ghost")
        expand.clicked.connect(lambda: self.showMaximized())
        details.addWidget(expand)
        details.addStretch()
        details.addWidget(_label("账号由快橙管理。\n伴航继续负责保活与临时分流。", "SkyHint", True))
        layout.addWidget(info)

        # Keep the official portrait window inside a dedicated screen area.
        # Its native parent and DPI context remain unchanged.
        viewport = QScrollArea()
        viewport.setWidgetResizable(True)
        viewport.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        viewport.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        viewport.setMinimumWidth(360)
        viewport.setMaximumWidth(480)
        self.vendor_host = VendorHost()
        self.vendor_host.setMinimumSize(340, 480)
        self.vendor_host.changed.connect(self._vendor_host_changed)
        placeholder = QVBoxLayout(self.vendor_host)
        placeholder.setContentsMargins(22, 20, 22, 20)
        placeholder.addStretch()
        slot_title = _label("快橙原界面", "SectionTitle")
        slot_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.addWidget(slot_title)
        slot_hint = _label("点击左侧「在此打开」\n区域较小时，点击「展开界面」", "SkyHint", True)
        slot_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.addWidget(slot_hint)
        placeholder.addStretch()
        viewport.setWidget(self.vendor_host)
        layout.addWidget(viewport, 1)

        hint = _card()
        hint.setMaximumWidth(218)
        side = QVBoxLayout(hint)
        side.setContentsMargins(18, 20, 18, 20)
        side.setSpacing(12)
        side.addWidget(_label("保持原来的快橙", "CardTitle"))
        side.addWidget(_label("这里显示的是官方客户端。线路、会员和连接操作与原来相同。", "Muted", True))
        side.addSpacing(10)
        side.addWidget(_label("随时独立打开", "CardTitle"))
        side.addWidget(_label("点击「独立窗口」或关闭伴航，快橙会恢复成原来的窗口并继续运行。", "Muted", True))
        side.addStretch()
        side.addWidget(_label("窗口较小时，请扩大伴航窗口，或使用「独立窗口」。", "Muted", True))
        layout.addWidget(hint)
        return page

    @Slot(str, bool)
    def _vendor_host_changed(self, message, attached):
        self.vendor_notice.setText(message)
        self.vendor_embed_button.setText("已在此打开" if attached else "重新打开")
        self.vendor_embed_button.setEnabled(not attached and not self._closing)
        self.vendor_detach_button.setEnabled(attached and not self._closing)

    def _request_vendor_embed(self):
        if self.preview:
            self.vendor_notice.setText("界面预览不会打开或接管快橙。")
            return
        self._vendor_auto_embed = True
        self._vendor_probe_attempts = 0
        self._vendor_probe_at = 0.0
        self.vendor_detach_button.setEnabled(not self._closing)
        if self.vendor_host.is_attached:
            return
        if self._vendor_launch_pending or self._busy_action:
            self.vendor_notice.setText("正在处理操作，请稍候。")
            return
        if self._status.get("vpn_running"):
            self._probe_vendor()
        else:
            self._vendor_launch_pending = True
            self.vendor_notice.setText("正在打开快橙…")
            self._submit("open_vpn" if self._status.get("vendor_installed", True) else "install_vpn", {"embed": True})

    def _probe_vendor(self):
        now = time.monotonic()
        if self._vendor_probe_attempts >= 5 or now - self._vendor_probe_at < 2:
            return
        self._vendor_probe_at = now
        self._vendor_probe_attempts += 1
        self.vendor_host.request_attach()

    def _detach_vendor(self):
        self._vendor_auto_embed = False
        self._vendor_launch_pending = False
        try:
            if not self.vendor_host.detach():
                raise RuntimeError("快橙窗口尚未恢复，请重试。")
            self.vendor_notice.setText("快橙已恢复为独立窗口。")
        except Exception as exc:
            self.vendor_notice.setText(f"暂未恢复独立窗口：{exc}")
            self.vendor_detach_button.setEnabled(True)

    def _open_whitelist(self):
        if self._closed or self._closing:
            return
        if not self.isVisible():
            self._show_window()
        if self._whitelist_dialog is not None:
            self._whitelist_dialog.show()
            self._whitelist_dialog.raise_()
            self._whitelist_dialog.activateWindow()
            self._whitelist_dialog.input.setFocus()
            return
        dialog = WhitelistDialog(self, button_factory=_button, preview=self.preview)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        self._whitelist_dialog = dialog
        # The official client is an independent native window. Hide its slot
        # while the editor is open so it cannot cover the editor's controls.
        self._whitelist_vendor_hidden = self.vendor_host.isVisible()
        if self._whitelist_vendor_hidden:
            self.vendor_host.hide()
        dialog.action_requested.connect(self._whitelist_action)
        dialog.finished.connect(self._whitelist_finished)
        if self.preview:
            dialog.apply_document({"version": 1, "revision": 0, "rules": [], "active": False})
        else:
            copied = QApplication.clipboard().text()
            if len(copied) <= 16384:
                try:
                    # Only prefill a recognizable single address, stripped of
                    # any query, path, userinfo and credentials.
                    dialog.fill_text(normalize_target(copied))
                except WhitelistError:
                    pass
            self._submit("whitelist-list")
        dialog.show()
        dialog.input.setFocus()

    def _whitelist_finished(self):
        dialog = self._whitelist_dialog
        self._whitelist_dialog = None
        if self._whitelist_vendor_hidden:
            self.vendor_host.show()
        self._whitelist_vendor_hidden = False
        if dialog is not None:
            dialog.deleteLater()

    def _whitelist_action(self, action, payload):
        if not self.preview and not self._closed and not self._closing:
            self._submit(action, payload)

    def _clipboard_whitelist(self, mode):
        if self.preview:
            self.message_label.setText("预览模式不会读取剪贴板或保存白名单。")
            return
        text = QApplication.clipboard().text()
        if not text.strip():
            self._open_whitelist()
            self._whitelist_dialog.show_error("剪贴板里没有网址，请粘贴或输入域名。")
            return
        self._whitelist_action("whitelist-add", {"text": text, "mode": mode, "subdomains": True})

    def _log_whitelist_menu(self, position):
        menu = self.log_view.createStandardContextMenu()
        cursor = self.log_view.cursorForPosition(position)
        hosts = recent_destinations([cursor.block().text()])
        if hosts:
            host = hosts[0]["host"]
            menu.addSeparator()
            for mode, title in (("direct", "固定直连"), ("vpn", "固定 VPN")):
                action = menu.addAction(title + " · " + host)
                action.triggered.connect(lambda checked=False, target=host, route=mode: self._whitelist_action(
                    "whitelist-add", {"text": target, "mode": route, "subdomains": True}))
        menu.exec(self.log_view.mapToGlobal(position))

    def _build_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        show_action = QAction("打开伴航", self)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)
        self.tray_toggle = QAction("启动伴航", self)
        self.tray_toggle.triggered.connect(self._toggle)
        menu.addAction(self.tray_toggle)
        open_action = QAction("打开快橙", self)
        open_action.triggered.connect(self._open_vendor)
        menu.addAction(open_action)
        whitelist_action = QAction("添加网址白名单…", self)
        whitelist_action.triggered.connect(self._open_whitelist)
        menu.addAction(whitelist_action)
        for mode, title in (("direct", "剪贴板网址 → 直连"), ("vpn", "剪贴板网址 → VPN")):
            action = QAction(title, self)
            action.triggered.connect(lambda checked=False, selected=mode: self._clipboard_whitelist(selected))
            menu.addAction(action)
        menu.addSeparator()
        exit_action = QAction("退出并恢复设置", self)
        exit_action.triggered.connect(self.close)
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    @Slot(int)
    def _navigate(self, index, open_vendor=True):
        titles = (
            ("主页", "快橙连接保活  ·  国内直连  ·  一键恢复"),
            ("运行记录", "查看当前会话的连接与恢复事件"),
            ("设置", "管理保活、分流与恢复行为"),
            ("关于", "快橙的桌面伴航工具"),
            ("快橙", "在伴航中操作快橙官方客户端"),
        )
        previous = self.pages.currentIndex()
        if previous != index and self.isVisible() and 4 not in (previous, index):
            # Restart from the displayed frame when users navigate again mid-flight.
            old = self.page_transition.current_content()
            self.page_transition.motion.stop()
            self.page_transition.hide()
            self.pages.setCurrentIndex(index)
            new = self.page_transition.snapshot(self.pages.currentWidget())
            backdrop = self.page_transition.capture_backdrop()
            self.page_transition.begin(old, new, 1 if index > previous else -1, backdrop)
        else:
            if previous != index:
                self.page_transition.finish()
            self.pages.setCurrentIndex(index)
        self.nav.button(index).setChecked(True)
        self.page_title.setText(titles[index][0])
        self.page_subtitle.setText(titles[index][1])
        self.energy_hero.pulse()
        if index == 4 and previous != index and open_vendor:
            self._request_vendor_embed()

    def _submit(self, action, payload=None):
        if self.preview:
            self.energy_hero.pulse()
            self.message_label.setText("当前是界面预览，未执行网络或安装操作。")
            return
        if action in self._tasks or self._closed:
            return
        if action not in ("status", "initialize", "whitelist-list"):
            if self._busy_action:
                if action.startswith("whitelist-") and self._whitelist_dialog is not None:
                    self._whitelist_dialog.show_error("正在处理其他操作，请稍后重试。")
                    self._whitelist_dialog.set_busy(False)
                return
            self._busy_action = action
            self.energy_hero.pulse()
            if action in ("start", "stop", "restore"):
                changing = "starting" if action == "start" else "stopping"
                self.energy_hero.set_connection(changing, False)
                self.hero_title.setText("正在唤醒连接引擎…" if action == "start" else "正在恢复原设置…")
            if action == "start":
                # Close must also stop a Start that is still in progress.
                self._owned = True
            self._render_controls()
        if action.startswith("whitelist-") and action != "whitelist-list" and self._whitelist_dialog is not None:
            self._whitelist_dialog.set_busy(True)
        if action == "whitelist-list":
            self._whitelist_list_epoch = self._whitelist_mutation_epoch
        elif action.startswith("whitelist-"):
            self._whitelist_mutation_epoch += 1
        task = ServiceTask(action, payload)
        self._tasks[action] = task
        task.signals.succeeded.connect(self._task_succeeded)
        task.signals.failed.connect(self._task_failed)
        task.signals.finished.connect(self._task_finished)
        self.pool.start(task)

    @Slot(str, object)
    def _task_succeeded(self, action, result):
        self._errors = 0
        self.service_chip.setText("本地服务 · 已连接")
        if action.startswith("whitelist-"):
            if self._whitelist_dialog is not None and (action != "whitelist-list" or self._whitelist_list_epoch == self._whitelist_mutation_epoch):
                self._whitelist_dialog.apply_document(result)
            if action != "whitelist-list":
                message = result.get("message", "白名单已保存。")
                self.message_label.setText(message)
                if not self.isVisible() and self.tray.isVisible():
                    self.tray.showMessage(APP_NAME, message, QSystemTrayIcon.MessageIcon.Information, 2500)
        if action in ("open_vpn", "install_vpn") and self._vendor_launch_pending:
            self._vendor_launch_pending = False
            if self._vendor_auto_embed and not self._closed and not self._closing and self.pages.currentIndex() == 4:
                self.vendor_notice.setText("等待快橙窗口准备就绪…")
                self._probe_vendor()
        if action == "start" and isinstance(result, dict) and result.get("started") is False:
            # A CLI may have acquired the session after our preceding status poll.
            # Merely attaching to that session does not transfer close ownership.
            self._owned = False
            if self._closing:
                self._finish_close()
                return
        if isinstance(result, dict):
            status = result.get("status", result)
            if isinstance(status, dict) and "state" in status:
                self._apply_status(status)
        if action == "initialize":
            self.poll_timer.start()
        if action in ("stop", "restore") and self._status.get("state") == "stopped":
            self._owned = False
        if self._closing and action == "stop":
            if self._status.get("state") == "stopped":
                self._finish_close()
            else:
                self.poll_timer.start(1000)

    @Slot(str, str)
    def _task_failed(self, action, message):
        if action.startswith("whitelist-"):
            if self._whitelist_dialog is not None:
                self._whitelist_dialog.show_error(message)
            self.message_label.setText(f"白名单操作未完成：{message}")
            return
        self._errors += 1
        self.service_chip.setText("本地服务 · 暂未响应")
        self.message_label.setText(f"操作未完成：{message}")
        if action in ("open_vpn", "install_vpn") and self._vendor_launch_pending:
            self._vendor_launch_pending = False
            self.vendor_notice.setText(f"暂未打开快橙：{message}")
        self.energy_hero.set_connection(self._status.get("state", "stopped"), bool(self._status.get("connected")))
        if action == "initialize":
            self.poll_timer.start(5000)
        if self._closing:
            # Keep a reviewable window open if restoration could not be confirmed.
            self._closing = False
            self._show_window()
            self.hero_title.setText("恢复尚未完成")
            self.hero_description.setText("本地服务暂未响应，请再次点击「停止并恢复」。")
            self.footer.setText("窗口保持打开，以便重新尝试恢复设置。")

    @Slot(str)
    def _task_finished(self, action):
        self._tasks.pop(action, None)
        if self._busy_action == action:
            self._busy_action = ""
        if action.startswith("whitelist-") and action != "whitelist-list" and self._whitelist_dialog is not None:
            self._whitelist_dialog.set_busy(False)
        self._render_controls()
        if self._closing and action != "stop" and not self._busy_action:
            self._submit("stop")

    def _poll(self):
        if not self._busy_action and not self._closed:
            self._submit("initialize" if not self._status or self._errors >= 3 else "status")
            if self._whitelist_dialog is not None and self._whitelist_dialog.isVisible():
                self._submit("whitelist-list")

    def _apply_status(self, status):
        self._status = dict(status)
        state = status.get("state", "stopped")
        connected = bool(status.get("connected"))
        paused = bool(status.get("paused"))
        titles = {"stopped": "准备就绪", "starting": "正在启动…",
                  "running": "连接守护中" if connected else "正在等待快橙连接",
                  "stopping": "正在恢复设置…", "error": "需要检查连接"}
        if paused and state == "running":
            titles["running"] = "分流运行 · 保活暂停"
        self.hero_title.setText(titles.get(state, "读取状态中…"))
        self._set_metrics_visible(state != "stopped")
        self.energy_hero.set_connection(state, connected)
        descriptions = {
            "stopped": "启动后，国内访问走本地网络，海外访问通过快橙。",
            "starting": "正在准备临时分流与连接保活，请稍候。",
            "running": "国内直连正在运行，海外访问保持快橙出口。" if connected else "国内分流已启用，等待快橙完成连接。",
            "stopping": "正在停止保活与分流，并恢复启动前的代理设置。",
            "error": status.get("message") or "查看运行记录，了解当前会话的状态。",
        }
        self.hero_description.setText(descriptions.get(state, "正在读取服务状态。"))
        for key, metric in self.metrics.items():
            metric.update_value(status.get(key))
        self.route_chip.setText("临时分流 · 运行中" if state == "running" else "等待启动" if state == "stopped" else "会话处理中")
        self.physical_label.setText(str(status.get("physical") or "物理网络") if state == "running" else "启动后生效")
        self.foreign_label.setText("快橙已连接" if connected else "快橙未连接" if status.get("vpn_running") else "快橙未运行")
        self.message_label.setText(status.get("message") or ("保活暂停期间，国内分流仍继续运行。" if paused else "临时会话已启动，关闭窗口可停止并恢复。" if state == "running" else "点击「启动伴航」开始保活与分流。"))
        self.session_label.setText(f"会话目录：{status['session_path']}" if status.get("session_path") else "当前没有会话")
        self.open_vpn.setText("快橙" if status.get("vendor_installed", True) else "安装快橙")
        logs = [str(line) for line in status.get("logs", []) or []][-300:]
        if logs != self._last_logs:
            at_bottom = self.log_view.verticalScrollBar().value() >= self.log_view.verticalScrollBar().maximum() - 5
            old = self._last_logs
            if old and len(logs) >= len(old) and logs[:len(old)] == old:
                self.log_view.appendPlainText("\n".join(logs[len(old):]))
            else:
                self.log_view.setPlainText("\n".join(logs))
            self._last_logs = logs
            if at_bottom:
                self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())
            self.logs_count.setText(f"{len(logs)} 条")
        if self._closing and state == "stopped":
            self._owned = False
            self._finish_close()
            return
        if not self._owned and state in ("running", "starting"):
            self.footer.setText("当前会话由其他入口运行；关闭此窗口不会停止它。")
        else:
            self.footer.setText("关闭窗口会停止本窗口启动的会话，恢复原设置。快橙本体继续运行。")
        self._render_controls()
        if self._vendor_auto_embed and self.pages.currentIndex() == 4 and not self.preview and not self._closing and status.get("vpn_running") and not self.vendor_host.is_attached:
            self._probe_vendor()

    def _render_controls(self):
        state = self._status.get("state", "stopped")
        working = state in ("running", "starting", "stopping") or self._owned
        active = bool(self._busy_action) or self._closing or state in ("starting", "stopping")
        texts = {"start": "正在启动…", "stop": "正在恢复…", "restore": "正在恢复…"}
        self.primary.setText(texts.get(self._busy_action, "停止并恢复" if working else "启动伴航"))
        self.primary.setEnabled(not active and (bool(self._status) or self.preview))
        self.open_vpn.setEnabled(not self._busy_action and not self._closing)
        self.whitelist_button.setEnabled(not self._closing)
        self.pause_button.setEnabled(state == "running" and not active)
        self.pause_button.setText("恢复保活" if self._status.get("paused") else "暂停保活")
        self.restore_button.setEnabled(state in ("stopped", "error") and not active)
        self.background_button.setEnabled(not self._closing)
        self.tray_toggle.setText("停止并恢复" if working else "启动伴航")
        self.tray_toggle.setEnabled(not active)
        self.tray.setToolTip(f"{APP_NAME} · {self.hero_title.text()}")

    def _toggle(self):
        state = self._status.get("state", "stopped")
        self._submit("stop" if state in ("running", "starting", "stopping") or self._owned else "start")

    def _open_vendor(self):
        if self.pages.currentIndex() == 4:
            self._request_vendor_embed()
        else:
            self._navigate(4)

    def _pause(self):
        self._submit("resume" if self._status.get("paused") else "pause")

    def _hide_to_tray(self):
        if self._whitelist_dialog is not None:
            self._whitelist_dialog.close()
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._hiding = True
            self.window_motion.stop()
            self.window_motion.setStartValue(self.windowOpacity())
            self.window_motion.setEndValue(0.0)
            self.window_motion.start()
            if not self._tray_hint_shown:
                self.tray.showMessage(APP_NAME, "伴航继续运行。点击通知区图标可重新打开。",
                                      QSystemTrayIcon.MessageIcon.Information, 2500)
                self._tray_hint_shown = True
        else:
            self.showMinimized()
        self.poll_timer.setInterval(5000)

    def _show_window(self):
        self._hiding = False
        self.window_motion.stop()
        self.setWindowOpacity(0.0)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.window_motion.setStartValue(0.0)
        self.window_motion.setEndValue(1.0)
        self.window_motion.start()
        if not self.preview:
            self.poll_timer.setInterval(2000)
            self._poll()

    def _window_motion_finished(self):
        if self._hiding:
            self.hide()
            self.setWindowOpacity(1.0)
            self._hiding = False

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "poll_timer"):
            self.poll_timer.setInterval(5000 if self.isMinimized() else 2000)

    def closeEvent(self, event):
        if self._whitelist_dialog is not None:
            self._whitelist_dialog.close()
        self._vendor_auto_embed = False
        self._vendor_launch_pending = False
        if hasattr(self, "vendor_host"):
            try:
                if not self.vendor_host.detach():
                    raise RuntimeError("请再点击一次「独立窗口」后退出。")
            except Exception as exc:
                self._closed = False
                self._closing = False
                self.vendor_notice.setText(f"快橙窗口恢复尚未完成：{exc}")
                self._navigate(4, open_vendor=False)
                self.vendor_detach_button.setEnabled(True)
                event.ignore()
                return
        if self._closed or self.preview:
            self.tray.hide()
            event.accept()
            return
        if not self._owned:
            self._closed = True
            self.poll_timer.stop()
            self.tray.hide()
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self.hero_title.setText("正在恢复设置…")
            self.energy_hero.set_connection("stopping", False)
            self.hero_description.setText("恢复完成后，窗口会自动关闭。快橙本体继续运行。")
            self._render_controls()
            if not self._busy_action:
                self._submit("stop")

    def _finish_close(self):
        self._closed = True
        self._closing = False
        self.poll_timer.stop()
        self.tray.hide()
        self.close()

    def capture(self, path: str) -> bool:
        """Exposed native capture hook used by packaging/visual QA."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        return self.grab().save(str(target), "PNG")


def run_gui(argv=None, screenshot_path: str | None = None, preview: bool = False) -> int:
    if not preview:
        initialize_storage()
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("FastOrangeCompanion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setQuitOnLastWindowClosed(True)
    window = CompanionWindow(preview=preview)
    window.show()
    if "--whitelist" in (argv or []):
        QTimer.singleShot(250, window._open_whitelist)
    if screenshot_path:
        def capture_and_close():
            if window._whitelist_dialog is not None and window._whitelist_dialog.isVisible():
                target = Path(screenshot_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                window._whitelist_dialog.grab().save(str(target), "PNG")
            else:
                window.capture(screenshot_path)
            window.close()
        QTimer.singleShot(1400, capture_and_close)
    result = app.exec()
    finish_pending_discovery()
    return result
