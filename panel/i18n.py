# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt Linguist catalogs and live UI translation; no audio dependencies."""
import configparser
import os
from pathlib import Path

from PyQt6.QtCore import (QCoreApplication, QLibraryInfo, QLocale, QObject,
                          QSettings, QTranslator, pyqtSignal, pyqtSlot)

translate = QCoreApplication.translate
CATALOGS = Path(__file__).resolve().parent / 'translations'


def system_language(config_dir=None):
    root = Path(config_dir or os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    plasma = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        plasma.read(root / 'plasma-localerc', encoding='utf-8')
    except (OSError, configparser.Error):
        plasma.clear()
    # Plasma can set the UI language separately from regional formats.
    preferred = plasma.get('Translations', 'LANGUAGE', fallback='').strip()
    preferred = preferred or os.environ.get('LANGUAGE', '').strip()
    if preferred:
        languages = preferred.split(':')
    else:
        languages = QLocale.system().uiLanguages()
        neutral = all(language.upper().split('.')[0] in ('C', 'POSIX') for language in languages)
        if neutral:
            # Launchers and development shells often force C.UTF-8. In that
            # case use Plasma's configured locale instead of guessing English.
            configured = plasma.get('Formats', 'LC_MESSAGES', fallback='') or plasma.get('Formats', 'LANG', fallback='')
            languages = [configured] if configured else languages
    for language in languages:
        base = language.replace('-', '_').split('_')[0].lower()
        if base in ('ru', 'en'):
            return base
    return 'en'


class LanguageManager(QObject):
    changed = pyqtSignal()

    def __init__(self, app, persist=False, config_dir=None, language=None):
        super().__init__(app)
        self.app, self.persist = app, persist
        root = Path(config_dir or os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
        self.config_dir = root
        self.settings = QSettings(str(root / 'maya44e/panel.ini'), QSettings.Format.IniFormat)
        mode = self.settings.value('appearance/language', 'system') if persist else 'system'
        self.translators = []
        self.set_language(language or (mode if mode in ('system', 'en', 'ru') else 'system'), save=False)

    def set_language(self, mode, save=True):
        if mode not in ('system', 'en', 'ru'):
            raise ValueError('Unknown language')
        self.system_language = system_language(self.config_dir)
        language = self.system_language if mode == 'system' else mode
        loaded = []
        if language != 'en':
            catalog = QTranslator(self)
            if not catalog.load(str(CATALOGS / f'maya44e_{language}.qm')):
                raise RuntimeError(f'Cannot load MAYA44e translation: {language}')
            qt = QTranslator(self)
            if qt.load(QLocale(language), 'qtbase', '_', QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
                loaded.append(qt)
            else:
                qt.deleteLater()
            loaded.append(catalog)
        for old in self.translators:
            self.app.removeTranslator(old)
            old.deleteLater()
        self.translators = loaded
        for catalog in loaded:
            self.app.installTranslator(catalog)
        self.mode, self.language = mode, language
        self.locale = QLocale(language)
        self.locale.setNumberOptions(QLocale.NumberOption.OmitGroupSeparator)
        if save and self.persist:
            self.settings.setValue('appearance/language', mode)
            self.settings.sync()
        self.changed.emit()


def number(value, decimals=None):
    app = QCoreApplication.instance()
    locale = app.maya_language.locale if app and hasattr(app, 'maya_language') else QLocale('en')
    return locale.toString(float(value), 'g' if decimals is None else 'f',
                           6 if decimals is None else decimals).replace('-', '−')


class TextBinding(QObject):
    """Own explicit text callbacks with their widget, preserving all UI state."""
    def __init__(self, widget):
        super().__init__(widget)
        self.widget, self.callbacks = widget, {}
        QCoreApplication.instance().maya_language.changed.connect(self.refresh)

    @pyqtSlot()
    def refresh(self):
        for setter, callback in self.callbacks.items():
            getattr(self.widget, setter)(callback())


def localize(widget, setter, callback):
    binding = getattr(widget, '_text_binding', None)
    if binding is None:
        binding = widget._text_binding = TextBinding(widget)
    binding.callbacks[setter] = callback
    getattr(widget, setter)(callback())
    return widget
