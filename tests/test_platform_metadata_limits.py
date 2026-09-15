import ast
import pathlib
import re
import unittest


def _load_acfun_helpers():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "acfun_uploader.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    selected = []
    function_names = {"compact_text", "build_upload_description"}
    variable_names = {"ACFUN_TITLE_LIMIT", "ACFUN_DESCRIPTION_LIMIT"}

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in variable_names:
                    selected.append(node)

    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {"re": re}
    exec(compile(isolated, str(module_path), "exec"), namespace)
    return namespace


def _load_bilibili_helpers():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "bilibili_uploader.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    selected = []
    function_names = {
        "_normalize_multiline_text",
        "_truncate_multiline_text",
        "_remove_redundant_original_url",
        "_clean_repost_notices_and_urls",
        "format_bilibili_description",
    }
    variable_names = {"BILIBILI_TITLE_LIMIT", "BILIBILI_DESCRIPTION_LIMIT", "BILIBILI_TAG_LIMIT", "BILIBILI_MAX_TAG_LENGTH"}

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in variable_names:
                    selected.append(node)

    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {"re": re}
    exec(compile(isolated, str(module_path), "exec"), namespace)
    return namespace


def _load_ai_output_limits():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "ai_enhancer.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_output_limits"
    ]
    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(isolated, str(module_path), "exec"), namespace)
    return namespace["_apply_output_limits"]


def _load_task_manager_limit_helper():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "modules" / "task_manager.py"
    source = module_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(module_path))

    selected = []
    function_names = {"normalize_upload_target", "_get_effective_metadata_limits"}
    variable_names = {"UPLOAD_TARGET_ACFUN", "UPLOAD_TARGET_BILIBILI", "UPLOAD_TARGET_BOTH", "VALID_UPLOAD_TARGETS"}

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            selected.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in variable_names:
                    selected.append(node)

    isolated = ast.Module(body=selected, type_ignores=[])
    namespace = {}
    exec(compile(isolated, str(module_path), "exec"), namespace)
    return namespace["_get_effective_metadata_limits"]


class PlatformMetadataLimitTests(unittest.TestCase):
    def test_acfun_limits_and_description_budget(self):
        ns = _load_acfun_helpers()

        self.assertEqual(ns["ACFUN_TITLE_LIMIT"], 50)
        self.assertEqual(ns["ACFUN_DESCRIPTION_LIMIT"], 1000)

        result = ns["build_upload_description"]("a" * 1200)
        self.assertEqual(len(result), 1000)
        self.assertTrue(result.endswith("..."))

    def test_bilibili_limits_and_description_budget(self):
        ns = _load_bilibili_helpers()

        self.assertEqual(ns["BILIBILI_TITLE_LIMIT"], 80)
        self.assertEqual(ns["BILIBILI_DESCRIPTION_LIMIT"], 2000)

        result = ns["format_bilibili_description"]("b" * 2300)
        self.assertEqual(len(result), 2000)
        self.assertTrue(result.endswith("..."))

        shared_result = ns["format_bilibili_description"]("c" * 1300, max_len=1000)
        self.assertEqual(len(shared_result), 1000)
        self.assertTrue(shared_result.endswith("..."))

    def test_bilibili_tag_limits(self):
        ns = _load_bilibili_helpers()
        self.assertEqual(ns["BILIBILI_TAG_LIMIT"], 10)
        self.assertEqual(ns["BILIBILI_MAX_TAG_LENGTH"], 20)

        from modules.bili_sdk.video_uploader import VideoMeta, Picture
        meta = VideoMeta(
            tid=1,
            title="test",
            desc="test",
            cover=Picture(),
            tags=["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9", "t10", "t11", "t12"]
        )
        self.assertEqual(len(meta.tags), 10)
        self.assertEqual(meta.tags, ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9", "t10"])

    def test_ai_output_limits_accept_bilibili_sized_metadata(self):
        apply_output_limits = _load_ai_output_limits()

        title = apply_output_limits("t" * 90, "title", title_limit=80, description_limit=2000)
        description = apply_output_limits("d" * 2300, "description", title_limit=80, description_limit=2000)

        self.assertEqual(len(title), 80)
        self.assertEqual(len(description), 2000)
        self.assertTrue(description.endswith("..."))

    def test_effective_limits_follow_upload_target(self):
        get_effective_metadata_limits = _load_task_manager_limit_helper()

        self.assertEqual(
            get_effective_metadata_limits("both"),
            {"title_limit": 50, "description_limit": 1000},
        )
        self.assertEqual(
            get_effective_metadata_limits("acfun"),
            {"title_limit": 50, "description_limit": 1000},
        )
        self.assertEqual(
            get_effective_metadata_limits("bilibili"),
            {"title_limit": 80, "description_limit": 2000},
        )

    def test_bilibili_submit_as_repost_setting_and_meta(self):
        import inspect
        from modules.config_manager import DEFAULT_CONFIG
        from modules.bilibili_uploader import BilibiliUploader
        from modules.bili_sdk.video_uploader import VideoMeta, Picture

        # 1. 验证默认配置为 False（即默认不按转载，按自制投稿）
        self.assertIn("BILIBILI_SUBMIT_AS_REPOST", DEFAULT_CONFIG)
        self.assertFalse(DEFAULT_CONFIG["BILIBILI_SUBMIT_AS_REPOST"])

        # 2. 验证 upload_video 方法参数签名默认 submit_as_repost=False
        sig = inspect.signature(BilibiliUploader.upload_video)
        self.assertIn("submit_as_repost", sig.parameters)
        self.assertEqual(sig.parameters["submit_as_repost"].default, False)

        # 3. 验证自制投稿 (original=True, source=None)
        meta_original = VideoMeta(
            tid=1,
            title="自制视频测试",
            desc="自制视频描述",
            cover=Picture(),
            tags=["测试"],
            original=True,
            source=None,
        )
        meta_dict_orig = meta_original.__dict__()
        self.assertEqual(meta_dict_orig["copyright"], 1)
        self.assertNotIn("source", meta_dict_orig)

        # 4. 验证转载投稿 (original=False, source=youtube_url)
        yt_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        meta_repost = VideoMeta(
            tid=1,
            title="转载视频测试",
            desc="转载视频描述",
            cover=Picture(),
            tags=["测试"],
            original=False,
            source=yt_url,
        )
        meta_dict_repost = meta_repost.__dict__()
        self.assertEqual(meta_dict_repost["copyright"], 2)
        # 5. 验证 format_bilibili_description 在自制模式 (submit_as_repost=False) 下不添加转载声明且清理已有声明与原URL
        yt_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        ns = _load_bilibili_helpers()
        format_desc = ns["format_bilibili_description"]

        desc_with_repost = (
            "本视频转载自YouTube，原始上传时间：2026-09-01，UP主：OriginalUP\n\n"
            "这是精彩的视频内容！\n"
            f"{yt_url}"
        )
        desc_orig = format_desc(
            desc_with_repost,
            original_url=yt_url,
            original_uploader="OriginalUP",
            original_upload_date="2026-09-01",
            append_repost_notice=True,
            submit_as_repost=False,
        )
        self.assertNotIn("本视频转载自", desc_orig)
        self.assertNotIn("OriginalUP", desc_orig)
        self.assertNotIn("dQw4w9WgXcQ", desc_orig)
        self.assertEqual(desc_orig, "这是精彩的视频内容！")

        # 6. 验证 format_bilibili_description 在转载模式 (submit_as_repost=True) 下正常追加转载声明
        desc_clean = "这是纯净简介"
        desc_repost = format_desc(
            desc_clean,
            original_url=yt_url,
            original_uploader="OriginalUP",
            original_upload_date="2026-09-01",
            append_repost_notice=True,
            submit_as_repost=True,
        )
        self.assertIn("本视频转载自YouTube", desc_repost)
        self.assertIn("UP主：OriginalUP", desc_repost)
        self.assertIn("原始上传时间：2026-09-01", desc_repost)
        self.assertIn("这是纯净简介", desc_repost)


if __name__ == "__main__":
    unittest.main()
