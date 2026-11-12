"""Offscreen GUI and bundled-backend regression checks; no real games launched.

Run from the repository root:
    QT_QPA_PLATFORM=offscreen python3 -m unittest -v tests/test_gui.py
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
LIB = Path(__file__).resolve().parents[1] / 'lib/cerun-gui'
sys.path.insert(0, str(LIB))
import cerun_gui as gui  # noqa: E402

APP = gui.QApplication.instance() or gui.QApplication([])


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config/executables.conf'
        self.steam = self.root / 'Steam folder'
        (self.steam / 'steamapps').mkdir(parents=True)
        self.exe = self.root / 'Tool with spaces $(literal).exe'
        self.exe.touch()
        self.other_exe = self.root / 'other.exe'
        self.other_exe.touch()
        self.values = dict.fromkeys(gui.KEYS, '')
        self.values.update(DEFAULT_EXE=str(self.exe), EXE_1=str(self.other_exe), STEAMPATH=str(self.steam))
        gui.save_config(self.config, self.values)
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.process(123, 'Fall of Avalon.exe', 42)
        self.backend = self.root / 'mock-backend'
        self.mock_backend('import sys,json\nprint(json.dumps(sys.argv[1:]),flush=True)\n')
        self.windows = []
        self.errors = []
        self.error_patch = patch.object(gui.Window, 'error', lambda w, msg: self.errors.append(msg))
        self.error_patch.start()

    def tearDown(self):
        for w in self.windows:
            for launch in w.launches:
                launch.child.wait(timeout=5)
            w.close()
        self.error_patch.stop()
        self.temp.cleanup()

    def mock_backend(self, body):
        self.backend.write_text('#!/usr/bin/python3\n' + body)
        self.backend.chmod(0o755)

    def process(self, pid, name, started, cmdline=None):
        p = self.proc / str(pid)
        p.mkdir(exist_ok=True)
        (p / 'comm').write_text(name[:15] + '\n')
        (p / 'cmdline').write_bytes(cmdline if cmdline is not None else name.encode() + b'\0')
        (p / 'stat').write_text(f'{pid} ({name[:15]}) S ' + '0 ' * 18 + str(started) + '\n')
        return p

    def window(self):
        w = gui.Window(self.config, self.backend, self.root / 'state', self.proc)
        self.windows.append(w)
        return w

    def finish(self, w):
        for launch in w.launches:
            launch.child.wait(timeout=5)
        w.poll()

    def test_config_validation_and_literal_paths(self):
        self.assertEqual(gui.read_config(self.config), self.values)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        before = self.config.read_bytes()
        for change in [dict(STEAMPATH='/missing'), dict(DEFAULT_EXE=''), dict(EXE_1='bad\npath'),
                       dict(EXE_2='relative.exe')]:
            with self.assertRaises(ValueError):
                gui.save_config(self.config, dict(self.values, **change))
            self.assertEqual(self.config.read_bytes(), before)

    def test_config_tolerates_whitespace_and_home(self):
        text = f'DEFAULT_EXE=~/tool.exe \r\n   \nSTEAMPATH=$HOME/Steam\n'
        values = gui.parse_config(text)
        self.assertEqual(values['DEFAULT_EXE'], str(gui.HOME_DIR / 'tool.exe'))
        self.assertEqual(values['STEAMPATH'], str(gui.HOME_DIR / 'Steam'))

    def test_process_names_and_disappearing_process(self):
        self.process(222, 'name with ) parentheses.exe', 55)
        (self.proc / '333').mkdir()
        self.process(444, 'normal-linux-app', 66)
        self.process(555, 'bash', 77, b'/bin/bash\0/opt/lib/cerun-backend.sh\0--pid\0123\0/x/tool.exe\0')
        self.process(666, 'A Very Long Game Name.exe', 88, b'Z:\\games\\A Very Long Game Name.exe -windowed\0')
        processes = gui.list_processes(self.proc)
        self.assertEqual([p.pid for p in processes], [123, 222, 666])
        self.assertEqual(processes[1].started, '55')
        self.assertEqual(processes[1].name, 'name with ) parentheses.exe')
        self.assertEqual(processes[2].name, 'A Very Long Game Name.exe')

    def test_refresh_preserves_identity_not_reused_pid(self):
        w = self.window()
        self.assertFalse(w.launch_button.isEnabled())
        w.table.selectRow(0)
        self.assertTrue(w.launch_button.isEnabled())
        w.refresh()
        self.assertEqual(w.selected_process().pid, 123)
        self.process(123, 'Fall of Avalon.exe', 99)
        w.refresh()
        self.assertIsNone(w.selected_process())
        self.assertFalse(w.launch_button.isEnabled())

    def test_edit_and_external_config_changes(self):
        w = self.window()
        w.paths['EXE_2'].setText(str(self.exe))
        self.assertTrue(w.save_paths())
        self.assertEqual(gui.read_config(self.config), dict(self.values, EXE_2=str(self.exe)))
        self.config.write_text(self.config.read_text() + '# external edit\n')
        before = self.config.read_bytes()
        self.assertFalse(w.save_paths())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIn('changed outside', self.errors[-1])

    def test_unparseable_config_can_be_replaced(self):
        self.config.write_text('DEFAULT_EXE=' + str(self.exe) + '\nSURPRISE=1\n')
        w = self.window()
        self.assertIn('Unrecognized', self.errors[-1])
        for key, value in self.values.items():
            w.paths[key].setText(value)
        self.assertTrue(w.save_paths(), self.errors)
        self.assertEqual(gui.read_config(self.config), self.values)

    def test_missing_shortcut_does_not_block_other_launches(self):
        self.other_exe.unlink()
        w = self.window()
        w.table.selectRow(0)
        self.assertTrue(w.save_paths())
        self.assertIn('EXE_1', w.status.text())
        w.launch()
        self.assertEqual(len(w.launches), 1, self.errors)
        self.finish(w)
        w.buttons.button(1).setChecked(True)
        w.launch()
        self.assertEqual(w.launches, [])
        self.assertIn('executable not found', self.errors[-1])

    def test_unchanged_config_is_not_rewritten(self):
        w = self.window()
        w.table.selectRow(0)
        inode = self.config.stat().st_ino
        w.launch()
        self.finish(w)
        self.assertEqual(self.config.stat().st_ino, inode)

    def test_stale_process_blocks_launch(self):
        w = self.window()
        w.table.selectRow(0)
        self.process(123, 'other.exe', 99)
        w.launch()
        self.assertEqual(w.launches, [])
        self.assertIn('replaced', self.errors[-1])

    def test_launch_arguments_log_and_reopen(self):
        w = self.window()
        w.table.selectRow(0)
        w.buttons.button(1).setChecked(True)
        w.launch()
        self.assertEqual(len(w.launches), 1, self.errors)
        self.assertTrue(w.timer.isActive())
        self.finish(w)
        self.assertFalse(w.timer.isActive())
        self.assertEqual(json.loads(w.last_log.read_text()),
                         ['--pid', '123', str(self.other_exe), str(self.steam)])
        self.assertTrue(w.log_button.isEnabled())
        other = self.window()
        self.assertEqual(other.last_log, w.last_log)
        with patch.object(gui.QDesktopServices, 'openUrl', return_value=True) as open_url:
            other.open_log()
            self.assertEqual(open_url.call_args.args[0].toLocalFile(), str(w.last_log))

    def test_second_executable_can_launch_while_first_runs(self):
        self.mock_backend('import time\ntime.sleep(0.5)\n')
        w = self.window()
        w.table.selectRow(0)
        w.launch()
        self.assertEqual(len(w.launches), 1, self.errors)
        self.assertFalse(w.launch_button.isEnabled())  # Same executable, same process.
        w.launch()
        self.assertEqual(len(w.launches), 1)
        w.buttons.button(1).setChecked(True)
        self.assertTrue(w.launch_button.isEnabled())
        w.launch()
        self.assertEqual(len(w.launches), 2, self.errors)
        self.finish(w)
        self.assertEqual(w.launches, [])
        self.assertTrue(w.launch_button.isEnabled())

    def test_failed_launch_selects_its_log(self):
        self.mock_backend('import sys\nprint("boom")\nsys.exit(3)\n')
        w = self.window()
        w.table.selectRow(0)
        w.launch()
        failed = w.launches[0].log
        self.finish(w)
        self.assertIn('exit 3', w.status.text())
        self.assertEqual(w.last_log, failed)

    def test_old_logs_are_pruned(self):
        state = self.root / 'state'
        state.mkdir()
        for i in range(30):
            (state / f'gui-20260101-0000{i:02d}-x.log').touch()
        (state / 'last-log').write_text('unrelated')
        gui.prune_logs(state)
        left = sorted(p.name for p in state.glob('gui-*.log'))
        self.assertEqual(len(left), gui.KEEP_LOGS)
        self.assertEqual(left[0], 'gui-20260101-000010-x.log')
        self.assertTrue((state / 'last-log').exists())

    def test_child_survives_window_close(self):
        self.mock_backend('import time\ntime.sleep(0.3)\n')
        w = self.window()
        w.table.selectRow(0)
        w.launch()
        child = w.launches[0].child
        w.close()
        self.assertIsNone(child.poll())
        self.assertEqual(child.wait(timeout=5), 0)
        w.poll()

    def test_missing_config_setup(self):
        self.config.unlink()
        w = self.window()
        self.assertFalse(self.config.exists())
        for key, value in self.values.items():
            w.paths[key].setText(value)
        self.assertTrue(w.save_paths())
        self.assertEqual(gui.read_config(self.config), self.values)

    def test_window_icon_is_packaged(self):
        w = self.window()
        self.assertTrue(gui.ICON_PATH.is_file())
        self.assertFalse(w.windowIcon().isNull())

    def test_bundled_backend_without_terminal(self):
        source = (LIB / 'cerun-backend.sh').read_text()
        prefix = self.root / 'prefix with spaces'
        (prefix / 'pfx/drive_c').mkdir(parents=True)
        (prefix / 'pfx/user.reg').touch()
        (prefix / 'config_info').write_text(str(self.root) + '/files/share/default_pfx/\n')
        proton = self.root / 'proton'
        proton.write_text('#!/usr/bin/python3\nimport sys,json\nprint("MOCK_PROTON:"+json.dumps(sys.argv[1:]))\n')
        proton.chmod(0o755)
        p = self.proc / '123'
        (p / 'environ').write_bytes(f'STEAM_COMPAT_DATA_PATH={prefix}\0'.encode())
        (p / 'status').write_text('PPid:\t1\n')
        script = self.root / 'backend.sh'
        script.write_text(source.replace('/proc/', str(self.proc) + '/'))
        cmd = ['bash', str(script), '--pid', '123', str(self.exe), str(self.steam)]
        before = set(Path('/tmp').glob('cerun.*.log'))
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('MOCK_PROTON:' + json.dumps(['runinprefix', str(self.exe)]), r.stdout)
        self.assertEqual(r.stderr, '')
        self.assertEqual(set(Path('/tmp').glob('cerun.*.log')), before)
        cmd[-1] = str(self.root / 'no steam here')
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('Steam installation not found', r.stdout)


if __name__ == '__main__':
    unittest.main()
