import os
import unittest
import tempfile
import sqlite3
import json
from unittest.mock import patch, MagicMock

from app import format_duration, format_filesize
import modules.task_manager as tm
from modules.youtube_monitor import YouTubeMonitor


class TestDurationFilesizeDisplay(unittest.TestCase):
    def test_format_duration(self):
        # 常见秒数测试
        self.assertEqual(format_duration(0), '-')
        self.assertEqual(format_duration(None), '-')
        self.assertEqual(format_duration(''), '-')
        self.assertEqual(format_duration('invalid'), '-')
        self.assertEqual(format_duration(65), '01:05')
        self.assertEqual(format_duration(125.4), '02:05')
        self.assertEqual(format_duration(3665), '1:01:05')
        self.assertEqual(format_duration(36000), '10:00:00')

        # YouTube ISO 8601 格式测试
        self.assertEqual(format_duration('PT3M36S'), '03:36')
        self.assertEqual(format_duration('PT1H2M3S'), '1:02:03')
        self.assertEqual(format_duration('PT45S'), '00:45')

        # 已格式化或字符串秒数
        self.assertEqual(format_duration('125'), '02:05')
        self.assertEqual(format_duration('02:05'), '02:05')

    def test_format_filesize(self):
        self.assertEqual(format_filesize(0), '-')
        self.assertEqual(format_filesize(None), '-')
        self.assertEqual(format_filesize(''), '-')
        self.assertEqual(format_filesize('invalid'), '-')
        self.assertEqual(format_filesize(500), '500 B')
        self.assertEqual(format_filesize(1024), '1.00 KB')
        self.assertEqual(format_filesize(1048576), '1.00 MB')
        self.assertEqual(format_filesize(15728640), '15.0 MB')
        self.assertEqual(format_filesize(1073741824), '1.00 GB')
        self.assertEqual(format_filesize('15.5MB'), '15.5MB')

    def test_task_metadata_map_and_backfill(self):
        temp_dir = tempfile.mkdtemp()
        test_db = os.path.join(temp_dir, 'tasks.db')

        # 创建历史 tasks 表（无 video_duration 与 video_filesize 字段）
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                youtube_url TEXT,
                upload_target TEXT DEFAULT 'bilibili',
                status TEXT,
                video_title_original TEXT,
                video_title_translated TEXT,
                video_path_local TEXT,
                metadata_json_path_local TEXT
            )
        """)

        # 准备 metadata.json 与 local video 文件
        meta_file = os.path.join(temp_dir, 'metadata.json')
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump({'duration': 240, 'filesize': 10485760}, f)

        video_file = os.path.join(temp_dir, 'video.mp4')
        with open(video_file, 'wb') as f:
            f.write(b'x' * 2048)

        # 插入两条任务
        cursor.execute("""
            INSERT INTO tasks (id, youtube_url, status, metadata_json_path_local, video_path_local)
            VALUES (?, ?, ?, ?, ?)
        """, ('task_001', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'completed', meta_file, video_file))
        cursor.execute("""
            INSERT INTO tasks (id, youtube_url, status, metadata_json_path_local, video_path_local)
            VALUES (?, ?, ?, ?, ?)
        """, ('task_002', 'https://www.youtube.com/watch?v=abcdefghijk', 'downloaded', None, video_file))
        conn.commit()
        conn.close()

        def mock_conn():
            c = sqlite3.connect(test_db)
            c.row_factory = sqlite3.Row
            return c

        # 模拟切换 tm 的数据库
        with patch.object(tm, 'DB_PATH', test_db), patch.object(tm, 'get_db_connection', side_effect=mock_conn):
            # 执行 init_db() 自动迁移回填
            tm.init_db()

            # 验证字段被成功添加与回填
            task1 = tm.get_task('task_001')
            self.assertIsNotNone(task1)
            self.assertEqual(task1.get('video_duration'), 240)
            # task1 优先使用了 metadata.json 中的 10485760 (10MB)
            self.assertEqual(task1.get('video_filesize'), 10485760)

            task2 = tm.get_task('task_002')
            self.assertIsNotNone(task2)
            # task2 没有 metadata.json，从 video.mp4 获取了真实大小 2048 字节
            self.assertEqual(task2.get('video_filesize'), 2048)

            # 测试 get_tasks_video_metadata_map
            meta_map = tm.get_tasks_video_metadata_map()
            self.assertIn('dQw4w9WgXcQ', meta_map)
            self.assertEqual(meta_map['dQw4w9WgXcQ']['duration'], 240)
            self.assertEqual(meta_map['dQw4w9WgXcQ']['filesize'], 10485760)

            self.assertIn('abcdefghijk', meta_map)
            self.assertEqual(meta_map['abcdefghijk']['filesize'], 2048)

    def test_youtube_monitor_metadata_enrichment(self):
        temp_dir = tempfile.mkdtemp()
        monitor_db = os.path.join(temp_dir, 'monitor.db')

        # 初始化 monitor 对象
        monitor = YouTubeMonitor.__new__(YouTubeMonitor)
        monitor.db_path = monitor_db
        monitor.scheduler = MagicMock()
        monitor.api_key = None
        monitor.youtube = None
        monitor.youtube_http = None
        monitor._api_proxy_enabled = False
        monitor._last_api_init_error = None
        monitor._last_fetch_had_errors = False
        with patch.object(monitor, '_restore_configs_from_files'), patch.object(monitor, '_restore_auto_enqueue_config_from_file'):
            monitor._init_database()

        # 插入配置和测试监控历史记录
        with sqlite3.connect(monitor_db) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO monitor_configs (id, name, monitor_type, enabled)
                VALUES (1, 'Test Config', 'youtube_search', 1)
            """)
            cursor.execute("""
                INSERT INTO monitor_history (
                    config_id, video_id, video_title, channel_title,
                    view_count, like_count, comment_count, duration,
                    run_time, added_to_tasks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (1, 'dQw4w9WgXcQ', 'Rick Astley - Never Gonna Give You Up', 'RickAstleyVEVO',
                  1000, 100, 10, 'PT3M33S', '2026-09-15 10:00:00', 1))
            conn.commit()

        # Mock tm.get_tasks_video_metadata_map
        mock_meta_map = {
            'dQw4w9WgXcQ': {
                'task_id': 'task_001',
                'duration': 213,
                'filesize': 25000000
            }
        }

        with patch('modules.task_manager.get_tasks_video_metadata_map', return_value=mock_meta_map):
            pagination = monitor.get_monitor_history_paginated(config_id=1)
            self.assertEqual(len(pagination['records']), 1)
            record = pagination['records'][0]
            self.assertEqual(record['video_id'], 'dQw4w9WgXcQ')
            self.assertEqual(record['video_filesize'], 25000000)
            self.assertEqual(record['duration'], 'PT3M33S')

            history_list = monitor.get_monitor_history(config_id=1)
            self.assertEqual(len(history_list), 1)
            self.assertEqual(history_list[0]['video_filesize'], 25000000)


if __name__ == '__main__':
    unittest.main()
