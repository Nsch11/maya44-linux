# SPDX-License-Identifier: GPL-3.0-or-later
"""Localization checks using fake audio and temporary preferences only."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
import string
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from PyQt6.QtCore import QCoreApplication, QEvent, QTranslator
from PyQt6.QtWidgets import QLabel, QPushButton
from check_panel import APP, FakeCard, wait_for
from i18n import CATALOGS, LanguageManager, number, system_language, translate
from maya44e import MixerWindow


class LanguageChecks(unittest.TestCase):
    def setUp(self):
        APP.maya_language.set_language('ru', save=False)
        self.card = FakeCard()
        self.window = MixerWindow(self.card, demo=True)
        wait_for(lambda: self.window.ready and self.window.playback_box.state is not None)

    def tearDown(self):
        self.window.request_exit()
        wait_for(lambda: not self.window.worker.isRunning())
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        # Also exercises automatic disconnection of deleted text bindings.
        APP.maya_language.set_language('en', save=False)
        APP.processEvents()

    def test_live_roundtrip_preserves_mutes_levels_and_unapplied_settings(self):
        w = self.window
        w.show()
        w.playback_box.rate.setCurrentIndex(w.playback_box.rate.findData(44100))
        w.playback_box.period.setCurrentIndex(w.playback_box.period.findData(128))
        w.pairs[1].mute.click()
        wait_for(lambda: self.card.values['out34.enabled'] == self.card.values['mix34.enabled'] == (0,))
        w.quiet.click()
        wait_for(lambda: self.card.values['out12.enabled'] == (0,))
        before, writes = self.card.read(), len(self.card.writes)
        muted_routes, pair_mutes = dict(w.muted_routes), dict(w.pair_mutes)
        for mode, direct, restore, rate in [('en','Disable source','Unmute','44.1 kHz'), ('ru','Откл. источник','Вернуть звук','44,1 кГц')]:
            w.language_choice.setCurrentIndex(w.language_choice.findData(mode))
            APP.processEvents()
            self.assertEqual(w.pairs[0].direct.text(), direct)
            self.assertEqual(w.quiet.text(), restore)
            self.assertEqual(w.pairs[1].mute.text(), restore)
            self.assertEqual(w.playback_box.rate.currentText(), rate)
            self.assertEqual(w.playback_box.selection(), {'rate':44100,'period':128})
            self.assertTrue(w.playback_box.apply_button.isEnabled())
            self.assertFalse(w.pairs[1].mix_on.isEnabled())
            self.assertEqual(w.muted_routes, muted_routes)
            self.assertEqual(w.pair_mutes, pair_mutes)
            self.assertEqual(self.card.read(), before)
            self.assertEqual(len(self.card.writes), writes)
            self.assertEqual(w.digital_box.tx.text(), 'Output' if mode == 'en' else 'Выход')
            self.assertEqual(w.digital_box.receiver.itemText(1), 'Loopback' if mode == 'en' else 'Петля')
            self.assertEqual(w.windowTitle(), 'MAYA44e')
            self.assertEqual(w.mixer_area.verticalScrollBar().maximum(), 0)
        w.quiet.click()
        wait_for(lambda: self.card.values['out12.enabled'] == (1,))
        self.assertEqual(self.card.values['out34.enabled'], (0,))
        w.pairs[1].mute.click()
        wait_for(lambda: self.card.values['out34.enabled'] == (1,) and self.card.values['mix34.enabled'] == (1,))

    def test_disconnected_controls_stay_disabled_and_messages_translate(self):
        w = self.window
        self.card.down = True
        wait_for(lambda: not w.ready)
        w.playback_box.failed('test error')
        writes = len(self.card.writes)
        APP.maya_language.set_language('en', save=False)
        self.assertEqual(w.status.text(), '● Device unavailable')
        self.assertEqual(w.playback_box.actual.text(), 'Device settings unavailable')
        self.assertFalse(w.pairs[0].direct.isEnabled())
        self.assertFalse(w.playback_box.rate.isEnabled())
        self.assertFalse(w.quiet.isEnabled())
        self.assertIn('1–2', w.pairs[0].faders[0].accessibleName())
        self.assertIn('3–4', w.pairs[1].faders[0].accessibleName())
        for widget in w.findChildren(QLabel) + w.findChildren(QPushButton):
            self.assertFalse(any('А' <= c <= 'я' or c in 'Ёё' for c in widget.text()), widget.text())
        self.assertEqual(len(self.card.writes), writes)
        self.card.down = False
        wait_for(lambda: w.ready)
        self.assertEqual(w.status.text(), '● Demo')
        self.assertEqual(w.status.accessibleName(), 'MAYA44e demo')
        self.assertEqual(len(self.card.writes), writes)

    def test_localized_numbers_and_current_readings_update_without_audio_writes(self):
        w = self.window
        state = dict(w.playback_box.state)
        state['hardware'] = {**state['hardware'], 'rate':44100, 'active':False}
        w.playback_box.update_state(state)
        w.update_playback_summary(state)
        APP.maya_language.set_language('en', save=False)
        self.assertEqual(number(44.1), '44.1')
        self.assertIn('Idle · last configuration: 44.1 kHz', w.playback_box.actual.text())
        self.assertIn('44.1 kHz', w.playback_summary.text())
        self.assertIn(' dB', w.pairs[0].faders[2].toolTip())
        APP.maya_language.set_language('ru', save=False)
        self.assertEqual(number(44.1), '44,1')
        self.assertIn('В простое · последний режим: 44,1 кГц', w.playback_box.actual.text())
        self.assertIn(' дБ', w.pairs[0].faders[2].toolTip())
        self.assertEqual(self.card.writes, [])

    def test_catalog_compiled_translations_and_format_fields_match(self):
        tree = ET.parse(CATALOGS / 'maya44e_ru.ts')
        catalog = QTranslator()
        self.assertTrue(catalog.load(str(CATALOGS / 'maya44e_ru.qm')))
        fields = lambda text: {field for _,field,_,_ in string.Formatter().parse(text) if field is not None}
        for context in tree.findall('context'):
            for message in context.findall('message'):
                source, target = message.findtext('source'), message.findtext('translation')
                self.assertTrue(target, source)
                self.assertEqual(fields(source), fields(target), source)
                self.assertEqual(catalog.translate(context.findtext('name').encode(), source.encode('utf-8')), target, source)

    def test_system_language_preference_persists_only_explicit_choices(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {'LANGUAGE':'ru_RU:en_US'}):
                self.assertEqual(system_language(root), 'ru')
            with patch.dict(os.environ, {'LANGUAGE':'en_GB:ru_RU'}):
                self.assertEqual(system_language(root), 'en')
            with patch.dict(os.environ, {'LANGUAGE':'de_DE'}), patch('i18n.QLocale.system') as locale:
                locale.return_value.uiLanguages.return_value = ['de-DE']
                self.assertEqual(system_language(root), 'en')
        APP.maya_language.set_language('en', save=False)
        with tempfile.TemporaryDirectory() as root:
            manager = LanguageManager(APP, persist=True, config_dir=root, language='en')
            self.assertFalse((Path(root) / 'maya44e/panel.ini').exists())
            manager.set_language('ru')
            self.assertEqual(manager.settings.value('appearance/language'), 'ru')
            manager.set_language('en', save=False)
            self.assertEqual(manager.settings.value('appearance/language'), 'ru')
            manager.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

            manager = LanguageManager(APP, persist=True, config_dir=root)
            self.assertEqual(manager.mode, 'ru')
            self.assertEqual(translate('Panel','Settings'), 'Настройки')
            manager.set_language('en', save=False)
            manager.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_system_language_uses_plasma_when_launcher_forces_c_locale(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'LANGUAGE':''}), patch('i18n.QLocale.system') as locale:
            locale.return_value.uiLanguages.return_value = ['C']
            config = Path(root) / 'plasma-localerc'
            config.write_text('[Formats]\nLANG=ru_RU.UTF-8\n')
            self.assertEqual(system_language(root), 'ru')
            with patch.object(APP.maya_language, 'config_dir', Path(root)):
                APP.maya_language.set_language('en', save=False)
                self.window.language_choice.setCurrentIndex(self.window.language_choice.findData('system'))
                self.assertEqual(APP.maya_language.language, 'ru')
                self.assertEqual(self.window.settings_button.text(), 'Настройки')
                self.assertEqual(self.window.language_choice.currentText(), 'Как в системе (Русский)')
                self.assertEqual(self.card.writes, [])
            # An explicit UI preference wins over regional formatting.
            config.write_text('[Formats]\nLANG=ru_RU.UTF-8\n[Translations]\nLANGUAGE=en_US:ru_RU\n')
            self.assertEqual(system_language(root), 'en')
            config.write_text('[Formats]\nLANG=ru_RU.UTF-8\nLC_MESSAGES=en_US.UTF-8\n')
            self.assertEqual(system_language(root), 'en')
            config.unlink()
            self.assertEqual(system_language(root), 'en')


if __name__ == '__main__':
    unittest.main()
