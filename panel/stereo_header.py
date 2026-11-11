# SPDX-License-Identifier: GPL-3.0-or-later
"""Stereo labels and a link button aligned with the actual fader axes."""
from PyQt6 import sip
from PyQt6.QtCore import QEvent, QPoint, QSize, Qt, QTimer
from PyQt6.QtWidgets import QLabel, QSizePolicy, QWidget


class StereoHeader(QWidget):
    """A 24px header for two vertical faders and an existing link button.

    The faders retain their parents. Only ``link`` is reparented here;
    its checked state and signal connections are preserved. Positioning is
    entirely within the common Qt top-level, so window-system coordinates
    and the compositor's window position are irrelevant.
    """

    _geometry_events = frozenset((
        QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show,
        QEvent.Type.ShowToParent, QEvent.Type.LayoutRequest,
        QEvent.Type.ParentChange, QEvent.Type.FontChange,
        QEvent.Type.StyleChange,
    ))

    def __init__(self, faders, link, parent=None):
        super().__init__(parent)
        if len(faders) != 2 or not all(isinstance(fader, QWidget) for fader in faders):
            raise ValueError('StereoHeader needs exactly two faders')
        self.faders = tuple(faders)
        self.link = link
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.labels = []
        for text in ('L', 'R'):
            label = QLabel(text, self)
            label.setObjectName('channel')
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setFixedSize(17, 24)
            self.labels.append(label)
        self.link.setParent(self)
        self.link.setFixedSize(24, 22)
        self._watched = set()
        self._position_timer = QTimer(self)
        self._position_timer.setSingleShot(True)
        self._position_timer.setInterval(0)
        self._position_timer.timeout.connect(self._position_children)
        self._schedule_position()

    def sizeHint(self):
        return QSize(112, 24)

    def minimumSizeHint(self):
        # The fader row owns the minimum width; this header must not widen it.
        return QSize(0, 24)

    def _schedule_position(self):
        if not self._position_timer.isActive():
            self._position_timer.start()

    def _watch_geometry(self):
        watched = set()
        for leaf in (self, *self.faders):
            if sip.isdeleted(leaf):
                continue
            widget = leaf
            while widget is not None:
                if widget is not self:
                    watched.add(widget)
                widget = widget.parentWidget()
        for widget in self._watched - watched:
            if not sip.isdeleted(widget):
                widget.removeEventFilter(self)
        for widget in watched - self._watched:
            widget.installEventFilter(self)
        self._watched = watched

    def axis_positions(self):
        """Return the two logical x coordinates, or None while unparented."""
        top = self.window()
        positions = []
        for fader in self.faders:
            if sip.isdeleted(fader) or fader.window() is not top:
                return None
            axis = fader.mapTo(top, QPoint(round(fader.rail_x()), 0))
            positions.append(self.mapFrom(top, axis).x())
        return tuple(positions)

    def _position_children(self):
        self._watch_geometry()
        axes = self.axis_positions()
        for child in (*self.labels, self.link):
            child.setVisible(axes is not None)
        if axes is None:
            return
        for label, axis in zip(self.labels, axes):
            label.move(round(axis - label.width() / 2), 0)
        middle = (axes[0] + axes[1]) / 2
        self.link.move(round(middle - self.link.width() / 2), 1)

    def eventFilter(self, watched, event):
        if event.type() in self._geometry_events:
            self._schedule_position()
        return False

    def event(self, event):
        result = super().event(event)
        if (event.type() in self._geometry_events and
                hasattr(self, '_position_timer')):
            self._schedule_position()
        return result
