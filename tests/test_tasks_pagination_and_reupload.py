import os
import unittest
import tempfile
import sqlite3
import json
from unittest.mock import patch, MagicMock

import app as flask_app
from modules.task_manager import (
    get_task_status_counts,
    get_tasks_paginated,
    TASK_STATES,
    PROCESSING_STATES,
)


class TestTasksPaginationAndReupload(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.temp_dir, 'tasks.db')

        # 初始化 tasks 表
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                youtube_url TEXT,
                upload_target TEXT DEFAULT 'bilibili',
                status TEXT,
                created_at TEXT,
                updated_at TEXT,
                video_title_original TEXT,
                video_title_translated TEXT,
                description_original TEXT,
                description_translated TEXT,
                tags_generated TEXT,
                recommended_partition_id TEXT,
                selected_partition_id TEXT,
                recommended_partition_id_acfun TEXT,
                selected_partition_id_acfun TEXT,
                recommended_partition_id_bilibili TEXT,
                selected_partition_id_bilibili TEXT,
                cover_path_local TEXT,
                video_path_local TEXT,
                subtitle_path_original TEXT,
                subtitle_path_translated TEXT,
                subtitle_language_detected TEXT,
                subtitle_qc_failed INTEGER DEFAULT 0,
                subtitle_qc_reason TEXT,
                subtitle_qc_score REAL,
                subtitle_qc_checked_at TEXT,
                metadata_json_path_local TEXT,
                moderation_result TEXT,
                error_message TEXT,
                pipeline_checkpoint TEXT,
                upload_progress TEXT,
                acfun_upload_response TEXT,
                bilibili_upload_response TEXT,
                asr_warning_message TEXT,
                subtitle_warning_message TEXT,
                auto_pipeline INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()

        self.db_patcher = patch('modules.task_manager.get_db_path', return_value=self.test_db)
        self.db_patcher.start()

        def _get_test_conn():
            conn = sqlite3.connect(self.test_db)
            conn.row_factory = sqlite3.Row
            return conn

        self.conn_patcher = patch('modules.task_manager.get_db_connection', side_effect=_get_test_conn)
        self.conn_patcher.start()

    def tearDown(self):
        self.conn_patcher.stop()
        self.db_patcher.stop()
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_sample_tasks(self):
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()

        tasks_data = [
            # 2 pending
            ('t-p1', 'pending', '2026-09-15 01:00:00', '标题P1'),
            ('t-p2', 'pending', '2026-09-15 01:01:00', '标题P2'),
            # 2 processing: downloading, encoding_video
            ('t-pr1', 'downloading', '2026-09-15 01:02:00', '标题PR1'),
            ('t-pr2', 'encoding_video', '2026-09-15 01:03:00', '标题PR2'),
            # 1 awaiting_manual_review
            ('t-ar1', 'awaiting_manual_review', '2026-09-15 01:04:00', '标题AR1'),
            # 1 ready_for_upload
            ('t-rf1', 'ready_for_upload', '2026-09-15 01:05:00', '标题RF1'),
            # 3 completed
            ('t-c1', 'completed', '2026-09-15 01:06:00', '标题C1'),
            ('t-c2', 'completed', '2026-09-15 01:07:00', '标题C2'),
            ('t-c3', 'completed', '2026-09-15 01:08:00', '标题C3'),
            # 2 failed
            ('t-f1', 'failed', '2026-09-15 01:09:00', '标题F1'),
            ('t-f2', 'failed', '2026-09-15 01:10:00', '标题F2'),
        ]

        for tid, status, created_at, title in tasks_data:
            bili_resp = json.dumps({'bvid': f'BV_{tid}'}) if status == 'completed' else None
            cursor.execute("""
                INSERT INTO tasks (id, status, created_at, updated_at, video_title_translated, bilibili_upload_response, upload_target, selected_partition_id_bilibili)
                VALUES (?, ?, ?, ?, ?, ?, 'bilibili', '17')
            """, (tid, status, created_at, created_at, title, bili_resp))

        conn.commit()
        conn.close()

    def test_get_task_status_counts(self):
        self._insert_sample_tasks()
        counts = get_task_status_counts()

        self.assertEqual(counts['all'], 11)
        self.assertEqual(counts['pending'], 2)
        self.assertEqual(counts['processing'], 2)  # downloading + encoding_video
        self.assertEqual(counts['awaiting_manual_review'], 1)
        self.assertEqual(counts['ready_for_upload'], 1)
        self.assertEqual(counts['completed'], 3)
        self.assertEqual(counts['failed'], 2)

    def test_get_tasks_paginated_all(self):
        self._insert_sample_tasks()
        res = get_tasks_paginated(page=1, per_page=5, status='all')
        self.assertEqual(res['total'], 11)
        self.assertEqual(len(res['tasks']), 5)
        self.assertEqual(res['total_pages'], 3)
        self.assertTrue(res['has_next'])
        self.assertFalse(res['has_prev'])

        # 第 2 页
        res2 = get_tasks_paginated(page=2, per_page=5, status='all')
        self.assertEqual(len(res2['tasks']), 5)
        self.assertTrue(res2['has_next'])
        self.assertTrue(res2['has_prev'])

        # 第 3 页
        res3 = get_tasks_paginated(page=3, per_page=5, status='all')
        self.assertEqual(len(res3['tasks']), 1)
        self.assertFalse(res3['has_next'])
        self.assertTrue(res3['has_prev'])

    def test_get_tasks_paginated_by_status(self):
        self._insert_sample_tasks()

        # 筛选 completed
        res_comp = get_tasks_paginated(page=1, per_page=10, status='completed')
        self.assertEqual(res_comp['total'], 3)
        self.assertEqual(len(res_comp['tasks']), 3)
        for t in res_comp['tasks']:
            self.assertEqual(t['status'], 'completed')

        # 筛选 processing 复合状态
        res_proc = get_tasks_paginated(page=1, per_page=10, status='processing')
        self.assertEqual(res_proc['total'], 2)
        self.assertEqual(len(res_proc['tasks']), 2)
        for t in res_proc['tasks']:
            self.assertIn(t['status'], PROCESSING_STATES)

        # 筛选 failed
        res_fail = get_tasks_paginated(page=1, per_page=10, status='failed')
        self.assertEqual(res_fail['total'], 2)

    @patch('app._start_background_force_upload')
    def test_reupload_task_route_success(self, mock_start_upload):
        self._insert_sample_tasks()

        flask_app.app.config['TESTING'] = True
        flask_app.app.config['LOGIN_DISABLED'] = True

        with flask_app.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['logged_in'] = True

            # 重新上传已完成任务 t-c1
            response = client.post('/tasks/t-c1/reupload', headers={'X-Requested-With': 'XMLHttpRequest'})
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertTrue(data['success'])
            self.assertIn('已启动重新上传', data['message'])

            # 验证后台触发了上传
            self.assertTrue(mock_start_upload.called)

            # 验证数据库中旧的 bilibili_upload_response 与 error_message 已清空，状态重置为 ready_for_upload
            conn = sqlite3.connect(self.test_db)
            cursor = conn.cursor()
            cursor.execute("SELECT status, bilibili_upload_response, error_message FROM tasks WHERE id = 't-c1'")
            row = cursor.fetchone()
            conn.close()

            self.assertEqual(row[0], TASK_STATES['READY_FOR_UPLOAD'])
            self.assertIsNone(row[1])
            self.assertIsNone(row[2])

    def test_reupload_task_route_rejects_processing(self):
        self._insert_sample_tasks()

        flask_app.app.config['TESTING'] = True
        flask_app.app.config['LOGIN_DISABLED'] = True

        with flask_app.app.test_client() as client:
            with client.session_transaction() as sess:
                sess['logged_in'] = True

            # 尝试重新上传正在处理中的任务 t-pr1
            response = client.post('/tasks/t-pr1/reupload', headers={'X-Requested-With': 'XMLHttpRequest'})
            self.assertEqual(response.status_code, 400)
            data = json.loads(response.data)
            self.assertFalse(data['success'])
            self.assertIn('正在处理中', data['message'])


if __name__ == '__main__':
    unittest.main()
