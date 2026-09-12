#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import unittest
import tempfile
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app, _extract_video_id


class TestTaskCover(unittest.TestCase):
    def test_extract_video_id(self):
        self.assertEqual(_extract_video_id("https://www.youtube.com/watch?v=eeC6MYHsbpI"), "eeC6MYHsbpI")
        self.assertEqual(_extract_video_id("https://www.youtube.com/watch?v=eeC6MYHsbpI&t=10s"), "eeC6MYHsbpI")
        self.assertEqual(_extract_video_id("https://youtu.be/eeC6MYHsbpI"), "eeC6MYHsbpI")
        self.assertEqual(_extract_video_id("https://www.youtube.com/shorts/eeC6MYHsbpI"), "eeC6MYHsbpI")
        self.assertEqual(_extract_video_id("eeC6MYHsbpI"), "eeC6MYHsbpI")
        self.assertIsNone(_extract_video_id(""))
        self.assertIsNone(_extract_video_id(None))
        self.assertIsNone(_extract_video_id("https://example.com/video"))

    def test_get_task_cover_not_found(self):
        client = app.test_client()
        with patch('app.load_config', return_value={'password_protection_enabled': False}):
            with patch('app.get_task', return_value=None):
                resp = client.get('/tasks/non-existent-id/cover')
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.mimetype, 'image/svg+xml')

    def test_get_task_cover_local_file_priority(self):
        client = app.test_client()
        temp_dir = tempfile.mkdtemp()
        cover_file = os.path.join(temp_dir, 'custom_cover.jpg')
        with open(cover_file, 'wb') as f:
            f.write(b'\xff\xd8\xff\xe0\x00\x10JFIF' + b'\x00' * 100)

        try:
            fake_task = {
                'id': 'task-test-local',
                'cover_path_local': cover_file,
                'youtube_url': 'https://www.youtube.com/watch?v=eeC6MYHsbpI'
            }
            with patch('app.load_config', return_value={'password_protection_enabled': False}):
                with patch('app.get_task', return_value=fake_task):
                    with patch('app._get_task_dir_real', return_value=temp_dir):
                        with patch('app._get_current_cover_path', return_value=cover_file):
                            resp = client.get('/tasks/task-test-local/cover')
                            self.assertEqual(resp.status_code, 200)
                            self.assertEqual(resp.mimetype, 'image/jpeg')
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_get_task_cover_fallback_to_youtube(self):
        client = app.test_client()
        fake_task = {
            'id': 'task-test-yt',
            'cover_path_local': None,
            'youtube_url': 'https://www.youtube.com/watch?v=eeC6MYHsbpI'
        }
        with patch('app.load_config', return_value={'password_protection_enabled': False}):
            with patch('app.get_task', return_value=fake_task):
                with patch('app._get_task_dir_real', return_value='C:\\dummy\\path'):
                    with patch('app._get_current_cover_path', return_value=None):
                        resp = client.get('/tasks/task-test-yt/cover')
                        self.assertEqual(resp.status_code, 200)
                        self.assertIn(resp.mimetype, ['image/jpeg', 'image/svg+xml'])


if __name__ == '__main__':
    unittest.main()