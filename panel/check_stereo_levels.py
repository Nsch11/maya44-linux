# SPDX-License-Identifier: GPL-3.0-or-later
"""Stereo presentation regressions; no ALSA or audio backend is opened."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import unittest

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint
from PyQt6.QtWidgets import QApplication
from level_meter import LevelMeter
from maya44e import apply_style
from plasma_widgets import Fader, label
from stereo_levels import DbScale, StereoLevels


APP = QApplication.instance() or QApplication([])
if not hasattr(APP, 'maya_theme'):
    apply_style(APP, language='ru')


def settle():
    for _ in range(8):
        APP.processEvents()


class StereoLevelChecks(unittest.TestCase):
    def setUp(self):
        self.theme = APP.maya_theme.mode
        self.language = APP.maya_language.mode
        self.faders, self.meters, self.displays = [], [], []
        for _ in range(2):
            fader = Fader(); fader.setRange(110, 255); fader.setValue(190)
            self.faders.append(fader)
            self.meters.append(LevelMeter(minimum_db=-72.5))
            self.displays.append(label('−32,5', 'digital'))
        self.widget = StereoLevels(self.faders, self.meters, self.displays)
        self.widget.resize(150, self.widget.sizeHint().height())
        self.widget.show(); settle()

    def tearDown(self):
        self.widget.close(); self.widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.maya_theme.set_mode(self.theme, save=False)
        APP.maya_language.set_language(self.language, save=False)
        settle()

    def test_one_shared_scale_and_symmetric_channel_order(self):
        widget = self.widget
        self.assertEqual(widget.findChildren(DbScale), [widget.scale])
        positions = [child.x() for child in
                     (self.faders[0], self.meters[0], widget.scale,
                      self.meters[1], self.faders[1])]
        self.assertEqual(positions, sorted(set(positions)))
        self.assertLessEqual(self.meters[0].x() + self.meters[0].width(), widget.scale.x())
        self.assertLessEqual(widget.scale.x() + widget.scale.width(), self.meters[1].x())
        axes = [fader.x() + fader.width() / 2 for fader in self.faders]
        self.assertLess(axes[0], widget.scale.x())
        self.assertGreater(axes[1], widget.scale.x() + widget.scale.width())
        self.assertEqual(sum(axes) / 2, widget.scale.x() + widget.scale.width() / 2)
        for fader, meter, display in zip(self.faders, self.meters, self.displays):
            self.assertEqual(fader.x() + fader.width() / 2,
                             display.x() + display.width() / 2)
            for raw, _ in widget.scale.ticks:
                position = widget.scale.tick_position(raw)
                self.assertEqual(fader._position(raw), position)
                self.assertEqual(round(meter._position((raw - 255) * 0.5)), position)
        # PairBox appends its MIX knob afterwards; the row must retain only L/R.
        self.faders.append(object()); self.displays.append(object())
        self.assertEqual(len(widget.faders), 2); self.assertEqual(len(widget.displays), 2)

    def test_values_never_shift_tracks_or_readouts(self):
        widget = self.widget
        children = (*widget.faders, *widget.meters, widget.scale, *widget.displays)
        for theme, language in (('dark', 'ru'), ('light', 'en')):
            APP.maya_theme.set_mode(theme, save=False)
            APP.maya_language.set_language(language, save=False)
            for width in (150, 151, 200, 300):
                widget.resize(width, widget.height()); settle()
                before = [child.geometry().getRect() for child in children]
                outer_size = widget.size()
                for raw, caption in ((255, '0,0'), (254, '−0,5'), (235, '−10,0'),
                                     (110, '−72,5'), (110, '−∞'), (255, '0.0')):
                    for fader, display in zip(widget.faders, widget.displays):
                        fader.setValue(raw); display.setText(caption)
                    settle()
                    self.assertEqual([child.geometry().getRect() for child in children], before)
                    self.assertEqual(widget.size(), outer_size)
                self.assertGreaterEqual(widget.displays[0].height(), 28)

    def test_input_controls_can_disable_without_disabling_meters(self):
        received = []
        self.faders[0].valueChanged.connect(received.append)
        self.faders[0].setValue(191)
        self.assertEqual(received, [191])
        for fader in self.faders:
            fader.setEnabled(False)
        settle()
        self.assertFalse(self.widget.scale.isEnabled())
        self.assertTrue(all(meter.isEnabled() for meter in self.meters))
        self.meters[0].set_peak(128)
        self.assertTrue(self.meters[0].available)
        for fader in self.faders:
            fader.setEnabled(True)
        settle()
        self.assertTrue(self.widget.scale.isEnabled())


if __name__ == '__main__':
    unittest.main()
