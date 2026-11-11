#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Native Qt6 / Breeze / Wayland panel for the experimental MAYA44e driver.
# Started as an adaptation of maya22gui 2.0 (Paul Petrov, GPL v3 or later).
# The current interface uses vector-painted controls, without its artwork.
# See COPYING.txt for the license.
from i18n import LanguageManager, translate, localize, number

import argparse
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QSize, QThread, QTimer, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget,
)
from plasma_widgets import Card, ContentDialog, Fader, InputPreview, MixDial, MixLines, ThemeManager, asset_icon, divider, label, toolbar_icon, stereo_link, pair_badge
from alsa_backend import AlsaBackend, DemoBackend, move_stereo, validate
from playback_backend import PlaybackBackend, DemoPlaybackBackend, PERIODS, RATES, validate_settings
from level_meter import LevelMeter
from meter_backend import MeterWorker, PeakReader
from functools import partial
from card_discovery import MODELS, MayaDevice, select_device
from stereo_header import StereoHeader
from connection_status import ConnectionStatus
from stereo_levels import StereoLevels
from digital_controls import DigitalBox

BASE = Path(__file__).resolve().parent
ROUTES = ("out12.enabled", "out34.enabled")
ICON = BASE / "assets/maya44e.svg"
MUTED_ICON = BASE / "assets/maya44e-muted.svg"


class MixerWorker(QThread):
    snapshot = pyqtSignal(int, object)
    unavailable = pyqtSignal(str)
    playback_snapshot = pyqtSignal(object)
    playback_error = pyqtSignal(str)
    playback_finished = pyqtSignal(bool, str)

    def __init__(self, backend, playback):
        super().__init__()
        self.backend = backend
        self.playback = playback
        self.pending_playback = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.pending = {}
        self.revision = 0
        self.closing = False
        self.connected = False

    def submit(self, key, values):
        values = validate(key, values)
        with self.lock:
            if self.closing or not self.connected:
                return
            self.pending[key] = values
            self.revision += 1
        self.wake.set()

    def current_revision(self):
        with self.lock:
            return self.revision

    def submit_playback(self, settings):
        settings = validate_settings(settings)
        with self.lock:
            if self.closing or not self.connected:
                return False
            self.pending_playback = settings
        self.wake.set()
        return True

    def shutdown(self):
        with self.lock:
            self.closing = True
        self.wake.set()

    def run(self):
        next_read = 0.0
        next_playback_read = 0.0
        while True:
            self.wake.wait(0.05)
            self.wake.clear()
            # Coalesce a drag's events before making an external command.
            time.sleep(0.025)
            with self.lock:
                changes, self.pending = self.pending, {}
                revision, closing = self.revision, self.closing
                playback, self.pending_playback = self.pending_playback, None
            try:
                for key, values in changes.items():
                    self.backend.write(key, values)
                if closing:
                    return
                if changes or time.monotonic() >= next_read:
                    values = self.backend.read()
                    with self.lock:
                        fresh = revision == self.revision
                        self.connected = True
                    if fresh:
                        self.snapshot.emit(revision, values)
                    next_read = time.monotonic() + 0.7
            except Exception as exc:
                # A disconnect discards requests; reconnect must not replay them.
                with self.lock:
                    self.pending.clear()
                    self.revision += 1
                    self.connected = False
                self.unavailable.emit(str(exc))
                if closing:
                    return
                next_read = time.monotonic() + 0.7
            if playback is not None:
                try:
                    if not self.connected:
                        raise RuntimeError(translate("Panel", 'The device disconnected; settings were cancelled'))
                    result = self.playback.apply(playback)
                    self.playback_snapshot.emit(result)
                    self.playback_finished.emit(True, translate("Panel", 'Settings applied and saved'))
                except Exception as exc:
                    self.playback_finished.emit(False, str(exc))
                next_playback_read = 0.0
            if time.monotonic() >= next_playback_read:
                try:
                    self.playback_snapshot.emit(self.playback.read())
                except Exception as exc:
                    self.playback_error.emit(str(exc))
                next_playback_read = time.monotonic() + 1.5


class PairBox(Card):
    def __init__(self, window, pair, name, other, mix):
        super().__init__()
        self.setObjectName('strip')
        self.window, self.pair, self.mix = window, pair, mix
        self.faders, self.displays, self.meters = [], [], []
        outer = self.body
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        heading = pair_badge(name)
        outer.addWidget(heading, alignment=Qt.AlignmentFlag.AlignHCenter)
        for channel, text in enumerate(('L', 'R')):
            slider = Fader(Qt.Orientation.Vertical)
            slider.setRange(110, 255)
            slider.setSingleStep(1)
            slider.setPageStep(6)
            slider.wheel_units = lambda: window.theme.wheel_db * 2
            localize(slider, 'setAccessibleName', lambda name=name, text=text: translate('Panel', 'Output {name} volume, channel {text}').format(name=name, text=text))
            slider.valueChanged.connect(lambda raw, c=channel: self.changed(c, raw))
            slider.equalize.connect(lambda c=channel: self.equalize(c))
            meter = LevelMeter(minimum_db=-72.5)
            localize(meter, 'setAccessibleName', lambda name=name, text=text:
                     translate('Panel', 'Output {name} signal level, channel {text}').format(name=name, text=text))
            self.meters.append(meter)
            display = label('—', 'digital')
            display.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.faders.append(slider)
            self.displays.append(display)
        self.levels = StereoLevels(self.faders, self.meters, self.displays)
        self.link = stereo_link()
        localize(self.link, 'setAccessibleName', lambda name=name: translate('Panel', 'Link L/R channels of output {name}').format(name=name))
        localize(self.link, 'setToolTip', lambda: translate('Panel', 'Link L/R while preserving balance. Right-click a fader to equalize the channels'))
        self.channel_header = StereoHeader(self.faders[:2], self.link)
        outer.addWidget(self.channel_header)
        outer.addWidget(self.levels)
        self.direct = localize(QPushButton(''), 'setText', lambda: translate('Panel', 'Disable source'))
        localize(self.direct, 'setAccessibleName', lambda name=name: translate('Panel', 'Disable computer source for output {name}').format(name=name))
        self.direct.setObjectName('route')
        self.direct.setCheckable(True)
        localize(self.direct, 'setToolTip', lambda name=name:
                 translate('Panel', 'Disable computer playback for output {name}. When highlighted, this source is off. Incoming MIX remains available. The disabled source also stops feeding the other output’s MIX.').format(name=name))
        self.direct.toggled.connect(lambda disabled: window.write(f'{pair}.enabled', (int(not disabled),)))
        outer.addWidget(self.direct)
        self.mute = QPushButton('Mute')
        localize(self.mute, 'setAccessibleName', lambda name=name: translate('Panel', 'Mute output {name}').format(name=name))
        self.mute.setObjectName('pairMute')
        self.mute.setCheckable(True)
        localize(self.mute, 'setToolTip', lambda name=name: translate('Panel', 'Mute output {name}, including incoming MIX. Its own signal will also stop feeding the other pair. Click again to restore the previous routes without changing levels').format(name=name))
        self.mute.clicked.connect(lambda: window.silence_pair(self))
        outer.addWidget(self.mute)
        self.mix_on = QPushButton('MIX')
        localize(self.mix_on, 'setAccessibleName', lambda name=name, other=other: translate('Panel', 'MIX from {other} to output {name}').format(other=other, name=name))
        self.mix_on.setMinimumWidth(90)
        self.mix_on.setObjectName('route')
        self.mix_on.setCheckable(True)
        localize(self.mix_on, 'setToolTip', lambda name=name, other=other: translate('Panel', 'MIX: add {other} to output {name}').format(other=other, name=name))
        self.mix_on.toggled.connect(lambda on: window.write(f'{mix}.enabled', (int(on),)))
        dial = MixDial()
        dial.wheel_units = lambda: max(1, window.theme.wheel_db)
        localize(dial, 'setAccessibleName', lambda name=name, other=other: translate('Panel', 'MIX level from {other} to {name}').format(other=other, name=name))
        dial.valueChanged.connect(lambda raw: self.changed(2, raw))
        self.faders.append(dial)
        display = label('—', 'mixReadout')
        display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.displays.append(display)

    def changed(self, channel, raw):
        if not self.window.ready:
            return
        if channel == 2:
            self.window.write(f"{self.mix}.level", (raw,))
        else:
            values = self.window.values[f"{self.pair}.volume"]
            self.window.write(
                f"{self.pair}.volume",
                move_stereo(values, channel, 0 if raw == 110 else raw, self.link.isChecked()),
            )

    def equalize(self, channel):
        if self.window.ready:
            raw = self.window.values[f"{self.pair}.volume"][1 - channel]
            self.window.write(f"{self.pair}.volume", (raw, raw))

    def update_values(self, values):
        raw_values = (*values[f"{self.pair}.volume"], values[f"{self.mix}.level"][0])
        for channel, (slider, display, raw) in enumerate(zip(self.faders, self.displays, raw_values)):
            slider.blockSignals(True)
            slider.setValue(max(110, raw) if channel < 2 else raw)
            slider.blockSignals(False)
            readout = lambda raw=raw, channel=channel: ('−∞' if channel < 2 and raw == 0 else
                       number((raw - 255) * 0.5, 1) if channel < 2 else number(raw - 48, 0))
            localize(display, 'setText', lambda channel=channel, readout=readout:
                     translate('Panel', '{text} dB').format(text=readout()) if channel == 2 else readout())
            localize(slider, 'setToolTip', lambda readout=readout: translate('Panel', '{text} dB').format(text=readout()))
        for checkbox, checked in (
            (self.direct, not bool(values[f"{self.pair}.enabled"][0])),
            (self.mix_on, bool(values[f"{self.mix}.enabled"][0])),
        ):
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)


class InputBox(InputPreview):
    def __init__(self, window, pair, name):
        super().__init__(name)
        self.window, self.pair = window, pair
        self.monitor_key = 'monitor' + pair[-2:] + '.enabled'
        for index, button in enumerate(self.source_buttons):
            button.clicked.connect(lambda checked, index=index: self.select_source(index))
        for ch, slider in enumerate(self.faders):
            slider.valueChanged.connect(lambda raw, ch=ch: self.changed(ch, raw))
            slider.equalize.connect(lambda ch=ch: self.equalize(ch))
            slider.wheel_units = lambda: window.theme.wheel_db * 2
        self.gain.wheel_units = lambda: window.theme.wheel_db * 2
        self.gain.valueChanged.connect(lambda raw: window.write(pair + '.gain', (raw,)))
        self.monitor.toggled.connect(lambda on: window.write(self.monitor_key, (int(on),)))
        self.mute.clicked.connect(lambda on: window.write(pair + '.enabled', (int(not on),)))
        if self.phantom is not None:
            self.phantom.clicked.connect(self.set_phantom)

    def set_available(self, on):
        self.controls.setEnabled(on)
        for widget in (*self.faders, *self.displays, self.link, self.monitor, self.mute):
            widget.setEnabled(on)
        if not on and self.phantom:
            self.phantom.setEnabled(False)

    def select_source(self, source):
        if source == self.window.values[self.pair + '.source'][0]:
            self.update_values(self.window.values)
            return
        # Kernel also drops phantom when leaving Mic. Mirror it immediately.
        if self.pair == 'in12':
            self.window.values['phantom.enabled'] = (0,)
        self.window.write(self.pair + '.source', (source,))

    def set_phantom(self, on):
        if on:
            answer = QMessageBox.question(self.window, translate('Panel', '48 V phantom power'),
                translate('Panel', 'Enable 48 V on the microphone input? Connect only a microphone that supports phantom power.'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                self.phantom.setChecked(False)
                return
        self.window.write('phantom.enabled', (int(on),))

    def changed(self, channel, raw):
        if not self.window.ready:
            return
        current = self.window.values[self.pair + '.volume']
        raw -= 110
        if self.link.isChecked():
            delta = max(-min(current), min(145-max(current), raw-current[channel]))
            values = tuple(value + delta for value in current)
        else:
            values = tuple(raw if ch == channel else value for ch, value in enumerate(current))
        self.window.write(self.pair + '.volume', values)

    def equalize(self, channel):
        if self.window.ready:
            raw = self.window.values[self.pair + '.volume'][1-channel]
            self.window.write(self.pair + '.volume', (raw, raw))

    def update_values(self, values):
        available = self.pair + '.volume' in values
        self.set_available(self.window.ready and available)
        if not available:
            return
        source = values[self.pair + '.source'][0]
        digital = self.pair == 'in12' and source == 2
        for index, button in enumerate(self.source_buttons):
            button.setChecked(index == source)
        gain = values[self.pair + '.gain'][0]
        self.gain.blockSignals(True)
        self.gain.setValue(gain)
        self.gain.blockSignals(False)
        self.gain.setEnabled(not digital)
        localize(self.gain_caption, 'setText', lambda gain=gain, digital=digital:
                 translate('Panel', 'Gain · {gain} dB').format(gain='—' if digital else number(gain * .5, 1)))
        for slider, display, raw in zip(self.faders, self.displays, values[self.pair + '.volume']):
            slider.blockSignals(True)
            slider.setValue(raw + 110)
            slider.blockSignals(False)
            slider.setEnabled(not digital)
            display.setEnabled(not digital)
            localize(display, 'setText', lambda raw=raw, digital=digital: '—' if digital else '−∞' if raw == 0 else number((raw-145)*.5, 1))
        for widget, on in ((self.monitor, values[self.monitor_key][0]),
                           (self.mute, not values[self.pair + '.enabled'][0])):
            widget.blockSignals(True)
            widget.setChecked(bool(on))
            widget.blockSignals(False)
        self.link.setEnabled(not digital)
        self.monitor.setEnabled(not digital and self.window.muted_routes is None)
        if self.phantom is not None:
            self.phantom.setEnabled(source == 1)
            self.phantom.setChecked(bool(values['phantom.enabled'][0]))


class PlaybackBox(Card):
    def __init__(self, window):
        super().__init__(lambda: translate('Panel', 'Playback'), lambda: translate('Panel', 'Sample rate and audio buffer size'))
        self.window = window
        self.state = None
        self.busy = False
        self.available = False
        outer = self.body
        outer.setSpacing(10)
        row = QHBoxLayout()
        row.addWidget(localize(QLabel(''), 'setText', lambda: translate('Panel', 'Sample rate')))
        self.rate = QComboBox()
        localize(self.rate, 'setAccessibleName', lambda: translate('Panel', 'Playback sample rate'))
        for rate in RATES:
            self.rate.addItem('', rate)
        localize(self.rate, 'setToolTip', lambda: translate('Panel', 'Auto: PipeWire selects the rate; it does not switch for each file'))
        row.addWidget(self.rate)
        row.addSpacing(10)
        row.addWidget(localize(QLabel(''), 'setText', lambda: translate('Panel', 'Buffer')))
        self.period = QComboBox()
        localize(self.period, 'setAccessibleName', lambda: translate('Panel', 'Buffer size in samples'))
        for period in PERIODS:
            self.period.addItem(translate("Panel", '{period} samples').format(period=period), period)
        self.period.setCurrentIndex(self.period.findData(256))
        localize(self.period, 'setToolTip', lambda: translate('Panel', 'Size of one period, as in the Windows panel. The full buffer contains two periods'))
        row.addWidget(self.period)
        row.addStretch()
        self.apply_button = localize(QPushButton(''), 'setText', lambda: translate('Panel', 'Apply'))
        self.apply_button.clicked.connect(self.apply)
        row.addWidget(self.apply_button)
        outer.addLayout(row)
        self.actual = localize(QLabel(''), 'setText', lambda: translate('Panel', 'Reading device settings…'))
        self.actual.setObjectName('secondary')
        outer.addWidget(self.actual)
        self.delay = QLabel()
        self.delay.setObjectName('secondary')
        outer.addWidget(self.delay)
        self.message = localize(QLabel(''), 'setText', lambda: translate('Panel', 'Applying settings may briefly interrupt audio.'))
        self.message.setWordWrap(True)
        self.message.setObjectName('secondary')
        self.message.setMaximumWidth(800)
        self.message.setMinimumHeight(24)
        outer.addWidget(self.message)
        self.rate.currentIndexChanged.connect(self.changed)
        self.period.currentIndexChanged.connect(self.changed)
        window.language.changed.connect(self.retranslate_options)
        self.retranslate_options()
        self.refresh_enabled()

    def retranslate_options(self):
        # Item data and draft selections stay intact; no settings are applied.
        for combo in (self.rate, self.period):
            combo.blockSignals(True)
        for index, rate in enumerate(RATES):
            self.rate.setItemText(index, translate('Panel', 'Auto') if rate == 0 else
                                  translate('Panel', '{rate} kHz').format(rate=number(rate / 1000)))
        for index, period in enumerate(PERIODS):
            self.period.setItemText(index, translate('Panel', '{period} samples').format(period=period))
        for combo in (self.rate, self.period):
            combo.blockSignals(False)

    def selection(self):
        return {'rate': self.rate.currentData(), 'period': self.period.currentData()}

    def update_state(self, state):
        # Preserve an unapplied edit while polling or observing external changes.
        if self.state is None or self.selection() == self.state['saved']:
            for widget, key in ((self.rate, 'rate'), (self.period, 'period')):
                widget.blockSignals(True)
                widget.setCurrentIndex(widget.findData(state['saved'][key]))
                widget.blockSignals(False)
        self.state = state
        self.available = True
        localize(self.actual, 'setToolTip', lambda: '')
        hw = state['hardware']
        localize(self.actual, 'setText', lambda hw=hw:
                 translate('Panel', '{status}: {rate} kHz · {samples} samples').format(
                     status=translate('Panel', 'Current') if hw['active'] else translate('Panel', 'Idle · last configuration'),
                     rate=number(hw['rate'] / 1000), samples=hw['period']))
        self.changed()

    def changed(self):
        rate = self.rate.currentData() or (self.state['hardware']['rate'] if self.state else 48000)
        period = self.period.currentData()
        localize(self.delay, 'setText', lambda period=period, rate=rate:
                 translate('Panel', 'At {rate} kHz: period {period_ms} ms · full buffer {buffer_ms} ms').format(
                     rate=number(rate / 1000), period_ms=number(1000 * period / rate, 2),
                     buffer_ms=number(2000 * period / rate, 2)))
        self.refresh_enabled()

    def refresh_enabled(self):
        on = self.available and not self.busy and not self.window.closing
        self.rate.setEnabled(on)
        self.period.setEnabled(on)
        dirty = self.state is not None and (
            self.selection() != self.state['saved'] or
            {**self.selection(), 'periods': 2, 'irq': True} != self.state['live'])
        self.apply_button.setEnabled(on and dirty)

    def apply(self):
        if not self.apply_button.isEnabled():
            return
        if self.window.worker.submit_playback(self.selection()):
            self.busy = True
            localize(self.message, 'setText', lambda: translate('Panel', 'Applying MAYA44e settings…').replace('MAYA44e', self.window.model))
            self.message.setObjectName('secondary')
            self.refresh_enabled()

    def finished(self, success, detail):
        self.busy = False
        localize(self.message, 'setText', lambda success=success, detail=detail:
                 translate('Panel', 'Settings applied and saved') if success else
                 translate('Panel', 'Not applied: {detail}').format(detail=detail.replace('\n', ' ')[:180]))
        localize(self.message, 'setToolTip', lambda detail=detail: detail)
        self.message.setObjectName('success' if success else 'error')
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self.refresh_enabled()

    def failed(self, detail):
        self.available = False
        localize(self.actual, 'setText', lambda: translate('Panel', 'Device settings unavailable'))
        localize(self.actual, 'setToolTip', lambda detail=detail: detail)
        self.refresh_enabled()


class MixerWindow(QMainWindow):
    exited = pyqtSignal()

    def __init__(self, backend, demo=False, start_worker=True, playback=None):
        super().__init__()
        self.ready, self.closing = False, False
        self.values = {}
        self.muted_routes = None
        self.pair_mutes = {}
        self.meter_values = None
        self.tray = self.tray_mute = None
        self.demo = demo
        self.device = getattr(backend, 'device', MayaDevice())
        self.model = self.device.model
        localize(self, 'setWindowTitle', lambda: self.model)
        self.setWindowIcon(asset_icon(ICON))
        self.theme = QApplication.instance().maya_theme
        self.language = QApplication.instance().maya_language
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(asset_icon(ICON).pixmap(30, 30))
        header.addWidget(icon)
        model_label = label(self.model, 'title')
        localize(model_label, 'setToolTip', lambda: translate('Panel', 'Experimental model: hardware validation pending') if self.device.subsystem != 1 else '')
        header.addWidget(model_label)
        header.addStretch()
        self.status = ConnectionStatus()
        self.status.setObjectName('connectionStatus')
        localize(self.status, 'setText', lambda: '● ' + translate('Panel', 'Connecting…'))
        localize(self.status, 'setAccessibleName', lambda: translate('Panel', 'Connecting to MAYA44e').replace('MAYA44e', self.model))
        localize(self.status, 'setToolTip', lambda: translate('Panel', 'Connecting to the device…'))
        layout.addLayout(header)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.mixer_area = area
        mixer = QWidget()
        groups = QHBoxLayout(mixer)
        groups.setContentsMargins(0, 0, 0, 0)
        groups.setSpacing(10)
        area.setWidget(mixer)
        layout.addWidget(area, 1)
        input_group = Card()
        output_group = Card()
        for group in (input_group, output_group):
            group.body.setContentsMargins(10, 12, 10, 12)
            group.body.setSpacing(8)
            groups.addWidget(group, 1)
        self.input_heading = label(lambda: translate('Panel', 'INPUTS'), 'sectionTitle')
        self.input_heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        input_group.body.addWidget(self.input_heading)
        output_heading = label(lambda: translate('Panel', 'OUTPUTS'), 'sectionTitle')
        output_heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        output_group.body.addWidget(output_heading)
        input_group.body.addWidget(divider())
        output_group.body.addWidget(divider())
        self.input_previews = [InputBox(self, 'in12', '1–2'), InputBox(self, 'in34', '3–4')]
        inputs = QHBoxLayout()
        inputs.setSpacing(8)
        for preview in self.input_previews:
            inputs.addWidget(preview, 1)
        input_group.body.addLayout(inputs, 1)
        self.pairs = [
            PairBox(self, 'out12', '1–2', '3–4', 'mix12'),
            PairBox(self, 'out34', '3–4', '1–2', 'mix34'),
        ]
        routing = QWidget()
        routing.setFixedHeight(161)
        routes = QVBoxLayout(routing)
        routes.setContentsMargins(0, 0, 0, 0)
        routes.setSpacing(3)
        sources = QHBoxLayout()
        sources.setSpacing(8)
        for text in ('1–2', '3–4'):
            source = label(text.replace('–', '/'), 'channel')
            source.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sources.addWidget(source, 1)
        routes.addLayout(sources)
        self.mix_lines = MixLines(self.pairs)
        routes.addWidget(self.mix_lines)
        knobs = QHBoxLayout()
        knobs.setSpacing(8)
        for pair in self.pairs:
            column = QVBoxLayout()
            column.setSpacing(1)
            column.addWidget(pair.mix_on, alignment=Qt.AlignmentFlag.AlignHCenter)
            column.addWidget(pair.faders[2], alignment=Qt.AlignmentFlag.AlignHCenter)
            column.addWidget(pair.displays[2], alignment=Qt.AlignmentFlag.AlignHCenter)
            knobs.addLayout(column, 1)
        routes.addLayout(knobs)
        output_group.body.addWidget(routing)
        outputs = QHBoxLayout()
        outputs.setSpacing(8)
        for pair in self.pairs:
            outputs.addWidget(pair, 1)
        output_group.body.addLayout(outputs, 1)
        self.digital_box = DigitalBox(self)
        groups.addWidget(self.digital_box)
        footer = QHBoxLayout()
        self.quiet = localize(QPushButton(''), 'setText', lambda: translate('Panel', 'Mute all'))
        self.quiet.setObjectName('quiet')
        self.quiet.setCheckable(True)
        self.quiet.setIcon(toolbar_icon('mute', self.palette()))
        self.quiet.setIconSize(QSize(24, 20))
        self.quiet.setMinimumWidth(135)
        localize(self.quiet, 'setToolTip', lambda: translate('Panel', 'Mute both sources; click again to restore the previous outputs. Levels and MIX are preserved'))
        self.quiet.clicked.connect(self.silence)
        footer.addWidget(self.quiet)
        footer.addSpacing(4)
        footer.addWidget(self.status)
        footer.addStretch()
        self.playback_summary = localize(QPushButton(''), 'setText', lambda: translate('Panel', 'Reading settings…'))
        self.playback_summary.setObjectName('linkButton')
        localize(self.playback_summary, 'setToolTip', lambda: translate('Panel', 'Sample rate and buffer'))
        self.playback_summary.clicked.connect(self.show_settings)
        footer.addWidget(self.playback_summary)
        footer.addSpacing(12)
        self.settings_button = localize(QPushButton(''), 'setText', lambda: translate('Panel', 'Settings'))
        self.settings_button.clicked.connect(self.show_settings)
        footer.addWidget(self.settings_button)
        layout.addLayout(footer)
        self.settings_dialog = ContentDialog(self)
        localize(self.settings_dialog, 'setWindowTitle', lambda: translate('Panel', 'Settings'))
        self.settings_dialog.setMinimumWidth(640)
        settings_layout = QVBoxLayout(self.settings_dialog)
        settings_layout.setSpacing(12)
        self.playback_box = PlaybackBox(self)
        settings_layout.addWidget(self.playback_box)
        appearance = Card(lambda: translate('Panel', 'Appearance and controls'))
        preferences = QGridLayout()
        preferences.setHorizontalSpacing(16)
        preferences.setVerticalSpacing(14)
        preferences.setColumnStretch(0, 1)
        names = QVBoxLayout()
        names.addWidget(label(lambda: translate('Panel', 'Color theme'), 'sectionTitle'))
        names.addWidget(label(lambda: translate('Panel', 'The system theme follows your Plasma settings'), 'caption'))
        preferences.addLayout(names, 0, 0)
        self.theme_choice = QComboBox()
        localize(self.theme_choice, 'setAccessibleName', lambda: translate('Panel', 'Color theme'))
        for title, mode in ((translate("Panel", 'System default'), 'system'), (translate("Panel", 'Light'), 'light'), (translate("Panel", 'Dark'), 'dark')):
            self.theme_choice.addItem(title, mode)
        self.theme_choice.setCurrentIndex(self.theme_choice.findData(self.theme.mode))
        self.theme_choice.currentIndexChanged.connect(lambda: self.theme.set_mode(self.theme_choice.currentData()))
        preferences.addWidget(self.theme_choice, 0, 1)
        preferences.addWidget(divider(), 1, 0, 1, 2)
        preferences.addWidget(label(lambda: translate('Panel', 'Language'), 'sectionTitle'), 2, 0)
        self.language_choice = QComboBox()
        localize(self.language_choice, 'setAccessibleName', lambda: translate('Panel', 'Language'))
        for title, mode in (('', 'system'), ('English', 'en'), ('Русский', 'ru')):
            self.language_choice.addItem(title, mode)
        self.language_choice.setCurrentIndex(self.language_choice.findData(self.language.mode))
        self.language_choice.currentIndexChanged.connect(lambda: self.language.set_language(self.language_choice.currentData()))
        preferences.addWidget(self.language_choice, 2, 1)
        preferences.addWidget(divider(), 3, 0, 1, 2)
        names = QVBoxLayout()
        names.addWidget(label(lambda: translate('Panel', 'Mouse wheel step'), 'sectionTitle'))
        self.wheel_hint = label('', 'caption', True)
        names.addWidget(self.wheel_hint)
        preferences.addLayout(names, 4, 0)
        self.wheel_choice = QComboBox()
        localize(self.wheel_choice, 'setAccessibleName', lambda: translate('Panel', 'Mouse wheel step'))
        for step in (0.5, 1, 2, 3):
            self.wheel_choice.addItem('', step)
        self.wheel_choice.setCurrentIndex(self.wheel_choice.findData(self.theme.wheel_db))
        self.wheel_choice.currentIndexChanged.connect(lambda: self.theme.set_wheel(self.wheel_choice.currentData()))
        preferences.addWidget(self.wheel_choice, 4, 1)
        appearance.body.addLayout(preferences)
        settings_layout.addWidget(appearance)
        settings_layout.addStretch()
        actions = QHBoxLayout()
        on_top = localize(QCheckBox(''), 'setText', lambda: translate('Panel', 'Keep above other windows'))
        on_top.toggled.connect(self.set_on_top)
        actions.addWidget(on_top)
        actions.addStretch()
        about = localize(QPushButton(''), 'setText', lambda: translate('Panel', 'About'))
        about.clicked.connect(self.about)
        actions.addWidget(about)
        close_settings = localize(QPushButton(''), 'setText', lambda: translate('Panel', 'Close'))
        close_settings.clicked.connect(self.settings_dialog.close)
        actions.addWidget(close_settings)
        settings_layout.addLayout(actions)
        self.theme.wheel_changed.connect(self.update_wheel_hint)
        self.theme.changed.connect(self.theme_updated)
        self.language.changed.connect(self.language_updated)
        self.language_updated()
        self.update_wheel_hint()
        self.set_enabled(False)
        self.worker = MixerWorker(backend, playback or (DemoPlaybackBackend() if demo else PlaybackBackend(device=self.device)))
        self.worker.snapshot.connect(self.received)
        self.worker.unavailable.connect(self.failed)
        self.worker.finished.connect(self.worker_finished)
        self.worker.playback_snapshot.connect(self.playback_box.update_state)
        self.worker.playback_snapshot.connect(self.update_playback_summary)
        self.worker.playback_error.connect(self.playback_box.failed)
        self.worker.playback_finished.connect(self.playback_box.finished)
        self.meter_worker = MeterWorker(partial(PeakReader, card=self.device.control))
        self.meter_worker.readings.connect(self.received_peaks)
        self.meter_worker.finished.connect(self.worker_finished)
        if start_worker:
            self.worker.start()
            if not demo:
                self.meter_worker.start()
        self.setMinimumSize(660, 680)
        self.resize(910, 710)
        QTimer.singleShot(0, self.fit_minimum_size)

    def fit_minimum_size(self):
        # Include the full strips: QScrollArea's own minimum would hide Mute.
        self.ensurePolished()
        layout = self.centralWidget().layout()
        margins = layout.contentsMargins()
        content = self.mixer_area.widget().minimumSizeHint()
        chrome = [layout.itemAt(i) for i in range(layout.count())
                  if layout.itemAt(i).widget() is not self.mixer_area]
        self.setMinimumSize(
            max(660, content.width(), *(item.minimumSize().width() for item in chrome)) + margins.left() + margins.right(),
            content.height() + sum(item.sizeHint().height() for item in chrome) +
            margins.top() + margins.bottom() + (layout.count() - 1) * layout.spacing() + 2,
        )

    def show_settings(self):
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, 'meter_worker'):
            self.meter_worker.set_active(True)

    def hideEvent(self, event):
        if hasattr(self, 'meter_worker'):
            self.meter_worker.set_active(False)
        self.received_peaks(None, '')
        super().hideEvent(event)

    def received_peaks(self, values, error=''):
        if values is not None and (len(values) != 8 or any(type(value) is not int or not 0 <= value <= 255 for value in values)):
            values, error = None, 'Invalid peak data'
        if not self.ready or self.closing or (not self.demo and not self.isVisible()):
            values = None
        self.meter_values = tuple(values) if values is not None else None
        strips = [*self.pairs, *self.input_previews]
        for index, strip in enumerate(strips):
            for channel, meter in enumerate(strip.meters):
                raw = None if values is None else values[index * 2 + channel]
                meter.set_peak(raw)
                localize(meter, 'setToolTip', lambda raw=raw, error=error:
                         translate('Panel', 'Hardware peak meter unavailable') + (': ' + error if error else '') if raw is None else
                         translate('Panel', 'Signal peak: {level} dBFS. Peak hold; red marks full scale.').format(
                             level='−∞' if raw == 0 else number(20 * math.log10(raw / 255), 1)))

    def update_wheel_hint(self):
        step = self.theme.wheel_db
        localize(self.wheel_hint, 'setText', lambda step=step:
                 translate('Panel', 'Per wheel step: output {output_step} dB, MIX {mix_step} dB.\nArrow keys always use the smallest step.').format(
                     output_step=number(step), mix_step=number(max(1, step))))

    def language_updated(self):
        for combo in (self.theme_choice, self.language_choice, self.wheel_choice):
            combo.blockSignals(True)
        for index, title in enumerate((translate('Panel', 'System default'), translate('Panel', 'Light'), translate('Panel', 'Dark'))):
            self.theme_choice.setItemText(index, title)
        self.language_choice.setItemText(0, translate('Panel', 'System default ({language})').format(
            language='Русский' if self.language.system_language == 'ru' else 'English'))
        self.language_choice.setCurrentIndex(self.language_choice.findData(self.language.mode))
        for index in range(self.wheel_choice.count()):
            self.wheel_choice.setItemText(index, translate('Panel', '{step} dB').format(step=number(self.wheel_choice.itemData(index))))
        for combo in (self.theme_choice, self.language_choice, self.wheel_choice):
            combo.blockSignals(False)
        QTimer.singleShot(0, self.fit_minimum_size)

    def theme_updated(self):
        self.quiet.setIcon(toolbar_icon('mute', QApplication.palette()))
        self.theme_choice.blockSignals(True)
        self.theme_choice.setCurrentIndex(self.theme_choice.findData(self.theme.mode))
        self.theme_choice.blockSignals(False)
        QTimer.singleShot(0, self.fit_minimum_size)
        self.update()

    def update_playback_summary(self, state):
        hw = state['hardware']
        localize(self.playback_summary, 'setText', lambda hw=hw:
                 translate('Panel', '{bits} bit · {rate} kHz · {samples} samples').format(
                     bits=hw.get('valid_bits') or hw.get('dac_bits') or '—',
                     rate=number(hw['rate'] / 1000), samples=hw['period']))
        localize(self.playback_summary, 'setToolTip', lambda hw=hw:
                 ('' if hw['active'] else translate('Panel', 'Idle · ')) +
                 translate('Panel', 'Configured DAC: {bits} bit\nALSA: {format} ({container}-bit container)\nSample rate and buffer').format(
                     bits=hw.get('dac_bits') or '—', format=hw.get('format') or '—', container=hw.get('container_bits') or '—'))

    def set_enabled(self, on):
        self.digital_box.setEnabled(on and 'spdif.output.enabled' in self.values)
        self.quiet.setEnabled(on)
        if self.tray_mute is not None:
            self.tray_mute.setEnabled(on)
        for strip in self.input_previews:
            strip.set_available(on and strip.pair + '.volume' in self.values)
        for pair in self.pairs:
            pair.setEnabled(on)
            pair.direct.setEnabled(on)
            pair.mix_on.setEnabled(on)
            pair.faders[2].setEnabled(on)

    def received(self, revision, values):
        if self.closing or revision != self.worker.current_revision():
            return
        first_ready = not self.ready
        self.values = values
        for pair, saved in list(self.pair_mutes.items()):
            if any(values[key][0] for key in saved):
                del self.pair_mutes[pair]
        # An external mixer enabling a DAC takes precedence over our mute.
        if self.muted_routes is not None and any(values[key][0] for key in self.muted_routes):
            self.muted_routes = None
        self.ready = True
        self.set_enabled(True)
        localize(self.status, 'setText', lambda self=self:
                 '● ' + (translate('Panel', 'Demo') if self.demo else translate('Panel', 'Connected')))
        localize(self.status, 'setAccessibleName', lambda self=self: translate('Panel', 'MAYA44e demo') if self.demo else translate('Panel', 'MAYA44e connected').replace('MAYA44e', self.model))
        localize(self.status, 'setToolTip', lambda self=self: translate('Panel', 'Demo: the device is not modified') if self.demo else translate('Panel', 'MAYA44e connected').replace('MAYA44e', self.model))
        self.status.setProperty('state', 'connected')
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.render()
        if first_ready:
            # Enabled controls may have different metrics from the loading state.
            QTimer.singleShot(0, self.fit_minimum_size)

    def failed(self, detail):
        self.ready = False
        self.received_peaks(None, detail)
        # Never restore routes saved for a card that disconnected or failed.
        self.muted_routes = None
        self.pair_mutes.clear()
        self.set_enabled(False)
        localize(self.status, 'setText', lambda: '● ' + translate('Panel', 'Device unavailable'))
        localize(self.status, 'setAccessibleName', lambda: translate('Panel', 'MAYA44e unavailable').replace('MAYA44e', self.model))
        localize(self.status, 'setToolTip', lambda detail=detail: detail)
        self.status.setProperty('state', 'error')
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.render_mute()

    def render(self):
        self.digital_box.update_values(self.values)
        for strip in self.input_previews:
            strip.update_values(self.values)
        for pair in self.pairs:
            pair.update_values(self.values)
            pair_muted = pair.pair in self.pair_mutes
            pair.direct.setEnabled(self.muted_routes is None and not pair_muted)
            pair.mix_on.setEnabled(not pair_muted)
            pair.mute.setChecked(pair_muted)
            localize(pair.mute, 'setText', lambda pair_muted=pair_muted: translate('Panel', 'Unmute') if pair_muted else 'Mute')
            localize(pair.mute, 'setAccessibleName', lambda pair=pair, pair_muted=pair_muted: (translate('Panel', 'Unmute output ') if pair_muted else translate('Panel', 'Mute output ')) + ('1–2' if pair.pair == 'out12' else '3–4'))
            pair.mute.setEnabled(self.muted_routes is None)
        self.mix_lines.update()
        self.render_mute()

    def render_mute(self):
        muted = self.muted_routes is not None
        self.quiet.setChecked(muted)
        localize(self.quiet, 'setText', lambda muted=muted: translate('Panel', 'Unmute') if muted else translate('Panel', 'Mute all'))
        if self.tray_mute is not None:
            self.tray_mute.setChecked(muted)
        if self.tray is not None:
            self.tray.setIcon(asset_icon(MUTED_ICON if muted else ICON))
            localize(self.tray, 'setToolTip', lambda self=self, muted=muted:
                     self.model + ' — ' + (translate('Panel', 'Device unavailable') if not self.ready else
                     translate('Panel', 'Muted') if muted else translate('Panel', 'Volume and MIX')))

    def write(self, key, values):
        if not self.ready or self.closing:
            return
        values = validate(key, values)
        self.values[key] = values
        self.worker.submit(key, values)
        self.render()

    def silence(self):
        if not self.ready or self.closing:
            return
        if self.muted_routes is None:
            routes = (*ROUTES, *(key for key in ("monitor12.enabled", "monitor34.enabled", "spdif.output.enabled") if key in self.values))
            self.muted_routes = {key: self.values[key] for key in routes}
            changes = {key: (0,) for key in routes}
        else:
            changes, self.muted_routes = self.muted_routes, None
        for key, values in changes.items():
            self.write(key, values)

    def silence_pair(self, pair):
        if not self.ready or self.closing or self.muted_routes is not None:
            return
        if pair.pair in self.pair_mutes:
            changes = self.pair_mutes.pop(pair.pair)
        else:
            monitor_key = 'monitor' + pair.pair[-2:] + '.enabled'
            keys = (f'{pair.pair}.enabled', f'{pair.mix}.enabled', *((monitor_key,) if monitor_key in self.values else ()))
            self.pair_mutes[pair.pair] = {key: self.values[key] for key in keys}
            changes = {key: (0,) for key in keys}
        for key, values in changes.items():
            self.write(key, values)

    def enable_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        self.tray = QSystemTrayIcon(asset_icon(ICON), self)
        self.tray_menu = QMenu(self)
        localize(self.tray_menu.addAction('', self.show_window), 'setText', lambda: translate('Panel', 'Open mixer'))
        localize(self.tray_menu.addAction('', self.show_settings), 'setText', lambda: translate('Panel', 'Settings'))
        self.tray_mute = localize(self.tray_menu.addAction(''), 'setText', lambda: translate('Panel', 'Muted'))
        self.tray_mute.setCheckable(True)
        self.tray_mute.setEnabled(self.ready)
        self.tray_mute.triggered.connect(self.silence)
        self.tray_menu.addSeparator()
        localize(self.tray_menu.addAction('', self.request_exit), 'setText', lambda: translate('Panel', 'Quit'))
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.tray_activated)
        self.render_mute()
        self.tray.show()
        return True

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.hide() if self.isVisible() else self.show_window()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window()

    def request_exit(self):
        self.settings_dialog.close()
        self.closing = True
        self.playback_box.refresh_enabled()
        if self.tray is not None:
            self.tray.hide()
        self.close()

    def set_on_top(self, on):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()

    def about(self):
        models = ', '.join(name for _, name in MODELS.values())
        QMessageBox.about(
            self, translate("Panel", 'About MAYA44e').replace('MAYA44e', self.model),
            translate(
                "Panel",
                'ESI control panel for Linux.\n\nModels: {models}'
                '\n\nProject author: Nordschamane\nLicense: GPL-3.0-or-later.',
            ).format(models=models),
        )

    def closeEvent(self, event):
        self.settings_dialog.hide()
        if not self.closing and self.tray is not None and self.tray.isVisible() and QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
            event.ignore()
            return
        if self.worker.isRunning() or self.meter_worker.isRunning():
            self.closing = True
            self.set_enabled(False)
            self.worker.shutdown()
            self.meter_worker.shutdown()
            event.ignore()
        else:
            event.accept()
            self.exited.emit()

    def worker_finished(self):
        if self.closing and not self.worker.isRunning() and not self.meter_worker.isRunning():
            self.close()


def apply_style(app, persist=False, theme=None, language=None):
    if not hasattr(app, 'maya_language'):
        app.maya_language = LanguageManager(app, persist=persist, language=language)
    app.maya_theme = ThemeManager(app, persist=persist)
    if theme is not None:
        app.maya_theme.set_mode(theme, save=False)


def dispose_window(window, server=None):
    """Release Qt objects while Python and the Qt bindings are still alive.

    Leaving signal-connected widgets/threads to interpreter finalization can
    crash SIP. Drain DeferredDelete explicitly after the main loop has ended.
    """
    for worker in (window.worker, window.meter_worker):
        worker.shutdown()
    for worker in (window.worker, window.meter_worker):
        worker.wait()
        worker.deleteLater()
    if server is not None:
        server.close()
        server.deleteLater()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--card", help="ALSA card ID, index or PCI address (required when several supported cards are present)")
    startup = parser.add_mutually_exclusive_group()
    startup.add_argument("--tray", action="store_true", help="Start in the tray; do not raise an existing instance")
    startup.add_argument("--show", action="store_true", help="Open the mixer window immediately")
    parser.add_argument("--quit", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--snapshot", type=Path, help="Render the demo offscreen to PNG")
    parser.add_argument("--capture-ready", type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--theme', choices=('system', 'light', 'dark'), help='Appearance override (not saved)')
    parser.add_argument('--language', choices=('system', 'en', 'ru'), help='Interface language override (not saved)')
    parser.add_argument('--page', choices=('mixer', 'settings'), default='mixer', help='Page to show')
    args = parser.parse_args()
    if args.snapshot:
        args.demo = True
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    elif os.environ.get("WAYLAND_DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "wayland"
    try:
        device = MayaDevice() if args.demo else select_device(args.card)
    except ValueError as error:
        parser.error(str(error))
    app = QApplication([sys.argv[0]])
    app.setApplicationName("maya44e-mixer")
    app.setApplicationDisplayName(device.model)
    app.setDesktopFileName("maya44e-mixer")
    app.setWindowIcon(asset_icon(ICON))
    app.setQuitOnLastWindowClosed(False)
    apply_style(app, persist=not args.demo, theme=args.theme, language=args.language)
    server = None
    if not args.demo:
        name = f"maya44e-mixer-{os.getuid()}" + ("" if device.card_id == "MAYA44e" else "-" + device.card_id)
        socket = QLocalSocket()
        socket.connectToServer(name)
        if socket.waitForConnected(150):
            command = b"quit\n" if args.quit else b"tray\n" if args.tray else b"show\n"
            if args.language and not args.quit:
                command = f'language {args.language} '.encode() + command
            socket.write(command)
            if not socket.waitForBytesWritten(500):
                return 1
            # Wait for dispatch, including orderly worker shutdown on --quit.
            socket.waitForDisconnected(3000)
            return 0
        if args.quit:
            return 0
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(name)
        if not server.listen(name):
            raise RuntimeError(server.errorString())
    window = MixerWindow(DemoBackend() if args.demo else AlsaBackend(device=device), args.demo)
    if args.page == 'settings':
        window.show_settings()
    window.exited.connect(app.quit)
    tray_available = not args.demo and window.enable_tray()
    if server:
        def receive_command():
            connection = server.nextPendingConnection()
            connection.disconnected.connect(connection.deleteLater)
            def dispatch():
                if not connection.canReadLine():
                    return
                command = bytes(connection.readLine()).strip()
                if command.startswith(b'language '):
                    fields = command.split()
                    if len(fields) == 3 and fields[1] in (b'system', b'en', b'ru'):
                        window.language.set_language(fields[1].decode(), save=False)
                        command = fields[2]
                if command == b"quit":
                    window.exited.connect(connection.disconnectFromServer)
                    window.request_exit()
                else:
                    if command == b"show":
                        window.show_window()
                    connection.disconnectFromServer()
            connection.readyRead.connect(dispatch)
            dispatch()
        server.newConnection.connect(receive_command)
    if args.show or not tray_available:
        window.show()
    if args.capture_ready:
        captured = False
        def save_ready(revision, values):
            nonlocal captured
            if captured or not window.ready:
                return
            captured = True
            def save():
                window.grab().save(str(args.capture_ready))
                args.capture_ready.with_suffix(".json").write_text(json.dumps({
                    "platform": app.platformName(), "visible": window.isVisible(),
                    "tray_visible": window.tray is not None and window.tray.isVisible(),
                    "controls": window.values, "demo": args.demo,
                    "meter_values": window.meter_values,
                    "meters_available": [meter.available for strip in [*window.pairs, *window.input_previews] for meter in strip.meters],
                    "playback": window.playback_box.state,
                    "language": window.language.language,
                    "window_title": window.windowTitle(),
                    "application_display_name": app.applicationDisplayName(),
                    "layout": {"size": [window.width(), window.height()],
                               "minimum": [window.minimumWidth(), window.minimumHeight()],
                               "scroll_max": window.mixer_area.verticalScrollBar().maximum()},
                }, ensure_ascii=False, indent=2) + "\n")
            QTimer.singleShot(400, save)
        window.worker.snapshot.connect(save_ready)
    if args.snapshot:
        def capture():
            if not window.ready:
                QTimer.singleShot(30, capture)
                return
            def save_snapshot():
                target = window.settings_dialog if args.page == 'settings' else window
                target.grab().save(str(args.snapshot))
                window.close()
            QTimer.singleShot(400, save_snapshot)
        QTimer.singleShot(80, capture)
    result = app.exec()
    dispose_window(window, server)
    return result


if __name__ == "__main__":
    sys.exit(main())
