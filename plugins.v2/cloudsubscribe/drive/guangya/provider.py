"""光鸭网盘能力适配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet

from .client import GuangyaClient
from .files import GuangyaFileService
from .offline import GuangyaOfflineService
from .share import GuangyaShareService
from .upload import GuangyaUploadService
from ...core.cloud import (
    CloudDriveCapability,
    CloudDrivePolicy,
    CloudDriveProvider,
    CloudFile,
)


@dataclass
class GuangyaDrive:
    client: GuangyaClient
    metadata_url_template: str
    page_size: int = 100

    def reset_api_call_count(self) -> None:
        self.client.reset_api_call_count()

    def get_api_call_count(self) -> int:
        return self.client.get_api_call_count()

    def close(self) -> None:
        """关闭底层 HTTP 会话。"""
        self.client.close()


@dataclass(frozen=True)
class GuangyaPlaybackReference:
    template_variables: FrozenSet[str] = frozenset({"file_id", "gcid"})

    @staticmethod
    def reference_values(file_item: CloudFile) -> Dict[str, str]:
        return dict(file_item.playback_values)


def create_guangya_provider(drive: GuangyaDrive) -> CloudDriveProvider:
    files = GuangyaFileService(drive.client, drive.page_size)
    offline = GuangyaOfflineService(
        drive.client, files, drive.metadata_url_template
    )
    share = GuangyaShareService(drive.client, files, offline)
    upload = GuangyaUploadService(drive.client, files)
    playback_reference = GuangyaPlaybackReference()
    return CloudDriveProvider(
        key="guangya",
        name="光鸭网盘",
        config_prefix="guangya",
        resource_types=frozenset({"guangya", "ed2k", "magnet"}),
        services={
            CloudDriveCapability.AUTHENTICATION: drive.client,
            CloudDriveCapability.ACCOUNT: drive.client,
            CloudDriveCapability.SHARE_TRANSFER: share,
            CloudDriveCapability.OFFLINE_DOWNLOAD: offline,
            CloudDriveCapability.DIRECTORY_READ: files,
            CloudDriveCapability.FILE_QUERY: files,
            CloudDriveCapability.FILE_MUTATION: files,
            CloudDriveCapability.PLAYBACK_REFERENCE: playback_reference,
            CloudDriveCapability.LOCAL_UPLOAD: upload,
            CloudDriveCapability.QRCODE_AUTH: drive.client,
        },
        policy=CloudDrivePolicy(
            pagination_mode="offset",
            max_page_size=100,
            supports_batch=True,
            max_batch_size=50,
            max_concurrency=2,
        ),
    )
