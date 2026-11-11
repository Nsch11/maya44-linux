# SPDX-License-Identifier: GPL-3.0-or-later
"""Offscreen GUI/transport checks; all writes go to an isolated fake card."""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QCheckBox, QLabel, QSlider, QSpinBox
from alsa_backend import DemoBackend, move_stereo, parse_contents, validate
from maya44e import MixerWindow, apply_style
from plasma_widgets import ThemeManager


class FakeCard(DemoBackend):
    def __init__(self):
        super().__init__()
        self.writes = []
        self.down = False

    def read(self):
        if self.down:
            raise OSError("card disconnected")
        return super().read()

    def write(self, key, values):
        if self.down:
            raise OSError("card disconnected")
        self.writes.append((key, values))
        super().write(key, values)


APP = QApplication([])
APP.setQuitOnLastWindowClosed(False)
apply_style(APP, language='ru')


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out")
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()


class PanelChecks(unittest.TestCase):
    def setUp(self):
        APP.maya_theme.set_wheel(0.5)
        self.card = FakeCard()
        self.window = MixerWindow(self.card, demo=True)
        wait_for(lambda: self.window.ready)

    def tearDown(self):
        self.window.request_exit()
        wait_for(lambda: not self.window.worker.isRunning())
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()

    def test_opening_and_external_sync_never_write(self):
        self.assertEqual(self.card.writes, [])
        self.card.values["out34.volume"] = (140, 150)
        wait_for(lambda: self.window.values["out34.volume"] == (140, 150))
        self.assertEqual(self.card.writes, [])
        self.assertIn("−57,5", self.window.pairs[1].displays[0].text())

    def test_linked_then_independent_volume(self):
        pair = self.window.pairs[0]
        pair.faders[0].setValue(193)
        wait_for(lambda: self.card.values["out12.volume"] == (193, 193))
        pair.link.setChecked(False)
        pair.faders[0].setValue(192)
        wait_for(lambda: self.card.values["out12.volume"] == (192, 193))
        self.assertEqual(move_stereo((192, 193), 0, 255, True), (254, 255))
        pair.equalize(0)
        wait_for(lambda: self.card.values["out12.volume"] == (193, 193))

    def test_disable_source_preserves_mix_and_external_changes_do_not_write(self):
        for pair in self.window.pairs:
            self.assertFalse(pair.direct.isChecked())
            before = self.card.read()
            pair.direct.click()
            wait_for(lambda: self.card.values[f'{pair.pair}.enabled'] == (0,))
            self.assertTrue(pair.direct.isChecked())
            self.assertEqual(self.card.read(), {**before, f'{pair.pair}.enabled': (0,)})
            pair.direct.click()
            wait_for(lambda: self.card.read() == before)
            self.assertFalse(pair.direct.isChecked())
        writes = len(self.card.writes)
        self.card.values['out34.enabled'] = (0,)
        wait_for(lambda: self.window.pairs[1].direct.isChecked())
        self.assertFalse(self.window.pairs[0].direct.isChecked())
        self.assertEqual(len(self.card.writes), writes)

    def test_mix_direction_and_silence_preserve_levels(self):
        self.window.pairs[0].mix_on.setChecked(True)
        wait_for(lambda: self.card.values["mix12.enabled"] == (1,))
        self.window.pairs[0].faders[2].setValue(40)
        wait_for(lambda: self.card.values["mix12.level"] == (40,))
        before = self.card.read()
        self.window.silence()
        wait_for(lambda: self.card.values["out12.enabled"] == self.card.values["out34.enabled"] == (0,))
        for key in before:
            if key not in ("out12.enabled", "out34.enabled", "spdif.output.enabled"):
                self.assertEqual(before[key], self.card.values[key])
        self.assertTrue(self.window.quiet.isChecked())
        self.window.quiet.click()
        wait_for(lambda: self.card.read() == before)
        self.assertFalse(self.window.quiet.isChecked())

    def test_mute_restores_only_previously_enabled_routes(self):
        self.window.write("out34.enabled", (0,))
        self.window.write("out12.volume", (141, 173))
        wait_for(lambda: self.card.values["out34.enabled"] == (0,) and self.card.values["out12.volume"] == (141, 173))
        before = self.card.read()
        self.window.quiet.click()
        wait_for(lambda: self.card.values["out12.enabled"] == (0,))
        self.assertFalse(self.window.pairs[0].direct.isEnabled())
        self.window.quiet.click()
        wait_for(lambda: self.card.read() == before)
        self.assertTrue(self.window.pairs[0].direct.isEnabled())

    def test_fast_mute_unmute_preserves_last_intent(self):
        before = self.card.read()
        self.window.quiet.click()
        self.window.quiet.click()
        wait_for(lambda: len(self.card.writes) >= 2 and self.card.read() == before)
        self.assertIsNone(self.window.muted_routes)

    def test_external_unmute_cancels_saved_routes(self):
        self.window.quiet.click()
        wait_for(lambda: self.card.values["out12.enabled"] == self.card.values["out34.enabled"] == (0,))
        self.card.values["out34.enabled"] = (1,)
        wait_for(lambda: self.window.muted_routes is None)
        self.assertFalse(self.window.quiet.isChecked())
        before = self.card.read()
        self.window.quiet.click()
        wait_for(lambda: self.card.values["out34.enabled"] == (0,))
        self.window.quiet.click()
        wait_for(lambda: self.card.read() == before)

    def test_disconnect_forgets_saved_mute_and_never_restores_it(self):
        self.window.quiet.click()
        wait_for(lambda: self.card.values["out12.enabled"] == self.card.values["out34.enabled"] == (0,))
        self.card.down = True
        wait_for(lambda: not self.window.ready)
        self.assertIsNone(self.window.muted_routes)
        count = len(self.card.writes)
        self.card.values["out34.enabled"] = (1,)
        self.card.down = False
        wait_for(lambda: self.window.ready)
        self.assertFalse(self.window.quiet.isChecked())
        self.assertEqual(len(self.card.writes), count)

    def test_close_hides_in_tray_without_changing_audio(self):
        class TrayStub:
            def isVisible(self):
                return True

            def hide(self):
                pass

        self.window.tray = TrayStub()
        self.window.show()
        with patch("maya44e.QSystemTrayIcon.isSystemTrayAvailable", return_value=True):
            self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.closing)
        self.assertTrue(self.window.worker.isRunning())
        self.assertEqual(self.card.writes, [])
        self.window.tray = None

    def test_disconnect_discards_changes_and_reconnect_reads(self):
        self.card.down = True
        self.window.write("out12.volume", (100, 100))
        wait_for(lambda: not self.window.ready)
        self.assertEqual(self.window.worker.pending, {})
        self.window.write("out34.volume", (90, 90))
        count = len(self.card.writes)
        self.card.down = False
        wait_for(lambda: self.window.ready)
        self.assertEqual(self.card.values["out12.volume"], (195, 195))
        self.assertEqual(self.card.values["out34.volume"], (195, 195))
        self.assertEqual(len(self.card.writes), count)

    def test_invalid_controls_and_missing_snapshot(self):
        for key, values in (
            ("out12.volume", (256, 0)), ("out12.volume", (1,)),
            ("mix34.level", (49,)), ("out12.enabled", (2,)),
        ):
            with self.assertRaises(ValueError):
                validate(key, values)
        with self.assertRaises(ValueError):
            parse_contents("numid=1,iface=MIXER,name='unrelated'\n  : values=255")

    def test_playback_selection_waits_for_apply_and_preserves_mixer(self):
        box = self.window.playback_box
        wait_for(lambda: box.available)
        before = self.card.read()
        box.rate.setCurrentIndex(box.rate.findData(44100))
        box.period.setCurrentIndex(box.period.findData(128))
        # A status poll must not overwrite the draft or apply it.
        box.update_state(self.window.worker.playback.read())
        self.assertEqual(box.selection(), {'rate': 44100, 'period': 128})
        self.assertEqual(self.window.worker.playback.saved, {'rate': 0, 'period': 256})
        self.assertTrue(box.apply_button.isEnabled())
        box.apply_button.click()
        wait_for(lambda: not box.busy)
        self.assertEqual(self.window.worker.playback.saved, {'rate': 44100, 'period': 128})
        self.assertIn('44,1', box.actual.text())
        self.assertFalse(box.apply_button.isEnabled())
        self.assertEqual(self.card.read(), before)
        self.assertEqual(self.card.writes, [])

    def test_playback_error_keeps_draft_and_allows_retry(self):
        box = self.window.playback_box
        wait_for(lambda: box.available)
        box.period.setCurrentIndex(box.period.findData(64))
        with patch.object(self.window.worker.playback, 'apply', side_effect=RuntimeError('test failure')):
            box.apply_button.click()
            wait_for(lambda: not box.busy)
        self.assertIn('Не применено', box.message.text())
        self.assertEqual(box.selection()['period'], 64)
        self.assertTrue(box.apply_button.isEnabled())
        self.assertEqual(self.card.writes, [])

    def test_settings_grid_and_wrapped_hint_survive_theme_language_and_resize(self):
        theme, language = APP.maya_theme, APP.maya_language
        old_theme, old_language = theme.mode, language.mode
        before = self.card.read()
        dialog = self.window.settings_dialog
        try:
            self.window.show_settings()
            for mode in ('dark', 'light'):
                theme.set_mode(mode)
                for locale in ('ru', 'en'):
                    language.set_language(locale, save=False)
                    for width in (640, 800):
                        with self.subTest(theme=mode, language=locale, width=width):
                            # Request a short dialog too: wrapped captions must
                            # determine its usable minimum height after reflow.
                            dialog.resize(width, 1)
                            for _ in range(3):
                                APP.processEvents()
                            controls = (self.window.theme_choice,
                                        self.window.language_choice,
                                        self.window.wheel_choice)
                            columns = [(control.mapTo(dialog, QPoint()).x(), control.width())
                                       for control in controls]
                            self.assertEqual(columns, [columns[0]] * len(columns))
                            hint = self.window.wheel_hint
                            self.assertGreaterEqual(hint.height(), hint.heightForWidth(hint.width()))
                            self.assertEqual(self.card.read(), before)
                            self.assertEqual(self.card.writes, [])
        finally:
            dialog.close()
            theme.set_mode(old_theme)
            language.set_language(old_language, save=False)
            APP.processEvents()

    def test_theme_changes_keep_audio_mute_and_playback_draft(self):
        box = self.window.playback_box
        wait_for(lambda: box.available)
        box.rate.setCurrentIndex(box.rate.findData(44100))
        self.window.silence()
        wait_for(lambda: self.card.values['out12.enabled'] == self.card.values['out34.enabled'] == (0,))
        before = self.card.read()
        count = len(self.card.writes)
        for mode, dark in (('light', False), ('dark', True), ('light', False)):
            self.window.theme_choice.setCurrentIndex(self.window.theme_choice.findData(mode))
            APP.processEvents()
            self.assertEqual(APP.palette().window().color().lightnessF() < 0.5, dark)
            self.assertEqual(box.selection()['rate'], 44100)
            self.assertTrue(self.window.quiet.isChecked())
            self.assertEqual(self.card.read(), before)
            self.assertEqual(len(self.card.writes), count)
        APP.maya_theme.set_mode('system')

    def test_wheel_has_exact_steps_and_keeps_keyboard_precision(self):
        pair = self.window.pairs[0]
        slider = pair.faders[0]
        def wheel(amount):
            slider.wheelEvent(QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(), QPoint(0, amount),
                                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                         Qt.ScrollPhase.NoScrollPhase, False))
        old_lines = APP.wheelScrollLines()
        APP.setWheelScrollLines(7)
        try:
            wheel(60)
            self.assertEqual(slider.value(), 195)
            wheel(60)
            wait_for(lambda: self.card.values['out12.volume'] == (196, 196))
            self.window.wheel_choice.setCurrentIndex(self.window.wheel_choice.findData(2))
            wheel(-120)
            wait_for(lambda: self.card.values['out12.volume'] == (192, 192))
            QTest.keyClick(slider, Qt.Key.Key_Up)
            wait_for(lambda: self.card.values['out12.volume'] == (193, 193))
            slider = pair.faders[2]
            wheel(120)
            wait_for(lambda: self.card.values['mix12.level'] == (20,))
        finally:
            APP.setWheelScrollLines(old_lines)

    def test_fader_drag_and_scale_click(self):
        slider = self.window.pairs[0].faders[0]
        self.window.show()
        APP.processEvents()
        start = QPoint(round(slider.width() * .65), slider._position(slider.value()))
        target = QPoint(start.x(), slider._position(207))
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(slider, target)
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=target)
        wait_for(lambda: 206 <= self.card.values['out12.volume'][0] <= 208)
        before = slider.value()
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(start.x(), 0))
        self.assertEqual(slider.value(), before + slider.pageStep())

    def test_input_controls_recording_gain_mute_and_source(self):
        strip = self.window.input_previews[0]
        self.assertTrue(strip.gain.isEnabled())
        self.assertFalse(strip.phantom.isEnabled())
        strip.source_buttons[1].click()
        wait_for(lambda: self.card.values['in12.source'] == (1,))
        self.assertTrue(strip.phantom.isEnabled())
        strip.gain.setValue(12)
        wait_for(lambda: self.card.values['in12.gain'] == (12,))
        strip.faders[0].setValue(245)
        wait_for(lambda: self.card.values['in12.volume'] == (135, 135))
        strip.mute.click()
        wait_for(lambda: self.card.values['in12.enabled'] == (0,))
        strip.mute.click()
        wait_for(lambda: self.card.values['in12.enabled'] == (1,))
        self.assertEqual(self.card.values['in12.volume'], (135, 135))
        self.assertEqual(self.card.values['phantom.enabled'], (0,))

    def test_digital_controls_visible_on_main_without_settings_or_writes(self):
        w = self.window
        w.show()
        APP.processEvents()
        self.assertFalse(w.settings_dialog.isVisible())
        self.assertFalse(w.settings_dialog.isAncestorOf(w.digital_box))
        self.assertTrue(w.digital_box.isVisibleTo(w))
        self.assertIs(w.digital_box.parentWidget(), w.mixer_area.widget())
        self.assertGreater(w.digital_box.x(), w.pairs[0].parentWidget().geometry().right())
        self.assertEqual(w.digital_box.heading.height(), w.input_heading.height())
        for control in (w.digital_box.tx, w.digital_box.output, w.digital_box.format,
                        w.digital_box.input, w.digital_box.receiver, w.digital_box.signal):
            self.assertTrue(control.isVisibleTo(w))
        self.assertEqual(w.mixer_area.verticalScrollBar().maximum(), 0)
        self.assertEqual(self.card.writes, [])

    def test_tray_start_and_tall_window_keep_channel_badges_compact(self):
        w = self.window
        # setUp processes the deferred fit while the window is still hidden,
        # matching --tray startup rather than only testing an already shown UI.
        self.assertFalse(w.isVisible())
        self.assertEqual(w.digital_box.input_badge.minimumHeight(), 0)
        minimum = w.minimumSize()
        w.show()
        APP.processEvents()
        badges = w.findChildren(QLabel, 'pairBadge')
        self.assertEqual(len(badges), 5)
        for extra in (0, 400):
            w.resize(w.width(), minimum.height() + extra)
            APP.processEvents()
            w.fit_minimum_size()
            APP.processEvents()
            self.assertEqual(w.minimumSize(), minimum)
            for badge in badges:
                self.assertEqual(badge.height(), badge.sizeHint().height())
        self.assertEqual(self.card.writes, [])

    def test_digital_input_restores_analog_mode_without_phantom(self):
        box = self.window.digital_box
        strip = self.window.input_previews[0]
        strip.source_buttons[1].click()
        wait_for(lambda: self.card.values['in12.source'] == (1,))
        self.card.values['phantom.enabled'] = (1,)
        self.card.values['monitor12.enabled'] = (1,)
        wait_for(lambda: self.window.values['phantom.enabled'] == (1,))
        box.input.setCurrentIndex(box.input.findData(1))
        wait_for(lambda: self.card.values['in12.source'] == (2,))
        self.assertEqual(self.card.values['phantom.enabled'], (0,))
        self.assertEqual(self.card.values['monitor12.enabled'], (0,))
        for widget in (strip.gain, strip.phantom, strip.monitor, strip.link, *strip.faders):
            self.assertFalse(widget.isEnabled())
        self.assertTrue(strip.mute.isEnabled())
        self.assertEqual([d.text() for d in strip.displays], ['—', '—'])
        self.window.received_peaks((0, 0, 0, 0, 0, 0, 0, 0))
        self.assertEqual(box.input.currentData(), 1)
        box.input.setCurrentIndex(box.input.findData(0))
        wait_for(lambda: self.card.values['in12.source'] == (1,))
        self.assertTrue(strip.gain.isEnabled())
        self.assertEqual(self.card.values['phantom.enabled'], (0,))
        strip.source_buttons[0].click()
        wait_for(lambda: self.card.values['in12.source'] == (0,))

    def test_digital_choice_buttons_mouse_keyboard_and_no_repeat_write(self):
        box = self.window.digital_box
        self.window.show()
        box.output.buttons[1].click()
        wait_for(lambda: self.card.values['spdif.output.source'] == (1,))
        self.assertEqual([b.isChecked() for b in box.output.buttons], [False, True])
        writes = len(self.card.writes)
        box.output.buttons[1].click()
        APP.processEvents()
        self.assertEqual(len(self.card.writes), writes)
        box.output.buttons[1].setFocus()
        QTest.keyClick(box.output.buttons[1], Qt.Key.Key_Left)
        wait_for(lambda: self.card.values['spdif.output.source'] == (0,))
        self.assertEqual([b.isChecked() for b in box.output.buttons], [True, False])

    def test_digital_lock_clears_when_device_disconnects(self):
        box = self.window.digital_box
        self.card.values.update({'spdif.capture.locked': (1,),
                                 'spdif.capture.rate': (96000,),
                                 'spdif.capture.professional': (1,)})
        wait_for(lambda: '96' in box.signal.text())
        self.assertEqual(box.signal.property('state'), 'locked')
        self.card.down = True
        wait_for(lambda: not self.window.ready)
        self.assertEqual(box.signal.property('state'), 'unavailable')
        self.assertNotIn('96', box.signal.text())
        self.assertFalse(box.isEnabled())
        self.card.down = False
        wait_for(lambda: self.window.ready and '96' in box.signal.text())
        self.assertEqual(box.signal.property('state'), 'locked')
        self.assertEqual(self.card.writes, [])

    def test_digital_routes_and_status_poll_without_feedback(self):
        box = self.window.digital_box
        box.output.setCurrentIndex(box.output.findData(1))
        box.format.setCurrentIndex(box.format.findData(1))
        box.receiver.setCurrentIndex(box.receiver.findData(1))
        wait_for(lambda: all(self.card.values[k] == (1,) for k in (
            'spdif.output.source', 'spdif.professional.enabled', 'spdif.loopback.enabled')))
        writes = len(self.card.writes)
        self.card.values.update({'spdif.capture.locked': (1,), 'spdif.capture.rate': (48000,),
                                 'spdif.capture.professional': (1,)})
        wait_for(lambda: '48' in box.signal.text())
        self.assertIn('PRO', box.signal.text())
        self.assertEqual(len(self.card.writes), writes)
        self.assertFalse(any(k.startswith('spdif.capture.') for k, _ in self.card.writes))

    def test_global_mute_preserves_digital_transmitter_state(self):
        box = self.window.digital_box
        for initially_on in (True, False):
            self.window.write('spdif.output.enabled', (int(initially_on),))
            wait_for(lambda: self.card.values['spdif.output.enabled'] == (int(initially_on),))
            self.window.quiet.click()
            wait_for(lambda: self.card.values['spdif.output.enabled'] == (0,) and self.card.values['out34.enabled'] == (0,))
            self.assertFalse(box.tx.isEnabled())
            self.window.quiet.click()
            wait_for(lambda: self.card.values['out34.enabled'] == (1,) and self.card.values['spdif.output.enabled'] == (int(initially_on),))
            self.assertTrue(box.tx.isEnabled())

    def test_input_controls_disabled_for_playback_only_module(self):
        from alsa_backend import CONTROLS
        self.card.values = {k:v for k,v in self.card.values.items() if k in CONTROLS}
        wait_for(lambda: 'in12.volume' not in self.window.values)
        self.assertFalse(self.window.digital_box.isEnabled())
        for strip in self.window.input_previews:
            self.assertFalse(strip.gain.isEnabled())
            self.assertFalse(strip.monitor.isEnabled())
            self.assertFalse(strip.link.isEnabled())

    def test_monitor_and_global_mute_restore(self):
        strip = self.window.input_previews[0]
        strip.monitor.click()
        wait_for(lambda: self.card.values['monitor12.enabled'] == (1,))
        self.window.quiet.click()
        wait_for(lambda: self.card.values['monitor12.enabled'] == (0,))
        self.window.quiet.click()
        wait_for(lambda: self.card.values['monitor12.enabled'] == (1,))

    def test_pair_mute_restores_incoming_mix_and_keeps_levels(self):
        pair = self.window.pairs[1]
        before = self.card.read()
        pair.mute.click()
        wait_for(lambda: self.card.values['out34.enabled'] == self.card.values['mix34.enabled'] == (0,))
        self.assertTrue(pair.mute.isChecked())
        self.assertFalse(pair.direct.isEnabled())
        self.assertFalse(pair.mix_on.isEnabled())
        for key in before:
            if key not in ('out34.enabled', 'mix34.enabled'):
                self.assertEqual(before[key], self.card.values[key])
        # Levels remain editable while muted and aren't overwritten on return.
        pair.faders[0].setValue(190)
        pair.faders[2].setValue(40)
        wait_for(lambda: self.card.values['out34.volume'] == (190, 190) and self.card.values['mix34.level'] == (40,))
        pair.mute.click()
        wait_for(lambda: self.card.values['out34.enabled'] == self.card.values['mix34.enabled'] == (1,))
        self.assertEqual(self.card.values['out34.volume'], (190, 190))
        self.assertEqual(self.card.values['mix34.level'], (40,))
        self.assertTrue(pair.mix_on.isEnabled())

    def test_global_mute_preserves_individual_pair_mute(self):
        pair = self.window.pairs[1]
        before = self.card.read()
        pair.mute.click()
        wait_for(lambda: self.card.values['out34.enabled'] == self.card.values['mix34.enabled'] == (0,))
        partial = self.card.read()
        self.window.quiet.click()
        wait_for(lambda: self.card.values['out12.enabled'] == (0,))
        self.assertFalse(pair.mute.isEnabled())
        pair.mute.click()
        self.assertIn('out34', self.window.pair_mutes)
        self.window.quiet.click()
        wait_for(lambda: self.card.read() == partial)
        self.assertTrue(pair.mute.isChecked())
        pair.mute.click()
        wait_for(lambda: self.card.read() == before)

    def test_pair_mute_preserves_an_already_disabled_direct_signal(self):
        pair = self.window.pairs[1]
        self.window.write('out34.enabled', (0,))
        wait_for(lambda: self.card.values['out34.enabled'] == (0,))
        before = self.card.read()
        pair.mute.click()
        pair.mute.click()
        wait_for(lambda: self.card.read() == before and len(self.card.writes) >= 3)
        self.assertFalse(pair.mute.isChecked())
        self.assertEqual(self.window.pair_mutes, {})

    def test_pair_mute_yields_to_external_route_change_and_disconnect(self):
        pair = self.window.pairs[1]
        pair.mute.click()
        wait_for(lambda: self.card.values['mix34.enabled'] == (0,))
        self.card.values['mix34.enabled'] = (1,)
        wait_for(lambda: not self.window.pair_mutes)
        self.assertFalse(pair.mute.isChecked())
        pair.mute.click()
        wait_for(lambda: self.card.values['mix34.enabled'] == (0,))
        self.card.down = True
        wait_for(lambda: not self.window.ready)
        self.assertEqual(self.window.pair_mutes, {})
        count = len(self.card.writes)
        self.card.down = False
        wait_for(lambda: self.window.ready)
        self.assertFalse(pair.mute.isChecked())
        self.assertEqual(len(self.card.writes), count)

    def test_mix_knob_uses_relative_drag_and_settings_do_not_change_audio(self):
        knob = self.window.pairs[1].faders[2]
        self.window.show()
        APP.processEvents()
        QTest.mouseClick(knob, Qt.MouseButton.LeftButton, pos=QPoint(5, 40))
        self.assertEqual(knob.value(), 48)
        QTest.mousePress(knob, Qt.MouseButton.LeftButton, pos=QPoint(29, 20))
        QTest.mouseMove(knob, QPoint(29, 44))
        QTest.mouseRelease(knob, Qt.MouseButton.LeftButton, pos=QPoint(29, 44))
        wait_for(lambda: self.card.values['mix34.level'] == (40,))
        before, count = self.card.read(), len(self.card.writes)
        self.window.settings_button.click()
        self.assertTrue(self.window.settings_dialog.isVisible())
        self.assertTrue(self.window.isVisible())
        self.window.settings_dialog.close()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.card.read(), before)
        self.assertEqual(len(self.card.writes), count)

    def test_system_theme_watches_atomic_kde_updates_and_remembers_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            light = Path('/usr/share/color-schemes/BreezeLight.colors').read_text()
            dark = Path('/usr/share/color-schemes/BreezeDark.colors').read_text()
            config = root / 'kdeglobals'
            config.write_text(light)
            theme = ThemeManager(APP, persist=True, config_dir=root)
            try:
                self.assertGreater(APP.palette().window().color().lightnessF(), .5)
                replacement = root / 'kdeglobals.new'
                replacement.write_text(dark)
                replacement.replace(config)
                wait_for(lambda: APP.palette().window().color().lightnessF() < .5)
                self.assertIn(str(config), theme.watcher.files())
                theme.set_mode('light')
                self.assertGreater(APP.palette().window().color().lightnessF(), .5)
                config.write_text(light + '\n# changed\n')
                wait_for(lambda: theme._last_kde.endswith('# changed\n'))
                self.assertEqual(theme.mode, 'light')
                theme.set_mode('system')
                config.write_text(dark)
                wait_for(lambda: APP.palette().window().color().lightnessF() < .5)
                theme.set_mode('dark')
                theme.set_wheel(3)
                again = ThemeManager(APP, persist=True, config_dir=root)
                self.assertEqual((again.mode, again.wheel_db), ('dark', 3))
                again.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertEqual(self.card.writes, [])
            finally:
                theme.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                APP.maya_theme.reload()


if __name__ == "__main__":
    unittest.main()
