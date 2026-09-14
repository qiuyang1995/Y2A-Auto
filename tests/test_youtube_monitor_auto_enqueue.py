import os
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import sqlite3

from modules.youtube_monitor import YouTubeMonitor


class TestYouTubeMonitorAutoEnqueue(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.temp_dir, 'test_monitor.db')
        self.monitor = YouTubeMonitor.__new__(YouTubeMonitor)
        self.monitor.db_path = self.test_db
        self.monitor.scheduler = MagicMock()
        self.monitor.api_key = None
        self.monitor.youtube = None
        self.monitor.youtube_http = None
        self.monitor._api_proxy_enabled = False
        self.monitor._last_api_init_error = None
        self.monitor._last_fetch_had_errors = False
        self.monitor._init_database()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_and_update_auto_enqueue_config(self):
        """测试获取与更新定时入队配置"""
        cfg = self.monitor.get_auto_enqueue_config()
        self.assertIsNotNone(cfg)
        self.assertFalse(cfg['enabled'])
        self.assertEqual(cfg['schedule_type'], 'daily_times')
        self.assertEqual(cfg['batch_count'], 2)

        # 更新配置
        update_data = {
            'enabled': True,
            'schedule_type': 'daily_times',
            'schedule_time_points': '13:00, 14:00, 15:00',
            'batch_count': 3,
            'order_by': 'view_count',
            'filter_config_id': 0
        }
        with patch.object(self.monitor, '_backup_auto_enqueue_config'):
            with patch.object(self.monitor, 'reload_auto_enqueue_schedule'):
                ok, msg = self.monitor.update_auto_enqueue_config(update_data)
                self.assertTrue(ok)

        new_cfg = self.monitor.get_auto_enqueue_config()
        self.assertTrue(new_cfg['enabled'])
        self.assertEqual(new_cfg['batch_count'], 3)
        self.assertEqual(new_cfg['schedule_time_points'], '13:00, 14:00, 15:00')

    def test_get_unadded_history_count(self):
        """测试获取未添加历史记录数量"""
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO monitor_history (config_id, video_id, video_title, added_to_tasks)
            VALUES (1, 'v1', 'Title 1', 0),
                   (1, 'v2', 'Title 2', 1),
                   (2, 'v3', 'Title 3', 0)
        """)
        conn.commit()
        conn.close()

        self.assertEqual(self.monitor.get_unadded_history_count(), 2)
        self.assertEqual(self.monitor.get_unadded_history_count(config_id=1), 1)
        self.assertEqual(self.monitor.get_unadded_history_count(config_id=2), 1)

    def test_execute_auto_enqueue_empty_pool(self):
        """测试待入队池为空时的处理"""
        # 手动执行空池
        ok, msg, count = self.monitor.execute_auto_enqueue(trigger_type='manual')
        self.assertTrue(ok)
        self.assertEqual(count, 0)
        self.assertIn('待入队池为空', msg)

    def test_execute_auto_enqueue_selection_and_order(self):
        """测试定时入队选片规则：高播放量优先排序并按数量限制入队"""
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO monitor_history (id, config_id, video_id, video_title, channel_title, view_count, added_to_tasks)
            VALUES (1, 1, 'vid_low', '低播放', 'Channel A', 100, 0),
                   (2, 1, 'vid_high', '高播放', 'Channel B', 50000, 0),
                   (3, 1, 'vid_mid', '中播放', 'Channel C', 10000, 0)
        """)
        conn.commit()
        conn.close()

        # 设置 batch_count = 2, order_by = 'view_count'
        with patch.object(self.monitor, '_backup_auto_enqueue_config'):
            with patch.object(self.monitor, 'reload_auto_enqueue_schedule'):
                self.monitor.update_auto_enqueue_config({
                    'enabled': True,
                    'schedule_type': 'daily_times',
                    'batch_count': 2,
                    'order_by': 'view_count'
                })

        mock_batch_add = MagicMock(return_value=(True, '添加成功', [2, 3]))
        with patch.object(self.monitor, 'batch_add_to_tasks', mock_batch_add):
            ok, msg, count = self.monitor.execute_auto_enqueue(trigger_type='scheduled')
            self.assertTrue(ok)
            self.assertEqual(count, 2)
            # 确认调用 batch_add_to_tasks 传进去的是最高播放量的前两个 ID (2, 3)
            mock_batch_add.assert_called_once_with([2, 3])

    def test_execute_auto_enqueue_disabled_scheduled_skip(self):
        """测试定时入队禁用时定时调度跳过，但手动触发可执行"""
        with patch.object(self.monitor, '_backup_auto_enqueue_config'):
            with patch.object(self.monitor, 'reload_auto_enqueue_schedule'):
                self.monitor.update_auto_enqueue_config({
                    'enabled': False,
                    'batch_count': 2
                })

        # 定时触发应跳过
        ok, msg, count = self.monitor.execute_auto_enqueue(trigger_type='scheduled')
        self.assertFalse(ok)
        self.assertIn('已禁用', msg)

        # 手动触发应继续执行（即便为空池也正常返回空结果）
        ok2, msg2, count2 = self.monitor.execute_auto_enqueue(trigger_type='manual')
        self.assertTrue(ok2)
        self.assertEqual(count2, 0)

    def test_run_monitor_no_truncation_when_max_reached(self):
        """测试监控抓取时即使启用了自动添加且达到单次上限，后续视频也会完整存入历史表"""
        # 准备一个监控配置
        config = {
            'id': 99,
            'name': '测试监控',
            'enabled': True,
            'auto_add_to_tasks': True,
            'rate_limit_requests': 1,  # 仅允许自动添加 1 个
            'channel_mode': 'latest',
            'channel_ids': ''
        }
        
        # 模拟抓取到 3 个视频
        fake_videos = [
            {'id': 'v1', 'title': '视频1', 'channel_title': 'C', 'view_count': 10, 'like_count': 1, 'comment_count': 0, 'duration': '1:00', 'published_at': '2026-09-01T00:00:00Z'},
            {'id': 'v2', 'title': '视频2', 'channel_title': 'C', 'view_count': 20, 'like_count': 2, 'comment_count': 0, 'duration': '2:00', 'published_at': '2026-09-02T00:00:00Z'},
            {'id': 'v3', 'title': '视频3', 'channel_title': 'C', 'view_count': 30, 'like_count': 3, 'comment_count': 0, 'duration': '3:00', 'published_at': '2026-09-03T00:00:00Z'}
        ]

        self.monitor.youtube = MagicMock()
        with patch.object(self.monitor, 'get_monitor_config', return_value=config):
            with patch.object(self.monitor, '_fetch_trending_videos', return_value=fake_videos):
                with patch.object(self.monitor, '_filter_videos', return_value=fake_videos):
                    with patch.object(self.monitor, '_add_video_to_tasks', return_value='task_123'):
                        with patch.object(self.monitor, '_update_last_run_time'):
                            ok, msg = self.monitor.run_monitor(99)
                            self.assertTrue(ok)

        # 检查数据库中 3 个视频是否全部被保存到 monitor_history
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("SELECT video_id, added_to_tasks FROM monitor_history WHERE config_id = 99 ORDER BY id")
        rows = cursor.fetchall()
        conn.close()

        # 验证共有 3 条记录，第 1 条被添加到任务队列，第 2、3 条未添加但成功存入库中
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], ('v1', 1))
        self.assertEqual(rows[1], ('v2', 0))
        self.assertEqual(rows[2], ('v3', 0))


if __name__ == '__main__':
    unittest.main()
