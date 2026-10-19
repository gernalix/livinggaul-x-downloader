"""Offline checks for the Oracle owner launcher; no SSH or media access."""
from pathlib import Path
import runpy
import shlex
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class OracleCliTests(unittest.TestCase):
    def invoke(self, *args, result=0):
        with patch.object(sys, 'argv', ['livinggaul-oracle-cli', *args]), \
                patch('subprocess.call', return_value=result) as call:
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(ROOT / 'livinggaul-oracle-cli'), run_name='__main__')
        return stopped.exception.code, call

    def test_start_requests_only_the_fixed_managed_service(self):
        code, call = self.invoke('--start-download-service', result=255)
        self.assertEqual(code, 255)
        argv = call.call_args.args[0]
        self.assertEqual(argv[:-1], [
            'ssh', '-F', str(Path.home() / '.ssh/config'), '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=15', 'oracle-vm'])
        self.assertEqual(shlex.split(argv[-1]), [
            'sudo', '-n', 'systemctl', '--no-block', 'start', 'livinggaul-download.service'])
        call.assert_called_once()

    def test_start_rejects_other_actions_and_service_arguments(self):
        for args in [('--start-download-service', '--run'),
                     ('--start-download-service', 'other.service')]:
            code, call = self.invoke(*args)
            self.assertEqual(code, 2)
            call.assert_not_called()

    def test_run_keeps_the_existing_lock_and_owner(self):
        code, call = self.invoke('--run')
        self.assertEqual(code, 0)
        argv = shlex.split(call.call_args.args[0][-1])
        self.assertIn('User=livinggaul', argv)
        self.assertIn('/home/livinggaul/runtime.lock', argv)
        self.assertEqual(argv[-3:], ['--run', '--cookies-mode', 'none'])


if __name__ == '__main__':
    unittest.main()
