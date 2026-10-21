"""Small painted primitives shared by the main window and the whitelist editor.

All colours come from ``theme.tokens()`` at paint time, so these widgets follow
theme switches without rebuilding pixmaps. Nothing here animates while idle.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPalette, QPen, QTextLayout, QTextOption,
)
from PySide6.QtWidgets import QCheckBox, QLabel, QSizePolicy, QWidget

from .theme import FONT_FAMILIES, LATIN_FAMILIES, PALETTES, is_dark_theme, tokens


def color(key: str, alpha: int | None = None) -> QColor:
    palette = tokens()
    value = QColor(palette[key]) if key in palette else QColor(key)
    if alpha is not None:
        value.setAlpha(max(0, min(255, int(alpha))))
    return value


def mix(first, second, amount: float) -> QColor:
    a, b = QColor(first), QColor(second)
    t = max(0.0, min(1.0, float(amount)))
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t,
    )


def ui_font(px: int, weight=QFont.Weight.Normal, latin: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(list(LATIN_FAMILIES if latin else FONT_FAMILIES))
    font.setPixelSize(int(px))
    font.setWeight(weight)
    return font


def apply_application_palette(app, mode: str) -> None:
    """Native dialogs and unstyled widgets follow the same ink/graphite palette."""
    t = PALETTES["light" if mode == "light" else "dark"]
    palette = QPalette()
    for role, key in (
        (QPalette.ColorRole.Window, "base"), (QPalette.ColorRole.Base, "panel"),
        (QPalette.ColorRole.AlternateBase, "panel_high"), (QPalette.ColorRole.Button, "raised"),
        (QPalette.ColorRole.Text, "ink"), (QPalette.ColorRole.WindowText, "ink"),
        (QPalette.ColorRole.ButtonText, "ink"), (QPalette.ColorRole.ToolTipBase, "raised"),
        (QPalette.ColorRole.ToolTipText, "ink"), (QPalette.ColorRole.Highlight, "selection"),
        (QPalette.ColorRole.HighlightedText, "ink"), (QPalette.ColorRole.PlaceholderText, "faint"),
        (QPalette.ColorRole.Link, "accent_text"),
    ):
        palette.setColor(role, QColor(t[key]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(t["faint"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(t["faint"]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(t["faint"]))
    app.setPalette(palette)


TONE_KEYS = {
    "neutral": "muted", "success": "success", "warn": "warn",
    "danger": "danger", "pending": "accent", "active": "accent",
}

TEXT_ROLES = {
    "lead": (13, QFont.Weight.Normal, "ink_soft"),
    "body": (12, QFont.Weight.Normal, "ink_soft"),
    "muted": (12, QFont.Weight.Normal, "muted"),
    "caption": (11, QFont.Weight.Normal, "muted"),
    "feedback": (12, QFont.Weight.Medium, "accent_text"),
    "subtitle": (12, QFont.Weight.Normal, "muted"),
}


class ElidedLabel(QLabel):
    """A plain-text label painted in at most ``lines`` lines with a trailing ellipsis.

    ``text()`` always returns the full assigned text; only the painting is
    shortened. This keeps long backend messages from pushing controls away.
    """

    def __init__(self, text: str = "", role: str = "muted", lines: int = 1,
                 hide_when_empty: bool = False, auto_tooltip: bool = False):
        super().__init__()
        self._role = role if role in TEXT_ROLES else "muted"
        self._lines = max(1, int(lines))
        self._hide_when_empty = hide_when_empty
        self._auto_tooltip = auto_tooltip
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(self._lines > 1)
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(self._lines > 1)
        self.setSizePolicy(policy)
        self.setMaximumHeight(self._line_height() * self._lines)
        self.setText(text)

    def set_role(self, role: str):
        if role in TEXT_ROLES and role != self._role:
            self._role = role
            self.update()

    def setText(self, text):
        text = str(text)
        super().setText(text)
        if self._auto_tooltip:
            self.setToolTip(text if text else "")
        if self._hide_when_empty:
            if not text:
                self.hide()
            elif self.parentWidget() is not None:
                # Never show a parentless label: that would open a top-level window.
                self.show()
        self.updateGeometry()
        self.update()

    def _font(self) -> QFont:
        px, weight, _ = TEXT_ROLES[self._role]
        return ui_font(px, weight)

    def _line_height(self) -> int:
        return int(math.ceil(QFontMetricsF(self._font()).lineSpacing())) + 1

    def _wrap(self, width: float) -> list[str]:
        text = " ".join(self.text().split()) if self._lines > 1 else self.text().replace("\n", " ")
        if not text:
            return []
        font = self._font()
        metrics = QFontMetricsF(font)
        width = max(8.0, float(width))
        if self._lines == 1 or any(ord(char) > 0xFFFF for char in text):
            return [metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)]
        layout = QTextLayout(text, font)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        lines = []
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            start, length = line.textStart(), line.textLength()
            if len(lines) == self._lines - 1:
                lines.append(metrics.elidedText(text[start:], Qt.TextElideMode.ElideRight, width))
                break
            lines.append(text[start:start + length].rstrip())
        layout.endLayout()
        return lines

    def hasHeightForWidth(self):
        return self._lines > 1

    def heightForWidth(self, width):
        if self._hide_when_empty and not self.text():
            return 0
        return max(1, len(self._wrap(width))) * self._line_height()

    def sizeHint(self):
        metrics = QFontMetricsF(self._font())
        width = min(560, int(math.ceil(metrics.horizontalAdvance(self.text()))) + 2)
        if self._lines > 1:
            return QSize(width, self.heightForWidth(max(width, 240)))
        return QSize(width, self._line_height())

    def minimumSizeHint(self):
        return QSize(0, self._line_height())

    def paintEvent(self, event):
        lines = self._wrap(self.width())
        if not lines:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(self._font())
        key = TEXT_ROLES[self._role][2]
        ink = color(key)
        if not self.isEnabled():
            ink = color("faint")
        p.setPen(ink)
        height = self._line_height()
        top = 0.0
        if len(lines) == 1 and self.height() > height and not self.wordWrap():
            top = (self.height() - height) / 2
        alignment = self.alignment()
        if alignment & Qt.AlignmentFlag.AlignHCenter:
            horizontal = Qt.AlignmentFlag.AlignHCenter
        elif alignment & Qt.AlignmentFlag.AlignRight:
            horizontal = Qt.AlignmentFlag.AlignRight
        else:
            horizontal = Qt.AlignmentFlag.AlignLeft
        for index, line in enumerate(lines):
            p.drawText(QRectF(0, top + index * height, self.width(), height),
                       horizontal | Qt.AlignmentFlag.AlignVCenter, line)


class StatusPill(QLabel):
    """A compact semantic status capsule; tone is explicit or derived from rules."""

    def __init__(self, text: str = "", tone: str = "neutral", rules=()):
        super().__init__()
        self._tone = tone if tone in TONE_KEYS else "neutral"
        self._rules = tuple(rules)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text):
        text = str(text)
        super().setText(text)
        for needle, tone in self._rules:
            if needle in text:
                self._tone = tone
                break
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def tone(self) -> str:
        return self._tone

    def set_tone(self, tone: str):
        tone = tone if tone in TONE_KEYS else "neutral"
        if tone != self._tone:
            self._tone = tone
            self.update()

    @staticmethod
    def _font() -> QFont:
        return ui_font(11, QFont.Weight.Medium)

    def sizeHint(self):
        width = QFontMetricsF(self._font()).horizontalAdvance(self.text())
        return QSize(int(math.ceil(width)) + 30, 24)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = is_dark_theme()
        key = TONE_KEYS[self._tone]
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        radius = rect.height() / 2
        if self._tone == "neutral":
            p.setBrush(color("ink", 12 if dark else 10))
            p.setPen(QPen(color("ink", 26 if dark else 30), 1))
        else:
            p.setBrush(color(key, 30 if dark else 26))
            p.setPen(QPen(color(key, 70 if dark else 90), 1))
        p.drawRoundedRect(rect, radius, radius)
        dot = QPointF(rect.left() + 11.5, rect.center().y())
        p.setPen(Qt.PenStyle.NoPen)
        if self._tone in ("success", "pending", "active"):
            p.setBrush(color(key, 60))
            p.drawEllipse(dot, 5, 5)
        p.setBrush(color(key))
        p.drawEllipse(dot, 3, 3)
        p.setFont(self._font())
        p.setPen(color("ink_soft") if self._tone == "neutral" else color(key))
        p.drawText(rect.adjusted(20, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


class StatusDot(QWidget):
    def __init__(self, tone: str = "neutral", size: int = 12):
        super().__init__()
        self._tone = tone if tone in TONE_KEYS else "neutral"
        self.setFixedSize(size, size)

    def set_tone(self, tone: str):
        tone = tone if tone in TONE_KEYS else "neutral"
        if tone != self._tone:
            self._tone = tone
            self.update()

    def tone(self) -> str:
        return self._tone

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        key = TONE_KEYS[self._tone]
        p.setPen(Qt.PenStyle.NoPen)
        if self._tone in ("success", "pending", "active"):
            p.setBrush(color(key, 55))
            p.drawEllipse(center, self.width() / 2, self.height() / 2)
        p.setBrush(color(key))
        p.drawEllipse(center, 3.2, 3.2)


class ToggleSwitch(QCheckBox):
    """A QCheckBox painted as a switch. Checked state stays fully owned by callers."""

    TRACK_W, TRACK_H = 36, 20

    def __init__(self, text: str, bold: bool = True):
        super().__init__(text)
        self._bold = bold
        self._offset = 0.0
        self._hover = False
        self._keyboard_focus = False
        self.motion = QVariantAnimation(self)
        self.motion.setDuration(170)
        self.motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.motion.valueChanged.connect(self._offset_changed)
        self.toggled.connect(self._animate)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _offset_changed(self, value):
        self._offset = float(value)
        self.update()

    def _animate(self, checked):
        target = 1.0 if checked else 0.0
        self.motion.stop()
        if not self.isVisible():
            self._offset = target
            self.update()
            return
        self.motion.setStartValue(self._offset)
        self.motion.setEndValue(target)
        self.motion.start()

    def showEvent(self, event):
        self.motion.stop()
        self._offset = 1.0 if self.isChecked() else 0.0
        super().showEvent(event)

    def _font(self) -> QFont:
        return ui_font(13, QFont.Weight.DemiBold if self._bold else QFont.Weight.Normal)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def sizeHint(self):
        width = QFontMetricsF(self._font()).horizontalAdvance(self.text())
        return QSize(self.TRACK_W + 12 + int(math.ceil(width)) + 6, 28)

    def minimumSizeHint(self):
        return self.sizeHint()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        self._keyboard_focus = event.reason() in (Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason)
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._keyboard_focus = False
        super().focusOutEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = is_dark_theme()
        enabled = self.isEnabled()
        track = QRectF(1.5, (self.height() - self.TRACK_H) / 2, self.TRACK_W, self.TRACK_H)
        radius = self.TRACK_H / 2
        if not enabled:
            p.setOpacity(.5)
        off_fill = color("raised") if dark else color("border")
        fill = mix(off_fill, color("accent"), self._offset)
        path = QPainterPath()
        path.addRoundedRect(track, radius, radius)
        p.fillPath(path, fill)
        edge = mix(color("border_strong"), color("accent_lo"), self._offset)
        if self._hover and enabled:
            edge = mix(edge, color("accent"), .55)
        p.setPen(QPen(edge, 1))
        p.drawPath(path)
        knob_x = track.left() + radius + self._offset * (self.TRACK_W - 2 * radius)
        knob = QPointF(knob_x, track.center().y())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color("shadow", 60 if dark else 34))
        p.drawEllipse(knob + QPointF(0, 1), 7.6, 7.6)
        p.setBrush(color("knob"))
        p.drawEllipse(knob, 7, 7)
        if self._keyboard_focus and self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(color("accent", 190), 1.5))
            p.drawRoundedRect(track.adjusted(-3, -3, 3, 3), radius + 3, radius + 3)
        p.setOpacity(1.0 if enabled else .55)
        p.setFont(self._font())
        p.setPen(color("ink"))
        text_rect = QRectF(track.right() + 11, 0, self.width() - track.right() - 11, self.height())
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
