# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistence tests on private config files, without opening a sound card."""
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

loader = importlib.machinery.SourceFileLoader('maya_state', str(Path(__file__).with_name('maya44e-state')))
spec = importlib.util.spec_from_loader(loader.name, loader)
state = importlib.util.module_from_spec(spec)
loader.exec_module(state)


def canonical(path):
    config = state.Config(path)
    try:
        output = path.with_suffix('.canonical')
        config.save(output)
        return output.read_text()
    finally:
        config.close()


class StateChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'asound.state'
        self.lock = self.path.with_suffix('.lock')

    def sanitize(self):
        return state.sanitize(self.path, self.lock)

    def test_all_models_and_duplicate_ids_only_phantom_changes(self):
        for model in ('MAYA44e', 'MAYA44XTe', 'MAYA44eX', 'MAYA44e_1'):
            with self.subTest(model=model):
                self.path.write_text(f'''state.{model} {{
                    control.1 {{ name 'Mic Phantom Power Switch' value true }}
                    control.2 {{ name 'Output 3-4 Playback Volume' value.0 211 value.1 210 }}
                    control.3 {{ name 'Output 3-4 Crossmix Playback Switch' value true }}
                    control.4 {{ name 'Input 1-2 Capture Source' value Mic }}
                }}
                state.NVidia {{ control.1 {{ name 'Mic Phantom Power Switch' value true }} }}
                state.MAYA44unknown {{ control.1 {{ name 'Mic Phantom Power Switch' value true }} }}
                ''')
                before = canonical(self.path)
                self.path.chmod(0o640)
                self.assertEqual(self.sanitize(), [model])
                after = canonical(self.path)
                self.assertEqual(after, before.replace('value true', 'value false', 1))
                self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
                saved = self.path.read_bytes()
                self.assertEqual(self.sanitize(), [])
                self.assertEqual(self.path.read_bytes(), saved)

    def test_missing_or_already_disabled_state_does_not_write(self):
        self.assertEqual(self.sanitize(), [])
        self.path.write_text("# keep this comment\nstate.MAYA44e { control.9 { name 'Mic Phantom Power Switch' value false } }\n")
        before = self.path.read_bytes()
        inode = self.path.stat().st_ino
        self.assertEqual(self.sanitize(), [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_integer_boolean(self):
        self.path.write_text("state.MAYA44eX { control.29 { name 'Mic Phantom Power Switch' value 1 } }")
        self.assertEqual(self.sanitize(), ['MAYA44eX'])
        self.assertIn('value 0', self.path.read_text())

    def test_simultaneous_cards(self):
        self.path.write_text(' '.join("state.%s { control.29 { name 'Mic Phantom Power Switch' value true } }" % model
                                      for model in ('MAYA44e', 'MAYA44XTe', 'MAYA44eX')))
        self.assertEqual(self.sanitize(), ['MAYA44e', 'MAYA44XTe', 'MAYA44eX'])
        self.assertNotIn('value true', self.path.read_text())

    def test_invalid_file_never_partially_replaced(self):
        for text in ('state {', 'state 123', "state.MAYA44e.control 'bad'",
                     "state.MAYA44e { control.1 { name 'Mic Phantom Power Switch' } }"):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises((OSError, ValueError)):
                    self.sanitize()
                self.assertEqual(self.path.read_text(), text)

    def test_posix_lock_contends_with_other_process(self):
        self.path.write_text("state.MAYA44e { control.1 { name 'Mic Phantom Power Switch' value true } }")
        code = '''import fcntl,sys
with open(sys.argv[1], 'a+b') as f:
 fcntl.lockf(f, fcntl.LOCK_EX, 11, 0)
 print('locked',flush=True)
 sys.stdin.read()
'''
        proc = subprocess.Popen([sys.executable, '-c', code, str(self.lock)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'locked')
            with self.assertRaises(TimeoutError):
                self.sanitize()
            self.assertIn('value true', self.path.read_text())
        finally:
            proc.communicate('release', timeout=5)
        self.assertEqual(self.sanitize(), ['MAYA44e'])

    def test_symlink_rejected(self):
        target = self.path.with_suffix('.real')
        target.write_text('state {}')
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.sanitize()
        self.assertEqual(target.read_text(), 'state {}')


if __name__ == '__main__':
    unittest.main()
