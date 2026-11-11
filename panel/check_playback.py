# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated transaction tests: no PipeWire, ALSA or real user config writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playback_backend import PlaybackBackend, policy as device_policy, read_hardware
from card_discovery import MayaDevice
DEVICE = MayaDevice('MAYA44e', 'MAYA44e', 2, '0000:05:00.0')
SINK, STEREO_SINKS, CARD = DEVICE.node_name(), DEVICE.stereo_sinks, DEVICE.pulse_card
def policy(settings):
    return device_policy(settings, DEVICE)


class FakePipeWire(PlaybackBackend):
    def __init__(self, path, *, device=DEVICE):
        super().__init__(path, device=device)
        self.commands = []
        self.live = {'rate': 0, 'period': 256, 'periods': 2, 'irq': True}
        self.active = True
        self.node_state = 'running'
        self.serial = 123
        self.fail_set_once = False
        self.fail_resume_once = False
        self.actual_mismatch_once = False
        self.parking = None
        self.stream_sink = 400
        self.starts = 1
        self.unstable = False

    def _pulse(self, kind):
        if kind == 'cards':
            return [{'name': self.card, 'active_profile': 'output:multichannel-output+input:multichannel-input'}]
        if kind == 'sinks':
            return [{'index': 400, 'name': self.sink}] + ([{'index': 500, 'name': self.parking}] if self.parking else [])
        return [{'index': 200, 'sink': self.stream_sink}, {'index': 201, 'sink': 999}]

    def _capture_node(self):
        return None

    def _capture_busy(self):
        return False

    def _node(self):
        names = {'rate': 'audio.rate', 'period': 'api.alsa.period-size', 'periods': 'api.alsa.period-num',
                 'irq': 'api.alsa.disable-tsched'}
        params = [v for key, value in self.live.items() for v in (names[key], value)]
        return {'id': 34, 'info': {'state': self.node_state,
                'props': {'object.serial': self.serial, 'node.name': self.sink},
                'params': {'Props': [{'params': params}]}}}

    def _hardware(self):
        period = self.live['period']
        if self.unstable and self.active and period == 64:
            self.starts += 2
        if self.active and self.actual_mismatch_once and period != 256:
            self.actual_mismatch_once = False
            period = 512
        return {'active': self.active, 'rate': self.live['rate'] or 48000,
                'period': period, 'buffer': period * self.live['periods'], 'starts': self.starts}

    def _run(self, *args):
        self.commands.append(args)
        if args[:2] == ('pactl', 'load-module'):
            self.parking = args[3].split('=', 1)[1]
            return '33'
        elif args[:2] == ('pactl', 'move-sink-input'):
            assert args[2] == '200', 'An unrelated stream must not move'
            if args[3] == self.sink:
                if self.fail_resume_once:
                    self.fail_resume_once = False
                    raise RuntimeError('Return stream failure')
                self.stream_sink = 400
                self.active = True
                self.node_state = 'running'
            else:
                assert args[3] == self.parking
                self.stream_sink = 500
                self.node_state = 'idle'
        elif args[:2] == ('pactl', 'unload-module'):
            self.parking = None
            if self.stream_sink == 500:
                self.stream_sink = 400
                self.active = True
                self.node_state = 'running'
        elif args[:2] == ('pw-cli', 'send-command'):
            assert args[2:] == ('34', 'Suspend')
            assert self.stream_sink != 400, 'Must unlink busy streams before suspend'
            self.active = False
            self.node_state = 'suspended'
        elif args[:2] == ('pw-cli', 'set-param'):
            assert not self.active, 'Must stop PCM before changing rate/buffer'
            assert args[2:4] == ('34', 'Props')
            if self.fail_set_once:
                self.fail_set_once = False
                raise RuntimeError('Write rejected')
            flat = json.loads(args[4])['params']
            values = dict(zip(flat[::2], flat[1::2]))
            self.live = {'rate': values['audio.rate'], 'period': values['api.alsa.period-size'],
                         'periods': values['api.alsa.period-num'], 'irq': values['api.alsa.disable-tsched']}
        else:
            raise AssertionError(args)
        return ''


class FakeStereoPipeWire(FakePipeWire):
    """Both public pairs, a hidden PCM parent, and one unrelated stream."""
    def __init__(self, path, *, device=DEVICE):
        super().__init__(path, device=device)
        self.routes = {200: 410, 202: 420, 201: 999}

    def _pulse(self, kind):
        if kind == 'cards':
            return [{'name': self.card, 'active_profile': 'output:multichannel-output+input:multichannel-input'}]
        if kind == 'sinks':
            return [{'index': 410, 'name': self.stereo_sinks[0]},
                    {'index': 420, 'name': self.stereo_sinks[1]}] + (
                        [{'index': 500, 'name': self.parking}] if self.parking else [])
        return [{'index': index, 'sink': sink} for index, sink in self.routes.items()]

    def _run(self, *args):
        if args[:2] == ('pactl', 'set-card-profile'):
            self.commands.append(args)
            assert args[2] == self.card
            assert all(sink not in (410, 420) for sink in self.routes.values())
            self.active = False
            self.node_state = 'suspended'
            if args[3] != 'off':
                self.serial += 1
            return ''
        if args[:2] == ('pactl', 'move-sink-input'):
            self.commands.append(args)
            index = int(args[2])
            assert index != 201, 'An unrelated stream must not move'
            targets = {self.stereo_sinks[0]: 410, self.stereo_sinks[1]: 420, self.parking: 500}
            target = targets[args[3]]
            if target != 500 and self.fail_resume_once:
                self.fail_resume_once = False
                raise RuntimeError('Return stream failure')
            self.routes[index] = target
            if target == 500:
                self.node_state = 'idle'
            else:
                self.active = True
                self.node_state = 'running'
            return ''
        if args[:2] == ('pw-cli', 'send-command'):
            assert all(sink not in (410, 420) for sink in self.routes.values())
            self.stream_sink = 500  # Base double also checks the legacy sink.
        return super()._run(*args)


class PlaybackChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'wireplumber.conf.d/52-maya44e-panel.conf'
        self.backend = FakePipeWire(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def hardware_fixture(self, raw, header='experimental playback-only 48000 Hz S32_LE 4 channels'):
        hw = Path(self.temp.name) / 'hw_params'
        status = Path(self.temp.name) / 'status'
        hw.write_text(raw)
        status.write_text(header + '\nperiod_frames=256 buffer_frames=512 periods=2\n'
                          'starts=7 fault=0 ack_errors=0 reset_timeouts=0 codec_timeouts=0\n')
        return read_hardware(DEVICE, hw=hw, status_path=status)

    def test_active_capture_blocks_clock_change_before_any_mutation(self):
        with patch.object(self.backend, '_capture_busy', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'Stop recording'):
                self.backend.apply({'rate': 96000, 'period': 128})
        self.assertEqual(self.backend.commands, [])
        self.assertEqual(self.backend.live['rate'], 0)

    def test_legacy_playback_policy_is_read_without_rewriting(self):
        data = policy({'rate': 0, 'period': 256})
        data['monitor.alsa.rules'][0]['matches'] = data['monitor.alsa.rules'][0]['matches'][:1]
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(data))
        self.assertEqual(self.backend.read()['saved'], {'rate': 0, 'period': 256})
        self.assertEqual(json.loads(self.path.read_text()), data)

    def test_active_format_uses_alsa_storage_width_not_a_guessed_valid_precision(self):
        for pcm_format, bits in (('S16_LE', 16), ('S24_3LE', 24), ('S24_LE', 32), ('S32_LE', 32)):
            with self.subTest(pcm_format=pcm_format):
                state = self.hardware_fixture(
                    f'format: {pcm_format}\nrate: 44100 (44100/1)\nperiod_size: 128\nbuffer_size: 256\n')
                self.assertTrue(state['active'])
                self.assertEqual(state['format'], pcm_format)
                self.assertEqual(state['format_source'], 'alsa')
                self.assertEqual(state['container_bits'], bits)
                self.assertEqual(state['valid_bits'], 16 if pcm_format == 'S16_LE' else 24)
                self.assertEqual(state['rate'], 44100)
                self.assertEqual(state['period'], 128)

    def test_idle_format_is_labelled_as_driver_contract(self):
        state = self.hardware_fixture('closed\n')
        self.assertFalse(state['active'])
        self.assertEqual(state['format'], 'S32_LE')
        self.assertEqual(state['format_source'], 'driver')
        self.assertEqual(state['container_bits'], 32)
        self.assertEqual(state['dac_bits'], 24)
        self.assertEqual(state['dac_bits_source'], 'driver-contract')

    def test_incomplete_pcm_format_is_not_reported_as_current(self):
        for raw in ('rate: 48000\nperiod_size: 256\nbuffer_size: 512\n',
                    'format: S32_LE changing\nrate: 48000\nperiod_size: 256\nbuffer_size: 512\n'):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(RuntimeError, 'configuration is changing'):
                    self.hardware_fixture(raw)

    def test_different_driver_contract_does_not_invent_dac_precision(self):
        state = self.hardware_fixture('closed\n', 'experimental playback-only 48000 Hz S16_LE 4 channels')
        self.assertEqual(state['container_bits'], 16)
        self.assertIsNone(state['dac_bits'])
        self.assertIsNone(state['dac_bits_source'])
        self.assertIsNone(state['valid_bits'])
        with self.assertRaisesRegex(RuntimeError, 'driver version'):
            self.hardware_fixture('closed\n', 'experimental playback-only 48000 Hz')

    def test_read_never_writes_or_creates_config(self):
        state = self.backend.read()
        self.assertEqual(state['saved'], {'rate': 0, 'period': 256})
        self.assertFalse(self.path.exists())
        self.assertEqual(self.backend.commands, [])

    def test_active_switch_targets_only_maya_and_persists(self):
        chosen = {'rate': 44100, 'period': 128}
        result = self.backend.apply(chosen)
        self.assertEqual(result['saved'], chosen)
        self.assertTrue(result['hardware']['active'])
        self.assertEqual(result['hardware']['rate'], 44100)
        self.assertEqual(result['hardware']['buffer'], 256)
        self.assertEqual(json.loads(self.path.read_text()), policy(chosen))
        self.assertEqual(PlaybackBackend(self.path, device=DEVICE)._saved(), chosen)
        self.assertEqual([c[:2] for c in self.backend.commands],
                         [('pactl', 'load-module'), ('pactl', 'move-sink-input'),
                          ('pw-cli', 'send-command'), ('pw-cli', 'set-param'),
                          ('pactl', 'move-sink-input'), ('pactl', 'unload-module')])
        self.assertIsNone(self.backend.parking)
        self.assertEqual(self.backend.stream_sink, 400)

    def test_unchanged_mode_does_not_interrupt(self):
        self.backend.apply({'rate': 0, 'period': 256})
        self.assertEqual(self.backend.commands, [])

    def test_both_stereo_pairs_return_to_their_own_endpoint(self):
        backend = FakeStereoPipeWire(self.path)
        result = backend.apply({'rate': 96000, 'period': 1024})
        self.assertEqual(backend.routes, {200: 410, 202: 420, 201: 999})
        self.assertEqual(result['hardware']['period'], 1024)
        self.assertIsNone(backend.parking)
        moves = [c for c in backend.commands if c[:2] == ('pactl', 'move-sink-input')]
        self.assertEqual([c[2] for c in moves], ['200', '202', '200', '202'])
        self.assertEqual([c[3] for c in moves[-2:]], list(STEREO_SINKS))

    def test_family_transactions_touch_only_the_selected_card_and_pairs(self):
        for card_id, model, sub in (('MAYA44XTe', 'MAYA44 XTe', 2), ('MAYA44eX', 'MAYA44 eX', 3)):
            device = MayaDevice(card_id, model, 8, '0000:09:08.0', sub)
            backend = FakeStereoPipeWire(self.path, device=device)
            self.assertEqual(backend.apply({'rate': 96000, 'period': 512})['hardware']['period'], 512)
            profile_commands = [c for c in backend.commands if c[:2] == ('pactl', 'set-card-profile')]
            self.assertTrue(profile_commands)
            self.assertTrue(all(c[2] == device.pulse_card for c in profile_commands))
            moves = [c for c in backend.commands if c[:2] == ('pactl', 'move-sink-input')]
            self.assertEqual([c[3] for c in moves[-2:]], list(device.stereo_sinks))
            self.assertEqual(backend.routes[201], 999)  # unrelated card's stream
            self.assertEqual(json.loads(self.path.read_text()), device_policy({'rate': 96000, 'period': 512}, device))
            self.path.unlink()

    def test_stereo_setting_failure_preserves_both_routes(self):
        backend = FakeStereoPipeWire(self.path)
        backend.fail_set_once = True
        with self.assertRaisesRegex(RuntimeError, 'Write rejected'):
            backend.apply({'rate': 44100, 'period': 512})
        self.assertEqual(backend.routes, {200: 410, 202: 420, 201: 999})
        self.assertEqual(backend.live['period'], 256)
        self.assertIsNone(backend.parking)
        self.assertFalse(self.path.exists())

    def test_idle_switch_does_not_start_playback(self):
        self.backend.active = False
        self.backend.node_state = 'suspended'
        with patch('playback_backend.time.sleep'):
            result = self.backend.apply({'rate': 96000, 'period': 64})
        self.assertFalse(result['hardware']['active'])
        self.assertEqual(len(self.backend.commands), 1)

    def test_failure_restores_live_and_keeps_saved_config(self):
        self.backend.apply({'rate': 0, 'period': 256})
        original = self.path.read_bytes()
        self.backend.fail_set_once = True
        with self.assertRaisesRegex(RuntimeError, 'Write rejected'):
            self.backend.apply({'rate': 88200, 'period': 512})
        self.assertEqual(self.backend.live, {'rate': 0, 'period': 256, 'periods': 2, 'irq': True})
        self.assertTrue(self.backend.active)
        self.assertEqual(self.path.read_bytes(), original)

    def test_save_failure_rolls_back_hardware(self):
        with patch.object(self.backend, '_save', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.backend.apply({'rate': 48000, 'period': 1024})
        self.assertEqual(self.backend.live, {'rate': 0, 'period': 256, 'periods': 2, 'irq': True})
        self.assertTrue(self.backend.active)
        self.assertFalse(self.path.exists())

    def test_actual_pcm_mismatch_is_not_reported_as_success(self):
        self.backend.actual_mismatch_once = True
        with self.assertRaisesRegex(RuntimeError, 'actual device configuration'):
            self.backend.apply({'rate': 44100, 'period': 64})
        self.assertEqual(self.backend.live['period'], 256)
        self.assertFalse(self.path.exists())

    def test_failed_stream_restore_cleans_up_and_rolls_back(self):
        self.backend.fail_resume_once = True
        with self.assertRaisesRegex(RuntimeError, 'restore the stream'):
            self.backend.apply({'rate': 48000, 'period': 512})
        self.assertTrue(self.backend.active)
        self.assertIsNone(self.backend.parking)
        self.assertEqual(self.backend.live['period'], 256)
        self.assertFalse(self.path.exists())

    def test_repeated_xrun_recovery_rolls_back_despite_matching_hw_params(self):
        self.backend.unstable = True
        with self.assertRaisesRegex(RuntimeError, 'unstable'):
            self.backend.apply({'rate': 44100, 'period': 64})
        self.assertEqual(self.backend.live['period'], 256)
        self.assertTrue(self.backend.active)
        self.assertIsNone(self.backend.parking)
        self.assertFalse(self.path.exists())

    def test_busy_direct_alsa_is_not_interrupted(self):
        self.backend.node_state = 'suspended'
        with self.assertRaisesRegex(RuntimeError, 'ALSA'):
            self.backend.apply({'rate': 44100, 'period': 128})
        self.assertEqual(self.backend.commands, [])
        self.assertFalse(self.path.exists())

    def test_bad_values_and_foreign_config_are_preserved(self):
        for value in ({'rate': True, 'period': 256}, {'rate': 192000, 'period': 256},
                      {'rate': 0, 'period': 48}):
            with self.assertRaises(ValueError):
                self.backend.apply(value)
        self.path.parent.mkdir()
        data = policy({'rate': 48000, 'period': 256})
        data['monitor.alsa.rules'][0]['matches'] = [{'node.name': '~alsa_output.*'}]
        self.path.write_text(json.dumps(data))
        original = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'edited manually'):
            self.backend.apply({'rate': 96000, 'period': 128})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.backend.commands, [])


if __name__ == '__main__':
    unittest.main()
