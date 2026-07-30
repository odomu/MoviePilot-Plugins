"""
API 处理模块
负责插件的外部 API 接口
"""
from typing import Callable

from app.core.config import settings
from app.log import logger
from ...core import CloudDriveCapability, CloudDriveProvider


class ApiHandler:
    """API 处理器"""

    def __init__(
            self,
            pansou_client,
            cloud_drive: CloudDriveProvider,
            save_path: str = "",
            get_data_func: Callable = None,
            save_data_func: Callable = None
    ):
        """
        初始化 API 处理器

        :param pansou_client: PanSou 客户端实例
        :param cloud_drive: 当前网盘提供方
        :param save_path: 默认转存目录
        :param get_data_func: 获取数据的函数
        :param save_data_func: 保存数据的函数
        """
        self._pansou_client = pansou_client
        self._cloud_drive = cloud_drive
        self._share_transfer = (
            cloud_drive.require(CloudDriveCapability.SHARE_TRANSFER)
            if cloud_drive and cloud_drive.supports(CloudDriveCapability.SHARE_TRANSFER)
            else None
        )
        self._cloud_directories = (
            cloud_drive.require(CloudDriveCapability.DIRECTORY_READ)
            if cloud_drive and cloud_drive.supports(CloudDriveCapability.DIRECTORY_READ)
            else None
        )
        self._save_path = save_path
        self._get_data = get_data_func
        self._save_data = save_data_func

    def search(self, keyword: str, apikey: str) -> dict:
        """
        API: 搜索网盘资源

        :param keyword: 搜索关键词
        :param apikey: API 密钥
        :return: 搜索结果
        """
        if apikey != settings.API_TOKEN:
            return {"error": "API密钥错误"}

        if not self._pansou_client:
            return {"error": "PanSou 客户端未初始化"}

        return self._pansou_client.search(
            keyword=keyword,
            cloud_types=["115", "magnet", "ed2k"],
            limit=10,
            refresh=True,
        )

    def transfer(self, share_url: str, save_path: str, apikey: str) -> dict:
        """
        API: 转存分享链接

        :param share_url: 分享链接
        :param save_path: 转存路径
        :param apikey: API 密钥
        :return: 转存结果
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "error": "API密钥错误"}

        if not self._share_transfer:
            return {"success": False, "error": "当前网盘不支持分享转存"}

        success = self._share_transfer.transfer_share(
            share_url, save_path or self._save_path
        )
        return {"success": success}

    def clear_history(self, apikey: str) -> dict:
        """
        API: 清空历史记录

        :param apikey: API 密钥
        :return: 操作结果
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "message": "API密钥错误"}

        if self._save_data:
            self._save_data('history', [])
        logger.info("网盘订阅助手历史记录已清空")
        return {"success": True, "message": "历史记录已清空"}

    def list_directories(self, path: str = "/", apikey: str = "") -> dict:
        """
        API: 列出当前网盘指定路径下的目录

        :param path: 目录路径
        :param apikey: API 密钥
        :return: 目录列表
        """
        if apikey != settings.API_TOKEN:
            return {"success": False, "error": "API密钥错误"}

        if not self._cloud_directories:
            return {"success": False, "error": "当前网盘不支持目录浏览"}

        try:
            directories = self._cloud_directories.list_directories(path)

            # 构建面包屑导航
            breadcrumbs = []
            if path and path != "/":
                parts = [p for p in path.split("/") if p]
                current_path = ""
                breadcrumbs.append({"name": "根目录", "path": "/"})
                for part in parts:
                    current_path = f"{current_path}/{part}"
                    breadcrumbs.append({"name": part, "path": current_path})
            else:
                breadcrumbs.append({"name": "根目录", "path": "/"})

            return {
                "success": True,
                "path": path,
                "breadcrumbs": breadcrumbs,
                "directories": directories
            }
        except Exception as e:
            logger.error(f"列出115目录失败: {e}")
            return {"success": False, "error": str(e)}
