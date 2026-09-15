import os
import unittest
import tempfile
import sqlite3
from unittest.mock import patch, MagicMock

from modules.youtube_monitor import YouTubeMonitor


class TestYouTubeMonitorBlacklist(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.temp_dir, 'monitor.db')

        self.monitor = YouTubeMonitor.__new__(YouTubeMonitor)
        self.monitor.db_path = self.test_db
        self.monitor.scheduler = MagicMock()
        self.monitor.api_key = None
        self.monitor.youtube = None
        self.monitor.youtube_http = None
        self.monitor._api_proxy_enabled = False
        self.monitor._last_api_init_error = None
        self.monitor._last_fetch_had_errors = False
        with patch.object(self.monitor, '_restore_configs_from_files'), patch.object(self.monitor, '_restore_auto_enqueue_config_from_file'):
            self.monitor._init_database()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_delete_single_record_persists_to_blacklist(self):
        """测试单条删除记录时，从历史表删除并存入黑名单表"""
        with sqlite3.connect(self.test_db) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO monitor_history (
                    config_id, video_id, video_title, channel_title,
                    view_count, like_count, comment_count, duration,
                    run_time, added_to_tasks
                ) VALUES (1, 'vid_del_001', '要删除的视频 1', '测试频道', 100, 10, 5, '03:00', '2026-09-15 10:00:00', 0)
            """)
            record_id = cursor.lastrowid
            conn.commit()

        # 执行删除
        success, msg = self.monitor.delete_monitor_history_records([record_id])
        self.assertTrue(success)

        # 验证 monitor_history 中已无该记录
        with sqlite3.connect(self.test_db) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM monitor_history WHERE id = ?", (record_id,))
            self.assertIsNone(cursor.fetchone())

            # 验证 monitor_video_blacklist 中存在该视频
            cursor.execute("SELECT config_id, video_id, video_title, reason FROM monitor_video_blacklist WHERE video_id = ?", ('vid_del_001',))
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 1)
            self.assertEqual(row[1], 'vid_del_001')
            self.assertEqual(row[2], '要删除的视频 1')
            self.assertEqual(row[3], 'deleted')

        # 验证黑名单判定函数
        self.assertTrue(self.monitor.is_video_blacklisted('vid_del_001', 1))
        self.assertFalse(self.monitor.is_video_blacklisted('vid_other_999', 1))

        # 验证 _is_video_processed 返回 True（即使 monitor_history 已删除）
        self.assertTrue(self.monitor._is_video_processed('vid_del_001', 1))
        self.assertFalse(self.monitor._is_video_processed('vid_other_999', 1))

        # 验证 _meets_criteria 会直接拦截黑名单视频
        config = {'id': 1, 'video_types': 'video'}
        self.assertFalse(self.monitor._meets_criteria({'id': 'vid_del_001', 'duration': 'PT3M', 'view_count': 1000, 'like_count': 100, 'comment_count': 10}, config))
        self.assertTrue(self.monitor._meets_criteria({'id': 'vid_other_999', 'duration': 'PT3M', 'view_count': 1000, 'like_count': 100, 'comment_count': 10}, config))

    def test_batch_delete_records_persists_to_blacklist(self):
        """测试批量删除多条记录时，全部写入黑名单"""
        with sqlite3.connect(self.test_db) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO monitor_history (config_id, video_id, video_title, added_to_tasks)
                VALUES (1, 'batch_v_1', '批量删除 1', 0),
                       (1, 'batch_v_2', '批量删除 2', 0),
                       (2, 'batch_v_3', '批量删除 3', 0)
            """)
            conn.commit()
            cursor.execute("SELECT id FROM monitor_history WHERE video_id IN ('batch_v_1', 'batch_v_2')")
            ids_to_del = [r[0] for r in cursor.fetchall()]

        # 批量删除
        success, msg = self.monitor.delete_monitor_history_records(ids_to_del)
        self.assertTrue(success)

        with sqlite3.connect(self.test_db) as conn:
            cursor = conn.cursor()
            # 确认 batch_v_1 与 batch_v_2 被删，batch_v_3 仍在
            cursor.execute("SELECT video_id FROM monitor_history")
            remaining = [r[0] for r in cursor.fetchall()]
            self.assertNotIn('batch_v_1', remaining)
            self.assertNotIn('batch_v_2', remaining)
            self.assertIn('batch_v_3', remaining)

            # 确认黑名单包含 batch_v_1 与 batch_v_2
            cursor.execute("SELECT video_id FROM monitor_video_blacklist")
            blacklisted = [r[0] for r in cursor.fetchall()]
            self.assertIn('batch_v_1', blacklisted)
            self.assertIn('batch_v_2', blacklisted)
            self.assertNotIn('batch_v_3', blacklisted)

    def test_blacklisted_videos_never_reenter_monitor(self):
        """测试黑名单中的视频在后续监控流程中被彻底过滤，不会重新入库"""
        # 向黑名单直接插入一个已删除视频
        with sqlite3.connect(self.test_db) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO monitor_video_blacklist (config_id, video_id, video_title, reason)
                VALUES (1, 'never_return_1', '已删除不想再看的视频', 'deleted')
            """)
            conn.commit()

        # 模拟待处理的一组新视频（包含被黑名单拦截的视频与正常视频）
        candidate_videos = [
            {
                'id': 'never_return_1',
                'snippet': {'title': '已删除不想再看的视频', 'channelTitle': '频道A', 'publishedAt': '2026-09-15T10:00:00Z'},
                'contentDetails': {'duration': 'PT3M00S'},
                'statistics': {'viewCount': 5000, 'likeCount': 200, 'commentCount': 50}
            },
            {
                'id': 'good_new_vid_2',
                'snippet': {'title': '正常新视频', 'channelTitle': '频道B', 'publishedAt': '2026-09-15T10:00:00Z'},
                'contentDetails': {'duration': 'PT3M00S'},
                'statistics': {'viewCount': 5000, 'likeCount': 200, 'commentCount': 50}
            }
        ]

        config = {
            'id': 1,
            'name': '测试配置',
            'video_types': 'video',
            'max_results': 10
        }

        # 验证 _filter_videos 过滤
        filtered = self.monitor._filter_videos(candidate_videos, config)
        filtered_ids = [v['id'] for v in filtered]
        self.assertNotIn('never_return_1', filtered_ids)
        self.assertIn('good_new_vid_2', filtered_ids)

        # 验证即便绕过 _filter_videos，在 _is_video_processed 中依然返回 True 并被跳过
        self.assertTrue(self.monitor._is_video_processed('never_return_1', 1))


if __name__ == '__main__':
    unittest.main()
