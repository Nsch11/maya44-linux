# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-device PipeWire settings. Reads never change the card or saved policy."""
from i18n import translate
from card_discovery import select_device

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

RATES = (0, 44100, 48000, 88200, 96000)
PERIODS = (64, 128, 256, 512, 1024)
DEFAULT = {'rate': 0, 'period': 256}
PCM_CONTAINER_BITS = {
    'S16_LE': 16, 'S24_3LE': 24, 'S24_LE': 32, 'S32_LE': 32,
    'FLOAT_LE': 32, 'FLOAT64_LE': 64,
}
PCM_VALID_BITS = {'S16_LE': 16, 'S24_3LE': 24, 'S24_LE': 24, 'S32_LE': 24}


def validate_settings(settings):
    if (set(settings) != {'rate', 'period'} or
            type(settings['rate']) is not int or settings['rate'] not in RATES or
            type(settings['period']) is not int or settings['period'] not in PERIODS):
        raise ValueError(translate("Panel", 'Invalid sample rate or buffer size'))
    return dict(settings)


def policy(settings, device=None, *, legacy=False):
    device = device or select_device()
    matches = ([{'node.name': '~alsa_output.pci-' + device.pci.replace(':', '_') + '.*'},
                {'node.name': '~alsa_input.pci-' + device.pci.replace(':', '_') + '.*'}]
               if legacy else [
                   {'alsa.id': device.card_id, 'node.name': '~alsa_output.*'},
                   {'alsa.id': device.card_id, 'node.name': '~alsa_input.*'}])
    settings = validate_settings(settings)
    return {'monitor.alsa.rules': [{
        'matches': matches,
        'actions': {'update-props': {
            'audio.rate': settings['rate'],
            'api.alsa.period-size': settings['period'],
            'api.alsa.period-num': 2,
            # Keep the graph rate/quantum aligned with the two-period PCM ring.
            'api.alsa.disable-tsched': True,
        }},
    }]}


def node_settings(node):
    params = {}
    for block in node['info']['params'].get('Props', []):
        values = block.get('params', [])
        params.update(zip(values[::2], values[1::2]))
    return {'rate': int(params['audio.rate']),
            'period': int(params['api.alsa.period-size']),
            'periods': int(params['api.alsa.period-num']),
            'irq': bool(params['api.alsa.disable-tsched'])}


def read_hardware(device=None, *, hw=None, status_path=None):
    device = device or select_device()
    raw = (hw or device.proc('pcm0p/sub0/hw_params')).read_text()
    status = (status_path or device.proc('maya44e_status')).read_text()
    last = re.search(r'^experimental (?:playback-only|analog-duplex) (\d+) Hz ([A-Z0-9_]+) (\d+) channels$', status, re.M)
    sizes = re.search(r'period_frames=(\d+) buffer_frames=(\d+)', status)
    if not last or not sizes:
        raise RuntimeError(translate("Panel", 'A driver version with adjustable buffer size is required'))
    result = {'active': raw.strip() != 'closed', 'rate': int(last[1]),
              'period': int(sizes[1]), 'buffer': int(sizes[2]),
              'format': last[2], 'format_source': 'driver'}
    # This driver fixes both WM8776 DAC interfaces to 24-bit I2S (R10=0x22)
    # and exposes no word-length control. The value describes that driver
    # contract, not a live codec-register readback or 32 valid DMA bits.
    result['dac_bits'] = 24 if last[2] == 'S32_LE' and int(last[3]) == 4 else None
    result['dac_bits_source'] = 'driver-contract' if result['dac_bits'] else None
    for name in ('starts', 'fault', 'ack_errors', 'reset_timeouts', 'codec_timeouts'):
        result[name] = int(re.search(rf'\b{name}=(\d+)', status)[1])
    if result['active']:
        pcm_format = re.search(r'^format:\s+([A-Z0-9_]+)\s*$', raw, re.M)
        if not pcm_format:
            raise RuntimeError(translate("Panel", 'The device configuration is changing; try again in a moment'))
        result['format'] = pcm_format[1]
        result['format_source'] = 'alsa'
        for field, name in (('rate', 'rate'), ('period', 'period_size'), ('buffer', 'buffer_size')):
            match = re.search(rf'^{name}: (\d+)\b', raw, re.M)
            if not match:
                raise RuntimeError(translate("Panel", 'The device configuration is changing; try again in a moment'))
            result[field] = int(match[1])
    # A storage/container width is deliberately not called valid precision.
    result['container_bits'] = PCM_CONTAINER_BITS.get(result['format'])
    result['valid_bits'] = PCM_VALID_BITS.get(result['format']) if result['dac_bits'] == 24 else None
    return result


class PlaybackBackend:
    def __init__(self, config_path=None, *, device=None):
        self.device = device or select_device()
        self.sink = self.device.node_name()
        self.stereo_sinks = self.device.stereo_sinks
        self.card = self.device.pulse_card
        self.config_path = Path(config_path) if config_path is not None else (
            Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') /
            f'wireplumber/wireplumber.conf.d/52-{self.device.stem}-panel.conf')

    def _run(self, *args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=5,
                                env={**os.environ, 'LC_ALL': 'C.UTF-8'})
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or translate("Panel", '{value0}: exit code {value1}').format(value0=args[0], value1=result.returncode))
        return result.stdout

    def _node(self):
        nodes = json.loads(self._run('pw-dump'))
        matches = [n for n in nodes if self._matches_node(n, capture=False)]
        if len(matches) != 1:
            raise RuntimeError(translate("Panel", 'MAYA44e output is unavailable in PipeWire').replace('MAYA44e', self.device.model))
        props = matches[0]['info']['props']
        self.sink = props['node.name']
        parent = next((n for n in nodes if n.get('id') == props.get('device.id') and
                       n.get('info', {}).get('props', {}).get('device.name')), None)
        if parent is not None:
            self.card = parent['info']['props']['device.name']
        # The tray may have started before the module. Refresh missing metadata
        # on reconnect without selecting a different card or changing controls.
        if not self.device.pci:
            try:
                self.device = select_device(self.device.card_id)
            except ValueError:
                pass
        return matches[0]

    def _matches_node(self, node, *, capture):
        props = node.get('info', {}).get('props', {})
        # Match the selected card's real PCM, excluding virtual stereo streams.
        return (props.get('alsa.id') == self.device.card_id and
                props.get('alsa.driver_name') == 'snd_maya44e' and
                props.get('device.profile.name') == ('multichannel-input' if capture else 'multichannel-output') and
                props.get('api.alsa.pcm.stream') == ('capture' if capture else 'playback'))

    def _capture_node(self):
        nodes = json.loads(self._run('pw-dump'))
        return next((n for n in nodes if self._matches_node(n, capture=True)), None)

    def _capture_busy(self):
        path = self.device.proc('pcm0c/sub0/hw_params')
        return path.exists() and path.read_text().strip() != 'closed'

    def _hardware(self):
        return read_hardware(self.device)

    def _saved(self):
        if not self.config_path.exists():
            return dict(DEFAULT)
        content = json.loads(self.config_path.read_text())
        props = content['monitor.alsa.rules'][0]['actions']['update-props']
        settings = validate_settings({'rate': props['audio.rate'],
                                      'period': props['api.alsa.period-size']})
        variants = []
        for legacy in (False, True):
            for inputs in (False, True):
                for irq in (False, True):
                    candidate = policy(settings, self.device, legacy=legacy)
                    if not inputs:
                        candidate['monitor.alsa.rules'][0]['matches'] = candidate['monitor.alsa.rules'][0]['matches'][:1]
                    if not irq:
                        del candidate['monitor.alsa.rules'][0]['actions']['update-props']['api.alsa.disable-tsched']
                    variants.append(candidate)
        if content not in variants:
            raise RuntimeError(translate("Panel", 'The panel settings file was edited manually; automatic overwriting is disabled'))
        return settings

    def _save(self, settings):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.maya44e-', dir=self.config_path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(json.dumps(policy(settings, self.device), ensure_ascii=False, indent=2) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.config_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def read(self):
        node = self._node()
        return {'saved': self._saved(), 'live': node_settings(node),
                'hardware': self._hardware(), 'serial': node['info']['props']['object.serial']}

    def _set(self, node, settings):
        params = ['audio.rate', settings['rate'],
                  'api.alsa.period-size', settings['period'],
                  'api.alsa.period-num', settings['periods'],
                  'api.alsa.disable-tsched', settings['irq']]
        self._run('pw-cli', 'set-param', str(node['id']), 'Props', json.dumps({'params': params}))

    def _pulse(self, kind):
        return json.loads(self._run('pactl', '--format=json', 'list', kind))

    def _park_streams(self):
        # Stereo sinks share a hidden ALSA parent. Park clients of both pairs
        # and remember each destination so a settings change cannot swap them.
        sinks = {s['index']: s['name'] for s in self._pulse('sinks')
                 if s['name'] in (self.sink, *self.stereo_sinks)}
        streams = {s['index']: sinks[s['sink']] for s in self._pulse('sink-inputs')
                   if s['sink'] in sinks}
        if not streams:
            return None
        name = 'maya44e_settings_' + uuid.uuid4().hex[:12]
        module = self._run('pactl', 'load-module', 'module-null-sink', 'sink_name=' + name,
                           "sink_properties='device.description=MAYA44e-settings priority.session=0'").strip()
        if not module.isdigit():
            raise RuntimeError(translate("Panel", 'Could not create a temporary silent output'))
        parking = {'module': module, 'name': name, 'targets': streams}
        try:
            for index in streams:
                if any(s['index'] == index and sinks.get(s['sink']) == streams[index]
                       for s in self._pulse('sink-inputs')):
                    self._run('pactl', 'move-sink-input', str(index), name)
        except Exception:
            self._restore_streams(parking)
            raise
        return parking

    def _restore_streams(self, parking):
        if parking is None:
            return
        sink = next((s for s in self._pulse('sinks') if s['name'] == parking['name']), None)
        errors = []
        if sink:
            for stream in self._pulse('sink-inputs'):
                if stream['sink'] == sink['index']:
                    try:
                        target = parking['targets'].get(stream['index'], self.stereo_sinks[0])
                        self._run('pactl', 'move-sink-input', str(stream['index']), target)
                    except Exception as exc:
                        errors.append(str(exc))
        # Unloading is also the fallback that releases streams if the card vanished.
        self._run('pactl', 'unload-module', parking['module'])
        if errors:
            raise RuntimeError(translate("Panel", 'Could not restore the stream to MAYA44e: ').replace('MAYA44e', self.device.model) + '; '.join(errors))

    def _change_stereo(self, settings):
        # Connected loopback ports prevent the ALSA parent from closing even
        # after Suspend. Recreate only this card's profile while its clients
        # wait on a silent sink; endpoint names and per-app targets stay stable.
        profile = next(c['active_profile'] for c in self._pulse('cards') if c['name'] == self.card)
        parking = None
        profile_off = False
        try:
            parking = self._park_streams()
            self._run('pactl', 'set-card-profile', self.card, 'off')
            profile_off = True
            self._run('pactl', 'set-card-profile', self.card, profile)
            profile_off = False
            deadline = time.monotonic() + 3
            while True:
                try:
                    node = self._node()
                    names = {s['name'] for s in self._pulse('sinks')}
                    if set(self.stereo_sinks) <= names:
                        break
                except RuntimeError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(translate("Panel", 'MAYA44e output is unavailable in PipeWire').replace('MAYA44e', self.device.model))
                time.sleep(.05)
            self._set(node, settings)
            capture_node = self._capture_node()
            if capture_node is not None:
                self._set(capture_node, settings)
            if node_settings(self._node()) != settings:
                raise RuntimeError(translate("Panel", 'PipeWire did not confirm the selected settings'))
        finally:
            try:
                if profile_off:
                    self._run('pactl', 'set-card-profile', self.card, profile)
            finally:
                self._restore_streams(parking)
        self._verify_hardware(settings, parking)

    def _change(self, settings):
        if self._capture_busy():
            raise RuntimeError(translate('Panel', 'Stop recording before changing the shared sample rate or buffer'))
        capture_node = self._capture_node()
        node = self._node()
        if any(s['name'] in self.stereo_sinks for s in self._pulse('sinks')):
            if self._hardware()['active'] and node['info']['state'] == 'suspended':
                raise RuntimeError(translate("Panel", 'The device is in use directly through ALSA. Stop that application'))
            return self._change_stereo(settings)
        serial = node['info']['props']['object.serial']
        hardware = self._hardware()
        parking = None
        try:
            if hardware['active']:
                if node['info']['state'] == 'suspended':
                    raise RuntimeError(translate("Panel", 'The device is in use directly through ALSA. Stop that application'))
                # PipeWire refuses Suspend with busy linked ports. Temporarily move
                # only this sink's Pulse streams, keeping their volume/mute untouched.
                parking = self._park_streams()
                deadline = time.monotonic() + 3
                while True:
                    # The shared PCM is an internal PipeWire node rather than
                    # a public Pulse sink when the stereo endpoints are enabled.
                    self._run('pw-cli', 'send-command', str(node['id']), 'Suspend')
                    if not self._hardware()['active']:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError(translate("Panel", 'Could not release the device PCM. Stop playback and try again'))
                    time.sleep(.1)
            node = self._node()
            if node['info']['props']['object.serial'] != serial:
                raise RuntimeError(translate("Panel", 'The device output was recreated; try applying again'))
            self._set(node, settings)
            if capture_node is not None:
                self._set(capture_node, settings)
            for _ in range(20):
                current = self._node()
                if current['info']['props']['object.serial'] != serial:
                    raise RuntimeError(translate("Panel", 'The device disconnected while applying settings'))
                if node_settings(current) == settings:
                    break
                time.sleep(.05)
            else:
                raise RuntimeError(translate("Panel", 'PipeWire did not confirm the selected settings'))
        finally:
            self._restore_streams(parking)
        self._verify_hardware(settings, parking)

    def _verify_hardware(self, settings, parking):
        # If playback resumes, require the real PCM to match, not just the request.
        for _ in range(20 if parking else 1):
            current = self._hardware()
            if current['active']:
                if (current['period'] != settings['period'] or
                        current['buffer'] != settings['period'] * settings['periods'] or
                        (settings['rate'] and current['rate'] != settings['rate'])):
                    raise RuntimeError(translate("Panel", 'The actual device configuration does not match the selection'))
                break
            time.sleep(.1)
        if parking and current['active']:
            # Matching hw_params alone misses a stream repeatedly recovering XRUNs.
            initial_starts = current.get('starts', 0)
            for _ in range(10):
                time.sleep(.05)
                current = self._hardware()
                if current.get('starts', 0) > initial_starts + 1:
                    raise RuntimeError(translate("Panel", 'Playback is unstable with these settings; restoring the previous configuration'))
                if any(current.get(k, 0) for k in ('fault', 'ack_errors', 'reset_timeouts', 'codec_timeouts')):
                    raise RuntimeError(translate("Panel", 'The driver reported an error; restoring the previous configuration'))

    def apply(self, settings):
        settings = validate_settings(settings)
        before = self.read()  # Also validates our existing file before any mutation.
        requested = {**settings, 'periods': 2, 'irq': True}
        hw = before['hardware']
        capture_node = self._capture_node()
        capture_changed = capture_node is not None and node_settings(capture_node) != requested
        changed = capture_changed or requested != before['live'] or (hw['active'] and (
            hw['period'] != requested['period'] or hw['buffer'] != requested['period'] * 2 or
            (requested['rate'] and hw['rate'] != requested['rate'])))
        try:
            if changed:
                self._change(requested)
            self._save(settings)
        except Exception as error:
            if changed:
                try:
                    # A failure before writing parameters does not need another
                    # interruption. Otherwise restore exactly the preceding request.
                    if node_settings(self._node()) != before['live']:
                        self._change(before['live'])
                except Exception as rollback:
                    raise RuntimeError(translate("Panel", '{error}. Could not restore the previous configuration: {rollback}').format(error=error, rollback=rollback)) from error
            raise
        return self.read()


class DemoPlaybackBackend:
    def __init__(self):
        self.saved = dict(DEFAULT)

    def read(self):
        return {'saved': dict(self.saved), 'live': {**self.saved, 'periods': 2, 'irq': True},
                'hardware': {'active': True, 'rate': self.saved['rate'] or 48000,
                             'period': self.saved['period'], 'buffer': self.saved['period'] * 2,
                             'format': 'S32_LE', 'format_source': 'demo', 'container_bits': 32,
                             'dac_bits': 24, 'dac_bits_source': 'demo'},
                'serial': 1}

    def apply(self, settings):
        self.saved = validate_settings(settings)
        return self.read()
