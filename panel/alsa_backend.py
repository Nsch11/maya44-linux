# SPDX-License-Identifier: GPL-3.0-or-later
"""ALSA playback controls for MAYA44e; reads never restore saved settings."""
import re
import subprocess
from meter_backend import ControlReader
from card_discovery import select_device

CONTROLS = {
    "out12.volume": ("Output 1-2 Playback Volume", 2, 255),
    "out34.volume": ("Output 3-4 Playback Volume", 2, 255),
    "out12.enabled": ("DAC 1-2 Playback Switch", 1, 1),
    "out34.enabled": ("DAC 3-4 Playback Switch", 1, 1),
    "mix12.level": ("Output 1-2 Crossmix Playback Volume", 1, 48),
    "mix34.level": ("Output 3-4 Crossmix Playback Volume", 1, 48),
    "mix12.enabled": ("Output 1-2 Crossmix Playback Switch", 1, 1),
    "mix34.enabled": ("Output 3-4 Crossmix Playback Switch", 1, 1),
}


INPUT_CONTROLS = {
    "in12.volume": ("Input 1-2 Capture Volume", 2, 145),
    "in34.volume": ("Input 3-4 Capture Volume", 2, 145),
    "in12.gain": ("Input 1-2 Capture Gain", 1, 48),
    "in34.gain": ("Input 3-4 Capture Gain", 1, 48),
    "in12.enabled": ("Input 1-2 Capture Switch", 1, 1),
    "in34.enabled": ("Input 3-4 Capture Switch", 1, 1),
    "in12.source": ("Input 1-2 Capture Source", 1, 2),
    "in34.source": ("Input 3-4 Capture Source", 1, 1),
    "monitor12.enabled": ("Input 1-2 Monitor Playback Switch", 1, 1),
    "monitor34.enabled": ("Input 3-4 Monitor Playback Switch", 1, 1),
    "phantom.enabled": ("Mic Phantom Power Switch", 1, 1),
}
SPDIF_CONTROLS = {
    "spdif.output.enabled": ("IEC958 Playback Switch", 1, 1),
    "spdif.output.source": ("IEC958 Playback Source", 1, 1),
    "spdif.professional.enabled": ("IEC958 Playback Professional Switch", 1, 1),
    "spdif.loopback.enabled": ("IEC958 Capture Loopback Switch", 1, 1),
    "spdif.capture.locked": ("IEC958 Capture Lock", 1, 1),
    "spdif.capture.rate": ("IEC958 Capture Rate", 1, 96000),
    "spdif.capture.professional": ("IEC958 Capture Professional", 1, 1),
}
SPDIF_READONLY = {"spdif.capture.locked", "spdif.capture.rate", "spdif.capture.professional"}
ALL_CONTROLS = {**CONTROLS, **INPUT_CONTROLS, **SPDIF_CONTROLS}
INPUT_DEFAULTS = {"in12.volume": (145, 145), "in34.volume": (145, 145),
                  "in12.gain": (0,), "in34.gain": (0,),
                  "in12.enabled": (1,), "in34.enabled": (1,),
                  "in12.source": (0,), "in34.source": (0,),
                  "monitor12.enabled": (0,), "monitor34.enabled": (0,),
                  "phantom.enabled": (0,)}


def validate(key, values):
    _, count, maximum = ALL_CONTROLS[key]
    values = tuple(values)
    if len(values) != count or any(
        type(v) is not int or not 0 <= v <= maximum for v in values
    ):
        raise ValueError(f"Invalid value for {key}: {values}")
    return values


def parse_contents(text):
    names = {spec[0]: key for key, spec in CONTROLS.items()}
    result, key = {}, None
    for line in text.splitlines():
        header = re.match(r"numid=\d+,iface=MIXER,name='([^']+)'", line)
        if header:
            key = names.get(header[1])
        elif key and (match := re.match(r"\s*: values=(.*)$", line)):
            values = tuple(
                1 if v == "on" else 0 if v == "off" else int(v)
                for v in match[1].strip().split(",")
            )
            result[key] = validate(key, values)
            key = None
    if set(result) != set(CONTROLS):
        raise ValueError("Required MAYA44e controls are missing")
    return result


class AlsaBackend:
    def __init__(self, inputs=True, *, device=None):
        self.inputs = inputs
        self.device = device or select_device()

    def _run(self, args):
        result = subprocess.run(
            ["/usr/bin/amixer", *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=2, check=True,
        )
        return result.stdout

    def read(self):
        # amixer contents would also read the volatile hardware meter, possibly
        # consuming its latch between meter frames. Read only the eight settings.
        values = {}
        for key, (name, count, maximum) in (ALL_CONTROLS if self.inputs else CONTROLS).items():
            try:
                boolean = key.endswith(('.enabled', '.locked', '.professional'))
                try:
                    reader = ControlReader(name, count, maximum, card=self.device.control, boolean=boolean,
                                           enumerated=key.endswith('.source'))
                except ValueError:
                    if key != 'in12.source':
                        raise
                    # Older analog-duplex modules expose only Line and Mic.
                    reader = ControlReader(name, count, 1, card=self.device.control, enumerated=True)
            except OSError as error:
                if key in (INPUT_CONTROLS | SPDIF_CONTROLS) and error.errno == 2:
                    continue  # Older modules remain usable without these controls.
                raise
            try:
                values[key] = validate(key, reader.read())
            finally:
                reader.close()
        return values

    def write(self, key, values):
        if key in SPDIF_READONLY:
            raise ValueError(f'Read-only control: {key}')
        values = validate(key, values)
        raw = (
            "on" if values[0] else "off"
        ) if key.endswith(".enabled") else ",".join(map(str, values))
        self._run(["-q", "-c", self.device.card_id, "cset", f"name={ALL_CONTROLS[key][0]}", raw])


class DemoBackend:
    """Separate in-memory transport for rendering/testing, never opens ALSA."""
    def __init__(self):
        self.values = {
            "out12.volume": (195, 195), "out34.volume": (195, 195),
            "out12.enabled": (1,), "out34.enabled": (1,),
            "mix12.level": (18,), "mix34.level": (48,),
            "mix12.enabled": (0,), "mix34.enabled": (1,),
            "spdif.output.enabled": (1,), "spdif.output.source": (0,), "spdif.professional.enabled": (0,),
            "spdif.loopback.enabled": (0,), "spdif.capture.locked": (0,),
            "spdif.capture.rate": (0,), "spdif.capture.professional": (0,),
            **INPUT_DEFAULTS,
        }

    def read(self):
        return self.values.copy()

    def write(self, key, values):
        values = validate(key, values)
        if key in SPDIF_READONLY:
            raise ValueError(f'Read-only control: {key}')
        if key == 'phantom.enabled' and values[0] and self.values['in12.source'][0] != 1:
            raise ValueError('Phantom power requires MIC')
        if key == 'monitor12.enabled' and values[0] and self.values['in12.source'][0] == 2:
            raise ValueError('Digital input has no analog monitor')
        if key == 'in12.source' and values != self.values[key]:
            self.values['phantom.enabled'] = (0,)
            if values == (2,):
                self.values['monitor12.enabled'] = (0,)
        self.values[key] = values


def move_stereo(values, channel, raw, linked):
    """Linked movement preserves balance and clamps both ends together."""
    left, right = values
    raw = min(255, max(0, raw))
    if linked:
        delta = max(-min(values), min(255 - max(values), raw - values[channel]))
        return left + delta, right + delta
    result = list(values)
    result[channel] = raw
    return tuple(result)
