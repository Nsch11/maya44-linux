# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only hardware metering through one persistent ALSA control handle."""
import ctypes as C
import threading

from PyQt6.QtCore import QThread, pyqtSignal

PEAK_CONTROL = b'Hardware Peak Meter'


class ControlReader:
    """One read-only control, without touching unrelated volatile controls."""
    def __init__(self, name, count, maximum, *, card='hw:MAYA44e', boolean=False, enumerated=False):
        self.count, self.maximum, self.boolean = count, maximum, boolean
        self.enumerated = enumerated
        self.handle, self.ident, self.value = C.c_void_p(), C.c_void_p(), C.c_void_p()
        self.lib = lib = C.CDLL('libasound.so.2')
        signatures = {
            'snd_ctl_open': ([C.POINTER(C.c_void_p), C.c_char_p, C.c_int], C.c_int),
            'snd_ctl_close': ([C.c_void_p], C.c_int),
            'snd_strerror': ([C.c_int], C.c_char_p),
            'snd_ctl_elem_id_malloc': ([C.POINTER(C.c_void_p)], C.c_int),
            'snd_ctl_elem_id_free': ([C.c_void_p], None),
            'snd_ctl_elem_id_set_interface': ([C.c_void_p, C.c_int], None),
            'snd_ctl_elem_id_set_name': ([C.c_void_p, C.c_char_p], None),
            'snd_ctl_elem_info_malloc': ([C.POINTER(C.c_void_p)], C.c_int),
            'snd_ctl_elem_info_free': ([C.c_void_p], None),
            'snd_ctl_elem_info_set_id': ([C.c_void_p, C.c_void_p], None),
            'snd_ctl_elem_info': ([C.c_void_p, C.c_void_p], C.c_int),
            'snd_ctl_elem_info_get_items': ([C.c_void_p], C.c_uint),
            'snd_ctl_elem_value_get_enumerated': ([C.c_void_p, C.c_uint], C.c_uint),
            'snd_ctl_elem_info_get_count': ([C.c_void_p], C.c_uint),
            'snd_ctl_elem_info_get_type': ([C.c_void_p], C.c_int),
            'snd_ctl_elem_info_get_min': ([C.c_void_p], C.c_long),
            'snd_ctl_elem_info_get_max': ([C.c_void_p], C.c_long),
            'snd_ctl_elem_value_malloc': ([C.POINTER(C.c_void_p)], C.c_int),
            'snd_ctl_elem_value_free': ([C.c_void_p], None),
            'snd_ctl_elem_value_set_id': ([C.c_void_p, C.c_void_p], None),
            'snd_ctl_elem_read': ([C.c_void_p, C.c_void_p], C.c_int),
            'snd_ctl_elem_value_get_integer': ([C.c_void_p, C.c_uint], C.c_long),
            'snd_ctl_elem_value_get_boolean': ([C.c_void_p, C.c_uint], C.c_int),
        }
        for function_name, (args, result) in signatures.items():
            function = getattr(lib, function_name)
            function.argtypes, function.restype = args, result
        info = C.c_void_p()
        try:
            self._check(lib.snd_ctl_open(C.byref(self.handle), card.encode(), 5))  # NONBLOCK | READONLY
            self._check(lib.snd_ctl_elem_id_malloc(C.byref(self.ident)))
            lib.snd_ctl_elem_id_set_interface(self.ident, 2)  # SND_CTL_ELEM_IFACE_MIXER
            lib.snd_ctl_elem_id_set_name(self.ident, name if isinstance(name, bytes) else name.encode())
            self._check(lib.snd_ctl_elem_info_malloc(C.byref(info)))
            lib.snd_ctl_elem_info_set_id(info, self.ident)
            self._check(lib.snd_ctl_elem_info(self.handle, info))
            if (lib.snd_ctl_elem_info_get_count(info) != count or
                    lib.snd_ctl_elem_info_get_type(info) != (3 if enumerated else 1 if boolean else 2) or
                    (enumerated and lib.snd_ctl_elem_info_get_items(info) != maximum+1) or
                    (not boolean and not enumerated and (lib.snd_ctl_elem_info_get_min(info) != 0 or
                                      lib.snd_ctl_elem_info_get_max(info) != maximum))):
                raise ValueError('Unsupported ALSA control format')
            self._check(lib.snd_ctl_elem_value_malloc(C.byref(self.value)))
            lib.snd_ctl_elem_value_set_id(self.value, self.ident)
        except Exception:
            self.close()
            raise
        finally:
            if info:
                lib.snd_ctl_elem_info_free(info)

    def _check(self, result):
        if result < 0:
            raise OSError(-result, self.lib.snd_strerror(result).decode('utf-8', 'replace'))

    def read(self):
        self._check(self.lib.snd_ctl_elem_read(self.handle, self.value))
        getter = (self.lib.snd_ctl_elem_value_get_enumerated if self.enumerated else
                  self.lib.snd_ctl_elem_value_get_boolean if self.boolean else self.lib.snd_ctl_elem_value_get_integer)
        values = tuple(getter(self.value, i) for i in range(self.count))
        if any(not 0 <= value <= self.maximum for value in values):
            raise ValueError('Invalid ALSA control value')
        return values

    def close(self):
        if self.value:
            self.lib.snd_ctl_elem_value_free(self.value)
            self.value = C.c_void_p()
        if self.ident:
            self.lib.snd_ctl_elem_id_free(self.ident)
            self.ident = C.c_void_p()
        if self.handle:
            self.lib.snd_ctl_close(self.handle)
            self.handle = C.c_void_p()


class PeakReader(ControlReader):
    """Eight bytes in Windows order: Output 1..4, Input 1..4."""
    def __init__(self, card='hw:MAYA44e'):
        super().__init__(PEAK_CONTROL, 8, 255, card=card)


class MeterWorker(QThread):
    readings = pyqtSignal(object, str)

    def __init__(self, factory=PeakReader):
        super().__init__()
        self.factory = factory
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.active = self.closing = False

    def set_active(self, active):
        with self.lock:
            self.active = bool(active)
        self.wake.set()

    def shutdown(self):
        with self.lock:
            self.closing = True
        self.wake.set()

    def run(self):
        reader = None
        try:
            while True:
                with self.lock:
                    active, closing = self.active, self.closing
                if closing:
                    return
                if not active:
                    if reader is not None:
                        reader.close()
                        reader = None
                        self.readings.emit(None, '')
                    delay = 1.0
                else:
                    try:
                        if reader is None:
                            reader = self.factory()
                        values = reader.read()
                        with self.lock:
                            publish = self.active and not self.closing
                        if publish:
                            self.readings.emit(values, '')
                        delay = .033
                    except Exception as error:
                        if reader is not None:
                            reader.close()
                            reader = None
                        self.readings.emit(None, str(error))
                        delay = 2.0
                self.wake.wait(delay)
                self.wake.clear()
        finally:
            if reader is not None:
                reader.close()
