import unittest
from unittest import mock
import types

from modules.bilibili_uploader import _ensure_valid_bilibili_credential


class BilibiliUploaderCookieCloudTests(unittest.TestCase):
    @mock.patch("modules.bilibili_uploader.validate_credential_remote")
    @mock.patch("modules.bilibili_uploader.load_credential_from_file")
    def test_ensure_valid_credential_already_valid(self, mock_load, mock_validate):
        fake_cred = types.SimpleNamespace()
        mock_load.return_value = fake_cred
        mock_validate.return_value = (True, "OK")

        ok, cred, msg = _ensure_valid_bilibili_credential("dummy_path")

        self.assertTrue(ok)
        self.assertEqual(cred, fake_cred)
        self.assertEqual(msg, "OK")
        mock_load.assert_called_once_with("dummy_path")
        mock_validate.assert_called_once_with(fake_cred)

    @mock.patch("modules.cookiecloud.try_cookiecloud_platform_sync")
    @mock.patch("modules.config_manager.load_config")
    @mock.patch("modules.bilibili_uploader.validate_credential_remote")
    @mock.patch("modules.bilibili_uploader.load_credential_from_file")
    def test_ensure_valid_credential_syncs_on_invalid(
        self, mock_load, mock_validate, mock_config, mock_sync
    ):
        fake_cred_old = types.SimpleNamespace(id="old")
        fake_cred_new = types.SimpleNamespace(id="new")
        mock_load.side_effect = [fake_cred_old, fake_cred_new]
        mock_validate.side_effect = [(False, "账号未登录"), (True, "OK")]
        mock_config.return_value = {
            "COOKIECLOUD_ENABLED": True,
            "COOKIECLOUD_BILIBILI_ENABLED": True,
        }
        mock_sync.return_value = (True, "同步成功")

        ok, cred, msg = _ensure_valid_bilibili_credential("dummy_path")

        self.assertTrue(ok)
        self.assertEqual(cred, fake_cred_new)
        self.assertEqual(msg, "OK")
        mock_sync.assert_called_once_with(mock_config.return_value, "bilibili")
        self.assertEqual(mock_load.call_count, 2)
        self.assertEqual(mock_validate.call_count, 2)

    @mock.patch("modules.cookiecloud.try_cookiecloud_platform_sync")
    @mock.patch("modules.config_manager.load_config")
    @mock.patch("modules.bilibili_uploader.validate_credential_remote")
    @mock.patch("modules.bilibili_uploader.load_credential_from_file")
    def test_ensure_valid_credential_cookiecloud_disabled(
        self, mock_load, mock_validate, mock_config, mock_sync
    ):
        fake_cred = types.SimpleNamespace()
        mock_load.return_value = fake_cred
        mock_validate.return_value = (False, "未登录")
        mock_config.return_value = {
            "COOKIECLOUD_ENABLED": False,
            "COOKIECLOUD_BILIBILI_ENABLED": True,
        }

        ok, cred, msg = _ensure_valid_bilibili_credential("dummy_path")

        self.assertFalse(ok)
        self.assertEqual(cred, fake_cred)
        self.assertEqual(msg, "未登录")
        mock_sync.assert_not_called()
