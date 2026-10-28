"""光鸭下载接口字段兼容性回归测试。"""

import unittest
from unittest.mock import MagicMock

from plugin_env import install_app_mocks, load_module, register_mock


install_app_mocks("app.core.cache")
register_mock("cloudsubscribe.core.transfer")
register_mock(
    "cloudsubscribe.drive.common",
    CloudDriveFileServiceBase=object,
    create_directory_cache=MagicMock(return_value={}),
    safe_int=int,
)
_cloud = load_module(
    "cloudsubscribe.core.cloud", "plugins.v2/cloudsubscribe/core/cloud.py"
)
_files = load_module(
    "cloudsubscribe.drive.guangya.files",
    "plugins.v2/cloudsubscribe/drive/guangya/files.py",
)


class TestGuangyaDownloadLink(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.API_BASE_URL = "https://api.guangyapan.com"
        self.client.data.side_effect = lambda response: response.get("data") or {}
        self.service = _files.GuangyaFileService(self.client)
        self.file = _cloud.CloudFile(
            id="file-123", name="episode.mkv", is_directory=False
        )
        self.url = "https://cdn.example.com/episode.mkv?signature=test"

    def test_account_download_accepts_signed_uppercase_url(self):
        self.client.request.return_value = {
            "msg": "success",
            "data": {"signedURL": self.url, "urlDuration": 21600},
        }

        self.assertEqual(self.service.resolve_download_link(self.file), (self.url, {}))
        self.client.request.assert_called_once_with(
            "POST",
            "https://api.guangyapan.com/userres/v1/get_res_download_url",
            json_data={"fileId": "file-123"},
        )

    def test_share_download_accepts_signed_uppercase_url(self):
        self.file.playback_values["share_access_token"] = "share-token"
        self.client.request.return_value = {
            "msg": "success", "data": {"signedURL": self.url}
        }

        self.assertEqual(self.service.resolve_download_link(self.file), (self.url, {}))
        self.client.request.assert_called_once_with(
            "POST",
            "https://api.guangyapan.com/userres/v1/get_share_download_url",
            json_data={"fileId": "file-123", "accessToken": "share-token"},
            authenticated=False,
        )

    def test_existing_download_url_fields_remain_supported(self):
        for key in ("signedUrl", "downloadUrl", "url", "fileDownloadUrl"):
            with self.subTest(key=key):
                self.client.request.return_value = {"data": {key: self.url}}
                self.assertEqual(
                    self.service.resolve_download_link(self.file), (self.url, {})
                )

    def test_missing_download_url_remains_an_error(self):
        self.client.request.return_value = {"msg": "下载地址不可用", "data": {}}

        with self.assertRaisesRegex(RuntimeError, "下载地址不可用"):
            self.service.resolve_download_link(self.file)


if __name__ == "__main__":
    unittest.main()
