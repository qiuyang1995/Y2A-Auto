import json
import os
import unittest
from unittest.mock import MagicMock, patch

import app as app_module
from modules import task_manager as tm
from modules.task_edit_ai import generate_edit_page_metadata


class EditPageAiGenerationTests(unittest.TestCase):
    def setUp(self):
        self.task_logger_patcher = patch(
            'modules.task_edit_ai.setup_task_logger',
            return_value=MagicMock(),
        )
        self.task_logger = self.task_logger_patcher.start().return_value
        self.addCleanup(self.task_logger_patcher.stop)
        self.task = {
            'id': 'task-edit-ai',
            'upload_target': 'bilibili',
            'youtube_url': 'https://www.youtube.com/watch?v=test',
            'video_title_original': '原标题',
            'description_original': '原始简介',
            'cover_path_local': '',
            'metadata_json_path_local': '',
        }
        self.config = {
            'OPENAI_API_KEY': 'sk-test',
            'OPENAI_MODEL_NAME': 'test-model',
            'OPENAI_THINKING_ENABLED': False,
        }

    @patch('modules.bilibili_zones.get_zone_list_sub', return_value=[{'tid': 1, 'name': '动画'}])
    @patch('modules.task_edit_ai.recommend_bilibili_partition')
    @patch('modules.task_edit_ai.generate_acfun_tags', return_value=['女团', '直拍', '', '女团'])
    @patch('modules.task_edit_ai.generate_bilibili_title_description')
    def test_generates_all_bilibili_edit_fields(
        self,
        generate_title_description,
        _generate_tags,
        recommend_partition,
        _zone_data,
    ):
        generate_title_description.return_value = {
            'success': True,
            'title': '生成标题',
            'description': '生成简介',
        }
        recommend_partition.return_value = {
            'id': '1',
            'source': 'ai',
            'confidence': 0.9,
            'reason_summary': '内容匹配',
        }

        result = generate_edit_page_metadata(
            self.task,
            self.config,
            {'title': '当前标题', 'description': '当前简介', 'tags': ['旧标签']},
        )

        self.assertTrue(result['success'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['title'], '生成标题')
        self.assertEqual(result['description'], '生成简介')
        self.assertEqual(result['tags'], ['女团', '直拍'])
        self.assertEqual(result['partitions']['bilibili']['id'], '1')
        title_kwargs = generate_title_description.call_args.kwargs
        self.assertEqual(title_kwargs['title_limit'], 80)
        self.assertEqual(title_kwargs['description_limit'], 2000)
        self.assertEqual(title_kwargs['current_metadata']['video_title_current'], '当前标题')
        self.assertEqual(recommend_partition.call_args.kwargs['tags'], ['女团', '直拍'])
        workflow_logs = '\n'.join(str(call) for call in self.task_logger.info.call_args_list)
        self.assertIn('编辑页AI自动生成开始', workflow_logs)
        self.assertIn('stage=标题和描述', workflow_logs)
        self.assertIn('stage=标签', workflow_logs)
        self.assertIn('stage=分区推荐', workflow_logs)
        self.assertIn('编辑页AI自动生成结束', workflow_logs)

    @patch('modules.bilibili_zones.get_zone_list_sub', return_value=[{'tid': 1, 'name': '动画'}])
    @patch('modules.task_edit_ai._load_acfun_partitions', return_value=[{'category': '动画', 'partitions': []}])
    @patch('modules.task_edit_ai.recommend_partitions_aio')
    @patch('modules.task_edit_ai.generate_acfun_tags', return_value=['标签'])
    @patch('modules.task_edit_ai.generate_bilibili_title_description')
    def test_both_platforms_returns_two_partition_fields(
        self,
        generate_title_description,
        _generate_tags,
        recommend_partitions,
        _acfun_mapping,
        _zone_data,
    ):
        task = dict(self.task, upload_target='both')
        generate_title_description.return_value = {
            'success': True,
            'title': '双平台标题',
            'description': '双平台简介',
        }
        recommend_partitions.return_value = {
            'acfun': {'id': '10', 'source': 'ai'},
            'bilibili': {'id': '20', 'source': 'ai'},
        }

        result = generate_edit_page_metadata(task, self.config)

        self.assertEqual(result['partitions']['acfun']['id'], '10')
        self.assertEqual(result['partitions']['bilibili']['id'], '20')
        self.assertEqual(generate_title_description.call_args.kwargs['title_limit'], 50)
        self.assertEqual(generate_title_description.call_args.kwargs['description_limit'], 1000)

    def test_missing_api_key_returns_clear_failure(self):
        result = generate_edit_page_metadata(self.task, {'OPENAI_API_KEY': ''})

        self.assertFalse(result['success'])
        self.assertIn('API 密钥', result['message'])


class EditPageAiRouteAndTemplateTests(unittest.TestCase):
    def test_route_returns_generated_fields_and_saves_pipeline_state(self):
        generated = {
            'success': True,
            'complete': True,
            'title': '标题',
            'description': '简介',
            'tags': ['标签'],
            'partitions': {'bilibili': {'id': '1'}},
            'warnings': [],
        }
        with app_module.app.test_request_context(
            '/tasks/task-1/ai_generate_metadata',
            method='POST',
            json={'title': '当前标题'},
        ), patch.object(app_module, 'get_task', return_value={'id': 'task-1'}), \
                patch.object(app_module, 'load_config', return_value={'password_protection_enabled': False}), \
                patch('modules.task_edit_ai.generate_edit_page_metadata', return_value=generated) as generate, \
                patch('modules.task_manager.apply_edit_ai_generation_result', return_value=True) as apply_result:
            response = app_module.ai_generate_task_metadata('task-1')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['title'], '标题')
        self.assertEqual(generate.call_args.args[2]['title'], '当前标题')
        apply_result.assert_called_once_with('task-1', generated)

    def test_saved_ai_result_marks_metadata_pipeline_stages_complete(self):
        task = {
            'id': 'task-1',
            'status': tm.TASK_STATES['AWAITING_REVIEW'],
            'upload_target': 'bilibili',
            'video_title_original': '原标题',
            'description_original': '原简介',
            'moderation_result': None,
            'pipeline_checkpoint': '{"version":1,"completed":["fetch_info"]}',
        }
        generated = {
            'success': True,
            'title': 'AI标题',
            'description': 'AI简介',
            'tags': ['标签一', '标签二'],
            'partitions': {'bilibili': {'id': '138'}},
        }
        with patch.object(tm, 'get_task', return_value=task), \
                patch.object(tm, 'update_task', return_value=True) as update_task:
            success = tm.apply_edit_ai_generation_result('task-1', generated)

        self.assertTrue(success)
        updates = update_task.call_args.kwargs
        checkpoint = json.loads(updates['pipeline_checkpoint'])
        self.assertEqual(updates['status'], tm.TASK_STATES['READY_FOR_UPLOAD'])
        self.assertEqual(updates['video_title_translated'], 'AI标题')
        self.assertEqual(updates['description_translated'], 'AI简介')
        self.assertEqual(updates['selected_partition_id_bilibili'], '138')
        self.assertIn(tm.PIPELINE_STAGE_GENERATE_TITLE_DESCRIPTION, checkpoint['completed'])
        self.assertIn(tm.PIPELINE_STAGE_GENERATE_TAGS, checkpoint['completed'])
        self.assertIn(tm.PIPELINE_STAGE_RECOMMEND_PARTITION, checkpoint['completed'])
        self.assertNotIn(tm.PIPELINE_STAGE_MODERATE_CONTENT, checkpoint['completed'])

        saved_task = dict(task, **{
            key: value for key, value in updates.items() if key != 'silent'
        })
        processor = tm.TaskProcessor({
            'GENERATE_TITLE_DESCRIPTION': True,
            'GENERATE_TAGS': True,
            'RECOMMEND_PARTITION': True,
            'CONTENT_MODERATION_ENABLED': False,
        })
        with patch.object(tm, 'get_task', return_value=saved_task), \
                patch.object(processor, '_generate_title_description') as generate_title, \
                patch.object(processor, '_generate_tags') as generate_tags, \
                patch.object(processor, '_recommend_partition') as recommend_partition:
            ready_task = processor._ensure_force_upload_metadata_ready('task-1', MagicMock())

        self.assertEqual(ready_task['status'], tm.TASK_STATES['READY_FOR_UPLOAD'])
        generate_title.assert_not_called()
        generate_tags.assert_not_called()
        recommend_partition.assert_not_called()

    def test_template_contains_top_button_and_all_field_fill_targets(self):
        template_path = os.path.join(os.path.dirname(__file__), '..', 'templates', 'edit_task.html')
        with open(template_path, 'r', encoding='utf-8') as template_file:
            template = template_file.read()

        self.assertIn('id="ai-auto-generate"', template)
        self.assertIn("url_for('ai_generate_task_metadata'", template)
        self.assertIn("titleInput.value = data.title", template)
        self.assertIn("descriptionTextarea.value = data.description", template)
        self.assertIn("generatedTags", template)
        self.assertIn("selectGeneratedPartition", template)


if __name__ == '__main__':
    unittest.main()
