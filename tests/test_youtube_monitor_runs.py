import os
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import sqlite3
from datetime import datetime

from modules.youtube_monitor import YouTubeMonitor
import app as flask_app


class TestYouTubeMonitorRuns(unittest.TestCase):
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

    def test_monitor_runs_table_and_flow(self):
        """测试监控执行批次的创建与结束回填"""
        # 1. 创建批次
        run_id = self.monitor._start_monitor_run(config_id=1, trigger_type='manual')
        self.assertIsNotNone(run_id)
        self.assertGreater(run_id, 0)

        # 2. 验证初始状态
        res = self.monitor.get_monitor_runs_paginated(config_id=1, status='all', page=1, per_page=10)
        self.assertEqual(res['total'], 1)
        self.assertEqual(res['runs'][0]['status'], 'running')
        self.assertEqual(res['runs'][0]['trigger_type'], 'manual')

        # 3. 关联保存一条抓取到的历史视频
        video_data = {
            'id': 'vid_test_123',
            'title': 'Test Video 1',
            'channel_title': 'Test Channel',
            'view_count': 1000,
            'like_count': 100,
            'comment_count': 10,
            'published_at': '2026-09-14T12:00:00Z',
            'url': 'https://youtube.com/watch?v=vid_test_123',
            'thumbnail': 'https://img.youtube.com/vi/vid_test_123/default.jpg'
        }
        self.monitor._save_video_history(video_data, config_id=1, auto_add_to_tasks=False, run_id=run_id)

        # 4. 正常结束批次
        self.monitor._finalize_monitor_run(
            run_id=run_id,
            status='success',
            fetched_count=10,
            filtered_count=4,
            new_count=1,
            added_count=0,
            error_message=None
        )

        # 5. 校验详情与关联视频
        run_details = self.monitor.get_monitor_run_details(run_id)
        self.assertIsNotNone(run_details)
        self.assertEqual(run_details['run']['status'], 'success')
        self.assertEqual(run_details['run']['fetched_count'], 10)
        self.assertEqual(run_details['run']['filtered_count'], 4)
        self.assertEqual(run_details['run']['new_count'], 1)
        self.assertEqual(len(run_details['videos']), 1)
        self.assertEqual(run_details['videos'][0]['video_id'], 'vid_test_123')

    def test_monitor_runs_failure_flow(self):
        """测试监控执行批次发生异常时的记录与回填"""
        run_id = self.monitor._start_monitor_run(config_id=2, trigger_type='scheduler')
        self.monitor._finalize_monitor_run(
            run_id=run_id,
            status='failed',
            fetched_count=0,
            filtered_count=0,
            new_count=0,
            added_count=0,
            error_message='API Quota Exceeded'
        )

        run_details = self.monitor.get_monitor_run_details(run_id)
        self.assertIsNotNone(run_details)
        self.assertEqual(run_details['run']['status'], 'failed')
        self.assertEqual(run_details['run']['trigger_type'], 'scheduler')
        self.assertEqual(run_details['run']['error_message'], 'API Quota Exceeded')

        # 测试筛选 status='failed'
        res = self.monitor.get_monitor_runs_paginated(config_id=2, status='failed', page=1, per_page=10)
        self.assertEqual(res['total'], 1)

        # 测试筛选 status='has_new' (应该为0)
        res_new = self.monitor.get_monitor_runs_paginated(config_id=2, status='has_new', page=1, per_page=10)
        self.assertEqual(res_new['total'], 0)

    def test_monitor_kpi_stats(self):
        """测试KPI统计指标计算"""
        # 批次1: 成功，有1个新视频
        r1 = self.monitor._start_monitor_run(config_id=1, trigger_type='manual')
        self.monitor._finalize_monitor_run(r1, status='success', fetched_count=5, filtered_count=2, new_count=1, added_count=1)
        # 批次2: 失败
        r2 = self.monitor._start_monitor_run(config_id=1, trigger_type='scheduler')
        self.monitor._finalize_monitor_run(r2, status='failed', fetched_count=0, filtered_count=0, new_count=0, added_count=0, error_message='Error')

        # 视频历史
        v1 = {
            'id': 'v_kpi_1',
            'title': 'KPI 1',
            'channel_title': 'C',
            'view_count': 100,
            'like_count': 10,
            'comment_count': 1,
            'published_at': '2026-09-14T10:00:00Z',
            'url': 'http://youtube.com/watch?v=v_kpi_1',
            'thumbnail': 'http://thumb'
        }
        self.monitor._save_video_history(v1, config_id=1, auto_add_to_tasks=False, run_id=r1)
        with sqlite3.connect(self.test_db) as conn:
            conn.cursor().execute("UPDATE monitor_history SET added_to_tasks = 1 WHERE video_id = 'v_kpi_1'")
            conn.commit()

        stats = self.monitor.get_monitor_kpi_stats(config_id=1)
        self.assertEqual(stats['total_runs'], 2)
        self.assertEqual(stats['failed_runs'], 1)
        self.assertEqual(stats['added_videos'], 1)
        self.assertEqual(stats['unadded_videos'], 0)
        self.assertEqual(stats['total_videos'], 1)
        self.assertEqual(stats['success_rate'], 50.0)

    def test_flask_routes_monitor_runs(self):
        """测试Flask路由 /youtube_monitor/records 以及单次执行详情 API"""
        flask_app.app.config['TESTING'] = True
        client = flask_app.app.test_client()
        with client.session_transaction() as sess:
            sess['logged_in'] = True

        with patch('app.youtube_monitor', self.monitor):
            # 1. 访问监控记录中心页面 (默认 runs tab)
            resp = client.get('/youtube_monitor/records')
            self.assertEqual(resp.status_code, 200)
            self.assertIn(b'tab=runs', resp.data)
            self.assertIn(b'tab=videos', resp.data)

            # 2. 访问 videos tab
            resp_videos = client.get('/youtube_monitor/history?tab=videos')
            self.assertEqual(resp_videos.status_code, 200)

            # 3. 创建一个 run 并请求 details API
            r_id = self.monitor._start_monitor_run(config_id=1, trigger_type='manual')
            self.monitor._finalize_monitor_run(r_id, status='success', fetched_count=8, filtered_count=3, new_count=2, added_count=0)

            resp_api = client.get(f'/youtube_monitor/run/{r_id}/details')
            self.assertEqual(resp_api.status_code, 200)
            json_data = resp_api.get_json()
            self.assertTrue(json_data['success'])
            self.assertEqual(json_data['data']['run']['id'], r_id)
            self.assertEqual(json_data['data']['run']['new_count'], 2)


if __name__ == '__main__':
    unittest.main()
