"""MoviePilot页面与API适配。"""

from .history import HistoryApi
from .qrcode import QRCodeService
from .registration import MoviePilotRegistration
from .vue import PluginApi

__all__ = ["HistoryApi", "MoviePilotRegistration", "PluginApi", "QRCodeService"]
