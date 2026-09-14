#!/usr/bin/env python
# -*- coding: utf-8 -*-

import unittest
import time
from unittest.mock import MagicMock, patch

from modules.ai_enhancer import (
    _parse_model_candidates,
    _request_chat_completion,
    _MODEL_EXHAUSTED_COOLDOWN_MAP,
)


class TestMultiModelFailover(unittest.TestCase):

    def setUp(self):
        _MODEL_EXHAUSTED_COOLDOWN_MAP.clear()

    def tearDown(self):
        _MODEL_EXHAUSTED_COOLDOWN_MAP.clear()

    def test_parse_model_candidates_splitting_and_dedup(self):
        # 逗号分隔
        res = _parse_model_candidates("gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash")
        self.assertEqual(res, ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"])

        # 混合多种分隔符与主模型+备用模型
        res2 = _parse_model_candidates(
            "gemini-3.8-flash; gemini-3.7-flash",
            "gemini-3.6-flash, gemini-3.8-flash|gemini-3.5-flash-lite\ngemini-3.1-flash-lite",
        )
        # 去重且保持优先级顺序
        self.assertEqual(
            res2,
            [
                "gemini-3.8-flash",
                "gemini-3.7-flash",
                "gemini-3.6-flash",
                "gemini-3.5-flash-lite",
                "gemini-3.1-flash-lite",
            ],
        )

        # 列表与字符串混合
        res3 = _parse_model_candidates(["model-a", "model-b"], "model-c, model-a")
        self.assertEqual(res3, ["model-a", "model-b", "model-c"])

    def test_multi_model_sequential_failover_on_429(self):
        # 模拟第一个模型报 429 额度耗尽，第二个模型成功返回
        mock_response = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = "成功响应"
        mock_response.choices = [mock_choice]

        call_records = []

        def fake_create_with_thinking(client, create_kwargs, **kwargs):
            model = create_kwargs.get("model")
            call_records.append(model)
            if model == "gemini-3.8-flash":
                raise Exception("Error code: 429 - Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash")
            elif model == "gemini-3.7-flash":
                return mock_response
            raise Exception(f"Unknown model: {model}")

        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control", side_effect=fake_create_with_thinking), \
             patch("modules.ai_enhancer.get_openai_client", return_value=MagicMock()):

            cfg = {
                "OPENAI_MODEL_NAME": "gemini-3.8-flash",
                "OPENAI_FALLBACK_MODEL_NAME": "gemini-3.7-flash, gemini-3.6-flash",
                "OPENAI_API_KEY": "test-key",
            }
            logger_mock = MagicMock()

            resp = _request_chat_completion(
                client=MagicMock(),
                model_name="gemini-3.8-flash",
                system_prompt="sys",
                payload={"key": "val"},
                temperature=0.7,
                thinking_enabled=False,
                logger_obj=logger_mock,
                scene_name="test_scene",
                openai_config=cfg,
            )

            # 验证返回值与调用顺序
            self.assertEqual(resp, mock_response)
            self.assertEqual(call_records, ["gemini-3.8-flash", "gemini-3.7-flash"])

            # 验证第一个超限模型已被记入冷却期
            self.assertIn("gemini-3.8-flash", _MODEL_EXHAUSTED_COOLDOWN_MAP)
            self.assertGreater(_MODEL_EXHAUSTED_COOLDOWN_MAP["gemini-3.8-flash"], time.time())

            # 验证后续再次调用时，会自动优先跳过冷却期的 gemini-3.8-flash，直接从 gemini-3.7-flash 开始！
            call_records.clear()
            resp2 = _request_chat_completion(
                client=MagicMock(),
                model_name="gemini-3.8-flash",
                system_prompt="sys",
                payload={"key": "val"},
                temperature=0.7,
                thinking_enabled=False,
                logger_obj=logger_mock,
                scene_name="test_scene_2",
                openai_config=cfg,
            )
            self.assertEqual(resp2, mock_response)
            self.assertEqual(call_records, ["gemini-3.7-flash"])

    def test_all_models_fail_raises_last_exception(self):
        def fake_create_with_thinking(client, create_kwargs, **kwargs):
            model = create_kwargs.get("model")
            raise RuntimeError(f"Model {model} failed with 500 server error")

        with patch("modules.ai_enhancer.openai_chat_create_with_thinking_control", side_effect=fake_create_with_thinking), \
             patch("modules.ai_enhancer.get_openai_client", return_value=MagicMock()):

            cfg = {
                "OPENAI_MODEL_NAME": "model-1",
                "OPENAI_FALLBACK_MODEL_NAME": "model-2",
                "OPENAI_API_KEY": "test-key",
            }
            logger_mock = MagicMock()

            with self.assertRaises(RuntimeError) as ctx:
                _request_chat_completion(
                    client=MagicMock(),
                    model_name="model-1",
                    system_prompt="sys",
                    payload={},
                    temperature=0.7,
                    thinking_enabled=False,
                    logger_obj=logger_mock,
                    scene_name="test_all_fail",
                    openai_config=cfg,
                )
            self.assertIn("Model model-2 failed", str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
