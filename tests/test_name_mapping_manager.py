#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch

from modules import name_mapping_manager


class TestNameMappingManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.orig_mapping_path = name_mapping_manager.get_mapping_file_path()
        self.test_file_path = os.path.join(self.test_dir, "name_mapping.json")
        self.patcher = patch.object(
            name_mapping_manager, "get_mapping_file_path", return_value=self.test_file_path
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_init_default_structure(self):
        """测试文件不存在时自动初始化默认空结构"""
        self.assertFalse(os.path.exists(self.test_file_path))
        data = name_mapping_manager.load_name_mapping_data()
        self.assertTrue(os.path.exists(self.test_file_path))
        self.assertIn("mappings", data)
        self.assertIn("records", data)
        self.assertEqual(data["mappings"], {})

    def test_parse_names_from_ai_text(self):
        """测试从 AI 输出的各种格式中解析人名对"""
        text = """
        - 재이 => J
        * 张叡恩 -> J
        윤 => YOON
        无
        - None
        {"original": "시은", "standard": "朴莳恩"}
        """
        pairs = name_mapping_manager.parse_names_from_ai_text(text)
        expected = [
            ("재이", "J"),
            ("张叡恩", "J"),
            ("윤", "YOON"),
            ("시은", "朴莳恩"),
        ]
        self.assertEqual(pairs, expected)

    def test_record_name_mappings_and_audit_info(self):
        """测试自动追加与审计字段记录（原标题、URL、task_id、频次）"""
        names = [("재이", "J")]
        cnt = name_mapping_manager.record_name_mappings(
            names,
            original_title="스테이씨 재이 직캠",
            video_url="https://youtube.com/watch?v=111",
            task_id="task-1",
        )
        self.assertEqual(cnt, 1)

        data = name_mapping_manager.load_name_mapping_data()
        self.assertEqual(data["mappings"]["재이"], "J")
        rec = data["records"]["재이"]
        self.assertEqual(rec["standard_name"], "J")
        self.assertEqual(rec["occurrences"], 1)
        self.assertIn("스테이씨 재이 직캠", rec["sample_titles"])
        self.assertIn("https://youtube.com/watch?v=111", rec["sample_urls"])
        self.assertIn("task-1", rec["task_ids"])
        self.assertFalse(rec["manual_verified"])

        # 第二次出现同一个人
        name_mapping_manager.record_name_mappings(
            names,
            original_title="스테이씨 재이 두번째 직캠",
            video_url="https://youtube.com/watch?v=222",
            task_id="task-2",
        )
        data2 = name_mapping_manager.load_name_mapping_data()
        rec2 = data2["records"]["재이"]
        self.assertEqual(rec2["occurrences"], 2)
        self.assertEqual(len(rec2["sample_titles"]), 2)
        self.assertIn("스테이씨 재이 두번째 직캠", rec2["sample_titles"])
        self.assertEqual(len(rec2["task_ids"]), 2)

    def test_manual_verified_protection(self):
        """测试人工确认过的条目不会被后续 AI 自动提取覆盖"""
        # 用户人工设置
        name_mapping_manager.record_name_mappings(
            [("재이", "J（张叡恩）")],
            original_title="手工配置",
            manual_verified=True,
        )

        data = name_mapping_manager.load_name_mapping_data()
        self.assertEqual(data["mappings"]["재이"], "J（张叡恩）")
        self.assertTrue(data["records"]["재이"]["manual_verified"])

        # 随后 AI 自动提取了简写的 J
        name_mapping_manager.record_name_mappings(
            [("재이", "J")],
            original_title="AI新视频",
            task_id="task-ai",
            manual_verified=False,
        )

        data_after = name_mapping_manager.load_name_mapping_data()
        # mappings 保持人工设定的 "J（张叡恩）"，而不是被覆盖为 "J"
        self.assertEqual(data_after["mappings"]["재이"], "J（张叡恩）")
        # 审计日志仍然正常更新
        self.assertEqual(data_after["records"]["재이"]["occurrences"], 2)
        self.assertIn("AI新视频", data_after["records"]["재이"]["sample_titles"])

    def test_get_mapping_prompt_text(self):
        """测试根据标题匹配注入 Prompt"""
        name_mapping_manager.record_name_mappings(
            [("재이", "J"), ("아이사", "ISA")],
            original_title="test",
        )

        # 标题包含 재이
        prompt_text = name_mapping_manager.get_mapping_prompt_text(
            original_title="스테이씨 재이 직캠",
            description="",
        )
        self.assertIn("【已知人名/专有名词映射参考】", prompt_text)
        self.assertIn("재이 => J", prompt_text)

    def test_ai_enhancer_metadata_parsers_names(self):
        """测试 ai_enhancer 中的元数据解析器能正确解析并返回 names"""
        import sys
        from unittest.mock import MagicMock
        if 'openai' not in sys.modules:
            sys.modules['openai'] = MagicMock()

        from modules.ai_enhancer import _parse_unified_bilibili_metadata_text, _parse_bilibili_title_description_text

        text1 = """
**标题**
【STAYC J直拍】论山青年节《Teddy Bear》现场！超甜短发美少女

**简介**
谁能抵抗 STAYC 忙内 J（张叡恩）的甜美魅力！

**分区**
音乐粉丝饭拍

**标签**
STAYC J直拍 TeddyBear

**人名**
재이 => J
장예은 => 张叡恩
"""
        res1 = _parse_unified_bilibili_metadata_text(text1)
        self.assertIsNotNone(res1)
        self.assertIn(("재이", "J"), res1.get("names", []))
        self.assertIn(("장예은", "张叡恩"), res1.get("names", []))

        json_text = """
{
  "title": "STAYC 张叡恩 J《So Bad》现场",
  "description": "来看 STAYC 成员 J（张叡恩）",
  "names": [
    {"original": "재이", "standard": "J"}
  ]
}
"""
        res2 = _parse_bilibili_title_description_text(json_text)
        self.assertIsNotNone(res2)
        self.assertIn(("재이", "J"), res2.get("names", []))

    def test_audit_queue_and_update(self):
        """测试审计队列获取及核验结果更新"""
        # 1. 录入两条记录，一条未核验，一条已人工确认
        name_mapping_manager.record_name_mappings(
            [("민주", "敏珠")],
            original_title="아일릿 민주 직캠",
            manual_verified=False,
        )
        name_mapping_manager.record_name_mappings(
            [("장원영", "张员瑛")],
            original_title="아이브 장원영 직캠",
            manual_verified=True,
        )

        # 增量队列只包含未核验的 "민주"
        inc_queue = name_mapping_manager.get_audit_queue(full=False)
        self.assertEqual(len(inc_queue), 1)
        self.assertEqual(inc_queue[0]["orig_name"], "민주")
        self.assertEqual(inc_queue[0]["current_name"], "敏珠")

        # 全量队列包含两者
        full_queue = name_mapping_manager.get_audit_queue(full=True)
        self.assertEqual(len(full_queue), 2)

        # 2. 单条更新核验结果（将 敏珠 纠正为 朴慜柱）
        ok = name_mapping_manager.update_audit_result(
            orig_name="민주",
            standard_name="朴慜柱",
            audit_status="verified",
            audit_evidence="ILLIT 成员 朴慜柱 官方汉字正名",
            manual_verified=True,
        )
        self.assertTrue(ok)

        # 再次检查增量队列，应该为空
        inc_queue2 = name_mapping_manager.get_audit_queue(full=False)
        self.assertEqual(len(inc_queue2), 0)

        # 验证 mappings 与 records 均已更新
        data = name_mapping_manager.load_name_mapping_data()
        self.assertEqual(data["mappings"]["민주"], "朴慜柱")
        self.assertEqual(data["records"]["민주"]["standard_name"], "朴慜柱")
        self.assertEqual(data["records"]["민주"]["audit_status"], "verified")
        self.assertIn("官方汉字正名", data["records"]["민주"]["audit_evidence"])

    def test_batch_update_audit_results(self):
        """测试批量核验结果写入"""
        batch = {
            "치하루": {
                "standard_name": "安藤千陽",
                "audit_evidence": "Ettone 成员 安藤千陽 官方姓名",
                "audit_status": "verified",
            },
            "승비": {
                "standard_name": "安承飛",
                "audit_evidence": "S2iT 成员 安承飛 官方汉字本名",
                "audit_status": "verified",
            },
        }
        cnt = name_mapping_manager.batch_update_audit_results(batch)
        self.assertEqual(cnt, 2)

        data = name_mapping_manager.load_name_mapping_data()
        self.assertEqual(data["mappings"]["치하루"], "安藤千陽")
        self.assertEqual(data["mappings"]["승비"], "安承飛")
        self.assertEqual(data["records"]["치하루"]["audit_status"], "verified")
        self.assertTrue(data["records"]["치하루"]["manual_verified"])


if __name__ == "__main__":
    unittest.main()


