"""UC 网盘能力适配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet

from .client import UCClient
from .files import UCFileService
from .share import UCShareService
from .upload import UCUploadService
from ...core.cloud import (
    CloudDriveCapability,
    CloudDrivePolicy,
    CloudDriveProvider,
    CloudFile,
)
from ...core.transfer import LocalRapidUploadAdapter


@dataclass
class UCDrive:
    client: UCClient
    page_size: int = 100

    def close(self) -> None:
        self.client.close()


@dataclass(frozen=True)
class UCPlaybackReference:
    template_variables: FrozenSet[str] = frozenset({"file_id"})
    proxy_identifier: str = "file_id"

    @staticmethod
    def reference_values(file_item: CloudFile) -> Dict[str, str]:
        return dict(file_item.playback_values)


def create_uc_provider(drive: UCDrive) -> CloudDriveProvider:
    files = UCFileService(drive.client, drive.page_size)
    share = UCShareService(drive.client, files)
    upload = UCUploadService(drive.client, files)

    return CloudDriveProvider(
        key="uc",
        name="UC网盘",
        config_prefix="uc",
        resource_types=frozenset({"uc"}),
        services={
            CloudDriveCapability.AUTHENTICATION: drive.client,
            CloudDriveCapability.ACCOUNT: drive.client,
            CloudDriveCapability.SHARE_TRANSFER: share,
            CloudDriveCapability.DIRECTORY_READ: files,
            CloudDriveCapability.FILE_QUERY: files,
            CloudDriveCapability.FILE_MUTATION: files,
            CloudDriveCapability.BATCH_FILE_MUTATION: files,
            CloudDriveCapability.PLAYBACK_REFERENCE: UCPlaybackReference(),
            CloudDriveCapability.LOCAL_UPLOAD: upload,
            CloudDriveCapability.RAPID_UPLOAD: LocalRapidUploadAdapter(upload, files, frozenset({"md5"})),
            CloudDriveCapability.FILE_DOWNLOAD: files,
            CloudDriveCapability.QRCODE_AUTH: UCClient,
        },
        policy=CloudDrivePolicy(
            pagination_mode="offset",
            max_page_size=100,
            supports_batch=True,
            max_batch_size=50,
            max_concurrency=2,
        ),
    )
