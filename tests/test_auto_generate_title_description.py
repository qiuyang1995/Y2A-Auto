import json
import logging
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from modules import config_manager
from modules import task_manager as tm
from modules.ai_enhancer import (
    BILIBILI_TITLE_DESCRIPTION_PROMPT,
    _BILIBILI_AI_CONTEXT_MAX_CHARS,
    _compact_bilibili_source_metadata,
    _parse_bilibili_title_description_text,
    _request_chat_completion,
    generate_bilibili_title_description,
)


class GenerateBilibiliTitleDescriptionTests(unittest.TestCase):
    def test_model_request_logs_full_output_and_omits_oversized_input(self):
        logger = MagicMock()
        response = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='stop',
            message=SimpleNamespace(
                content='{"title":"完整标题","description":"完整简介"}',
                reasoning_content='完整推理内容',
                parsed=None,
            ),
        )])
        with patch(
            'modules.ai_enhancer.openai_chat_create_with_thinking_control',
            return_value=response,
        ):
            returned = _request_chat_completion(
                MagicMock(),
                'test-model',
                'system prompt',
                {},
                temperature=0.2,
                thinking_enabled=True,
                logger_obj=logger,
                scene_name='logging_test',
                user_content='x' * 13000,
            )

        self.assertIs(returned, response)
        log_calls = '\n'.join(str(call) for call in logger.info.call_args_list)
        self.assertIn('AI输入内容已省略', log_calls)
        self.assertIn('AI输出完整内容', log_calls)
        self.assertIn('完整推理内容', log_calls)
        self.assertIn('完整标题', log_calls)

    def test_sends_compact_semantic_metadata_and_applies_limits(self):
        source_metadata = {
            'id': 'video-1',
            'title': 'Original title',
            'description': 'Original description',
            'tags': ['music', 'live'],
            'chapters': [{'title': 'Opening', 'start_time': 0}],
            'uploader': 'Original uploader',
            'upload_date': '20260715',
            'formats': [{
                'format_id': '137',
                'height': 1080,
                'width': 1920,
                'fps': 60,
                'vcodec': 'avc1',
                'url': 'https://download.example/video?' + ('x' * 10000),
            }],
            'automatic_captions': {
                'ko': [{'url': 'https://captions.example/ko?' + ('y' * 10000)}],
            },
            'thumbnails': [{'url': 'https://images.example/cover'}],
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
        compact = payload['source_metadata']
        self.assertEqual(compact['title'], source_metadata['title'])
        self.assertEqual(compact['tags'], source_metadata['tags'])
        self.assertEqual(compact['chapters'], source_metadata['chapters'])
        self.assertEqual(compact['formats_summary']['max_height'], 1080)
        self.assertEqual(compact['formats_summary']['fps'], [60])
        self.assertEqual(compact['automatic_captions_summary']['languages'], ['ko'])
        self.assertEqual(compact['thumbnails_summary']['count'], 1)
        self.assertNotIn('download.example', json.dumps(compact, ensure_ascii=False))
        self.assertNotIn('captions.example', json.dumps(compact, ensure_ascii=False))
        self.assertEqual(payload['current_metadata']['upload_target'], 'bilibili')
        self.assertEqual(request_json.call_args.kwargs['system_prompt'], BILIBILI_TITLE_DESCRIPTION_PROMPT)
        self.assertIs(
            request_json.call_args.kwargs['text_fallback_parser'],
            _parse_bilibili_title_description_text,
        )

    def test_bloated_yt_dlp_metadata_has_a_hard_context_limit(self):
        formats = [
            {
                'height': 2160,
                'width': 3840,
                'fps': 60,
                'url': 'https://download.example/' + ('f' * 10000),
            }
            for _ in range(50)
        ]
        captions = {
            f'lang-{index}': [{'url': 'https://caption.example/' + ('c' * 10000)}]
            for index in range(170)
        }
        compact = _compact_bilibili_source_metadata({
            'id': 'video-1',
            'title': '原始标题',
            'description': '原始简介' * 10000,
            'tags': [f'tag-{index}' for index in range(200)],
            'formats': formats,
            'automatic_captions': captions,
        })
        serialized = json.dumps(compact, ensure_ascii=False)

        self.assertLessEqual(len(serialized), _BILIBILI_AI_CONTEXT_MAX_CHARS)
        self.assertEqual(compact['automatic_captions_summary']['language_count'], 170)
        self.assertEqual(compact['formats_summary']['count'], 50)
        self.assertIn('原始标题', serialized)
        self.assertNotIn('download.example', serialized)
        self.assertNotIn('caption.example', serialized)

    def test_parses_glm_markdown_title_and_description_sections(self):
        parsed = _parse_bilibili_title_description_text(
            '**标题**\n韩国女团舞台直拍 4K\n\n**简介：**\n'
            '本视频记录了现场舞台表演。\n欢迎点赞收藏。'
        )

        self.assertEqual(parsed['title'], '韩国女团舞台直拍 4K')
        self.assertEqual(parsed['description'], '本视频记录了现场舞台表演。\n欢迎点赞收藏。')

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
