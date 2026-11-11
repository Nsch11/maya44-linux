#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Install/remove the driver via DKMS without unloading the running module.

Debian/Ubuntu with systemd, udev and alsa-utils. The mixer and user PipeWire
policies remain separate. Run from the source tree with sudo.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'packaging'
STATE = Path('/var/lib/maya44e')
RECEIPT = STATE / 'installation.json'
MANAGED = {
    Path('/etc/modprobe.d/maya44e.conf'): 'maya44e.conf',
    Path('/etc/udev/rules.d/89-maya44e-state.rules'): '89-maya44e-state.rules',
    Path('/etc/systemd/system/alsa-restore.service.d/maya44e-state.conf'): 'maya44e-state.conf',
    Path('/usr/local/libexec/maya44e-state'): 'maya44e-state',
    Path('/usr/local/sbin/maya44e-install'): 'install.py',
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*args, capture=False):
    print('+', ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          env={**os.environ, 'LC_ALL': 'C.UTF-8'})


def atomic_write(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.maya44e-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2) + '\n').encode())


@contextmanager
def locked():
    STATE.mkdir(mode=0o755, parents=True, exist_ok=True)
    with open(STATE / 'install.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def check_managed(receipt):
    hashes = receipt.get('managed', {}) if receipt else {}
    for path in MANAGED:
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or hashes.get(str(path)) != digest(path):
            raise RuntimeError(f'Unmanaged or edited file; refusing to replace {path}')


def kernels(requested):
    result = requested or [os.uname().release]
    if not requested:
        result += sorted(p.name for p in Path('/lib/modules').iterdir()
                         if (p / 'build/Makefile').is_file())
    result = list(dict.fromkeys(result))
    for kernel in result:
        if not re.fullmatch(r'[A-Za-z0-9_.+-]+', kernel):
            raise ValueError('Invalid kernel release')
        if not Path('/lib/modules', kernel, 'build/Makefile').is_file():
            raise RuntimeError(f'Missing headers for {kernel}')
    return result


def integration():
    run('systemctl', 'daemon-reload')
    run('udevadm', 'control', '--reload-rules')
    # No trigger, restart, modprobe or unload: live audio must stay untouched.


def install(args, previous):
    if not (ROOT / 'kernel/maya44e.c').is_file():
        raise RuntimeError('Specify the driver source tree: install --source /path/to/maya44-linux')
    for command in ('dkms', 'make', 'gcc', 'modinfo', 'modprobe', 'depmod',
                    'alsactl', 'systemctl', 'udevadm'):
        if not shutil.which(command):
            raise RuntimeError(f'Required program is missing: {command}')
    if Path('/etc/alsa/state-daemon.conf').exists():
        raise RuntimeError('This installer supports alsa-restore.service, not the ALSA state daemon')
    if not Path('/usr/lib/systemd/system/alsa-restore.service').exists():
        raise RuntimeError('The distribution ALSA restore unit is required')
    # This kernel driver has only been built/tested on x86_64 so far.
    if os.uname().machine != 'x86_64':
        raise RuntimeError('Only x86_64 is currently validated')
    if Path('/sys/firmware/efi').exists() and shutil.which('mokutil'):
        secure = run('mokutil', '--sb-state', capture=True).stdout
        if 'SecureBoot enabled' in secure:
            raise RuntimeError('Secure Boot is enabled; configure trusted DKMS signing before installation')
    targets = kernels(args.kernel)
    check_managed(previous)
    source_hash = digest(ROOT / 'kernel/maya44e.c')
    version = '0.1.20261027.' + source_hash[:12]
    source = Path('/usr/src') / ('maya44e-' + version)
    contents = {
        'maya44e.c': (ROOT / 'kernel/maya44e.c').read_bytes(),
        'Kbuild': (ROOT / 'kernel/Kbuild').read_bytes(),
        'dkms.conf': (PACKAGE / 'dkms.conf').read_bytes().replace(b'@VERSION@', version.encode()),
    }
    if source.exists():
        if not source.is_dir() or any(not (source / n).is_file() or
                                      (source / n).read_bytes() != data for n, data in contents.items()):
            raise RuntimeError(f'Different source already exists at {source}; use a new package version')
    backup = STATE / 'backups' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup.mkdir(parents=True)
    if RECEIPT.exists():
        shutil.copy2(RECEIPT, backup / 'installation.json')
    state_file = Path('/var/lib/alsa/asound.state')
    if state_file.exists():
        shutil.copy2(state_file, backup / 'asound.state')
    # Keep the exact manually loaded binary as an independent recovery artifact.
    if (ROOT / 'kernel/snd-maya44e.ko').exists():
        shutil.copy2(ROOT / 'kernel/snd-maya44e.ko', backup / 'manual-snd-maya44e.ko')
    for path in MANAGED:
        if path.exists():
            destination = backup / path.relative_to('/')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    write_json(backup / 'before.json', {
        'kernel': os.uname().release, 'targets': targets,
        'dkms': run('dkms', 'status', capture=True).stdout,
        'loaded_srcversion': Path('/sys/module/snd_maya44e/srcversion').read_text().strip()
        if Path('/sys/module/snd_maya44e/srcversion').exists() else None,
    })
    fresh = not source.exists()
    registered = False
    touched = False
    try:
        if fresh:
            source.mkdir()
            for name, data in contents.items():
                atomic_write(source / name, data)
        if not Path('/var/lib/dkms/maya44e', version, 'source').exists():
            run('dkms', 'add', '-m', 'maya44e', '-v', version)
            registered = True
        # Build every target before changing any installed module or config.
        for kernel in targets:
            run('dkms', 'build', '-m', 'maya44e', '-v', version, '-k', kernel)
        for kernel in targets:
            run('dkms', 'install', '-m', 'maya44e', '-v', version, '-k', kernel)
        for path, name in MANAGED.items():
            touched = True
            atomic_write(path, (PACKAGE / name).read_bytes(),
                         0o755 if name in ('maya44e-state', 'install.py') else 0o644)
        # Store now without changing hardware, then sanitize the persisted +48V.
        # Preserve all other cards; alsactl serializes updates with its own lock.
        for path in Path('/sys/class/sound').glob('card[0-9]*'):
            driver = path / 'device/driver'
            if driver.resolve().name == 'snd-maya44e':
                card_id = (path / 'id').read_text().strip()
                run('alsactl', 'store', card_id)
        run('/usr/local/libexec/maya44e-state')
        integration()
        for kernel in targets:
            installed = run('modinfo', '-k', kernel, '-F', 'filename', 'snd_maya44e', capture=True).stdout.strip()
            if '/updates/dkms/' not in installed:
                raise RuntimeError(f'DKMS module not selected for {kernel}: {installed}')
            vermagic = run('modinfo', '-k', kernel, '-F', 'vermagic', 'snd_maya44e', capture=True).stdout
            if not vermagic.startswith(kernel + ' '):
                raise RuntimeError(f'Wrong module kernel version for {kernel}')
        if previous and previous['version'] != version:
            # Retain its source and backup, but keep exactly one DKMS version
            # registered: autoinstall otherwise selects the highest version,
            # silently undoing a deliberate rollback on a new kernel.
            run('dkms', 'remove', '-m', 'maya44e', '-v', previous['version'], '--all')
        history = list(previous.get('history', [])) if previous else []
        if previous and previous['version'] != version:
            history.append(previous['version'])
        receipt = {'version': version, 'source_sha256': source_hash, 'kernels': targets,
                   'backup': str(backup), 'history': list(dict.fromkeys(history)),
                   'managed': {str(p): digest(p) for p in MANAGED}}
        write_json(RECEIPT, receipt)
        write_json(backup / 'result.json', {'success': True, **receipt})
        print('Installed. Running module and audio streams were not replaced.')
    except BaseException as error:
        # Revert integration and candidate DKMS registration on a fresh install.
        # Preserve the running module; never attempt force unload or insmod here.
        recovery_errors = []
        if touched:
            for path in MANAGED:
                old = backup / path.relative_to('/')
                try:
                    if old.exists():
                        atomic_write(path, old.read_bytes(), old.stat().st_mode & 0o777)
                    elif path.exists():
                        path.unlink()
                except Exception as exc:
                    recovery_errors.append(str(exc))
        if registered:
            try:
                run('dkms', 'remove', '-m', 'maya44e', '-v', version, '--all')
                if previous and previous['version'] != version:
                    if not Path('/var/lib/dkms/maya44e', previous['version'], 'source').exists():
                        run('dkms', 'add', '-m', 'maya44e', '-v', previous['version'])
                    for kernel in previous['kernels']:
                        run('dkms', 'build', '-m', 'maya44e', '-v', previous['version'], '-k', kernel)
                        run('dkms', 'install', '-m', 'maya44e', '-v', previous['version'], '-k', kernel)
            except Exception as exc:
                recovery_errors.append(str(exc))
        if fresh and not recovery_errors and source.exists():
            shutil.rmtree(source)
        if touched:
            try:
                integration()
            except Exception as exc:
                recovery_errors.append(str(exc))
        write_json(backup / 'result.json', {'success': False, 'error': str(error),
                                           'recovery_errors': recovery_errors})
        raise


def remove(previous):
    if not previous:
        raise RuntimeError('No installation receipt; nothing managed to remove')
    check_managed(previous)
    for path in MANAGED:
        if path.exists():
            path.unlink()
    integration()
    for version in dict.fromkeys([previous['version'], *previous.get('history', [])]):
        if Path('/var/lib/dkms/maya44e', version, 'source').exists():
            run('dkms', 'remove', '-m', 'maya44e', '-v', version, '--all')
        source = Path('/usr/src') / ('maya44e-' + version)
        if source.is_dir() and not source.is_symlink():
            shutil.rmtree(source)
    shutil.copy2(RECEIPT, STATE / 'removed-installation.json')
    RECEIPT.unlink()
    print('Removed from disk. Live module remains loaded until shutdown or manual unload.')


def rollback(previous, version):
    if not previous or version not in previous.get('history', []):
        raise RuntimeError('Choose a retained version from installation.json history')
    check_managed(previous)
    targets = kernels(None)
    if not Path('/var/lib/dkms/maya44e', version, 'source').exists():
        run('dkms', 'add', '-m', 'maya44e', '-v', version)
    try:
        for kernel in targets:
            run('dkms', 'build', '-m', 'maya44e', '-v', version, '-k', kernel)
        for kernel in targets:
            run('dkms', 'install', '--force', '-m', 'maya44e', '-v', version, '-k', kernel)
    except BaseException:
        run('dkms', 'remove', '-m', 'maya44e', '-v', version, '--all')
        for kernel in previous['kernels']:
            run('dkms', 'install', '-m', 'maya44e', '-v', previous['version'], '-k', kernel)
        raise
    old = previous['version']
    run('dkms', 'remove', '-m', 'maya44e', '-v', old, '--all')
    previous['version'] = version
    previous['kernels'] = targets
    previous['history'] = [v for v in previous['history'] if v != version] + [old]
    previous['source_sha256'] = digest(Path('/usr/src') / ('maya44e-' + version) / 'maya44e.c')
    write_json(RECEIPT, previous)
    print('Previous version selected on disk. The live module was not replaced.')


def main():
    global ROOT, PACKAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'remove', 'status', 'rollback'))
    parser.add_argument('--kernel', action='append', help='default: running kernel and installed headers')
    parser.add_argument('--version', help='retained package version for rollback')
    parser.add_argument('--source', type=Path, help='driver source tree for install/update')
    args = parser.parse_args()
    if args.source:
        ROOT = args.source.resolve()
        PACKAGE = ROOT / 'packaging'
    if args.action == 'status':
        print(RECEIPT.read_text() if RECEIPT.exists() else 'Not installed by this installer')
        run('dkms', 'status')
        return
    if os.geteuid() != 0:
        parser.error('Use sudo for install, remove or rollback')
    with locked():
        previous = json.loads(RECEIPT.read_text()) if RECEIPT.exists() else None
        if args.action == 'install':
            install(args, previous)
        elif args.action == 'remove':
            remove(previous)
        else:
            rollback(previous, args.version)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('maya44e-install: ' + str(exc), file=sys.stderr)
        sys.exit(1)
