import json
import logging
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from modules import config_manager
from modules import task_manager as tm
from modules.ai_enhancer import (
    BILIBILI_TITLE_DESCRIPTION_PROMPT,
    generate_bilibili_title_description,
)


class GenerateBilibiliTitleDescriptionTests(unittest.TestCase):
    def test_sends_complete_source_metadata_and_applies_limits(self):
        source_metadata = {
            'id': 'video-1',
            'title': 'Original title',
            'description': 'Original description',
            'tags': ['music', 'live'],
            'chapters': [{'title': 'Opening', 'start_time': 0}],
            'uploader': 'Original uploader',
            'upload_date': '20260715',
            'formats': [{'format_id': '137', 'height': 1080}],
        }
        parsed = {
            'title': '这是一个超过限制的中文标题',
            'description': '第一段\n\n第二段',
        }

        with patch('modules.ai_enhancer.get_openai_client', return_value=MagicMock()), \
             patch('modules.ai_enhancer._request_json_object', return_value=parsed) as request_json:
            result = generate_bilibili_title_description(
                source_metadata,
                current_metadata={'upload_target': 'bilibili'},
                openai_config={
                    'OPENAI_API_KEY': 'sk-test',
                    'OPENAI_MODEL_NAME': 'test-model',
                },
                task_id='metadata-test',
                title_limit=6,
                description_limit=100,
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['title'], '这是一个超过')
        self.assertEqual(result['description'], '第一段\n\n第二段')
        payload = request_json.call_args.kwargs['payload']
        self.assertEqual(payload['source_metadata'], source_metadata)
        self.assertEqual(payload['source_metadata']['formats'][0]['height'], 1080)
        self.assertEqual(payload['current_metadata']['upload_target'], 'bilibili')
        self.assertEqual(request_json.call_args.kwargs['system_prompt'], BILIBILI_TITLE_DESCRIPTION_PROMPT)

    def test_missing_description_is_reported_as_failure(self):
        with patch('modules.ai_enhancer.get_openai_client', return_value=MagicMock()), \
             patch('modules.ai_enhancer._request_json_object', return_value={'title': '标题'}):
            result = generate_bilibili_title_description(
                {'title': 'source'},
                openai_config={'OPENAI_API_KEY': 'sk-test'},
            )

        self.assertFalse(result['success'])
        self.assertIn('简介', result['error_message'])


class TaskPipelineGenerationTests(unittest.TestCase):
    def test_generation_stage_is_between_translation_and_tags(self):
        order = tm.PIPELINE_STAGE_ORDER
        self.assertLess(
            order.index(tm.PIPELINE_STAGE_TRANSLATE_CONTENT),
            order.index(tm.PIPELINE_STAGE_GENERATE_TITLE_DESCRIPTION),
        )
        self.assertLess(
            order.index(tm.PIPELINE_STAGE_GENERATE_TITLE_DESCRIPTION),
            order.index(tm.PIPELINE_STAGE_GENERATE_TAGS),
        )

    def test_processor_loads_entire_metadata_file_and_saves_generated_fields(self):
        source_metadata = {
            'title': 'Original title',
            'description': 'Original description',
            'tags': ['tag-a', 'tag-b'],
            'chapters': [{'title': 'Part 1'}],
            'uploader': 'Uploader',
        }
        handle = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8')
        try:
            json.dump(source_metadata, handle, ensure_ascii=False)
            handle.close()
            task = {
                'id': 'task-generate-meta',
                'youtube_url': 'https://www.youtube.com/watch?v=video-1',
                'upload_target': 'bilibili',
                'metadata_json_path_local': handle.name,
                'video_title_original': 'Original title',
                'description_original': 'Original description',
                'video_title_translated': '',
                'description_translated': '',
                'tags_generated': None,
            }
            updates = {}

            def fake_update(_task_id, **kwargs):
                updates.update({key: value for key, value in kwargs.items() if key != 'silent'})
                task.update(updates)
                return True

            processor = tm.TaskProcessor({
                'OPENAI_API_KEY': 'sk-test',
                'OPENAI_MODEL_NAME': 'test-model',
            })
            generated = {
                'success': True,
                'title': '生成后的标题',
                'description': '生成后的简介',
            }
            with patch.object(tm, 'get_task', side_effect=lambda _task_id: dict(task)), \
                 patch.object(tm, 'update_task', side_effect=fake_update), \
                 patch('modules.ai_enhancer.generate_bilibili_title_description', return_value=generated) as generate:
                result = processor._generate_title_description('task-generate-meta', logging.getLogger('test'))

            self.assertTrue(result)
            self.assertEqual(updates['video_title_translated'], '生成后的标题')
            self.assertEqual(updates['description_translated'], '生成后的简介')
            self.assertEqual(generate.call_args.args[0], source_metadata)
            self.assertEqual(generate.call_args.kwargs['title_limit'], 80)
            self.assertEqual(generate.call_args.kwargs['description_limit'], 2000)
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass


class ConfigurationAndSettingsTests(unittest.TestCase):
    def test_defaults_use_bilibili_and_generation_is_opt_in(self):
        self.assertEqual(config_manager.DEFAULT_CONFIG['UPLOAD_TARGET_DEFAULT'], 'bilibili')
        self.assertFalse(config_manager.DEFAULT_CONFIG['GENERATE_TITLE_DESCRIPTION'])

    def test_settings_template_contains_generation_toggle(self):
        template_path = os.path.join(os.path.dirname(__file__), '..', 'templates', 'settings.html')
        with open(template_path, 'r', encoding='utf-8') as template_file:
            template = template_file.read()
        self.assertIn('name="GENERATE_TITLE_DESCRIPTION"', template)
        self.assertIn('自动生成标题描述', template)


if __name__ == '__main__':
    unittest.main()
