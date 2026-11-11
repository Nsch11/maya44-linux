# SPDX-License-Identifier: GPL-3.0-or-later
"""Narrow read-only level display. Samples are supplied by the caller.

This module never opens an audio device. A peak byte is displayed as
20*log10(raw/255), using the original Windows panel's -48…0 dBFS range.
"""
import math
import time

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QApplication, QWidget


def _blend(a, b, amount):
    return QColor(*(round(x * (1 - amount) + y * amount) for x, y in
                    zip(a.getRgb()[:3], b.getRgb()[:3])))


class LevelMeter(QWidget):
    """8×174 signal meter with fast attack, decay, peak hold and a max-code flag.

    ``set_peak(raw, available=True)`` accepts an unsigned 8-bit peak value.
    Zero means measured digital silence; ``None`` or ``available=False``
    means that no measurement is available. ``set_available(False)`` clears
    all displayed state immediately. Disabled widgets are also always empty.

    Feed each new device measurement, including zeros, rather than feeding
    only changed values. Without fresh measurements for ``stale_timeout``
    seconds, the meter clears instead of presenting an old level as live.
    """

    availabilityChanged = pyqtSignal(bool)

    def __init__(self, parent=None, *, width=8, height=174, minimum_db=-48.0,
                 stale_timeout=0.75):
        super().__init__(parent)
        self.setFixedSize(width, height)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.minimum_db = float(minimum_db)
        if not self.minimum_db < 0:
            raise ValueError('Meter minimum must be below 0 dBFS')
        self.stale_timeout = float(stale_timeout)
        if self.stale_timeout <= 0:
            raise ValueError('Meter stale timeout must be positive')
        self.decay_db_per_second = 30.0
        self.peak_decay_db_per_second = 18.0
        self.peak_hold_seconds = 1.0
        self.clip_hold_seconds = 1.5
        self._available = False
        self._raw = None
        self._target_db = self.minimum_db
        self._display_db = self.minimum_db
        self._held_db = self.minimum_db
        self._hold_until = 0.0
        self._clip_until = 0.0
        self._last_sample = 0.0
        self._last_tick = time.monotonic()
        self._tick_levels = ()
        self._ticks_on_right = False
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)

    @property
    def available(self):
        return self._available and self._raw is not None and self.isEnabled()

    @property
    def peak_byte(self):
        return self._raw if self.available else None

    @property
    def level_db(self):
        """Latest sample in dBFS, or None for silence/unavailable."""
        if not self.available or self._raw == 0:
            return None
        return 20.0 * math.log10(self._raw / 255.0)

    @property
    def held_peak_db(self):
        return self._held_db if self.available else None

    @property
    def clipped(self):
        """Whether a full-scale code was seen during the hold interval.

        A maximum byte indicates the meter's measurement ceiling. It alone
        cannot distinguish a sample exactly at full scale from overloading.
        """
        return self.available and time.monotonic() < self._clip_until

    def set_peak(self, raw, *, available=True):
        if raw is None or not available:
            self.set_available(False)
            return
        if type(raw) is not int or not 0 <= raw <= 255:
            raise ValueError('Peak must be an unsigned byte')
        if not self.isEnabled():
            self._clear()
            return
        now = time.monotonic()
        if not self._available or now - self._last_sample > self.stale_timeout:
            self._clear()
        became_available = not self._available or self._raw is None
        self._available = True
        self._raw = raw
        self._last_sample = now
        self._target_db = (max(self.minimum_db, 20.0 * math.log10(raw / 255.0))
                           if raw else self.minimum_db)
        # A brief peak must appear even if it falls between animation ticks.
        self._display_db = max(self._display_db, self._target_db)
        if self._target_db >= self._held_db:
            self._held_db = self._target_db
            self._hold_until = now + self.peak_hold_seconds
        if raw == 255:
            self._clip_until = now + self.clip_hold_seconds
        if became_available:
            self.availabilityChanged.emit(True)
        self._sync_timer()
        self.update()

    def set_available(self, available):
        if not available:
            self._clear()
        # Becoming available without a sample must not invent a level.
        self.update()

    def _clear(self):
        had_data = self._available and self._raw is not None
        self._available = False
        self._raw = None
        self._target_db = self._display_db = self._held_db = self.minimum_db
        self._hold_until = self._clip_until = 0.0
        self._timer.stop()
        if had_data:
            self.availabilityChanged.emit(False)

    def _sync_timer(self):
        on = self.available and self.isVisible()
        if on and not self._timer.isActive():
            self._last_tick = time.monotonic()
            self._timer.start()
        elif not on:
            self._timer.stop()

    def _advance(self):
        now = time.monotonic()
        elapsed = min(0.25, max(0.0, now - self._last_tick))
        self._last_tick = now
        if not self.available or now - self._last_sample > self.stale_timeout:
            self._clear()
        else:
            self._display_db = max(self._target_db,
                                   self._display_db - self.decay_db_per_second * elapsed)
            if now > self._hold_until:
                self._held_db = max(self._display_db,
                                    self._held_db - self.peak_decay_db_per_second * elapsed)
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._advance()
        self._sync_timer()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange:
            if not self.isEnabled():
                self._clear()
            self._sync_timer()
        if event.type() in (QEvent.Type.EnabledChange, QEvent.Type.PaletteChange):
            self.update()
        super().changeEvent(event)

    def _position(self, db):
        top, bottom = 14.0, self.height() - 14.0
        fraction = (min(0.0, max(self.minimum_db, db)) - self.minimum_db) / -self.minimum_db
        return bottom - fraction * (bottom - top)

    def set_ticks(self, levels, *, on_right):
        self._tick_levels = tuple(levels)
        self._ticks_on_right = on_right
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        background, foreground = palette.button().color(), palette.windowText().color()
        dark = background.lightnessF() < 0.5
        app = QApplication.instance()
        accent = app.property('maya44eMeterColor') if app is not None else None
        if not isinstance(accent, QColor) or not accent.isValid():
            accent = palette.link().color()
        warning = QColor('#e8b85b' if dark else '#a66b00')
        overload = QColor('#ff8d99' if dark else '#a72b3a')
        track = QRectF(1, 14, self.width() - 2, self.height() - 28)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_blend(background, foreground, .11 if self.available else .07))
        painter.drawRoundedRect(track, 2.5, 2.5)
        clip = QPainterPath()
        clip.addRoundedRect(track, 2.5, 2.5)
        painter.save()
        painter.setClipPath(clip)
        if self.available:
            y = self._position(self._display_db)
            painter.fillRect(QRectF(track.left(), y, track.width(), track.bottom() - y), accent)
            # Semantic upper bands stay distinct under either Breeze palette.
            for threshold, color in ((-6.0, warning), (-1.0, overload)):
                band_bottom = self._position(threshold)
                if y < band_bottom:
                    painter.fillRect(QRectF(track.left(), y, track.width(), band_bottom - y), color)
        tick = QColor(foreground)
        tick.setAlphaF(.32)
        tick_width = track.width() / 2
        tick_x = track.center().x() if self._ticks_on_right else track.left()
        for db in self._tick_levels:
            painter.fillRect(QRectF(tick_x, round(self._position(db)) - .5, tick_width, 1), tick)
        if self.available and self._held_db > self.minimum_db:
            marker = self._position(self._held_db)
            painter.setPen(QPen(foreground, 1.5))
            painter.drawLine(QPointF(track.left(), marker), QPointF(track.right(), marker))
        painter.restore()
        if self.clipped:
            painter.fillRect(QRectF(1, 5, self.width() - 2, 4), overload)
