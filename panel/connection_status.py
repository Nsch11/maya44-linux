# SPDX-License-Identifier: GPL-3.0-or-later
"""A connection caption whose status dot follows the letter body's centre."""
import math

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QFontMetricsF, QPainter, QPalette
from PyQt6.QtWidgets import QLabel


class ConnectionStatus(QLabel):
    """Keep QLabel text/i18n semantics, but draw the dot independently.

    A font's black-circle glyph has its own baseline metrics. Aligning that
    glyph as ordinary text can put its centre below Cyrillic or Latin text.
    The circle here shares the cap-height centre of the letter body. Using
    the full ink bounds would let descenders (notably Cyrillic д) lift the
    main body of the caption above the dot.
    """
    dot_radius = 4.0
    dot_gap = 6.0

    def _caption(self):
        text = self.text()
        return text[1:].lstrip() if text.startswith('●') else text

    def sizeHint(self):
        self.ensurePolished()
        metrics = QFontMetricsF(self.font())
        text = self._caption()
        ink = metrics.tightBoundingRect(text)
        text_width = max(ink.width(), metrics.horizontalAdvance(text) - ink.left())
        margins = self.contentsMargins()
        padding = 2 * self.margin()
        return QSize(
            math.ceil(2 * self.dot_radius + self.dot_gap + text_width) + padding + margins.left() + margins.right(),
            math.ceil(max(2 * self.dot_radius, metrics.height())) + padding + margins.top() + margins.bottom(),
        )

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self.font())
        palette = self.palette()
        group = palette.currentColorGroup() if self.isEnabled() else QPalette.ColorGroup.Disabled
        color = palette.color(group, QPalette.ColorRole.WindowText)
        painter.setPen(color)
        painter.setBrush(color)

        rect = QRectF(self.contentsRect()).adjusted(self.margin(), self.margin(), -self.margin(), -self.margin())
        metrics = QFontMetricsF(self.font())
        text = self._caption()
        ink = metrics.tightBoundingRect(text)
        centre_y = rect.center().y()
        baseline_y = centre_y + metrics.capHeight() / 2
        baseline_x = rect.left() + 2 * self.dot_radius + self.dot_gap - ink.left()
        painter.drawText(QPointF(baseline_x, baseline_y), text)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(rect.left() + self.dot_radius, centre_y), self.dot_radius, self.dot_radius)
