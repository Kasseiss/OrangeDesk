import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app import engine, single_file


class SingleFileTests(unittest.TestCase):
    def test_cli_dispatch_does_not_load_ui(self):
        with patch.object(sys, 'orangedesk_single_file', False, create=True), patch.object(single_file, 'prepare_console') as console, patch('app.cli.cli_main', return_value=7) as cli, patch('app.main.main') as gui:
            self.assertEqual(single_file.single_main(['whitelist', 'list']), 7)
            console.assert_called_once()
            cli.assert_called_once_with(['whitelist', 'list'])
            gui.assert_not_called()

    def test_double_click_and_internal_roles_use_windowed_entry(self):
        with patch.object(sys, 'orangedesk_single_file', False, create=True), patch.object(single_file, 'prepare_console') as console, patch('app.main.main', return_value=0) as gui:
            for args in ([], ['--daemon'], ['--vendor-guard', 'record'], ['--whitelist']):
                self.assertEqual(single_file.single_main(args), 0)
                gui.assert_called_with(args)
            console.assert_not_called()

    def test_renamed_single_executable_starts_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            executable=Path(directory)/'伴航自定义名字.exe'
            executable.touch()
            with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'orangedesk_single_file', True, create=True), patch.object(sys, 'executable', str(executable)):
                self.assertEqual(engine.gui_command('--daemon'), [str(executable), '--daemon'])

    def test_independent_child_has_own_extraction_and_preserves_environment(self):
        original={'LOCALAPPDATA': 'isolated', 'CUSTOM': 'value'}
        with patch.object(sys, 'orangedesk_single_file', True, create=True), patch.object(sys, 'executable', 'C:/apps/OrangeDesk.exe'), patch.object(engine.subprocess, 'Popen') as spawn:
            engine.hidden_spawn(['C:/apps/OrangeDesk.exe', '--daemon'], env=original)
            self.assertEqual(spawn.call_args.kwargs['env'], dict(original, PYINSTALLER_RESET_ENVIRONMENT='1'))
            self.assertEqual(original, {'LOCALAPPDATA': 'isolated', 'CUSTOM': 'value'})
            engine.hidden_spawn(['C:/Windows/powershell.exe'], env=original)
            self.assertEqual(spawn.call_args.kwargs['env'], original)

    @unittest.skipUnless(os.name == 'nt', 'Windows process identity')
    def test_direct_child_identity_uses_real_windows_parent(self):
        executable=engine.process_identity(os.getpid())[1]
        child=subprocess.Popen([executable, '-c', 'import time;time.sleep(10)'], creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertEqual(engine.process_parent_id(child.pid), os.getpid())
            self.assertNotEqual(engine.process_parent_id(os.getpid()), child.pid)
        finally:
            child.terminate()
            child.wait(5)
