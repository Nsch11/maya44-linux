# SPDX-License-Identifier: GPL-3.0-or-later
"""Analog-only RFC regression; offscreen widgets and an in-memory card only."""
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication, QEvent
from alsa_backend import validate
from check_panel import APP, FakeCard, wait_for
from maya44e import MixerWindow


class RfcCard(FakeCard):
    def __init__(self):
        super().__init__()
        self.values = {key: value for key, value in self.values.items()
                       if key != 'phantom.enabled' and not key.startswith('spdif.')}

    def write(self, key, values):
        if key not in self.values:
            raise OSError(2, 'Control is not provided by the RFC driver')
        values = validate(key, values)
        if key.endswith('.source') and values[0] > 1:
            raise ValueError('RFC capture sources are analog only')
        self.writes.append((key, values))
        self.values[key] = values


class NoPrivateSettings:
    def read(self):
        raise FileNotFoundError('maya44e_status is not provided by the RFC driver')


class RfcPanelChecks(unittest.TestCase):
    def test_analog_controls_work_without_optional_capabilities(self):
        card = RfcCard()
        self.assertEqual(len(card.values), 18)
        window = MixerWindow(card, demo=True, start_worker=False,
                             playback=NoPrivateSettings())
        try:
            # Call synchronously so a missing-key regression is a test failure,
            # rather than an uncaught exception in a Qt signal callback.
            window.received(window.worker.current_revision(), card.read())
            self.assertTrue(window.ready)
            self.assertFalse(window.digital_box.isEnabled())
            self.assertFalse(window.input_previews[0].phantom.isEnabled())
            self.assertFalse(window.input_previews[0].phantom.isChecked())
            self.assertEqual(card.writes, [])

            window.worker.start()
            wait_for(lambda: window.worker.connected and
                     'maya44e_status' in window.playback_box.actual.toolTip())
            self.assertFalse(window.playback_box.available)
            self.assertFalse(window.playback_box.apply_button.isEnabled())
            window.received_peaks(None, 'Hardware Peak Meter is unavailable')
            self.assertIsNone(window.meter_values)

            with patch('maya44e.QMessageBox.question') as question:
                window.input_previews[0].set_phantom(True)
                window.input_previews[0].set_phantom(False)
                question.assert_not_called()
            self.assertEqual(card.writes, [])

            for strip in window.input_previews:
                for source in (1, 0):
                    strip.source_buttons[source].click()
                    self.assertNotIn('phantom.enabled', window.values)
                    wait_for(lambda: card.values[strip.pair + '.source'] == (source,))
                    self.assertFalse(window.input_previews[0].phantom.isEnabled())
                strip.gain.setValue(6)
                wait_for(lambda: card.values[strip.pair + '.gain'] == (6,))
                strip.faders[0].setValue(250)
                wait_for(lambda: card.values[strip.pair + '.volume'] == (140, 140))
                strip.mute.click()
                wait_for(lambda: card.values[strip.pair + '.enabled'] == (0,))
                strip.mute.click()
                wait_for(lambda: card.values[strip.pair + '.enabled'] == (1,))
                strip.monitor.click()
                wait_for(lambda: card.values[strip.monitor_key] == (1,))

            window.pairs[0].faders[0].setValue(193)
            wait_for(lambda: card.values['out12.volume'] == (193, 193))
            window.pairs[0].mix_on.click()
            wait_for(lambda: card.values['mix12.enabled'] == (1,))
            before_mute = card.read()
            window.quiet.click()
            wait_for(lambda: card.values['monitor12.enabled'] == (0,) and
                     card.values['out34.enabled'] == (0,))
            window.quiet.click()
            wait_for(lambda: card.read() == before_mute)
            self.assertEqual(len(card.values), 18)
            self.assertTrue(window.ready)
            self.assertFalse(window.digital_box.isEnabled())
            self.assertTrue(all(key in card.values for key, _ in card.writes))
        finally:
            window.request_exit()
            wait_for(lambda: not window.worker.isRunning())
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            APP.processEvents()


if __name__ == '__main__':
    unittest.main()
