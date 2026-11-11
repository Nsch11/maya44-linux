# SPDX-License-Identifier: GPL-3.0-or-later
"""Breeze widgets and local appearance preferences. No audio transport here."""
from i18n import translate, localize, number

import configparser
import math
import os
from pathlib import Path

from PyQt6.QtCore import QEvent, QFileSystemWatcher, QObject, QPointF, QRectF, QSettings, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QImageReader, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDial, QDialog, QFrame, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QSlider, QSpinBox, QStyle, QStyleFactory, QToolButton, QVBoxLayout, QWidget,
)
from level_meter import LevelMeter
from stereo_header import StereoHeader
from stereo_levels import StereoLevels


def blend(a, b, amount):
    return QColor(*(round(x * (1 - amount) + y * amount) for x, y in
                    zip(a.getRgb()[:3], b.getRgb()[:3])))


def label(text, role='', wrap=False):
    widget = QLabel('' if callable(text) else text)
    if callable(text):
        localize(widget, 'setText', text)
    widget.setObjectName(role)
    widget.setWordWrap(wrap)
    return widget


def divider():
    widget = QFrame()
    widget.setObjectName('divider')
    widget.setFixedHeight(1)
    return widget


def pair_badge(pair):
    widget = label(pair.replace('–', '/'), 'pairBadge')
    widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
    widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    return widget


class StereoLink(QToolButton):
    def __init__(self):
        super().__init__()
        self.setObjectName('stereoLink')
        self.setCheckable(True)
        self.setChecked(True)
        self.setAutoRaise(True)
        self.setIconSize(QSize(18, 18))
        self._icon_ready = True
        self._refresh_icon()

    def _refresh_icon(self):
        # Original Lucide link-2 geometry; tint its alpha mask with Qt roles.
        path = Path(__file__).resolve().parent / 'assets/link-2.svg'
        palette = self.palette()
        icon = QIcon()
        for size in (18, 24, 36, 48):
            reader = QImageReader(str(path))
            reader.setScaledSize(QSize(size, size))
            source = QPixmap.fromImage(reader.read())
            for mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
                for state in (QIcon.State.Off, QIcon.State.On):
                    pixmap = QPixmap(source)
                    painter = QPainter(pixmap)
                    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                    color = (palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText)
                             if mode == QIcon.Mode.Disabled else palette.buttonText().color())
                    painter.fillRect(pixmap.rect(), color)
                    painter.end()
                    icon.addPixmap(pixmap, mode, state)
        self.setIcon(icon)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and getattr(self, '_icon_ready', False):
            self._refresh_icon()


def stereo_link():
    return StereoLink()


def asset_icon(path):
    # Render multicolour artwork directly; KDE's icon engine may apply its
    # symbolic colour scheme to solid fills in an arbitrary SVG file.
    icon = QIcon()
    for size in (16, 22, 32, 48, 64, 128):
        reader = QImageReader(str(path))
        reader.setScaledSize(QSize(size, size))
        icon.addPixmap(QPixmap.fromImage(reader.read()))
    return icon


def toolbar_icon(name, palette):
    # Extra trailing space belongs to the mute icon's canvas, so the native
    # button keeps a comfortable gap without adding spaces to its label.
    pixmap = QPixmap(40 if name == 'menu' else 48, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(2)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(palette.windowText().color(), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    if name == 'menu':
        for y in (5, 10, 15):
            painter.drawLine(QPointF(3, y), QPointF(17, y))
    else:
        painter.translate(0, 1.3)
        painter.drawLine(QPointF(3, 7), QPointF(6, 7))
        painter.drawLine(QPointF(3, 7), QPointF(3, 13))
        painter.drawLine(QPointF(3, 13), QPointF(6, 13))
        painter.drawLine(QPointF(6, 7), QPointF(10, 3))
        painter.drawLine(QPointF(10, 3), QPointF(10, 17))
        painter.drawLine(QPointF(10, 17), QPointF(6, 13))
        painter.drawLine(QPointF(14, 7), QPointF(18, 13))
        painter.drawLine(QPointF(18, 7), QPointF(14, 13))
    painter.end()
    return QIcon(pixmap)


class ThemeManager(QObject):
    changed = pyqtSignal()
    wheel_changed = pyqtSignal(float)

    def __init__(self, app, persist=False, config_dir=None):
        super().__init__(app)
        self.app = app
        self.persist = persist
        self.config_dir = Path(config_dir or os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        self.kde_config = self.config_dir / 'kdeglobals'
        self.settings = QSettings(str(self.config_dir / 'maya44e/panel.ini'), QSettings.Format.IniFormat)
        mode = self.settings.value('appearance/theme', 'system') if persist else 'system'
        self.mode = mode if mode in ('system', 'light', 'dark') else 'system'
        try:
            wheel = float(self.settings.value('controls/wheel_db', 0.5)) if persist else 0.5
        except (ValueError, TypeError):
            wheel = 0.5
        self.wheel_db = wheel if wheel in (0.5, 1, 2, 3) else 0.5
        self._last_kde = None
        self._applying = False
        if 'breeze' in [key.lower() for key in QStyleFactory.keys()]:
            app.setStyle('breeze')
        self.base_palette = app.style().standardPalette()
        self.watcher = QFileSystemWatcher(self)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.system_changed)
        self.watcher.fileChanged.connect(lambda _: self.timer.start())
        self.watcher.directoryChanged.connect(lambda _: self.timer.start())
        self._watch()
        self.reload()

    def _watch(self):
        # KDE replaces kdeglobals atomically. Watching its directory lets us
        # attach to the new inode after every theme change.
        for path in (self.config_dir, self.kde_config):
            if path.exists() and str(path) not in self.watcher.files() + self.watcher.directories():
                self.watcher.addPath(str(path))

    def system_changed(self):
        self._watch()
        try:
            text = self.kde_config.read_text()
        except OSError:
            text = ''
        if text != self._last_kde:
            self.reload()

    def set_mode(self, mode, save=True):
        if mode not in ('system', 'light', 'dark'):
            raise ValueError('Unknown theme')
        self.mode = mode
        if save and self.persist:
            self.settings.setValue('appearance/theme', mode)
            self.settings.sync()
        self.reload()

    def set_wheel(self, value):
        value = float(value)
        if value not in (0.5, 1, 2, 3):
            raise ValueError('Unknown wheel step')
        self.wheel_db = value
        if self.persist:
            self.settings.setValue('controls/wheel_db', value)
            self.settings.sync()
        self.wheel_changed.emit(value)

    def reload(self):
        if self._applying:
            return
        self._applying = True
        try:
            config = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                self._last_kde = self.kde_config.read_text()
                config.read_string(self._last_kde)
            except (OSError, configparser.Error):
                self._last_kde = ''
                config.clear()
            font = QFont()
            description = config.get('General', 'font', fallback='')
            if description and font.fromString(description):
                self.app.setFont(font)
            if self.mode != 'system':
                config.clear()
                config.read(f'/usr/share/color-schemes/Breeze{self.mode.title()}.colors')
            palette = QPalette(self.base_palette)
            roles = {
                'Window': ('Window', 'BackgroundNormal'),
                'WindowText': ('Window', 'ForegroundNormal'),
                'Base': ('View', 'BackgroundNormal'),
                'AlternateBase': ('View', 'BackgroundAlternate'),
                'Text': ('View', 'ForegroundNormal'),
                'Button': ('Button', 'BackgroundNormal'),
                'ButtonText': ('Button', 'ForegroundNormal'),
                'Highlight': ('Selection', 'BackgroundNormal'),
                'HighlightedText': ('Selection', 'ForegroundNormal'),
                'ToolTipBase': ('Tooltip', 'BackgroundNormal'),
                'ToolTipText': ('Tooltip', 'ForegroundNormal'),
                'Link': ('View', 'ForegroundLink'),
                'LinkVisited': ('View', 'ForegroundVisited'),
                'PlaceholderText': ('View', 'ForegroundInactive'),
                'Accent': ('Selection', 'BackgroundNormal'),
            }
            for role, (group, key) in roles.items():
                try:
                    parts = [int(x) for x in config.get(f'Colors:{group}', key).split(',')]
                    if len(parts) not in (3, 4) or any(x < 0 or x > 255 for x in parts):
                        continue
                    palette.setColor(getattr(QPalette.ColorRole, role), QColor(*parts))
                except (configparser.Error, ValueError):
                    pass
            for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                         QPalette.ColorRole.ButtonText, QPalette.ColorRole.Highlight):
                palette.setColor(QPalette.ColorGroup.Disabled, role,
                                 blend(palette.color(role), palette.window().color(), 0.55))
            meter_color = palette.link().color()
            try:
                parts = [int(x) for x in config.get('Colors:View', 'ForegroundPositive').split(',')]
                if len(parts) in (3, 4) and all(0 <= x <= 255 for x in parts):
                    meter_color = QColor(*parts)
            except (configparser.Error, ValueError):
                pass
            self.app.setProperty('maya44eMeterColor', meter_color)
            self.app.setPalette(palette)
            QIcon.setThemeName('breeze-dark' if palette.window().color().lightnessF() < 0.5 else 'breeze')
            self.app.setStyleSheet(self.stylesheet(palette))
        finally:
            self._applying = False
        self.changed.emit()

    def stylesheet(self, palette):
        text = palette.windowText().color()
        surface = palette.button().color()
        accent = palette.highlight().color()
        edge = blend(surface, text, 0.17).name()
        secondary = blend(surface, text, 0.68).name()
        muted = blend(surface, text, 0.46).name()
        tint = blend(surface, accent, 0.13).name()
        accent_text = blend(text, accent, 0.45).name()
        pair_edge = blend(surface, accent, 0.42).name()
        dark = surface.lightnessF() < 0.5
        error = '#ff8d99' if dark else '#a72b3a'
        success = '#7ed8a0' if dark else '#247343'
        return f'''
            QMainWindow {{ background: palette(window); }}
            QScrollArea {{ border: none; background: transparent; }}
            QScrollArea > QWidget > QWidget {{ background: palette(window); }}
            QFrame#card {{ background: palette(button); border: 1px solid {edge}; border-radius: 12px; }}
            QLabel#title {{ font-size: 21px; font-weight: 700; }}
            QLabel#cardTitle {{ font-size: 19px; font-weight: 600; }}
            QLabel#sectionTitle {{ font-size: 15px; font-weight: 600; }}
            QLabel#secondary {{ color: {secondary}; }}
            QLabel#caption {{ color: {secondary}; font-size: 12px; }}
            QLabel#digital {{ color: {secondary}; background: palette(window); border: 1px solid {edge}; border-radius: 5px; padding: 3px 2px; font-size: 13px; font-weight: 500; }}
            QLabel#digitalSignal {{ color: {secondary}; background: palette(window); border: 1px solid {edge}; border-radius: 5px; padding: 6px; font-size: 13px; font-weight: 500; }}
            QLabel#digitalSignal[state="locked"]:enabled {{ color: {success}; border-color: {pair_edge}; }}
            QLabel#mixReadout {{ color: {secondary}; background: palette(window); border: 1px solid {edge}; border-radius: 5px; padding: 2px 7px; font-size: 12px; font-weight: 500; }}
            QLabel#pairBadge {{ color: palette(window-text); background: {tint}; border: 1px solid {pair_edge}; border-radius: 7px; padding: 4px 17px; font-size: 15px; font-weight: 600; }}
            QLabel#channel {{ color: {secondary}; font-size: 12px; font-weight: 600; }}
            QLabel#badge {{ color: {accent_text}; background: {tint}; border-radius: 6px; padding: 5px 9px; font-size: 12px; }}
            QLabel#number {{ color: {accent_text}; background: {tint}; border-radius: 9px; padding: 8px 12px; font-size: 21px; font-weight: 600; }}
            QLabel#success {{ color: {success}; }}
            QLabel#error {{ color: {error}; }}
            QLabel#connectionStatus {{ color: {secondary}; font-size: 12px; }}
            QLabel#connectionStatus[state="connected"] {{ color: {success}; }}
            QLabel#connectionStatus[state="error"] {{ color: {error}; }}
            QLabel:disabled {{ color: {muted}; }}
            QFrame#divider {{ background: {edge}; border: none; }}
            QPushButton {{ padding: 6px 12px; }}
            QPushButton#route, QPushButton#source {{ padding: 4px 8px; border: 1px solid {edge}; border-radius: 5px; }}
            QPushButton#route:checked, QPushButton#source:checked {{ color: {accent_text}; border-color: palette(highlight); background: {tint}; }}
            QPushButton#route:hover:enabled, QPushButton#source:hover:enabled {{ border-color: palette(highlight); background: {tint}; }}
            QPushButton#route:focus:enabled, QPushButton#source:focus:enabled {{ border: 1px dashed palette(window-text); }}
            QPushButton#route:disabled, QPushButton#source:disabled {{ color: {muted}; border-color: {edge}; background: palette(button); }}
            QPushButton#pairMute {{ padding: 4px 10px; border: 1px solid {edge}; border-radius: 5px; background: palette(button); }}
            QPushButton#quiet {{ border: 1px solid {edge}; border-radius: 5px; background: palette(button); }}
            QPushButton#pairMute:hover:enabled, QPushButton#quiet:hover:enabled {{ background: {tint}; }}
            QPushButton#pairMute:checked {{ color: {error}; border: 1px solid {error}; border-radius: 5px; background: palette(button); }}
            QPushButton#quiet:checked {{ color: {error}; border: 1px solid {error}; border-radius: 5px; background: palette(button); }}
            QPushButton#pairMute:focus:enabled, QPushButton#quiet:focus:enabled {{ border: 1px dashed palette(window-text); border-radius: 5px; }}
            QPushButton#linkButton {{ border: 1px solid transparent; color: {accent_text}; padding: 0px; text-align: left; font-size: 12px; font-weight: 400; }}
            QPushButton#linkButton:focus {{ border: 1px dashed palette(window-text); }}
            QToolButton#stereoLink {{ border: 1px solid transparent; border-radius: 4px; padding: 0; }}
            QToolButton#stereoLink:checked {{ border-color: palette(highlight); background: {tint}; }}
            QToolButton#stereoLink:hover:enabled {{ border-color: palette(highlight); }}
            QToolButton#stereoLink:focus:enabled {{ border: 1px dashed palette(window-text); }}
            QToolButton#stereoLink:disabled {{ border-color: {edge}; background: palette(button); }}
            QTabBar::tab {{ padding: 10px 20px; margin-right: 8px; border: none; border-bottom: 3px solid transparent; color: {secondary}; }}
            QTabBar::tab:selected {{ color: palette(window-text); border-bottom: 3px solid palette(highlight); }}
            QTabBar::tab:hover {{ background: {tint}; }}
            QTabBar::tab:focus {{ border-top: 1px solid palette(highlight); }}
            QComboBox {{ padding: 4px 8px; min-width: 100px; }}
            QSpinBox {{ padding: 4px; }}
        '''


class ContentDialog(QDialog):
    """Respect wrapped text when sizing a settings dialog and changing locale."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_content)

    def _fit_content(self):
        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        height = layout.minimumSize().height()
        if layout.hasHeightForWidth():
            height = max(height, layout.totalHeightForWidth(max(self.width(), self.minimumWidth())))
        if height != self.minimumHeight():
            self.setMinimumHeight(height)

    def event(self, event):
        result = super().event(event)
        if (event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Resize, QEvent.Type.Show)
                and hasattr(self, '_fit_timer')):
            self._fit_timer.start(0)
        return result


class Card(QFrame):
    def __init__(self, title='', subtitle=''):
        super().__init__()
        self.setObjectName('card')
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(22, 20, 22, 20)
        self.body.setSpacing(14)
        if title:
            self.body.addWidget(label(title, 'cardTitle'))
        if subtitle:
            self.body.addWidget(label(subtitle, 'secondary', True))


class Fader(QSlider):
    """A dB scale, with one explicit step per wheel detent, independent of KDE's scroll-lines setting."""
    equalize = pyqtSignal()

    def __init__(self, orientation=Qt.Orientation.Vertical):
        super().__init__(orientation)
        self.wheel_units = lambda: 1
        self._wheel_remainder = 0
        self._drag_offset = None
        self.draw_scale = True
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if not self.isEnabled():
            event.ignore()
            return
        amount = event.angleDelta().y() or event.angleDelta().x()
        if not amount:
            amount = (event.pixelDelta().y() or event.pixelDelta().x()) * 3
        self._wheel_remainder += amount
        steps = math.trunc(self._wheel_remainder / 120)
        self._wheel_remainder -= steps * 120
        if steps:
            self.setValue(self.value() + steps * max(1, round(self.wheel_units())))
        event.accept()

    def _position(self, value):
        return 14 + QStyle.sliderPositionFromValue(self.minimum(), self.maximum(), value, self.height() - 28, True)

    def rail_x(self):
        return self.width() - 15 if self.draw_scale else self.width() / 2

    def paintEvent(self, event):
        if self.orientation() == Qt.Orientation.Horizontal:
            return super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        text, bg = pal.windowText().color(), pal.button().color()
        accent = pal.highlight().color() if self.isEnabled() else blend(bg, text, 0.3)
        secondary = blend(bg, text, 0.66)
        x = self.rail_x()
        top, bottom = 14, self.height() - 14
        # A round-capped line extends 2.5px beyond each end. Draw inside the
        # same 14px insets as LevelMeter instead of lengthening the rail.
        track = QRectF(x - 2.5, top, 5, bottom - top)
        clip = QPainterPath()
        clip.addRoundedRect(track, 2.5, 2.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(blend(bg, text, 0.16))
        painter.drawPath(clip)
        y = self._position(self.value())
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(QRectF(track.left(), y, track.width(), bottom - y), accent)
        painter.restore()
        font = painter.font()
        font.setPointSizeF(max(8, font.pointSizeF() - 2))
        painter.setFont(font)
        for raw, text_value in ([(255, '0'), (231, '−12'), (207, '−24'), (183, '−36'), (159, '−48'), (135, '−60'), (110, '−∞')] if self.draw_scale else []):
            pos = self._position(raw)
            painter.setPen(secondary)
            painter.drawText(QRectF(0, pos - 9, x - 14, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text_value)
            painter.drawLine(QPointF(x - 10, pos), QPointF(x - 7, pos))
        painter.setPen(QPen(accent if self.hasFocus() else blend(bg, text, 0.4), 1.5))
        painter.setBrush(blend(bg, text, 0.10))
        painter.drawRoundedRect(QRectF(x - 13, y - 8, 26, 16), 4, 4)
        painter.setPen(QPen(accent, 2))
        painter.drawLine(QPointF(x - 8, y), QPointF(x + 8, y))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.equalize.emit()
            event.accept()
        elif self.orientation() == Qt.Orientation.Vertical and event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            y = self._position(self.value())
            if abs(event.position().y() - y) <= 12:
                self._drag_offset = event.position().y() - y
                self.setSliderDown(True)
            else:
                # Clicking the scale takes a small page step; it cannot jump
                # instantly from a quiet level to full volume.
                self.setValue(self.value() + (self.pageStep() if event.position().y() < y else -self.pageStep()))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            pos = round(event.position().y() - self._drag_offset - 14)
            self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), pos, self.height() - 28, True))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_offset is not None:
            self._drag_offset = None
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class MixDial(QDial):
    """Compact level knob. Drag vertically; never jump on a click."""
    wheelEvent = Fader.wheelEvent

    def __init__(self):
        super().__init__()
        self.setRange(0, 48)
        self.setFixedSize(58, 58)
        self.setNotchesVisible(False)
        self.wheel_units = lambda: 1
        self._wheel_remainder = 0
        self._drag = None

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        bg, fg = pal.button().color(), pal.windowText().color()
        accent = pal.highlight().color() if self.isEnabled() else blend(bg, fg, .25)
        rect = QRectF(5, 5, 48, 48)
        p.setPen(QPen(blend(bg, fg, .15), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 225 * 16, -270 * 16)
        portion = (self.value() - self.minimum()) / max(1, self.maximum() - self.minimum())
        p.setPen(QPen(accent, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, 225 * 16, round(-270 * 16 * portion))
        p.setBrush(blend(bg, fg, .055))
        p.setPen(QPen(accent if self.hasFocus() else blend(bg, fg, .22), 1))
        p.drawEllipse(QRectF(12, 12, 34, 34))
        angle = math.radians(225 - 270 * portion)
        p.setPen(QPen(accent, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(29 + 7 * math.cos(angle), 29 - 7 * math.sin(angle)),
                   QPointF(29 + 13 * math.cos(angle), 29 - 13 * math.sin(angle)))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._drag = (event.position().y(), self.value())
            self.setSliderDown(True)
            event.accept()
        else:
            event.ignore()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            y, value = self._drag
            self.setValue(value + round((y - event.position().y()) / 3))
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag = None
        self.setSliderDown(False)
        event.accept()


class MixLines(QWidget):
    def __init__(self, pairs):
        super().__init__()
        self.pairs = pairs
        self.setFixedHeight(32)
        for pair in pairs:
            pair.mix_on.toggled.connect(self.update)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        x1, x2 = [self.mapFromGlobal(pair.mix_on.mapToGlobal(pair.mix_on.rect().center())).x() + .5 for pair in self.pairs]
        routes = [(self.pairs[1], x1, x2), (self.pairs[0], x2, x1)]
        routes.sort(key=lambda route: route[0].mix_on.isChecked() and route[0].mix_on.isEnabled())
        for pair, start, end in routes:
            active = pair.mix_on.isChecked() and pair.mix_on.isEnabled()
            color = pal.highlight().color() if active else blend(pal.button().color(), pal.windowText().color(), .18)
            path = QPainterPath(QPointF(start, 1))
            path.lineTo(start, 6)
            path.lineTo(end, 22)
            path.lineTo(end, 28)
            # A narrow break at the crossing separates the two routes.
            p.setPen(QPen(pal.button().color(), 5, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
            p.setPen(QPen(color, 1.6, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            p.drawPath(path)
            arrow = QPainterPath(QPointF(end - 3.5, 24.5))
            arrow.lineTo(end, 28)
            arrow.lineTo(end + 3.5, 24.5)
            p.drawPath(arrow)


class InputPreview(Card):
    """Compact input strip; controller attaches the ALSA transport."""
    def __init__(self, pair):
        super().__init__()
        self.setObjectName('strip')
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        self.form = QWidget()
        self.form.setToolTip('')
        form = QVBoxLayout(self.form)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        controls = self.controls = QWidget()
        controls.setEnabled(False)
        controls.setFixedHeight(161)
        top = QVBoxLayout(controls)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(3)
        sources = QHBoxLayout()
        sources.setSpacing(4)
        self.source_buttons = []
        for text in (('LINE', 'MIC') if pair == '1–2' else ('LINE', 'Hi-Z')):
            button = QPushButton(text)
            button.setObjectName('source')
            button.setCheckable(True)
            button.setChecked(text == 'LINE')
            localize(button, 'setToolTip', lambda text=text: translate('Panel', 'Line input') if text == 'LINE' else translate('Panel', 'Microphone') if text == 'MIC' else translate('Panel', 'Instrument input'))
            sources.addWidget(button)
            self.source_buttons.append(button)
        top.addLayout(sources)
        option = QHBoxLayout()
        self.phantom = None
        if pair == '1–2':
            self.phantom = localize(QPushButton(''), 'setText', lambda: translate('Panel', '+48 V'))
            self.phantom.setObjectName('source')
            self.phantom.setCheckable(True)
            localize(self.phantom, 'setToolTip', lambda: translate('Panel', '48 V phantom power; available only for MIC. Enable only for a compatible microphone'))
            option.addWidget(self.phantom)
        else:
            spacer = QWidget()
            spacer.setFixedHeight(29)
            option.addWidget(spacer)
        top.addLayout(option)
        gain = QHBoxLayout()
        self.gain = MixDial()
        self.gain.setRange(0, 48)
        localize(self.gain, 'setToolTip', lambda: translate('Panel', 'Input gain: 0 to +24 dB'))
        gain.addStretch()
        gain.addWidget(self.gain)
        gain.addStretch()
        top.addLayout(gain)
        caption = self.gain_caption = label(lambda: translate('Panel', 'Gain · — dB'), 'caption')
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(caption)
        form.addWidget(controls)
        heading = pair_badge(pair)
        form.addWidget(heading, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.faders, self.meters, self.displays = [], [], []
        for channel, text in enumerate(('L', 'R')):
            slider = Fader(Qt.Orientation.Vertical)
            slider.setEnabled(False)
            slider.setRange(110, 255)
            slider.setValue(190)
            meter = LevelMeter(minimum_db=-72.5)
            localize(meter, 'setAccessibleName', lambda pair=pair, text=text:
                     translate('Panel', 'Input {pair} signal level, channel {text}').format(pair=pair, text=text))
            self.meters.append(meter)
            display = label('—', 'digital')
            display.setEnabled(False)
            display.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.displays.append(display)
            self.faders.append(slider)
        self.levels = StereoLevels(self.faders, self.meters, self.displays)
        self.link = stereo_link()
        self.link.setEnabled(False)
        localize(self.link, 'setToolTip', lambda:
                 translate('Panel', 'Link L/R while preserving balance. Right-click a fader to equalize the channels'))
        localize(self.link, 'setAccessibleName', lambda pair=pair:
                 translate('Panel', 'Link L/R channels of input {pair}').format(pair=pair))
        self.channel_header = StereoHeader(self.faders[:2], self.link)
        form.addWidget(self.channel_header)
        form.addWidget(self.levels)
        self.monitor = QPushButton('Monitor')
        self.monitor.setEnabled(False)
        self.monitor.setObjectName('route')
        self.monitor.setCheckable(True)
        localize(self.monitor, 'setToolTip', lambda: translate('Panel', 'Analog input monitoring to the corresponding output pair. Analog input gain affects its level; digital capture attenuation does not'))
        form.addWidget(self.monitor)
        self.mute = QPushButton('Mute')
        self.mute.setEnabled(False)
        self.mute.setObjectName('pairMute')
        self.mute.setCheckable(True)
        form.addWidget(self.mute)
        self.body.addWidget(self.form)
