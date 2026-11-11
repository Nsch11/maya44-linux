# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only metering regressions; no real ALSA device is opened."""
import threading
import time
import unittest
from unittest.mock import patch
from PyQt6.QtCore import QCoreApplication,QEvent,QPoint
from PyQt6.QtWidgets import QLabel
from check_panel import APP,FakeCard,wait_for
from alsa_backend import AlsaBackend,CONTROLS,ALL_CONTROLS
from maya44e import MixerWindow
from meter_backend import MeterWorker
from level_meter import LevelMeter


class MeterChecks(unittest.TestCase):
    def test_painted_rail_and_meter_have_the_same_visible_endpoints(self):
        card=FakeCard();w=MixerWindow(card,demo=True)
        theme=APP.maya_theme.mode
        try:
            w.show();wait_for(lambda:w.ready)
            for mode in ('dark','light'):
                APP.maya_theme.set_mode(mode,save=False)
                for _ in range(5):APP.processEvents()
                for strip in [*w.pairs,*w.input_previews]:
                    for fader,meter in zip(strip.faders[:2],strip.meters):
                        fader.blockSignals(True);fader.setValue(190);fader.blockSignals(False)
                        fader.clearFocus();meter.set_available(False)
                        bounds=[]
                        for widget,x in ((fader,round(fader.rail_x())),(meter,meter.width()//2)):
                            image=widget.grab().toImage();scale=widget.devicePixelRatioF()
                            x=round(x*scale);background=image.pixelColor(x,0)
                            pixels=[y for y in range(image.height()) if image.pixelColor(x,y)!=background]
                            origin=round(widget.mapTo(w,QPoint()).y()*scale)
                            bounds.append((min(pixels)+origin,max(pixels)+origin))
                        for rail,meter_edge in zip(*bounds):
                            self.assertLessEqual(abs(rail-meter_edge),1,bounds)
            self.assertEqual(card.writes,[])
        finally:
            w.request_exit();wait_for(lambda:not w.worker.isRunning())
            w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            APP.maya_theme.set_mode(theme,save=False)

    def test_stereo_layout_shares_axes_through_theme_locale_and_resize(self):
        card=FakeCard();w=MixerWindow(card,demo=True)
        theme,language=APP.maya_theme.mode,APP.maya_language.mode
        try:
            w.show();wait_for(lambda:w.ready)
            w.pairs[0].link.setChecked(False)
            for mode,locale in (('dark','ru'),('light','en')):
                APP.maya_theme.set_mode(mode,save=False)
                APP.maya_language.set_language(locale,save=False)
                for width,height in ((700,710),(850,820),(w.minimumWidth(),w.minimumHeight())):
                    w.resize(width,height)
                    for _ in range(8):APP.processEvents()
                    self.assertEqual(w.mixer_area.verticalScrollBar().maximum(),0)
                    for strip in [*w.pairs,*w.input_previews]:
                        header=strip.channel_header
                        axes=[f.mapTo(w,QPoint(round(f.rail_x()),0)).x() for f in strip.faders[:2]]
                        for items in (header.labels,strip.findChildren(QLabel,'digital')):
                            centres=[item.mapTo(w,item.rect().center()).x() for item in items]
                            self.assertEqual(len(centres),2)
                            for axis,centre in zip(axes,centres):self.assertLessEqual(abs(axis-centre),1)
                        middle=sum(axes)/2
                        items=[strip.link,strip.findChild(QLabel,'pairBadge'),strip.mute]
                        if strip in w.pairs:items.extend([strip.faders[2],strip.mix_on,strip.displays[2],strip.direct])
                        else:items.extend([strip.gain,strip.monitor])
                        for item in items:
                            centre=item.mapTo(w,QPoint()).x()+item.width()/2
                            self.assertLessEqual(abs(centre-middle),1,(item.objectName(),centre,middle))
                        self.assertLess(strip.link.mapTo(w,QPoint()).y(),strip.faders[0].mapTo(w,QPoint()).y())
                    self.assertFalse(w.pairs[0].link.isChecked())
            self.assertEqual(card.writes,[])
        finally:
            w.request_exit();wait_for(lambda:not w.worker.isRunning())
            w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            APP.maya_theme.set_mode(theme,save=False)
            APP.maya_language.set_language(language,save=False)

    def test_raw_channels_and_input_control_isolation(self):
        card=FakeCard();w=MixerWindow(card,demo=True)
        try:
            w.show();wait_for(lambda:w.ready)
            before=card.read()
            w.received_peaks((11,22,33,44,55,66,77,255))
            strips=[*w.pairs,*w.input_previews]
            self.assertEqual([m.peak_byte for s in strips for m in s.meters],[11,22,33,44,55,66,77,255])
            self.assertTrue(w.input_previews[1].meters[1].clipped)
            self.assertTrue(w.input_previews[0].gain.isEnabled())
            self.assertTrue(w.input_previews[0].faders[0].isEnabled())
            APP.maya_language.set_language('en',save=False)
            self.assertEqual(w.pairs[0].direct.text(),'Disable source')
            self.assertIn('dBFS',w.pairs[0].meters[0].toolTip())
            self.assertEqual(card.read(),before);self.assertEqual(card.writes,[])
            w.failed('test disconnect')
            self.assertTrue(all(not m.available for s in strips for m in s.meters))
        finally:
            w.request_exit();wait_for(lambda:not w.worker.isRunning())
            w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            APP.maya_language.set_language('ru',save=False)

    def test_zero_is_silence_and_stale_or_disabled_meters_clear(self):
        m=LevelMeter(stale_timeout=.1);m.show();APP.processEvents()
        try:
            m.set_peak(255);self.assertTrue(m.clipped);self.assertAlmostEqual(m.level_db,0)
            m.set_peak(0);self.assertTrue(m.available);self.assertIsNone(m.level_db)
            wait_for(lambda:not m.available)
            m.set_peak(128);self.assertAlmostEqual(m.level_db,-5.9866,places=3)
            m.setEnabled(False);self.assertFalse(m.available);self.assertIsNone(m.peak_byte)
            m.setEnabled(True);self.assertFalse(m.available)
        finally:
            m.close();m.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def test_worker_releases_handle_while_hidden_and_reopens(self):
        events=[]
        class Reader:
            def __init__(self):events.append('open')
            def read(self):events.append('read');return tuple(range(8))
            def close(self):events.append('close')
        worker=MeterWorker(Reader);received=[]
        worker.readings.connect(lambda values,error:received.append(values))
        worker.start()
        try:
            self.assertEqual(events,[])
            worker.set_active(True);wait_for(lambda:bool(received))
            worker.set_active(False);wait_for(lambda:'close' in events)
            reads=events.count('read');time.sleep(.08);APP.processEvents()
            self.assertEqual(events.count('read'),reads)
            worker.set_active(True);wait_for(lambda:events.count('open')==2)
        finally:
            worker.shutdown();wait_for(lambda:not worker.isRunning())
        self.assertEqual(events.count('open'),events.count('close'))

    def test_shutdown_during_read_does_not_publish_late_values(self):
        entered=threading.Event();release=threading.Event();closed=[];received=[]
        class Reader:
            def read(self):entered.set();release.wait(2);return tuple(range(8))
            def close(self):closed.append(True)
        worker=MeterWorker(Reader);worker.readings.connect(lambda values,error:received.append(values))
        worker.set_active(True);worker.start()
        try:
            wait_for(entered.is_set);worker.shutdown();release.set();wait_for(lambda:not worker.isRunning())
            self.assertEqual(received,[]);self.assertEqual(closed,[True])
        finally:
            release.set();worker.shutdown();wait_for(lambda:not worker.isRunning())

    def test_settings_poll_never_consumes_volatile_peak_control(self):
        opened=[];closed=[]
        values=FakeCard().read()
        names={spec[0]:key for key,spec in ALL_CONTROLS.items()}
        class Reader:
            def __init__(self,name,count,maximum,boolean=False,enumerated=False,card=None):
                self.name=name;opened.append(name)
                self.assertion=names[name]
            def read(self):return values[self.assertion]
            def close(self):closed.append(self.name)
        with patch('alsa_backend.ControlReader',Reader):
            self.assertEqual(AlsaBackend().read(),values)
        self.assertEqual(set(opened),set(names));self.assertEqual(opened,closed)
        self.assertNotIn('Hardware Peak Meter',opened)


if __name__=='__main__':unittest.main()
