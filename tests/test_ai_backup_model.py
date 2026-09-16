"""
单元测试：AI 灾备兜底模型 (Disaster Recovery Fallback)
验证主模型及备选模型链在重试耗尽后正确切换至兜底模型，以及相关配置生效。
"""

import sys
import unittest
from unittest.mock import MagicMock, patch
import json

def _mock_pkg(name):
    m = MagicMock()
    m.__path__ = []
    sys.modules[name] = m
    return m

for pkg in ['PIL', 'apscheduler', 'apscheduler.schedulers', 'apscheduler.executors', 'werkzeug']:
    if pkg not in sys.modules:
        _mock_pkg(pkg)

for mod in [
    'PIL.Image', 'openai', 'requests', 'flask',
    'werkzeug.security',
    'apscheduler.schedulers.background', 'apscheduler.schedulers.base',
    'apscheduler.executors.pool'
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()


class TestAIBackupModel(unittest.TestCase):
    def setUp(self):
        from modules.ai_enhancer import _MODEL_EXHAUSTED_COOLDOWN_MAP
        _MODEL_EXHAUSTED_COOLDOWN_MAP.clear()

    def test_primary_success_does_not_trigger_backup(self):
        """主模型成功时不应触发兜底模型"""
        from modules.ai_enhancer import _request_chat_completion

        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "OK"
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]

        called_models = []

        def fake_create(client, create_kwargs, **kwargs):
            called_models.append(create_kwargs.get("model"))
            return mock_resp

        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control", side_effect=fake_create), \
             patch("modules.ai_enhancer.get_openai_client", return_value=MagicMock()):

            cfg = {
                "OPENAI_MODEL_NAME": "gemini-3.8-flash",
                "OPENAI_FALLBACK_MODEL_NAME": "gemini-3.7-flash",
                "OPENAI_BACKUP_MODEL_NAME": "deepseek-chat",
                "OPENAI_API_KEY": "test-key",
            }
            resp = _request_chat_completion(
                client=MagicMock(),
                model_name="gemini-3.8-flash",
                system_prompt="sys",
                payload={"k": "v"},
                temperature=0.7,
                thinking_enabled=False,
                logger_obj=MagicMock(),
                scene_name="test_primary",
                openai_config=cfg,
            )
            self.assertEqual(resp, mock_resp)
            self.assertEqual(called_models, ["gemini-3.8-flash"])

    def test_fallback_success_does_not_trigger_backup(self):
        """主模型失败但备选模型成功时，不触发最终兜底模型"""
        from modules.ai_enhancer import _request_chat_completion

        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "OK from fallback"
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]

        called_models = []

        def fake_create(client, create_kwargs, **kwargs):
            m = create_kwargs.get("model")
            called_models.append(m)
            if m == "gemini-3.8-flash":
                raise RuntimeError("429 Resource has been exhausted")
            return mock_resp

        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control", side_effect=fake_create), \
             patch("modules.ai_enhancer.get_openai_client", return_value=MagicMock()):

            cfg = {
                "OPENAI_MODEL_NAME": "gemini-3.8-flash",
                "OPENAI_FALLBACK_MODEL_NAME": "gemini-3.7-flash",
                "OPENAI_BACKUP_MODEL_NAME": "deepseek-chat",
                "OPENAI_API_KEY": "test-key",
            }
            resp = _request_chat_completion(
                client=MagicMock(),
                model_name="gemini-3.8-flash",
                system_prompt="sys",
                payload={"k": "v"},
                temperature=0.7,
                thinking_enabled=False,
                logger_obj=MagicMock(),
                scene_name="test_fallback",
                openai_config=cfg,
            )
            self.assertEqual(resp, mock_resp)
            self.assertEqual(called_models, ["gemini-3.8-flash", "gemini-3.7-flash"])

    def test_all_fail_triggers_backup_success(self):
        """主模型及备选链全部失败后（如地区不支持），自动切换兜底模型并成功返回"""
        from modules.ai_enhancer import _request_chat_completion

        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Rescued by DeepSeek"
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]

        called_models = []
        created_client_cfgs = []

        def fake_get_client(cfg):
            created_client_cfgs.append(dict(cfg))
            return MagicMock()

        def fake_create(client, create_kwargs, **kwargs):
            m = create_kwargs.get("model")
            called_models.append(m)
            if "gemini" in m:
                raise RuntimeError("User location is not supported for the API use (400)")
            elif m == "deepseek-chat":
                return mock_resp
            raise RuntimeError(f"Unexpected model {m}")

        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control", side_effect=fake_create), \
             patch("modules.ai_enhancer.get_openai_client", side_effect=fake_get_client):

            cfg = {
                "OPENAI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "OPENAI_API_KEY": "gemini-key",
                "OPENAI_MODEL_NAME": "gemini-3.8-flash",
                "OPENAI_FALLBACK_MODEL_NAME": "gemini-3.7-flash, gemini-3.6-flash",
                "OPENAI_BACKUP_BASE_URL": "https://api.deepseek.com/v1",
                "OPENAI_BACKUP_API_KEY": "deepseek-key",
                "OPENAI_BACKUP_MODEL_NAME": "deepseek-chat",
                "OPENAI_BACKUP_THINKING_ENABLED": True,
            }
            logger_mock = MagicMock()
            resp = _request_chat_completion(
                client=MagicMock(),
                model_name="gemini-3.8-flash",
                system_prompt="sys",
                payload={"k": "v"},
                temperature=0.7,
                thinking_enabled=False,
                logger_obj=logger_mock,
                scene_name="test_rescue",
                openai_config=cfg,
            )
            self.assertEqual(resp, mock_resp)
            # 确认调用序列：先尝试所有 gemini 模型，全部失败后再调用 deepseek-chat
            self.assertEqual(called_models, ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "deepseek-chat"])

            # 确认兜底客户端使用了独立的 Base URL 和 Key
            backup_clients = [c for c in created_client_cfgs if c.get("OPENAI_MODEL_NAME") == "deepseek-chat"]
            self.assertTrue(len(backup_clients) >= 1)
            self.assertEqual(backup_clients[0]["OPENAI_BASE_URL"], "https://api.deepseek.com/v1")
            self.assertEqual(backup_clients[0]["OPENAI_API_KEY"], "deepseek-key")

            # 确认输出了包含触发兜底与成功的日志
            warning_templates = [call[0][0] for call in logger_mock.warning.call_args_list]
            self.assertTrue(any("触发最终灾备兜底模型" in tmpl for tmpl in warning_templates))
            info_calls = [call[0] for call in logger_mock.info.call_args_list]
            self.assertTrue(any("最终灾备兜底模型 [%s] 调用成功" in call[0] and call[1] == "deepseek-chat" for call in info_calls))

    def test_backup_fails_raises_comprehensive_error(self):
        """当主模型、备选链与兜底模型均失败时，抛出包含各模型失败原因的汇总异常"""
        from modules.ai_enhancer import _request_chat_completion

        def fake_create(client, create_kwargs, **kwargs):
            m = create_kwargs.get("model")
            if "gemini" in m:
                raise RuntimeError("User location is not supported (400)")
            raise RuntimeError(f"Model {m} failed with 500 internal server error")

        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control", side_effect=fake_create), \
             patch("modules.ai_enhancer.get_openai_client", return_value=MagicMock()):

            cfg = {
                "OPENAI_MODEL_NAME": "gemini-3.8-flash",
                "OPENAI_FALLBACK_MODEL_NAME": "gemini-3.7-flash",
                "OPENAI_BACKUP_MODEL_NAME": "deepseek-chat",
                "OPENAI_API_KEY": "test-key",
            }
            with self.assertRaises(RuntimeError) as ctx:
                _request_chat_completion(
                    client=MagicMock(),
                    model_name="gemini-3.8-flash",
                    system_prompt="sys",
                    payload={},
                    temperature=0.7,
                    thinking_enabled=False,
                    logger_obj=MagicMock(),
                    scene_name="test_all_fail",
                    openai_config=cfg,
                )
            err_msg = str(ctx.exception)
            self.assertIn("所有备选模型及最终兜底模型均调用失败", err_msg)
            self.assertIn("兜底:deepseek-chat", err_msg)

    def test_multi_backup_models_chain(self):
        """支持配置多个兜底模型（逗号分隔），第一个兜底失败后尝试第二个兜底"""
        from modules.ai_enhancer import _request_chat_completion

        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "Rescued by qwen"
        mock_choice.finish_reason = "stop"
        mock_resp.choices = [mock_choice]

        called_models = []

        def fake_create(client, create_kwargs, **kwargs):
            m = create_kwargs.get("model")
            called_models.append(m)
            if m == "deepseek-chat":
                raise RuntimeError("DeepSeek 503 Overloaded")
            elif m == "qwen-plus":
                return mock_resp
            raise RuntimeError("Gemini failed")

        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control", side_effect=fake_create), \
             patch("modules.ai_enhancer.get_openai_client", return_value=MagicMock()):

            cfg = {
                "OPENAI_MODEL_NAME": "gemini-3.8-flash",
                "OPENAI_FALLBACK_MODEL_NAME": "",
                "OPENAI_BACKUP_MODEL_NAME": "deepseek-chat, qwen-plus",
                "OPENAI_API_KEY": "test-key",
            }
            resp = _request_chat_completion(
                client=MagicMock(),
                model_name="gemini-3.8-flash",
                system_prompt="sys",
                payload={},
                temperature=0.7,
                thinking_enabled=False,
                logger_obj=MagicMock(),
                scene_name="test_multi_backup",
                openai_config=cfg,
            )
            self.assertEqual(resp, mock_resp)
            self.assertEqual(called_models, ["gemini-3.8-flash", "deepseek-chat", "qwen-plus"])

    def test_subtitle_translator_backup_failover(self):
        """测试字幕翻译器在主模型失败时能够无缝切换至灾备兜底模型完成翻译"""
        from modules.subtitle_translator import LLMRequester

        mock_resp = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({"translations": ["你好世界"]})
        mock_resp.choices = [mock_choice]

        called_models = []

        def fake_create(client, create_kwargs, **kwargs):
            m = create_kwargs.get("model")
            called_models.append(m)
            if m == "gemini-3.8-flash":
                raise RuntimeError("Gemini 400 location not supported")
            elif m == "deepseek-chat":
                return mock_resp
            raise RuntimeError(f"Unexpected model: {m}")

        with patch("modules.subtitle_translator.openai_chat_create_with_thinking_control", side_effect=fake_create), \
             patch("modules.subtitle_translator.get_openai_client", return_value=MagicMock()):

            cfg = {
                "OPENAI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "OPENAI_API_KEY": "gemini-key",
                "OPENAI_MODEL_NAME": "gemini-3.8-flash",
                "OPENAI_BACKUP_BASE_URL": "https://api.deepseek.com/v1",
                "OPENAI_BACKUP_API_KEY": "deepseek-key",
                "OPENAI_BACKUP_MODEL_NAME": "deepseek-chat",
            }
            requester = LLMRequester(cfg, task_id="test_sub_task")
            res = requester.translate_batch(["Hello world"], target_language="zh", batch_id="batch_1")
            self.assertEqual(res, ["你好世界"])
            self.assertEqual(called_models, ["gemini-3.8-flash", "deepseek-chat"])


if __name__ == '__main__':
    unittest.main()

