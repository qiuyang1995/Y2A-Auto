#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import unittest
import tempfile
import sqlite3

# 保证能导入 modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from modules.youtube_monitor import normalize_search_keywords, YouTubeMonitor


class TestYouTubeMonitorKeywordsAndCover(unittest.TestCase):
    def test_normalize_search_keywords_empty(self):
        self.assertEqual(normalize_search_keywords(""), "")
        self.assertEqual(normalize_search_keywords(None), "")
        self.assertEqual(normalize_search_keywords("   "), "")

    def test_normalize_search_keywords_single_token(self):
        # 单词无空格
        self.assertEqual(normalize_search_keywords("직캠"), "직캠")
        # 单词有空格但无竖线（保持原样）
        self.assertEqual(normalize_search_keywords("IVE fancam"), "IVE fancam")

    def test_normalize_search_keywords_multi_phrases(self):
        # 典型的竖线多词短语
        raw = "아이브 직캠|IVE fancam|aespa fancam"
        expected = '"아이브 직캠"|"IVE fancam"|"aespa fancam"'
        self.assertEqual(normalize_search_keywords(raw), expected)

    def test_normalize_search_keywords_fullwidth_pipe(self):
        # 全角竖线
        raw = "아이브 직캠｜IVE fancam｜aespa fancam"
        expected = '"아이브 직캠"|"IVE fancam"|"aespa fancam"'
        self.assertEqual(normalize_search_keywords(raw), expected)

    def test_normalize_search_keywords_already_quoted(self):
        # 已经有引号的短语不重复加引号
        raw = '"아이브 직캠"|\'IVE fancam\'|aespa fancam|단일'
        expected = '"아이브 직캠"|\'IVE fancam\'|"aespa fancam"|단일'
        self.assertEqual(normalize_search_keywords(raw), expected)

    def test_database_thumbnail_url_migration_and_fallback(self):
        # 测试临时数据库中的字段迁移和 fallback
        temp_dir = tempfile.mkdtemp()
        test_db = os.path.join(temp_dir, 'test_monitor.db')
        try:
            monitor = YouTubeMonitor.__new__(YouTubeMonitor)
            monitor.db_path = test_db
            monitor.scheduler = None
            monitor.api_key = None
            monitor.youtube = None
            monitor.youtube_http = None
            monitor._api_proxy_enabled = False
            monitor._last_api_init_error = None
            monitor._init_database()

            # 验证 monitor_history 表中存在 thumbnail_url
            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(monitor_history)")
            cols = [row[1] for row in cursor.fetchall()]
            self.assertIn('thumbnail_url', cols)

            # 确保配置 ID=1 存在
            cursor.execute("SELECT id FROM monitor_configs WHERE id = 1")
            if not cursor.fetchone():
                cursor.execute("INSERT INTO monitor_configs (id, name) VALUES (1, '测试配置')")
                conn.commit()
            conn.close()

            # 保存一条包含 thumbnail_url 的记录
            video_info = {
                'id': 'abc123xyz',
                'video_type': 'video',
                'title': '测试视频标题',
                'channel_title': '测试频道',
                'view_count': 1234,
                'like_count': 56,
                'comment_count': 7,
                'duration': 'PT3M15S',
                'published_at': '2026-09-11T12:00:00Z',
                'thumbnail_url': 'https://i.ytimg.com/vi/abc123xyz/hqdefault.jpg'
            }
            monitor._save_video_history(video_info, config_id=1)

            # 获取历史记录并验证
            history = monitor.get_monitor_history(config_id=1)
            self.assertGreaterEqual(len(history), 1)
            saved = [r for r in history if r['video_id'] == 'abc123xyz'][0]
            self.assertEqual(saved['thumbnail_url'], 'https://i.ytimg.com/vi/abc123xyz/hqdefault.jpg')

            # 插入一条旧数据（thumbnail_url 为 NULL），验证 fallback
            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO monitor_history (config_id, video_id, video_title, channel_title, run_time, thumbnail_url)
                VALUES (1, 'old_video_999', '老视频', '老频道', CURRENT_TIMESTAMP, NULL)
            """)
            conn.commit()
            conn.close()

            history2 = monitor.get_monitor_history(config_id=1)
            old_record = [r for r in history2 if r['video_id'] == 'old_video_999'][0]
            self.assertEqual(old_record['thumbnail_url'], 'https://i.ytimg.com/vi/old_video_999/hqdefault.jpg')
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_cover_route_unauthorized_and_fallback(self):
        from unittest.mock import patch
        from app import app
        client = app.test_client()
        
        # 开启密码保护时，未登录访问应重定向到 login
        with patch('app.load_config', return_value={'password_protection_enabled': True}):
            resp = client.get('/youtube_monitor/cover/test12345')
            self.assertEqual(resp.status_code, 302)

        # 登录 session 或免密模式下访问非法 video_id 返回 404
        resp_invalid = client.get('/youtube_monitor/cover/invalid!id@')
        self.assertEqual(resp_invalid.status_code, 404)

        # 访问合法 ID，如果无本地缓存或网络失败返回 SVG 占位图
        resp_valid = client.get('/youtube_monitor/cover/valid12345')
        self.assertEqual(resp_valid.status_code, 200)
        self.assertIn(resp_valid.mimetype, ['image/jpeg', 'image/svg+xml'])


if __name__ == '__main__':
    unittest.main()
