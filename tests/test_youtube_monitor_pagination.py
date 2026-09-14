import os
import unittest
import tempfile
import sqlite3
from unittest.mock import patch, MagicMock

from modules.youtube_monitor import YouTubeMonitor, _generate_iter_pages


class TestYouTubeMonitorPagination(unittest.TestCase):
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
        with patch.object(self.monitor, '_restore_configs_from_files'), patch.object(self.monitor, '_restore_auto_enqueue_config_from_file'):
            self.monitor._init_database()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_sample_history(self):
        """插入 25 条历史记录用于测试分页和状态筛选"""
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM monitor_history")
        cursor.execute("DELETE FROM monitor_configs")

        cursor.execute("""
            INSERT INTO monitor_configs (id, name, monitor_type, enabled)
            VALUES (1, 'Config 1', 'youtube_search', 1),
                   (2, 'Config 2', 'channel_monitor', 1)
        """)

        for i in range(1, 26):
            cfg_id = 1 if i <= 15 else 2
            added = 1 if (i % 3 == 0) else 0
            cursor.execute("""
                INSERT INTO monitor_history (
                    config_id, video_id, video_title, channel_title,
                    view_count, like_count, comment_count, duration,
                    published_at, run_time, added_to_tasks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cfg_id,
                f'vid_{i:03d}',
                f'Video Title {i}',
                f'Channel {cfg_id}',
                i * 1000,
                i * 10,
                i,
                'PT10M',
                '2026-09-01T00:00:00Z',
                f'2026-09-14 12:{i:02d}:00',
                added
            ))
        conn.commit()
        conn.close()

    def test_generate_iter_pages(self):
        """测试页码迭代器生成"""
        # 小于等于7页全部显示
        self.assertEqual(_generate_iter_pages(1, 5), [1, 2, 3, 4, 5])
        self.assertEqual(_generate_iter_pages(3, 7), [1, 2, 3, 4, 5, 6, 7])
        
        # 10页，当前在第 1 页
        pages_1 = _generate_iter_pages(1, 10)
        self.assertEqual(pages_1, [1, 2, 3, None, 9, 10])
        
        # 10页，当前在第 5 页：1,2 + 3,4,5,6,7 + None + 9,10
        pages_5 = _generate_iter_pages(5, 10)
        self.assertEqual(pages_5, [1, 2, 3, 4, 5, 6, 7, None, 9, 10])

        # 10页，当前在第 10 页
        pages_10 = _generate_iter_pages(10, 10)
        self.assertEqual(pages_10, [1, 2, None, 8, 9, 10])

    def test_get_monitor_history_stats(self):
        """测试监控历史统计数据获取"""
        empty_stats = self.monitor.get_monitor_history_stats()
        self.assertEqual(empty_stats['total_records'], 0)
        self.assertEqual(empty_stats['added_to_tasks'], 0)
        self.assertEqual(empty_stats['unadded_records'], 0)

        self._insert_sample_history()

        stats = self.monitor.get_monitor_history_stats()
        self.assertEqual(stats['total_records'], 25)
        self.assertEqual(stats['added_to_tasks'], 8)
        self.assertEqual(stats['unadded_records'], 17)
        self.assertGreater(stats['avg_views'], 0)
        self.assertGreater(stats['avg_likes'], 0)

        stats_cfg1 = self.monitor.get_monitor_history_stats(config_id=1)
        self.assertEqual(stats_cfg1['total_records'], 15)
        self.assertEqual(stats_cfg1['added_to_tasks'], 5)
        self.assertEqual(stats_cfg1['unadded_records'], 10)

        stats_cfg2 = self.monitor.get_monitor_history_stats(config_id=2)
        self.assertEqual(stats_cfg2['total_records'], 10)
        self.assertEqual(stats_cfg2['added_to_tasks'], 3)
        self.assertEqual(stats_cfg2['unadded_records'], 7)

    def test_get_monitor_history_paginated_all(self):
        """测试分页获取全部记录"""
        self._insert_sample_history()

        p1 = self.monitor.get_monitor_history_paginated(status='all', page=1, per_page=10)
        self.assertEqual(p1['total'], 25)
        self.assertEqual(p1['page'], 1)
        self.assertEqual(p1['per_page'], 10)
        self.assertEqual(p1['total_pages'], 3)
        self.assertEqual(len(p1['records']), 10)
        self.assertFalse(p1['has_prev'])
        self.assertTrue(p1['has_next'])
        self.assertIsNone(p1['prev_page'])
        self.assertEqual(p1['next_page'], 2)
        self.assertEqual(p1['status'], 'all')

        p2 = self.monitor.get_monitor_history_paginated(status='all', page=2, per_page=10)
        self.assertEqual(len(p2['records']), 10)
        self.assertTrue(p2['has_prev'])
        self.assertTrue(p2['has_next'])
        self.assertEqual(p2['prev_page'], 1)
        self.assertEqual(p2['next_page'], 3)

        p3 = self.monitor.get_monitor_history_paginated(status='all', page=3, per_page=10)
        self.assertEqual(len(p3['records']), 5)
        self.assertTrue(p3['has_prev'])
        self.assertFalse(p3['has_next'])
        self.assertIsNone(p3['next_page'])

    def test_get_monitor_history_paginated_status_filter(self):
        """测试按状态筛选 (未添加/已添加) 分页"""
        self._insert_sample_history()

        unadded = self.monitor.get_monitor_history_paginated(status='unadded', page=1, per_page=10)
        self.assertEqual(unadded['total'], 17)
        self.assertEqual(unadded['total_pages'], 2)
        self.assertEqual(len(unadded['records']), 10)
        for r in unadded['records']:
            self.assertEqual(r['added_to_tasks'], 0)

        unadded_p2 = self.monitor.get_monitor_history_paginated(status='unadded', page=2, per_page=10)
        self.assertEqual(len(unadded_p2['records']), 7)

        added = self.monitor.get_monitor_history_paginated(status='added', page=1, per_page=10)
        self.assertEqual(added['total'], 8)
        self.assertEqual(added['total_pages'], 1)
        self.assertEqual(len(added['records']), 8)
        for r in added['records']:
            self.assertEqual(r['added_to_tasks'], 1)

    def test_get_monitor_history_paginated_with_config_id(self):
        """测试指定 config_id 时的分页与状态筛选"""
        self._insert_sample_history()

        cfg1_all = self.monitor.get_monitor_history_paginated(config_id=1, status='all', page=1, per_page=10)
        self.assertEqual(cfg1_all['total'], 15)
        self.assertEqual(cfg1_all['total_pages'], 2)
        for r in cfg1_all['records']:
            self.assertEqual(r['config_id'], 1)

        cfg1_unadded = self.monitor.get_monitor_history_paginated(config_id=1, status='unadded', page=1, per_page=10)
        self.assertEqual(cfg1_unadded['total'], 10)
        self.assertEqual(len(cfg1_unadded['records']), 10)

        cfg1_added = self.monitor.get_monitor_history_paginated(config_id=1, status='added', page=1, per_page=10)
        self.assertEqual(cfg1_added['total'], 5)
        self.assertEqual(len(cfg1_added['records']), 5)

    def test_get_monitor_history_paginated_edge_cases(self):
        """测试页码与条数边界情况"""
        self._insert_sample_history()

        p_zero = self.monitor.get_monitor_history_paginated(page=0, per_page=10)
        self.assertEqual(p_zero['page'], 1)

        p_over = self.monitor.get_monitor_history_paginated(page=999, per_page=10)
        self.assertEqual(p_over['page'], 3)

        p_large = self.monitor.get_monitor_history_paginated(page=1, per_page=500)
        self.assertEqual(p_large['per_page'], 100)

        p_neg = self.monitor.get_monitor_history_paginated(page=1, per_page=-5)
        self.assertEqual(p_neg['per_page'], 1)


if __name__ == '__main__':
    unittest.main()


class TestYouTubeMonitorPaginationRoutes(unittest.TestCase):
    def test_routes_with_pagination_and_status(self):
        from app import app
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['logged_in'] = True

        # 测试监控中心主页带分页和状态
        resp = client.get('/youtube_monitor?status=unadded&page=1&per_page=10')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')
        self.assertIn('状态筛选', html)
        self.assertIn('未添加', html)
        self.assertIn('已添加', html)

        # 测试单配置历史页面
        resp_history = client.get('/youtube_monitor/history/1?status=all&page=1&per_page=10')
        if resp_history.status_code == 200:
            html_h = resp_history.data.decode('utf-8')
            self.assertIn('状态筛选', html_h)
            self.assertIn('总记录数', html_h)
            self.assertIn('未添加', html_h)
