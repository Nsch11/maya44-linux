# SPDX-License-Identifier: GPL-3.0-or-later
"""S/PDIF routing controls; status polling never writes to the device."""
from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QVBoxLayout, QWidget
from i18n import translate, localize, number
from plasma_widgets import Card, label, divider, pair_badge


class ChoiceButtons(QWidget):
    """Two explicit choices, styled like the panel's existing source buttons.

    The small selection API lets snapshots and localization retain the same
    state semantics as the former combo boxes. Reading/rendering never emits
    an audio change when the selection is unchanged.
    """
    currentIndexChanged = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.buttons, self.data = [], []
        self.index = -1
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(4)
        self.group.idClicked.connect(self.setCurrentIndex)

    def addItem(self, text, data):
        button = QPushButton(text)
        button.setObjectName('source')
        button.setCheckable(True)
        self.group.addButton(button, len(self.buttons))
        self.buttons.append(button)
        self.data.append(data)
        self.row.addWidget(button, 1)
        if self.index < 0:
            self.setCurrentIndex(0)

    def minimumSizeHint(self):
        if not self.buttons:
            return QSize()
        sizes = [button.minimumSizeHint() for button in self.buttons]
        return QSize(len(sizes) * max(size.width() for size in sizes) +
                     (len(sizes) - 1) * self.row.spacing(),
                     max(size.height() for size in sizes))

    def sizeHint(self):
        return self.minimumSizeHint()

    def currentData(self):
        return self.data[self.index] if self.index >= 0 else None

    def currentIndex(self):
        return self.index

    def currentText(self):
        return self.itemText(self.index) if self.index >= 0 else ''

    def itemText(self, index):
        return self.buttons[index].text()

    def setItemText(self, index, text):
        self.buttons[index].setText(text)
        self.buttons[index].setAccessibleName(self.accessibleName() + ': ' + text)

    def setAccessibleName(self, name):
        super().setAccessibleName(name)
        for button in self.buttons:
            button.setAccessibleName(name + ': ' + button.text())

    def findData(self, data):
        return self.data.index(data) if data in self.data else -1

    def setCurrentIndex(self, index):
        if not 0 <= index < len(self.buttons):
            return
        self.buttons[index].setChecked(True)
        if self.index != index:
            self.index = index
            self.currentIndexChanged.emit(index)


class DigitalBox(Card):
    def __init__(self, window):
        super().__init__()
        self.body.setContentsMargins(10, 12, 10, 12)
        self.body.setSpacing(8)
        self.setMinimumWidth(206)
        self.window = window
        self.last_analog_source = 0
        title = self.heading = label('S/PDIF', 'sectionTitle')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        localize(title, 'setToolTip', lambda: translate('Panel', 'S/PDIF: 32 / 44.1 / 48 / 96 kHz · input replaces 1/2'))
        self.body.addWidget(title)
        self.body.addWidget(divider())
        output_section = QWidget()
        output_section.setFixedHeight(161)
        output_layout = QVBoxLayout(output_section)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.setSpacing(5)
        self.tx = localize(QPushButton(''), 'setText', lambda: translate('Panel', 'Output'))
        self.tx.setObjectName('route')
        self.tx.setCheckable(True)
        localize(self.tx, 'setAccessibleName', lambda: translate('Panel', 'Digital output'))
        self.tx.toggled.connect(lambda on: window.write('spdif.output.enabled', (int(on),)))
        localize(self.tx, 'setToolTip', lambda: translate('Panel', 'Digital output bypasses analog volume, MIX and output Mute. Mute all also disables S/PDIF.'))
        output_layout.addWidget(self.tx)
        source_label = label(lambda: translate('Panel', 'Source'), 'caption')
        source_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        output_layout.addWidget(source_label)
        self.output = ChoiceButtons()
        self.output.addItem('1/2', 0)
        self.output.addItem('3/4', 1)
        localize(self.output, 'setAccessibleName', lambda: translate('Panel', 'Digital output source'))
        self.output.currentIndexChanged.connect(lambda: window.write('spdif.output.source', (self.output.currentData(),)))
        output_layout.addWidget(self.output)
        format_label = label(lambda: translate('Panel', 'Format'), 'caption')
        format_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        output_layout.addWidget(format_label)
        self.format = ChoiceButtons()
        self.format.addItem('CON', 0)
        self.format.addItem('PRO', 1)
        localize(self.format, 'setAccessibleName', lambda: translate('Panel', 'Digital output format'))
        localize(self.format, 'setToolTip', lambda: translate('Panel', 'CON: consumer format · PRO: professional format'))
        self.format.currentIndexChanged.connect(lambda: window.write('spdif.professional.enabled', (self.format.currentData(),)))
        output_layout.addWidget(self.format)
        output_layout.addStretch()
        self.body.addWidget(output_section)
        input_badge = self.input_badge = pair_badge('')
        localize(input_badge, 'setText', lambda: translate('Panel', 'Input 1/2'))
        self.body.addWidget(input_badge, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.input = ChoiceButtons()
        self.input.addItem('', 0)
        self.input.addItem('S/PDIF', 1)
        localize(self.input, 'setAccessibleName', lambda: translate('Panel', 'Input 1/2 mode'))
        self.input.currentIndexChanged.connect(self.select_input)
        localize(self.input, 'setToolTip', lambda: translate('Panel', 'S/PDIF: 32 / 44.1 / 48 / 96 kHz · input replaces 1/2'))
        self.body.addWidget(self.input)
        self.receiver = ChoiceButtons()
        self.receiver.addItem('', 0)
        self.receiver.addItem('', 1)
        localize(self.receiver, 'setAccessibleName', lambda: translate('Panel', 'Digital input source'))
        self.receiver.currentIndexChanged.connect(lambda: window.write('spdif.loopback.enabled', (self.receiver.currentData(),)))
        localize(self.receiver, 'setToolTip', lambda: translate('Panel', 'Set the card to the source sample rate before recording. Internal loopback records the selected digital output. Other digital rates are unverified.'))
        self.body.addWidget(self.receiver)
        self.body.addSpacing(8)
        signal_heading = label(lambda: translate('Panel', 'Input signal'), 'caption')
        signal_heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(signal_heading)
        self.signal = label('', 'digitalSignal', True)
        self.signal.setMinimumHeight(64)
        self.signal.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.signal)
        self.body.addStretch()
        window.language.changed.connect(self.retranslate_options)
        self.retranslate_options()

    def retranslate_options(self):
        for combo in (self.input, self.receiver):
            combo.blockSignals(True)
        self.input.setItemText(0, translate('Panel', 'Analog'))
        self.receiver.setItemText(0, translate('Panel', 'External'))
        self.receiver.setItemText(1, translate('Panel', 'Loopback'))
        for combo in (self.input, self.receiver):
            combo.blockSignals(False)

    def setEnabled(self, on):
        super().setEnabled(on)
        if not on and hasattr(self, 'signal'):
            self.signal.setProperty('state', 'unavailable')
            localize(self.signal, 'setText', lambda: translate('Panel', 'Digital controls unavailable'))

    def select_input(self):
        digital = self.input.currentData() == 1
        self.window.values['phantom.enabled'] = (0,)
        if digital:
            self.window.values['monitor12.enabled'] = (0,)
        self.window.write('in12.source', (2 if digital else self.last_analog_source,))

    def update_values(self, values):
        required = ('spdif.output.enabled', 'spdif.output.source',
                    'spdif.professional.enabled', 'spdif.loopback.enabled', 'in12.source')
        available = all(key in values for key in required)
        self.setEnabled(self.window.ready and available)
        if not available or not self.window.ready:
            return
        source = values['in12.source'][0]
        if source != 2:
            self.last_analog_source = source
        self.tx.blockSignals(True)
        self.tx.setChecked(bool(values['spdif.output.enabled'][0]))
        self.tx.blockSignals(False)
        self.tx.setEnabled(self.window.muted_routes is None)
        for combo, value in ((self.output, values['spdif.output.source'][0]),
                             (self.format, values['spdif.professional.enabled'][0]),
                             (self.input, int(source == 2)),
                             (self.receiver, values['spdif.loopback.enabled'][0])):
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(value))
            combo.blockSignals(False)
        rate = values.get('spdif.capture.rate', (0,))[0]
        locked = values.get('spdif.capture.locked', (0,))[0]
        professional = values.get('spdif.capture.professional', (0,))[0]
        localize(self.signal, 'setText', lambda: translate('Panel', 'No signal') + '\n—' if not locked else
                 translate('Panel', 'Locked') + '\n' +
                 translate('Panel', '{rate} kHz · {format}').format(
                     rate=number(rate / 1000) if rate else '—', format='PRO' if professional else 'CON'))
        self.signal.setProperty('state', 'locked' if locked else 'idle')
        self.signal.style().unpolish(self.signal)
        self.signal.style().polish(self.signal)
