#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按平台 CookieCloud 能力 + 失效检测的单元测试。

覆盖两块新增能力：
1. cookiecloud.py 的平台化重构（B 站序列化、平台规格、按平台落盘）
2. cookiecloud_health.py 的「检测失效才更新」编排
"""

import json
import os
import pathlib
import shutil
import unittest
from unittest.mock import patch

from modules.cookiecloud import (
    CookieCloudConfigError,
    CookieCloudDataError,
    PLATFORM_BILIBILI,
    PLATFORM_YOUTUBE,
    build_bilibili_cookies_json,
    build_platform_cookies,
    enabled_platforms,
    get_platform_spec,
    is_platform_related_domain,
    sync_cookiecloud_to_platform_file,
    try_cookiecloud_platform_sync,
)
from modules.cookiecloud_health import (
    STATE_INVALID,
    STATE_MISSING,
    STATE_UNKNOWN,
    STATE_VALID,
    check_platform_cookie,
    describe_platform_states,
    run_health_check,
)

BASE_SETTINGS = {
    "COOKIECLOUD_ENABLED": True,
    "COOKIECLOUD_SERVER_URL": "https://cookiecloud.example.com",
    "COOKIECLOUD_UUID": "platform-test-uuid",
    "COOKIECLOUD_PASSWORD": "platform-test-password",
    "COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT": True,
    "COOKIECLOUD_CRYPTO_TYPE": "auto",
    "COOKIECLOUD_YOUTUBE_ENABLED": True,
    "COOKIECLOUD_BILIBILI_ENABLED": True,
}


def _bilibili_payload():
    return {
        "cookie_data": {
            "youtube.com": [
                {"domain": ".youtube.com", "path": "/", "name": "SAPISID", "value": "yt-value"}
            ],
            "bilibili.com": [
                {"domain": ".bilibili.com", "path": "/", "name": "SESSDATA", "value": "sess-main"},
                {"domain": ".bilibili.com", "path": "/", "name": "bili_jct", "value": "jct-main"},
                {"domain": ".bilibili.com", "path": "/", "name": "DedeUserID", "value": "10086"},
                {"domain": ".bilibili.com", "path": "/", "name": "buvid3", "value": "buvid3-value"},
            ],
            "bilibili.cn": [
                {"domain": ".bilibili.cn", "path": "/", "name": "SESSDATA", "value": "sess-stale"},
                {"domain": ".bilibili.cn", "path": "/", "name": "DedeUserID__ckMd5", "value": "ck"},
            ],
            "evil-bilibili.com.attacker.net": [
                {"domain": "evil-bilibili.com.attacker.net", "path": "/", "name": "SESSDATA", "value": "evil"}
            ],
        },
        "local_storage_data": {},
    }


class CookieCloudPlatformTests(unittest.TestCase):
    def setUp(self):
        self.work_dir = pathlib.Path(__file__).resolve().parents[1] / "temp" / "unit-tests" / "cookiecloud-platform"
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _settings_with_outputs(self):
        settings = dict(BASE_SETTINGS)
        settings["YOUTUBE_COOKIES_PATH"] = str(self.work_dir / "yt_cookies.txt")
        settings["BILIBILI_COOKIES_PATH"] = str(self.work_dir / "bili_cookies.json")
        return settings

    # ---------- 平台规格 ----------

    def test_get_platform_spec_rejects_unknown_platform(self):
        with self.assertRaises(CookieCloudConfigError):
            get_platform_spec("acfun")

    def test_platform_domain_matching_stays_boundary_safe(self):
        self.assertTrue(is_platform_related_domain("bilibili", ".bilibili.com"))
        self.assertTrue(is_platform_related_domain("bilibili", "www.bilibili.com"))
        self.assertTrue(is_platform_related_domain("bilibili", "bilibili.com"))
        self.assertFalse(is_platform_related_domain("bilibili", "evil-bilibili.com.attacker.net"))
        self.assertFalse(is_platform_related_domain("bilibili", "youtube.com"))

    def test_enabled_platforms_follows_per_platform_switches(self):
        settings = dict(BASE_SETTINGS, COOKIECLOUD_BILIBILI_ENABLED=False)
        keys = [spec.key for spec in enabled_platforms(settings)]
        self.assertEqual(keys, [PLATFORM_YOUTUBE.key])

        settings = dict(BASE_SETTINGS, COOKIECLOUD_YOUTUBE_ENABLED=False)
        keys = [spec.key for spec in enabled_platforms(settings)]
        self.assertEqual(keys, [PLATFORM_BILIBILI.key])

    # ---------- B 站序列化 ----------

    def test_bilibili_serializer_keeps_required_fields_only(self):
        content, count = build_bilibili_cookies_json(_bilibili_payload())
        items = json.loads(content)
        by_name = {item["name"]: item for item in items}

        self.assertEqual(by_name["SESSDATA"]["value"], "sess-main")
        self.assertEqual(by_name["bili_jct"]["value"], "jct-main")
        self.assertEqual(by_name["DedeUserID"]["value"], "10086")
        self.assertEqual(by_name["buvid3"]["value"], "buvid3-value")
        # 不应混入 YouTube 或伪装的攻击者域名
        self.assertNotIn("SAPISID", by_name)
        self.assertEqual(count, len(items))

    def test_bilibili_serializer_prefers_bilibili_com_on_same_name(self):
        content, _count = build_bilibili_cookies_json(_bilibili_payload())
        items = json.loads(content)
        by_name = {item["name"]: item for item in items}

        # bilibili.cn 上还有一份 SESSDATA，必须让位给 .bilibili.com
        self.assertEqual(by_name["SESSDATA"]["value"], "sess-main")
        self.assertEqual(by_name["SESSDATA"]["domain"], "bilibili.com")
        # 只在 .bilibili.cn 出现的字段仍然保留
        self.assertEqual(by_name["DedeUserID__ckMd5"]["value"], "ck")

    def test_bilibili_serializer_output_is_parseable_by_bilibili_auth(self):
        content, _count = build_bilibili_cookies_json(_bilibili_payload())
        from modules.bilibili_auth import _parse_cookies_text

        cookies = _parse_cookies_text(content)
        self.assertEqual(cookies.get("SESSDATA"), "sess-main")
        self.assertEqual(cookies.get("bili_jct"), "jct-main")
        self.assertEqual(cookies.get("DedeUserID"), "10086")

    def test_bilibili_serializer_raises_without_bilibili_cookies(self):
        payload = {"cookie_data": {"youtube.com": [{"domain": ".youtube.com", "name": "a", "value": "b"}]}}
        with self.assertRaises(CookieCloudDataError):
            build_bilibili_cookies_json(payload)

    def test_build_platform_cookies_dispatches_by_format(self):
        youtube_content, _ = build_platform_cookies("youtube", _bilibili_payload())
        bilibili_content, _ = build_platform_cookies("bilibili", _bilibili_payload())

        self.assertTrue(youtube_content.startswith("# Netscape HTTP Cookie File"))
        self.assertNotIn("SESSDATA", youtube_content)
        self.assertTrue(bilibili_content.lstrip().startswith("["))
        self.assertIn("SESSDATA", bilibili_content)

    # ---------- 按平台落盘 ----------

    def test_sync_writes_bilibili_json_to_configured_path(self):
        settings = self._settings_with_outputs()
        with patch(
            "modules.cookiecloud.fetch_cookiecloud_payload",
            return_value=_bilibili_payload(),
        ):
            result = sync_cookiecloud_to_platform_file(settings, PLATFORM_BILIBILI.key)

        target = self.work_dir / "bili_cookies.json"
        self.assertTrue(target.exists())
        self.assertEqual(result["platform"], "bilibili")
        self.assertEqual(result["cookie_count"], 5)
        self.assertTrue(result["changed"])

        written = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual({item["name"] for item in written}, {
            "SESSDATA", "bili_jct", "DedeUserID", "buvid3", "DedeUserID__ckMd5",
        })

    def test_sync_skips_write_when_content_is_unchanged(self):
        settings = self._settings_with_outputs()
        with patch(
            "modules.cookiecloud.fetch_cookiecloud_payload",
            return_value=_bilibili_payload(),
        ):
            first = sync_cookiecloud_to_platform_file(settings, PLATFORM_BILIBILI.key)
            second = sync_cookiecloud_to_platform_file(settings, PLATFORM_BILIBILI.key)

        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])

    def test_try_sync_reports_disabled_platform(self):
        settings = dict(BASE_SETTINGS, COOKIECLOUD_BILIBILI_ENABLED=False)
        ok, message = try_cookiecloud_platform_sync(settings, PLATFORM_BILIBILI.key)
        self.assertFalse(ok)
        self.assertIn("Bilibili", message)

    def test_try_sync_reports_missing_plaintext_opt_in(self):
        settings = dict(BASE_SETTINGS, COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT=False)
        ok, message = try_cookiecloud_platform_sync(settings, PLATFORM_BILIBILI.key)
        self.assertFalse(ok)
        self.assertIn("明文导出", message)


class CookieCloudHealthCheckTests(unittest.TestCase):
    """检测失效才更新的编排逻辑（全部打桩，不联网）。"""

    def test_valid_cookie_skips_update(self):
        with patch("modules.cookiecloud_health.check_platform_cookie", return_value=(STATE_VALID, "ok")) as checker, \
             patch("modules.cookiecloud_health.try_cookiecloud_platform_sync") as syncer:
            result = run_health_check(dict(BASE_SETTINGS))

        self.assertTrue(result["checked"])
        self.assertEqual(result["updated"], [])
        self.assertFalse(syncer.called)
        self.assertEqual(checker.call_count, 2)
        self.assertIn("无需更新", result["summary"])

    def test_invalid_cookie_triggers_update(self):
        sync_result = {
            "output_path": "/tmp/bili_cookies.json",
            "cookie_count": 5,
            "changed": True,
        }
        with patch("modules.cookiecloud_health.check_platform_cookie", return_value=(STATE_INVALID, "已失效")), \
             patch("modules.cookiecloud_health.try_cookiecloud_platform_sync", return_value=(True, sync_result)) as syncer:
            result = run_health_check(dict(BASE_SETTINGS))

        self.assertEqual(syncer.call_count, 2)
        self.assertEqual(sorted(result["updated"]), ["bilibili", "youtube"])
        self.assertEqual(result["failed_updates"], [])
        self.assertTrue(result["platforms"]["youtube"]["updated"])
        self.assertIn("已从 CookieCloud 更新 5 条", result["platforms"]["youtube"]["update_message"])

    def test_missing_local_file_triggers_update(self):
        with patch("modules.cookiecloud_health.check_platform_cookie", return_value=(STATE_MISSING, "文件不存在")), \
             patch("modules.cookiecloud_health.try_cookiecloud_platform_sync", return_value=(True, {"cookie_count": 1, "changed": True})) as syncer:
            result = run_health_check(dict(BASE_SETTINGS))

        self.assertEqual(syncer.call_count, 2)
        self.assertEqual(len(result["updated"]), 2)

    def test_unknown_state_never_updates(self):
        with patch("modules.cookiecloud_health.check_platform_cookie", return_value=(STATE_UNKNOWN, "网络抖动")), \
             patch("modules.cookiecloud_health.try_cookiecloud_platform_sync") as syncer:
            result = run_health_check(dict(BASE_SETTINGS))

        self.assertFalse(syncer.called)
        self.assertEqual(result["updated"], [])
        self.assertEqual(result["failed_updates"], [])

    def test_failed_update_is_reported(self):
        with patch("modules.cookiecloud_health.check_platform_cookie", return_value=(STATE_INVALID, "已失效")), \
             patch("modules.cookiecloud_health.try_cookiecloud_platform_sync", return_value=(False, "CookieCloud 同步失败")):
            result = run_health_check(dict(BASE_SETTINGS))

        self.assertEqual(sorted(result["failed_updates"]), ["bilibili", "youtube"])
        self.assertTrue(all("更新失败" in entry["update_message"] for entry in result["platforms"].values()))

    def test_preflight_blocks_when_cookiecloud_disabled(self):
        settings = dict(BASE_SETTINGS, COOKIECLOUD_ENABLED=False)
        with patch("modules.cookiecloud_health.check_platform_cookie") as checker:
            result = run_health_check(settings)

        self.assertFalse(result["checked"])
        self.assertIn("未启用", result["reason"])
        self.assertFalse(checker.called)

    def test_scheduled_run_requires_plaintext_export_but_manual_does_not(self):
        settings = dict(BASE_SETTINGS, COOKIECLOUD_ALLOW_PLAINTEXT_EXPORT=False)

        scheduled = run_health_check(settings, require_writable=True)
        self.assertFalse(scheduled["checked"])
        self.assertIn("明文导出", scheduled["reason"])

        with patch("modules.cookiecloud_health.check_platform_cookie", return_value=(STATE_VALID, "ok")):
            manual = run_health_check(settings, require_writable=False)
        self.assertTrue(manual["checked"])

    def test_check_platform_cookie_defaults_to_unknown_on_internal_error(self):
        # 未注册平台由 get_platform_spec 抛错前就被拦，这里覆盖检测器抛异常的分支
        with patch.dict(
            "modules.cookiecloud_health._PLATFORM_CHECKERS",
            {"youtube": lambda cookie_file, settings: (_ for _ in ()).throw(RuntimeError("boom"))},
        ):
            state, message = check_platform_cookie("youtube", dict(BASE_SETTINGS))
        self.assertEqual(state, STATE_UNKNOWN)
        self.assertIn("boom", message)

    def test_describe_platform_states_renders_each_platform(self):
        with patch("modules.cookiecloud_health.check_platform_cookie", return_value=(STATE_VALID, "ok")), \
             patch("modules.cookiecloud_health.try_cookiecloud_platform_sync"):
            result = run_health_check(dict(BASE_SETTINGS))

        text = describe_platform_states(result)
        self.assertIn("YouTube", text)
        self.assertIn("Bilibili", text)


if __name__ == "__main__":
    unittest.main()
