# SPDX-License-Identifier: GPL-3.0-or-later
"""A fixed stereo geometry: fader, meter, shared scale, meter, fader.

Only presentation lives here. Existing controls retain their connections;
changing a value or its translated readout cannot relayout the signal paths.
"""
import math

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QPainter, QPalette
from PyQt6.QtWidgets import QSizePolicy, QStyle, QWidget


def _blend(a, b, amount):
    return QColor(*(round(x * (1 - amount) + y * amount) for x, y in
                    zip(a.getRgb()[:3], b.getRgb()[:3])))


class DbScale(QWidget):
    """One common, read-only scale for both sides of a stereo pair."""
    ticks = ((255, '0'), (231, '−12'), (207, '−24'), (183, '−36'),
             (159, '−48'), (135, '−60'), (110, '−∞'))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 174)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def tick_position(self, raw):
        return 14 + QStyle.sliderPositionFromValue(110, 255, raw, self.height() - 28, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        palette = self.palette()
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        background = palette.color(group, QPalette.ColorRole.Button)
        foreground = palette.color(group, QPalette.ColorRole.WindowText)
        painter.setPen(_blend(background, foreground, .66))
        font = self.font()
        font.setPointSizeF(max(8, font.pointSizeF() - 2))
        painter.setFont(font)
        for raw, caption in self.ticks:
            position = self.tick_position(raw)
            painter.drawText(QRectF(0, position - 9, self.width(), 18),
                             Qt.AlignmentFlag.AlignCenter, caption)


class StereoLevels(QWidget):
    """Own two existing faders, meters and readouts, preserving L/R order.

    The 150px group remains centred when its parent grows. Track positions
    do not depend on readout contents, so e.g. 0.0 → −0.5 → −∞ cannot move
    either channel. Faders keep their own numeric/drag implementation.
    """
    group_width = 150
    track_height = 174
    readout_gap = 4
    readout_width = 48

    def __init__(self, faders, meters, displays, parent=None):
        super().__init__(parent)
        if any(len(items) != 2 for items in (faders, meters, displays)):
            raise ValueError('StereoLevels needs two faders, meters and readouts')
        self.faders = tuple(faders)
        self.meters = tuple(meters)
        self.displays = tuple(displays)
        self.setMinimumWidth(self.group_width)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scale = DbScale(self)
        self._readout_height = 28
        for fader in self.faders:
            fader.draw_scale = False
            fader.setParent(self)
            fader.setFixedSize(28, self.track_height)
            fader.installEventFilter(self)
        # The rounded ends of each meter already mark the scale's endpoints.
        tick_levels = tuple((raw - 255) * 0.5 for raw, _ in self.scale.ticks[1:-1])
        for channel, meter in enumerate(self.meters):
            meter.setParent(self)
            meter.setFixedSize(8, self.track_height)
            meter.set_ticks(tick_levels, on_right=channel == 0)
        for display in self.displays:
            display.setParent(self)
            display.setAlignment(Qt.AlignmentFlag.AlignCenter)
            display.installEventFilter(self)
        self._update_metrics()
        self._sync_scale_enabled()

    def _update_metrics(self):
        # Font height is stable while numbers change. Never use text width
        # or QLabel.sizeHint(), which would make a new value affect layout.
        self._readout_height = max(28, *(math.ceil(display.fontMetrics().height() + 10)
                                         for display in self.displays))
        self.setFixedHeight(self.track_height + self.readout_gap + self._readout_height)
        self._position_children()
        self.updateGeometry()

    def _sync_scale_enabled(self):
        self.scale.setEnabled(any(fader.isEnabled() for fader in self.faders))

    def _position_children(self):
        offset = (self.width() - self.group_width) // 2
        for fader, x in zip(self.faders, (10, 112)):
            fader.move(offset + x, 0)
        for meter, x in zip(self.meters, (46, 96)):
            meter.move(offset + x, 0)
        self.scale.move(offset + (self.group_width - self.scale.width()) // 2, 0)
        for display, x in zip(self.displays, (0, 102)):
            display.setGeometry(offset + x, self.track_height + self.readout_gap,
                                self.readout_width, self._readout_height)

    def sizeHint(self):
        return QSize(self.group_width, self.track_height + self.readout_gap + self._readout_height)

    def minimumSizeHint(self):
        return self.sizeHint()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_children()

    def eventFilter(self, watched, event):
        if watched in self.faders and event.type() == QEvent.Type.EnabledChange:
            self._sync_scale_enabled()
        if watched in self.displays and event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._update_metrics()
        return False
