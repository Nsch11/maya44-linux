# SPDX-License-Identifier: GPL-3.0-or-later
"""New family captions/layout use fake cards only; no device writes."""
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QLabel
from check_panel import APP, FakeCard, wait_for
from card_discovery import MayaDevice
from maya44e import MixerWindow
import unittest


class FamilyGuiChecks(unittest.TestCase):
    def test_model_titles_and_meters_target_the_same_selected_card(self):
        for subsystem, card_id, model in ((1, 'MAYA44e', 'MAYA44e'),
                                          (2, 'MAYA44XTe', 'MAYA44 XTe'),
                                          (3, 'MAYA44eX', 'MAYA44 eX')):
            card = FakeCard();card.device = MayaDevice(card_id, model, 8, '0000:09:08.0', subsystem)
            window = MixerWindow(card, demo=True)
            try:
                wait_for(lambda: window.ready)
                for theme, language in (('light', 'en'), ('dark', 'ru')):
                    APP.maya_theme.set_mode(theme, save=False)
                    APP.maya_language.set_language(language, save=False)
                    window.show();APP.processEvents();window.fit_minimum_size()
                    self.assertEqual(window.windowTitle(), model)
                    self.assertEqual(window.meter_worker.factory.keywords['card'], 'hw:' + card_id)
                    title = next(w for w in window.findChildren(QLabel) if w.objectName()=='title')
                    self.assertEqual(title.text(), model)
                    self.assertEqual(bool(title.toolTip()), subsystem != 1)
                    self.assertEqual(window.mixer_area.verticalScrollBar().maximum(), 0)
                    self.assertEqual(card.writes, [])
            finally:
                window.request_exit();wait_for(lambda: not window.worker.isRunning())
                window.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        APP.maya_language.set_language('ru', save=False)


if __name__ == '__main__': unittest.main()
