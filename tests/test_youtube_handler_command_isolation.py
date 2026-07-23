import logging
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules.youtube_handler import (
    _find_yt_dlp_command,
    test_video_availability as check_video_availability,
)


class YouTubeDlpCommandIsolationTests(unittest.TestCase):
    def test_python_module_command_ignores_user_cli_config(self):
        version_result = SimpleNamespace(returncode=0)

        with patch('modules.youtube_handler.sys.executable', r'C:\Python\python.exe'), \
             patch('modules.youtube_handler.subprocess.run', return_value=version_result):
            command = _find_yt_dlp_command(logging.getLogger('test'))

        self.assertEqual(
            command,
            [r'C:\Python\python.exe', '-m', 'yt_dlp', '--ignore-config'],
        )

    def test_preflight_returns_actionable_yt_dlp_error(self):
        process_result = SimpleNamespace(
            returncode=1,
            stdout='',
            stderr='ERROR: Could not copy Chrome cookie database',
        )

        with patch('modules.youtube_handler.load_config', return_value={}), \
             patch('modules.youtube_handler._append_yt_dlp_network_args'), \
             patch('modules.youtube_handler.subprocess.run', return_value=process_result):
            available, _info, error = check_video_availability(
                'https://www.youtube.com/watch?v=test',
                ['yt-dlp', '--ignore-config'],
                logger=logging.getLogger('test'),
            )

        self.assertFalse(available)
        self.assertIn('Could not copy Chrome cookie database', error)


if __name__ == '__main__':
    unittest.main()
