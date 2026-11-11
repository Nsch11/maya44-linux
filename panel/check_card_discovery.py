# SPDX-License-Identifier: GPL-3.0-or-later
"""Family selection and isolation tests; fake sysfs, no live hardware writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from card_discovery import MayaDevice, discover_cards, select_device
from alsa_backend import AlsaBackend, ALL_CONTROLS
from playback_backend import PlaybackBackend, policy


class DiscoveryChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, index, subsystem=1, *, vendor=0x19fe, device=0x7000,
             subvendor=0x0e51, driver='snd-maya44e', card_id=None):
        path = self.root / f'sound/card{index}'
        pci = self.root / f'pci/0000:09:{index:02x}.0'
        pci.mkdir(parents=True)
        path.mkdir(parents=True)
        for key, value in zip(('vendor', 'device', 'subsystem_vendor', 'subsystem_device'),
                              (vendor, device, subvendor, subsystem)):
            (pci / key).write_text(hex(value))
        drivers = self.root / 'drivers' / driver
        drivers.mkdir(parents=True, exist_ok=True)
        (pci / 'driver').symlink_to(drivers)
        (path / 'device').symlink_to(pci)
        (path / 'id').write_text(card_id or {1: 'MAYA44e', 2: 'MAYA44XTe', 3: 'MAYA44eX'}.get(subsystem, 'Other'))
        return path

    def test_three_models_actual_ids_and_arbitrary_pci_slots(self):
        for index, subsystem in ((4, 1), (8, 2), (9, 3)):
            self.card(index, subsystem)
        cards = discover_cards(self.root / 'sound')
        self.assertEqual([c.model for c in cards], ['MAYA44e', 'MAYA44 XTe', 'MAYA44 eX'])
        self.assertEqual(cards[1].control, 'hw:MAYA44XTe')
        self.assertEqual(cards[2].pci, '0000:09:09.0')
        self.assertEqual(cards[2].stereo_sinks, ('maya44ex_output_12', 'maya44ex_output_34'))
        with self.assertRaisesRegex(ValueError, 'Several'):
            select_device(cards=cards)
        for selector in ('MAYA44XTe', 'hw:MAYA44XTe', '8', '0000:09:08.0'):
            self.assertEqual(select_device(selector, cards=cards), cards[1])

    def test_rejects_wrong_subsystems_vendors_devices_and_drivers(self):
        for i, kwargs in enumerate(({'subsystem': 0}, {'subsystem': 0xffff},
                                   {'subsystem': 4}, {'subvendor': 0x1234},
                                   {'vendor': 0xffff}, {'device': 0xffff},
                                   {'driver': 'snd-other'})):
            self.card(i, **kwargs)
        self.assertEqual(discover_cards(self.root / 'sound'), [])
        with self.assertRaises(ValueError):
            select_device('Other', cards=[])

    def test_two_same_model_cards_have_distinct_endpoints_and_selection(self):
        self.card(1, 3)
        self.card(2, 3, card_id='MAYA44eX_1')
        cards = discover_cards(self.root / 'sound')
        self.assertNotEqual(cards[0].stereo_sinks, cards[1].stereo_sinks)
        self.assertEqual(select_device('MAYA44eX_1', cards=cards), cards[1])
        self.assertEqual(MayaDevice().stereo_sinks, ('maya44e_output_12', 'maya44e_output_34'))

    def test_missing_and_incomplete_sysfs_are_safe(self):
        path = self.card(7)
        (path / 'id').unlink()
        self.assertEqual(discover_cards(self.root / 'sound'), [])
        self.assertEqual(select_device(cards=[]).card_id, 'MAYA44e')

    def test_all_control_reads_and_writes_use_selected_card(self):
        selected = MayaDevice('MAYA44eX', 'MAYA44 eX', 9, '0000:09:09.0', 3)
        opened = []
        class Reader:
            def __init__(self, name, count, maximum, **kw):
                opened.append(kw['card']); self.count = count
            def read(self): return (0,) * self.count
            def close(self): pass
        backend = AlsaBackend(device=selected)
        with patch('alsa_backend.ControlReader', Reader):
            self.assertEqual(len(backend.read()), len(ALL_CONTROLS))
        self.assertEqual(set(opened), {'hw:MAYA44eX'})
        with patch.object(backend, '_run') as run:
            backend.write('out34.volume', (100, 101))
            self.assertEqual(run.call_args.args[0][2], 'MAYA44eX')

    def test_pipewire_node_selection_excludes_other_cards_and_virtual_sinks(self):
        device = MayaDevice('MAYA44XTe', 'MAYA44 XTe', 8, '0000:09:08.0', 2)
        backend = PlaybackBackend(self.root / 'policy.json', device=device)
        def node(index, profile='multichannel-output', stream='playback', name='actual-selected-pcm'):
            return {'info': {'props': {'api.alsa.pcm.card': index, 'node.name': name,
                                     'device.profile.name': profile, 'api.alsa.pcm.stream': stream,
                                     'alsa.id': device.card_id if index == 8 else 'MAYA44e',
                                     'alsa.driver_name': 'snd_maya44e'}}}
        nodes = [node(2), node(8, name='actual-selected-pcm'),
                 node(8, 'multichannel-input', 'capture'),
                 {'info': {'props': {'node.name': 'maya44xte_output_12'}}}]
        with patch.object(backend, '_run', return_value=json.dumps(nodes)):
            self.assertEqual(backend._node(), nodes[1])
            self.assertEqual(backend.sink, 'actual-selected-pcm')
            self.assertEqual(backend._capture_node(), nodes[2])
        self.assertEqual(backend.card, 'alsa_card.pci-0000_09_08.0')
        nodes[1]['info']['props']['device.id'] = 987
        nodes.append({'id': 987, 'info': {'props': {'device.name': 'actual-device-name'}}})
        with patch.object(backend, '_run', return_value=json.dumps(nodes)):
            backend._node()
        self.assertEqual(backend.card, 'actual-device-name')
        self.assertEqual(backend.stereo_sinks, ('maya44xte_output_12', 'maya44xte_output_34'))
        settings = {'rate': 96000, 'period': 256}
        data = policy(settings, device)
        self.assertEqual(data['monitor.alsa.rules'][0]['matches'][0]['alsa.id'], 'MAYA44XTe')
        self.assertNotIn('09_08', str(data))
        backend._save(settings)
        self.assertEqual(backend._saved(), settings)
        self.assertEqual(json.loads(backend.config_path.read_text()), data)
        # A previous machine's/card's rule must never be silently overwritten.
        backend.config_path.write_text(json.dumps(policy(settings, MayaDevice())))
        with self.assertRaisesRegex(RuntimeError, 'manually'):
            backend._saved()

    def test_legacy_selected_slot_policy_is_read_without_rewrite(self):
        device = MayaDevice('MAYA44e', 'MAYA44e', 4, '0000:09:04.0')
        settings = {'rate': 48000, 'period': 512}
        backend = PlaybackBackend(self.root / 'policy.json', device=device)
        data = json.dumps(policy(settings, device, legacy=True))
        backend.config_path.write_text(data)
        self.assertEqual(backend._saved(), settings)
        self.assertEqual(backend.config_path.read_text(), data)


if __name__ == '__main__':
    unittest.main()
