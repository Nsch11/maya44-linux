# Code provenance

The Linux driver implements the ES7010L register protocol using observed PCI
transactions and analysis of the ESI Windows driver. The existing [ALSA driver
for the PCI MAYA44](https://github.com/torvalds/linux/blob/v7.0/sound/pci/ice1712/maya44.c)
was a reference for the WM8776 command format. The [RevSpace ESP1010e
investigation](https://revspace.nl/ProjectReverseEngineerESIdriver) helped
locate the PCI initialization path in the vendor driver. The distributed
module is built from the C source in this repository.

OpenAI Codex assisted with reverse engineering, the Linux driver, the mixer
and test tooling. The project owner tested playback on the physical MAYA44e
and confirmed the audible results. The implementation used direct analysis
of the vendor driver, so this project does not claim a clean-room process.

The earliest mixer was adapted from [maya22gui 2.0 by Paul Petrov](https://sourceforge.net/projects/maya22gui/),
licensed under GPL v3 or later. The current Qt mixer draws its own controls
and communicates with ALSA. The stereo link icon is from Lucide/Feather and
retains its [license notices](../panel/assets/LUCIDE-LICENSE.txt).
