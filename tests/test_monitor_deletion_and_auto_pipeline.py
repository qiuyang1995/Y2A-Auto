import os
import unittest
from unittest.mock import patch, MagicMock
import tempfile
import sqlite3

from modules.youtube_monitor import YouTubeMonitor
from modules.task_manager import add_task, get_task, TaskProcessor, TASK_STATES, delete_task


class TestMonitorDeletionAndAutoPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db = os.path.join(self.temp_dir, 'test_monitor.db')
        self.monitor = YouTubeMonitor.__new__(YouTubeMonitor)
        self.monitor.db_path = self.test_db
        self.monitor.scheduler = None
        self.monitor.api_key = None
        self.monitor.youtube = None
        self.monitor.youtube_http = None
        self.monitor._api_proxy_enabled = False
        self.monitor._last_api_init_error = None
        self.monitor._init_database()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_delete_monitor_history_single_and_batch(self):
        """测试单条与批量删除监控历史记录"""
        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO monitor_history (id, config_id, video_id, video_title, channel_title)
            VALUES (101, 1, 'vid_1', '标题1', '频道1'),
                   (102, 1, 'vid_2', '标题2', '频道2'),
                   (103, 1, 'vid_3', '标题3', '频道3')
        """)
        conn.commit()
        conn.close()

        # 1. 单条删除 101
        ok, msg = self.monitor.delete_monitor_history_records([101])
        self.assertTrue(ok)
        self.assertIn('成功删除 1 条记录', msg)

        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM monitor_history ORDER BY id")
        rows = [r[0] for r in cursor.fetchall()]
        conn.close()
        self.assertEqual(rows, [102, 103])

        # 2. 批量删除 102 和 103
        ok, msg = self.monitor.delete_monitor_history_records(['102', '103'])
        self.assertTrue(ok)
        self.assertIn('成功删除 2 条记录', msg)

        conn = sqlite3.connect(self.test_db)
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM monitor_history")
        count = cursor.fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

        # 3. 空参数测试
        ok, msg = self.monitor.delete_monitor_history_records([])
        self.assertFalse(ok)

    def test_add_task_auto_pipeline(self):
        """测试添加任务时 auto_pipeline 与 upload_target 正确写入"""
        task_id = add_task('https://www.youtube.com/watch?v=mock_auto_vid', upload_target='bilibili', auto_pipeline=True)
        self.assertIsNotNone(task_id)

        task = get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.get('upload_target'), 'bilibili')
        self.assertEqual(task.get('auto_pipeline'), 1)
        self.assertEqual(task.get('status'), TASK_STATES['PENDING'])

        # 清理测试任务
        delete_task(task_id)

    @patch('modules.task_manager.TaskProcessor._download_video_file')
    @patch('modules.task_manager.TaskProcessor._fetch_video_info')
    @patch('modules.task_manager.TaskProcessor._upload_to_bilibili')
    @patch('modules.task_edit_ai.generate_edit_page_metadata')
    def test_process_monitor_auto_task_pipeline(self, mock_ai_generate, mock_upload, mock_fetch, mock_download):
        """测试监控自动任务执行流水线：下载 -> AI生成 -> 上传B站"""
        task_id = add_task('https://www.youtube.com/watch?v=mock_pipeline_vid', upload_target='bilibili', auto_pipeline=True)

        mock_ai_generate.return_value = {
            'success': True,
            'title': '【4K直拍】AI生成直拍标题',
            'description': 'AI生成直拍简介内容',
            'tags': ['女团', '直拍', '4K'],
            'partitions': {
                'bilibili': {'id': '21', 'source': 'ai_recommend'}
            }
        }

        processor = TaskProcessor({'FIXED_PARTITION_ID_BILIBILI': '21'})
        logger_mock = MagicMock()

        task = get_task(task_id)
        processor._process_monitor_auto_task(task_id, task, logger_mock)

        # 验证步骤调用
        self.assertTrue(mock_download.called, "应调用 _download_video_file")
        self.assertTrue(mock_ai_generate.called, "应调用 generate_edit_page_metadata")
        self.assertTrue(mock_upload.called, "应调用 _upload_to_bilibili")

        # 验证 AI 生成结果已保存到数据库
        updated_task = get_task(task_id)
        self.assertEqual(updated_task.get('video_title_translated'), '【4K直拍】AI生成直拍标题')
        self.assertEqual(updated_task.get('description_translated'), 'AI生成直拍简介内容')
        self.assertEqual(updated_task.get('selected_partition_id_bilibili'), '21')

        delete_task(task_id)


if __name__ == '__main__':
    unittest.main()
