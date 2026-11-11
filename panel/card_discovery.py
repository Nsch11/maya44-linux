# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact PCI-subsystem discovery; never bind an arbitrary Tenor controller."""
from dataclasses import dataclass
from pathlib import Path
import re

MODELS = {
    1: ('MAYA44e', 'MAYA44e'),
    2: ('MAYA44XTe', 'MAYA44 XTe'),
    3: ('MAYA44eX', 'MAYA44 eX'),
}


@dataclass(frozen=True)
class MayaDevice:
    card_id: str = 'MAYA44e'
    model: str = 'MAYA44e'
    index: int = -1
    pci: str = ''
    subsystem: int = 1

    @property
    def control(self):
        return 'hw:' + self.card_id

    @property
    def stem(self):
        # Preserve existing MAYA44e endpoint names; ALSA suffixes distinguish
        # multiple cards of the same model instead of aliasing their outputs.
        return self.card_id.lower()

    @property
    def stereo_sinks(self):
        return tuple(f'{self.stem}_output_{pair}' for pair in ('12', '34'))

    @property
    def pulse_card(self):
        return 'alsa_card.pci-' + self.pci.replace(':', '_')

    def node_name(self, capture=False):
        return ('alsa_input' if capture else 'alsa_output') + '.pci-' + self.pci.replace(':', '_') + (
            '.multichannel-input' if capture else '.multichannel-output')

    def proc(self, name):
        return Path('/proc/asound') / self.card_id / name


def discover_cards(root=Path('/sys/class/sound')):
    result = []
    for path in Path(root).glob('card[0-9]*'):
        if not re.fullmatch(r'card\d+', path.name):
            continue
        device = path / 'device'
        try:
            pci = device.resolve().name
            if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', pci):
                continue
            ids = tuple(int((device / key).read_text().strip(), 16) for key in
                        ('vendor', 'device', 'subsystem_vendor', 'subsystem_device'))
            if ids[:3] != (0x19fe, 0x7000, 0x0e51) or ids[3] not in MODELS:
                continue
            # A proprietary/other driver must not be controlled with our ABI.
            if (device / 'driver').resolve().name != 'snd-maya44e':
                continue
            card_id = (path / 'id').read_text().strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]+', card_id):
                continue
            result.append(MayaDevice(card_id, MODELS[ids[3]][1], int(path.name[4:]), pci, ids[3]))
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda card: card.index)


def select_device(selector=None, *, cards=None):
    cards = discover_cards() if cards is None else cards
    if selector is not None:
        selector = str(selector).removeprefix('hw:')
        matches = [c for c in cards if selector in (c.card_id, str(c.index), c.pci)]
        if len(matches) != 1:
            raise ValueError(f'No unique supported card for --card {selector}')
        return matches[0]
    if len(cards) > 1:
        raise ValueError('Several MAYA44 cards found; select --card ' + '|'.join(c.card_id for c in cards))
    # Preserve the existing unavailable-device UI when no module is loaded.
    # It cannot write to an unrelated model because this ID is our original ABI.
    return cards[0] if cards else MayaDevice()
