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

    def test_fancam_whitelist_filtering(self):
        """测试直拍白名单筛选机制：拦截团综口语切片，放行真直拍"""
        monitor = YouTubeMonitor.__new__(YouTubeMonitor)
        config = {
            'name': '韩国女团饭拍直拍（全站发现）',
            'keywords': '"트리플에스 직캠"|"tripleS fancam"',
            'exclude_keywords': 'Official MV,Teaser',
            'exclude_channel_ids': '',
            'channel_ids': '',
            'min_view_count': 0,
            'min_like_count': 0,
            'min_comment_count': 0,
            'min_duration': 0,
            'max_duration': 0,
            'video_types': 'video,short,live'
        }

        # 真实的真直拍案例
        valid_video = {
            'id': 'v1',
            'title': '260905 트리플에스 나경 - Girls Never Die 4K 직캠 @천안 K-컬쳐 박람회 (tripleS KIMNAGYOUNG FANCAM)',
            'channel_id': 'c1',
            'view_count': 1000,
            'like_count': 100,
            'comment_count': 10,
            'duration': 'PT3M',
            'video_type': 'video'
        }
        self.assertTrue(monitor._meets_criteria(valid_video, config))

        # 真实的团综切片案例（无 직캠 / fancam）
        badge_war_video = {
            'id': 'v2',
            'title': '요원 vs 스파이 마피아 게임의 우승자가 밝혀진다 #tripleS #트리플에스 #BadgeWar4 #배지전쟁4',
            'channel_id': 'c2',
            'view_count': 1000,
            'like_count': 100,
            'comment_count': 10,
            'duration': 'PT1M',
            'video_type': 'short'
        }
        self.assertFalse(monitor._meets_criteria(badge_war_video, config))

        # 非直拍监控配置，不受直拍白名单影响
        normal_config = {
            'name': '普通音乐节目监控',
            'keywords': 'KPOP Music',
            'exclude_keywords': '',
            'exclude_channel_ids': '',
            'channel_ids': '',
            'min_view_count': 0,
            'min_like_count': 0,
            'min_comment_count': 0,
            'min_duration': 0,
            'max_duration': 0,
            'video_types': 'video,short,live'
        }
        self.assertTrue(monitor._meets_criteria(badge_war_video, normal_config))

    def test_clear_monitor_history_cascade_reset(self):
        """测试清空监控历史时级联重置配置运行状态与偏移量"""
        temp_dir = tempfile.mkdtemp()
        test_db = os.path.join(temp_dir, 'test_cascade.db')
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

            # 插入或替换带状态的配置与历史记录
            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO monitor_configs (id, name, last_run_time, historical_offset, historical_progress_date)
                VALUES (1, '测试级联', '2026-09-11 12:00:00', 50, '2026-09-01')
            """)
            cursor.execute("""
                INSERT INTO monitor_history (config_id, video_id, video_title, channel_title)
                VALUES (1, 'test_vid_1', '测试视频1', '测试频道')
            """)
            conn.commit()
            conn.close()

            # 执行清理单个配置历史
            success, msg = monitor.clear_monitor_history(config_id=1)
            self.assertTrue(success)

            # 验证历史已清空且配置状态被重置
            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM monitor_history WHERE config_id = 1")
            self.assertEqual(cursor.fetchone()[0], 0)

            cursor.execute("SELECT last_run_time, historical_offset, historical_progress_date FROM monitor_configs WHERE id = 1")
            row = cursor.fetchone()
            self.assertIsNone(row[0])
            self.assertEqual(row[1], 0)
            self.assertEqual(row[2], '')
            conn.close()
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_add_video_to_tasks_manually(self):
        """测试手动添加视频到任务队列功能"""
        from unittest.mock import patch
        temp_dir = tempfile.mkdtemp()
        test_db = os.path.join(temp_dir, 'test_manual_add.db')
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

            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO monitor_history (config_id, video_id, video_title, channel_title, added_to_tasks)
                VALUES (1, 'vid_manual_1', '直拍测试视频', '频道A', 0)
            """)
            conn.commit()
            conn.close()

            # Mock add_task 返回假任务ID 'task-uuid-12345'
            with patch('modules.youtube_monitor.add_task', return_value='task-uuid-12345'):
                success, message = monitor.add_video_to_tasks_manually('vid_manual_1', 1)
                self.assertTrue(success)
                self.assertIn('task-uuid-12345', message)

            # 验证数据库中 added_to_tasks 已被置为 1
            conn = sqlite3.connect(test_db)
            cursor = conn.cursor()
            cursor.execute("SELECT added_to_tasks FROM monitor_history WHERE video_id = 'vid_manual_1' AND config_id = 1")
            self.assertEqual(cursor.fetchone()[0], 1)
            conn.close()

            # 再次添加同一视频，应被拦截提示已添加
            success_repeat, message_repeat = monitor.add_video_to_tasks_manually('vid_manual_1', 1)
            self.assertFalse(success_repeat)
            self.assertIn('已经添加到任务队列', message_repeat)
        finally:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
