"""网盘提供方能力规范与运行时注册表。"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterable, Iterator, Mapping, Protocol, runtime_checkable


class CloudDriveCapability(str, Enum):
    """订阅流程可按需使用的独立网盘能力。"""

    AUTHENTICATION = "authentication"
    ACCOUNT = "account"
    SHARE_TRANSFER = "share_transfer"
    OFFLINE_DOWNLOAD = "offline_download"
    DIRECTORY_READ = "directory_read"
    FILE_QUERY = "file_query"
    FILE_MUTATION = "file_mutation"
    CHECKSUM_RENAME = "checksum_rename"
    BATCH_FILE_MUTATION = "batch_file_mutation"
    PLAYBACK_REFERENCE = "playback_reference"
    OFFLINE_TASKS = "offline_tasks"
    QRCODE_AUTH = "qrcode_auth"
    CACHE_MAINTENANCE = "cache_maintenance"


class CloudDriveCapabilityError(RuntimeError):
    """当前网盘不支持调用方要求的能力。"""


@dataclass(frozen=True)
class CloudDrivePolicy:
    """提供方级执行边界，调用层据此决定并发、分页和批量方式。"""

    pagination_mode: str = "none"
    max_page_size: int = 0
    supports_batch: bool = False
    max_batch_size: int = 1
    supports_cancel: bool = False
    max_concurrency: int = 1
    cache_ttl_seconds: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mode = str(self.pagination_mode or "none").strip().lower()
        if mode not in {"none", "offset", "cursor"}:
            raise ValueError(f"不支持的网盘分页模式：{mode}")
        object.__setattr__(self, "pagination_mode", mode)
        object.__setattr__(self, "max_page_size", max(0, int(self.max_page_size or 0)))
        object.__setattr__(self, "max_batch_size", max(1, int(self.max_batch_size or 1)))
        object.__setattr__(self, "max_concurrency", max(1, int(self.max_concurrency or 1)))
        object.__setattr__(
            self,
            "cache_ttl_seconds",
            {
                str(key): max(0, int(value or 0))
                for key, value in dict(self.cache_ttl_seconds or {}).items()
            },
        )


@dataclass(frozen=True)
class CloudFile(Mapping[str, Any]):
    """网盘工作流使用的统一文件模型，提供方私有响应仅保留在适配器内部。"""

    id: str
    name: str
    is_directory: bool
    size: int = 0
    sha1: str = ""
    playback_values: Mapping[str, str] = field(default_factory=dict)
    native: Any = field(default=None, repr=False, compare=False)

    def __getitem__(self, key: str) -> Any:
        values = {
            "id": self.id,
            "name": self.name,
            "is_dir": self.is_directory,
            "size": self.size,
            "sha1": self.sha1,
        }
        return values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("id", "name", "is_dir", "size", "sha1"))

    def __len__(self) -> int:
        return 5


@dataclass(frozen=True)
class DirectoryLookup:
    """目录解析结果；checked=False 表示提供方未能完成确认。"""

    checked: bool
    directory_id: str | None = None


@dataclass(frozen=True)
class DirectoryListing:
    """目录读取结果；空目录与读取失败由 checked 明确区分。"""

    checked: bool
    files: tuple[CloudFile, ...] = ()


@dataclass
class ShareLinkStatus:
    """网盘分享链接的统一可用状态。"""

    is_valid: bool = False
    is_expired: bool = False
    is_cancelled: bool = False
    is_deleted: bool = False
    error_code: int = 0
    error_message: str = ""
    file_count: int = 0
    share_info: Dict[str, Any] = field(default_factory=dict)

    @property
    def status_text(self) -> str:
        if self.is_valid:
            return "有效"
        if self.is_expired:
            return "已过期"
        if self.is_cancelled:
            return "已取消"
        if self.is_deleted:
            return "文件已删除"
        return self.error_message or "未知状态"


@runtime_checkable
class AuthenticationOperations(Protocol):
    def check_login(self) -> bool: ...

    def reset_api_call_count(self) -> None: ...

    def get_api_call_count(self) -> int: ...


@runtime_checkable
class AccountOperations(Protocol):
    @property
    def is_vip(self) -> bool: ...

    def get_account_info(self) -> Dict[str, Any]: ...


@runtime_checkable
class ShareTransferOperations(Protocol):
    def extract_share_info(self, share_url: str) -> Dict[str, Any]: ...

    def check_share_status(self, share_url: str) -> ShareLinkStatus: ...

    def list_share_files(self, share_url: str, **kwargs: Any) -> list: ...

    def transfer_share(self, share_url: str, save_path: str) -> bool: ...

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str, target_name: str
    ) -> bool: ...

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs: Any
    ) -> tuple: ...


@runtime_checkable
class OfflineDownloadOperations(Protocol):
    def is_offline_url(self, url: str) -> bool: ...

    def is_ed2k_url(self, url: str) -> bool: ...

    def is_magnet_url(self, url: str) -> bool: ...

    def parse_ed2k_link(self, url: str) -> Dict[str, Any]: ...

    def parse_magnet_link(self, url: str, **kwargs: Any) -> Dict[str, Any]: ...

    def add_offline_download(self, url: str, save_path: str, **kwargs: Any) -> bool: ...


@runtime_checkable
class DirectoryReadOperations(Protocol):
    def resolve_directory(
            self, path: str, create: bool = False
    ) -> DirectoryLookup: ...

    def list_directory(self, directory_id: str) -> DirectoryListing: ...

    def list_directories(self, path: str) -> list[Dict[str, str]]: ...


@runtime_checkable
class FileQueryOperations(Protocol):
    def list_files_recursive(self, path: str, **kwargs: Any) -> list[CloudFile]: ...

    def find_file(
            self, path: str, file_name: str, **kwargs: Any
    ) -> CloudFile | None: ...

    def find_file_strict(self, path: str, file_name: str) -> CloudFile | None: ...

    def get_cached_file(self, path: str, file_name: str) -> CloudFile | None: ...


@runtime_checkable
class FileMutationOperations(Protocol):
    def rename_file(
            self, path: str, item: CloudFile, target_name: str
    ) -> bool: ...

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> CloudFile | None: ...

    def delete_file(self, file_id: str) -> bool: ...


@runtime_checkable
class ChecksumRenameOperations(Protocol):
    def rename_file_by_checksum(
            self,
            path: str,
            checksum: str,
            target_name: str,
            algorithm: str = "sha1",
            **kwargs: Any,
    ) -> bool: ...


@runtime_checkable
class BatchFileMutationOperations(Protocol):
    def rename_files(self, path: str, items: dict) -> dict[str, CloudFile]: ...

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]: ...

    def delete_files(self, file_ids: list[str]) -> set[str]: ...


@runtime_checkable
class PlaybackReferenceOperations(Protocol):
    @property
    def template_variables(self) -> FrozenSet[str]: ...

    def reference_values(self, file_item: CloudFile) -> Dict[str, str]: ...


@runtime_checkable
class OfflineTaskOperations(Protocol):
    def get_offline_task_list_snapshot(
            self, force: bool = False
    ) -> Dict[str, Any]: ...

    def get_offline_tasks_snapshot(self, force: bool = False) -> Dict[str, Any]: ...

    def restart_offline_task(self, task_id: str) -> bool: ...

    def delete_offline_task(self, task_id: str, **kwargs: Any) -> bool: ...

    def delete_offline_tasks(self, task_ids: list, **kwargs: Any) -> int: ...


@runtime_checkable
class QrCodeAuthOperations(Protocol):
    def create_qrcode_login(self, client_type: str) -> Dict[str, Any]: ...

    def check_qrcode_login(self, **kwargs: Any) -> Dict[str, Any]: ...


@runtime_checkable
class CacheMaintenanceOperations(Protocol):
    def get_cache_stats(self) -> Dict[str, Any]: ...

    def clear_cache(self) -> Dict[str, int]: ...


CAPABILITY_CONTRACTS = {
    CloudDriveCapability.AUTHENTICATION: AuthenticationOperations,
    CloudDriveCapability.ACCOUNT: AccountOperations,
    CloudDriveCapability.SHARE_TRANSFER: ShareTransferOperations,
    CloudDriveCapability.OFFLINE_DOWNLOAD: OfflineDownloadOperations,
    CloudDriveCapability.DIRECTORY_READ: DirectoryReadOperations,
    CloudDriveCapability.FILE_QUERY: FileQueryOperations,
    CloudDriveCapability.FILE_MUTATION: FileMutationOperations,
    CloudDriveCapability.CHECKSUM_RENAME: ChecksumRenameOperations,
    CloudDriveCapability.BATCH_FILE_MUTATION: BatchFileMutationOperations,
    CloudDriveCapability.PLAYBACK_REFERENCE: PlaybackReferenceOperations,
    CloudDriveCapability.OFFLINE_TASKS: OfflineTaskOperations,
    CloudDriveCapability.QRCODE_AUTH: QrCodeAuthOperations,
    CloudDriveCapability.CACHE_MAINTENANCE: CacheMaintenanceOperations,
}


def _implements_contract(service: Any, contract: type) -> bool:
    """验证能力对象的公开成员"""

    for name, declaration in contract.__dict__.items():
        if name.startswith("_"):
            continue
        try:
            value = getattr(service, name)
        except AttributeError:
            return False
        if callable(declaration) and not callable(value):
            return False
    return True


@dataclass(frozen=True)
class CloudDriveProvider:
    """一个网盘及其实际具备的能力集合。

    同一个服务对象可以实现多个能力；未注册的能力不会出现在提供方上，
    调用方必须通过 :meth:`require` 获取后再使用。
    """

    key: str
    name: str
    services: Mapping[CloudDriveCapability, Any]
    resource_types: FrozenSet[str] = frozenset()
    config_prefix: str = ""
    policy: CloudDrivePolicy = field(default_factory=CloudDrivePolicy)

    def __post_init__(self) -> None:
        key = str(self.key or "").strip().lower()
        if not key:
            raise ValueError("网盘提供方 key 不能为空")
        object.__setattr__(self, "key", key)
        object.__setattr__(
            self,
            "resource_types",
            frozenset(
                str(value or "").strip().lower()
                for value in self.resource_types
                if str(value or "").strip()
            ),
        )
        for capability, service in self.services.items():
            contract = CAPABILITY_CONTRACTS.get(capability)
            if contract is None:
                raise ValueError(f"未定义的网盘能力：{capability}")
            if not _implements_contract(service, contract):
                raise TypeError(
                    f"{self.name}的 {capability.value} 服务不符合接口 {contract.__name__}"
                )

    @property
    def capabilities(self) -> FrozenSet[CloudDriveCapability]:
        return frozenset(self.services)

    def supports(self, capability: CloudDriveCapability) -> bool:
        return capability in self.services

    def supports_resource_type(self, resource_type: str) -> bool:
        return str(resource_type or "").strip().lower() in self.resource_types

    def require(self, capability: CloudDriveCapability) -> Any:
        service = self.services.get(capability)
        if service is None:
            raise CloudDriveCapabilityError(
                f"{self.name}不支持能力：{capability.value}"
            )
        return service


class CloudDriveRegistry:
    """按稳定 key 管理网盘提供方，不隐式选择第一个实现。"""

    def __init__(self, providers: Iterable[CloudDriveProvider] = ()):
        self._providers: Dict[str, CloudDriveProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: CloudDriveProvider, replace: bool = False) -> None:
        key = str(provider.key or "").strip().lower()
        if not key:
            raise ValueError("网盘提供方 key 不能为空")
        if key in self._providers and not replace:
            raise ValueError(f"网盘提供方重复注册：{key}")
        self._providers[key] = provider

    def get(self, key: str) -> CloudDriveProvider:
        normalized = str(key or "").strip().lower()
        provider = self._providers.get(normalized)
        if provider is None:
            raise KeyError(f"网盘提供方未注册：{normalized or '<empty>'}")
        return provider

    def available(self) -> list:
        return list(self._providers.values())
