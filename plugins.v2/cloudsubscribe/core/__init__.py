"""插件核心基础能力。"""

from .cloud import (
    CloudDriveCapability,
    CloudDriveCapabilityError,
    CloudDrivePolicy,
    CloudFile,
    CloudDriveProvider,
    CloudDriveRegistry,
    DirectoryListing,
    DirectoryLookup,
)
from .delegation import OwnerDelegator, get_component, resolve_component
from .scraper import MediaScraper

__all__ = [
    "OwnerDelegator",
    "CloudDriveCapability",
    "CloudDriveCapabilityError",
    "CloudDrivePolicy",
    "CloudFile",
    "CloudDriveProvider",
    "CloudDriveRegistry",
    "DirectoryListing",
    "DirectoryLookup",
    "MediaScraper",
    "get_component",
    "resolve_component",
]
