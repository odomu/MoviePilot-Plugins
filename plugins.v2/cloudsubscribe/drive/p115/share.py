"""115 分享链接校验与文件遍历。"""

import copy
import time
from dataclasses import asdict
from itertools import islice
from typing import Any, Dict, List, Tuple

from app.log import logger

from ..common import DRIVE_RETRY_EXCEPTIONS, should_skip_season_dir
from ...core.cloud import ShareLinkStatus
from ...core.delegation import OwnerDelegator
from ...utils.share_expiry import (
    is_expired_share_error,
    is_share_expired,
    mark_share_expired,
)

try:
    from p115client.tool.iterdir import share_iterdir, share_search_iter
    from p115client.util import share_extract_payload

    P115_AVAILABLE = True
except ImportError:
    P115_AVAILABLE = False


class ShareService(OwnerDelegator):
    """处理115分享链接及分享文件树。"""

    def extract_share_info(self, url: str) -> Dict[str, str]:
        """
        解析分享链接，获取 share_code 和 receive_code（带缓存）

        :param url: 115 分享链接
        :return: {"share_code": ..., "receive_code": ...}
        """
        if not P115_AVAILABLE:
            return {}

        with self._share_cache_lock:
            cached = self._share_info_cache.get(url)
            if cached:
                return dict(cached)

        try:
            payload = share_extract_payload(url)
            result = {
                "share_code": payload.get("share_code", ""),
                "receive_code": payload.get("receive_code", "")
            }
            self._share_info_cache[url] = result
            return result
        except Exception as e:
            logger.error(f"解析分享链接失败: {e}")
            return {}

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        """
        检查分享链接的状态（是否有效、过期、失效等）

        :param share_url: 115 分享链接
        :return: ShareLinkStatus 对象，包含详细的状态信息
        """
        # 默认返回无效状态
        status = ShareLinkStatus()

        if self.is_offline_url(share_url):
            file_info = (
                self.parse_ed2k_link(share_url)
                if self.is_ed2k_url(share_url)
                else self.parse_magnet_link(share_url)
            )
            if not file_info:
                status.error_message = "无效的 ED2K 或 Magnet 链接"
                return status
            if not self.client:
                status.error_message = "客户端未初始化"
                return status
            if not self._login_checked and not self.check_login():
                status.error_message = "115 登录状态无效"
                return status
            if not self.is_vip:
                status.error_message = "当前 115 账号不是会员，无法使用离线下载"
                return status
            status.is_valid = True
            status.file_count = 1 if self.is_ed2k_url(share_url) else 0
            status.share_info = {
                "share_title": file_info["name"],
                "resource_type": (
                    "ed2k" if self.is_ed2k_url(share_url) else "magnet"
                ),
            }
            return status

        # 先检查转存阶段确认过的失效记录：share_snap 仍可能返回有效
        if is_share_expired(share_url):
            status.is_expired = True
            status.error_code = 4100018
            status.error_message = "分享链接已过期（转存时确认）"
            return status
        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        cached = self._share_status_cache.get(share_url)
        if isinstance(cached, dict):
            logger.debug(f"复用115分享状态缓存：{share_url}")
            return ShareLinkStatus(**cached)

        if not self.client:
            status.error_message = "客户端未初始化"
            return status

        if not share_code:
            status.error_message = "无效的分享链接格式"
            return status

        try:
            # 使用 share_snap 接口检查分享状态
            payload = {
                "share_code": share_code,
                "receive_code": receive_code or "",
                "cid": 0,
                "limit": 1,  # 只获取1条记录，用于验证
                "offset": 0,
            }
            resp = self._rate_limited_call(
                self.client.share_snap,
                payload,
                **self._ios_request_kwargs(app=False),
            )

            # 检查响应状态
            state = resp.get("state")

            if state is True or state == 1:
                # 分享有效
                status.is_valid = True
                status.error_code = 0

                # 获取分享信息
                data = resp.get("data", {})
                share_info = data.get("shareinfo", {})
                file_list = data.get("list", [])

                status.file_count = int(data.get("count", len(file_list)))
                status.share_info = {
                    "share_title": share_info.get("share_title", ""),
                    "share_state": share_info.get("share_state", ""),
                    "file_count": status.file_count,
                    "create_time": share_info.get("create_time", ""),
                    "expire_time": share_info.get("expire_time", ""),
                    "user_name": share_info.get("user_name", ""),
                }
            else:
                # 分享无效，解析错误信息
                status.is_valid = False
                status.error_code = resp.get("errno", resp.get("errcode", -1))
                status.error_message = resp.get("error", resp.get("message", "未知错误"))

                # 根据错误码判断具体原因
                error_msg_lower = status.error_message.lower()
                error_msg = status.error_message

                # 判断是否过期
                if "过期" in error_msg or "expired" in error_msg_lower:
                    status.is_expired = True

                # 判断是否取消
                if "取消" in error_msg or "cancel" in error_msg_lower:
                    status.is_cancelled = True

                # 判断是否删除
                if "删除" in error_msg or "不存在" in error_msg or "delete" in error_msg_lower:
                    status.is_deleted = True

                logger.info(f"分享链接无效: {status.error_message} (errno: {status.error_code})")

        except Exception as e:
            status.error_message = f"检查分享状态异常: {str(e)}"
            logger.error(status.error_message)

        if not status.error_message.startswith("检查分享状态异常"):
            # 平台缓存走 pickle：只存纯数据，避免热重载后旧类实例无法序列化。
            self._share_status_cache[share_url] = asdict(status)
            if not status.is_valid:
                self._discard_share_file_cache(share_url)
        return status

    def is_share_valid(self, share_url: str) -> bool:
        """
        快速检查分享链接是否有效

        :param share_url: 115 分享链接
        :return: True 表示有效，False 表示无效或失效
        """
        status = self.check_share_status(share_url)
        return status.is_valid

    @staticmethod
    def _is_share_expired_error(error_code: Any, error_msg: Any) -> bool:
        """识别“链接已过期/分享失效”类错误。"""
        return is_expired_share_error(error_code, error_msg)

    def list_share_files(
            self,
            share_url: str,
            cid: int = 0,
            max_depth: int = 3,
            target_season: int = None,
            log_prefix: str = "",
    ) -> List[dict]:
        """
        列出分享链接内的文件

        :param share_url: 115 分享链接
        :param cid: 目录 ID，0 为根目录
        :param max_depth: 最大递归深度
        :param target_season: 目标季数，用于优化递归（跳过明显不匹配的目录）
        :param log_prefix: 并发任务日志前缀
        :return: 文件列表
        """
        prefix = f"{log_prefix} " if log_prefix else ""
        if self.is_ed2k_url(share_url):
            file_info = self.parse_ed2k_link(share_url)
            if not file_info:
                logger.error(f"{prefix}无效的 ED2K 文件链接")
                return []
            return [{
                "id": file_info["hash"],
                "url": file_info["url"],
                "name": file_info["name"],
                "size": file_info["size"],
                "is_dir": False,
                "sha1": "",
                "pick_code": "",
                "resource_type": "ed2k",
            }]
        if self.is_magnet_url(share_url):
            logger.debug(f"{prefix}Magnet 文件清单需等待115离线下载完成后读取")
            return []
        season_cache_val = (
            tuple(sorted(int(s) for s in target_season if str(s).isdigit()))
            if isinstance(target_season, (set, list, tuple))
            else target_season
        )
        cache_key = f"{share_url}|{cid}|{max_depth}|{season_cache_val}"
        cached = self._share_file_cache.get(cache_key)
        if cached is not None:
            logger.debug(f"{prefix}复用115分享文件缓存：{share_url}")
            return copy.deepcopy(cached)

        if not self.client:
            return []

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error(f"{prefix}无效的分享链接或解析失败")
            return []

        files = self._list_share_files_recursive(
            share_code=share_code,
            receive_code=receive_code,
            cid=cid,
            depth=1,
            max_depth=max_depth,
            target_season=target_season,
            log_prefix=log_prefix,
        )
        if files:
            self._share_file_cache[cache_key] = copy.deepcopy(files)
        return files

    def list_share_directory(
            self, share_url: str, parent_id: str = ""
    ) -> List[dict]:
        """列出分享中的当前目录，并向预览接口保留真实异常。"""
        if not P115_AVAILABLE:
            raise RuntimeError("p115client 未安装")
        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")
        if not share_code or not receive_code:
            raise ValueError("无效的 115 分享链接或缺少提取码")
        rows = self.rate_limiter.call(
            lambda: list(share_iterdir(
                self.client or None,
                share_code=share_code,
                receive_code=receive_code,
                cid=int(parent_id or 0),
                app="web",
                cooldown=self.rate_limiter.min_interval,
                max_workers=0,
            ))
        )
        return [
            {
                "id": str(item.get("id") or ""),
                "parent_id": str(item.get("parent_id") or ""),
                "name": str(item.get("name") or ""),
                "size": int(item.get("size") or 0),
                "is_dir": bool(item.get("is_dir")),
                "sha1": str(item.get("sha1") or ""),
            }
            for item in rows
        ]

    def search_share_files(
            self, share_url: str, keyword: str, limit: int = 200
    ) -> List[dict]:
        """在分享内按关键词服务端搜索文件，适用于超大合集（免逐层遍历）。"""
        if not P115_AVAILABLE:
            raise RuntimeError("p115client 未安装")
        if not self.client:
            raise RuntimeError("115 客户端未初始化，请先配置 Cookies")
        text = str(keyword or "").strip()
        if not text:
            raise ValueError("搜索关键词不能为空")
        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")
        if not share_code or not receive_code:
            raise ValueError("无效的 115 分享链接或缺少提取码")
        rows = self.rate_limiter.call(
            lambda: list(islice(
                share_search_iter(
                    self.client,
                    share_code=share_code,
                    receive_code=receive_code,
                    search_value=text,
                    page_size=115,
                ),
                max(1, int(limit)),
            ))
        )
        return [
            {
                "id": str(item.get("id") or ""),
                "parent_id": str(item.get("parent_id") or ""),
                "name": str(item.get("name") or ""),
                "size": int(item.get("size") or 0),
                "is_dir": bool(item.get("is_dir")),
                "sha1": str(item.get("sha1") or ""),
            }
            for item in rows
        ]

    def _list_share_files_recursive(
            self,
            share_code: str,
            receive_code: str,
            cid: int = 0,
            depth: int = 1,
            max_depth: int = 3,
            target_season: int = None,
            log_prefix: str = "",
            parent_path: str = "",
    ) -> List[dict]:
        """递归列出分享文件（带速率限制和季数过滤优化）"""
        if depth > max_depth:
            return []

        files = []
        try:
            rows = self.rate_limiter.call(
                lambda: list(share_iterdir(
                    self.client,
                    share_code=share_code,
                    receive_code=receive_code,
                    cid=cid,
                    app="web",
                    cooldown=self.rate_limiter.min_interval,
                    max_workers=0,
                ))
            )

            for item in rows:
                file_info = {
                    "id": str(item.get("id", "")),
                    "name": item.get("name", ""),
                    "size": item.get("size", 0),
                    "is_dir": item.get("is_dir", False),
                    "sha1": item.get("sha1", ""),
                    "pick_code": item.get("pick_code", ""),
                }

                # 目录仅用于递归定位文件，不进入分享文件结果。
                if file_info["is_dir"]:
                    if depth >= max_depth:
                        continue
                    dir_name = file_info["name"]

                    # 如果指定了目标季数，跳过明显不匹配的季目录
                    if target_season is not None and should_skip_season_dir(dir_name, target_season):
                        prefix = f"{log_prefix} " if log_prefix else ""
                        season_desc = (
                            f"S{'/S'.join(str(s) for s in sorted(int(x) for x in target_season if str(x).isdigit()))}"
                            if isinstance(target_season, (set, list, tuple))
                            else f"S{target_season}"
                        )
                        logger.info(
                            f"{prefix}跳过非目标季目录：{dir_name}（目标：{season_desc}）"
                        )
                        continue
                    sub_cid = int(item.get("id", 0))
                    sub_parent_path = (
                        f"{parent_path}/{dir_name}".strip("/") if parent_path else dir_name
                    )
                    children = self._list_share_files_recursive(
                        share_code=share_code,
                        receive_code=receive_code,
                        cid=sub_cid,
                        depth=depth + 1,
                        max_depth=max_depth,
                        target_season=target_season,
                        log_prefix=log_prefix,
                        parent_path=sub_parent_path,
                    )
                    files.extend(children)
                    continue

                if parent_path:
                    file_info["parent_path"] = parent_path
                    file_info["relative_path"] = f"{parent_path}/{file_info['name']}"
                files.append(file_info)

        except Exception as e:
            prefix = f"{log_prefix} " if log_prefix else ""
            logger.error(f"{prefix}列出分享文件失败：{e}")

        return files

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        """
        转存整个分享链接到指定目录

        :param share_url: 115 分享链接
        :param save_path: 保存路径
        :return: 是否成功
        """
        if self.is_offline_url(share_url):
            return self.add_offline_download(share_url, save_path)

        if not self.client:
            return False

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return False

        # 获取目标目录 CID
        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return False

        logger.debug(f"转存分享到目录 ID: {parent_id} ({save_path})")

        # 执行转存 (file_id=0 表示转存所有内容)
        return self._do_transfer(
            share_code=share_code,
            receive_code=receive_code,
            file_id="0",
            parent_id=parent_id,
            save_path=save_path,
            share_url=share_url,
        )

    def transfer_file(
            self,
            share_url: str,
            file_id: str,
            save_path: str,
            target_name: str = None,
            source_sha1: str = None,
    ) -> bool:
        """转存单个文件。"""
        if self.is_offline_url(share_url):
            return self.add_offline_download(
                share_url, save_path, target_name=target_name
            )
        if not self.client:
            return False
        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")
        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return False
        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return False
        return self._do_transfer(
            share_code=share_code,
            receive_code=receive_code,
            file_id=str(file_id),
            parent_id=parent_id,
            save_path=save_path,
            share_url=share_url,
        ) is not False

    def transfer_files_batch(
            self,
            share_url: str,
            file_ids: List[str],
            save_path: str,
            batch_size: int = 20,
            batch_interval: float = 3.0,
            rename_items: Dict[str, Dict[str, str]] = None,
            **kwargs: Any,
    ) -> Tuple[List[str], List[str]]:
        """
        批量转存分享中的多个文件，减少 API 调用次数以避免风控

        :param share_url: 115 分享链接
        :param file_ids: 文件 ID 列表
        :param save_path: 保存路径
        :param batch_size: ED2K 离线批量大小；115 分享固定按 115 个文件分页
        :param batch_interval: 批次之间的间隔时间（秒），默认 3 秒
        :return: (成功的 file_ids 列表, 失败的 file_ids 列表)
        """
        success_ids: List[str] = []
        failed_ids: List[str] = []
        batch_size = int(batch_size)
        if self.is_offline_url(share_url):
            ed2k_items = []
            for file_id in file_ids:
                rename_item = (rename_items or {}).get(str(file_id), {})
                ed2k_items.append({
                    "url": rename_item.get("url") or share_url,
                    "target_name": rename_item.get("target_name"),
                })
            success_hashes, _ = self.add_offline_downloads_batch(
                ed2k_items,
                save_path=save_path,
                batch_size=batch_size,
                batch_interval=batch_interval,
            )
            success_set = set(success_hashes)
            success_ids = [
                file_id for file_id in file_ids
                if self._normalize_hash(file_id) in success_set
            ]
            failed_ids = [file_id for file_id in file_ids if file_id not in success_ids]
            return success_ids, failed_ids

        if not self.client:
            return success_ids, file_ids

        if not file_ids:
            return success_ids, failed_ids

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return success_ids, file_ids

        # 获取目标目录 CID（只需获取一次）
        parent_id = self.get_pid_by_path(save_path, mkdir=True)
        if parent_id == -1:
            logger.error(f"无法获取或创建目标目录: {save_path}")
            return success_ids, file_ids

        page_size = max(1, min(batch_size, self.SHARE_TRANSFER_PAGE_SIZE))
        total_pages = (len(file_ids) + page_size - 1) // page_size
        stop_requested = kwargs.get("stop_requested")
        logger.debug(
            f"115 批量转存：共 {len(file_ids)} 个文件，"
            f"分 {total_pages} 页（每页最多 {page_size} 个）"
        )

        for page_index in range(0, len(file_ids), page_size):
            if stop_requested and stop_requested():
                logger.debug(
                    f"收到停止请求，剩余 {len(file_ids) - page_index} 个文件未转存"
                )
                break
            page_ids = file_ids[page_index:page_index + page_size]
            page_num = page_index // page_size + 1
            if total_pages > 1:
                logger.debug(
                    f"处理第 {page_num}/{total_pages} 页，包含 {len(page_ids)} 个文件"
                )

            # 直接接收进指定目录（由调用方给出订阅名+年度+季），「已存在」视为已就绪
            if self._do_transfer(
                    share_code=share_code,
                    receive_code=receive_code,
                    file_id=",".join(page_ids),
                    parent_id=parent_id,
                    save_path=save_path,
                    share_url=share_url,
            ) is not False:
                success_ids.extend(page_ids)
            else:
                failed_ids.extend(page_ids)
            if page_index + len(page_ids) < len(file_ids) and batch_interval:
                time.sleep(max(0.0, min(float(batch_interval), 60.0)))

        logger.debug(f"批量转存完成: 成功 {len(success_ids)} 个，失败 {len(failed_ids)} 个")
        return success_ids, failed_ids


    def _do_transfer(
            self,
            share_code: str,
            receive_code: str,
            file_id: str,
            parent_id: int,
            save_path: str,
            max_retries: int = None,
            share_url: str = "",
    ) -> bool:
        """
        执行实际转存操作（带重试）

        :param share_code: 分享码
        :param receive_code: 接收码
        :param file_id: 文件ID，"0" 表示转存全部
        :param parent_id: 目标目录 ID
        :param save_path: 保存路径（用于日志）
        :param max_retries: 最大重试次数
        :return: 是否成功
        """
        if max_retries is None:
            max_retries = self.DEFAULT_MAX_RETRIES
        if is_share_expired(share_url):
            logger.warning(f"115 分享已确认失效，跳过转存：{share_url}")
            return False

        payload = {
            "share_code": share_code,
            "receive_code": receive_code,
            "file_id": file_id,
            "cid": parent_id,
            "is_check": 0,
        }

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                resp = self._rate_limited_call(
                    self.client.share_receive,
                    payload,
                    retry_exceptions=DRIVE_RETRY_EXCEPTIONS,
                    max_retries=0,
                    **self._ios_request_kwargs(app=False),
                )

                if resp.get("state"):
                    if file_id == "0":
                        logger.debug(f"115 转存完成：{save_path}")
                    else:
                        file_count = len([item for item in str(file_id).split(",") if item])
                        logger.debug(f"115 文件转存完成：{file_count} 个，目录 {save_path}")
                    return True
                else:
                    error_msg = resp.get("error", "未知错误")
                    error_code = resp.get("errno", resp.get("errcode", 0))

                    # 检查是否是重复文件
                    if "重复" in error_msg or "已存在" in error_msg:
                        logger.debug(
                            f"115返回文件已存在，等待目标目录复核: {file_id}，"
                            f"错误码: {error_code}"
                        )
                        return None

                    # 检查是否是可重试的错误（如限流）
                    if error_code in (990001, 990002, 990009):  # 常见的限流错误码
                        if attempt < max_retries:
                            wait_time = min(30.0, 1.0 * (2 ** attempt))
                            logger.debug(f"遇到限流，{wait_time}秒后重试 (尝试 {attempt + 1}/{max_retries + 1})")
                            time.sleep(wait_time)
                            continue

                    if self._is_share_expired_error(error_code, error_msg):
                        mark_share_expired(share_url, str(error_msg), error_code)
                        return False

                    logger.error(f"转存失败: {error_msg} (错误码: {error_code})")
                    return False

            except DRIVE_RETRY_EXCEPTIONS as e:
                if attempt < max_retries:
                    wait_time = min(30.0, 0.75 * (2 ** attempt))
                    logger.debug(f"转存异常: {e}, {wait_time:.1f}秒后重试 (尝试 {attempt + 1}/{max_retries + 1})")
                    time.sleep(wait_time)
                else:
                    logger.error(f"转存过程中发生异常: {e}")
                    return False
            except Exception as e:
                logger.error(f"转存过程中发生不可重试异常: {e}")
                return False

        return False

    def _discard_share_file_cache(self, share_url: str) -> int:
        keys = [
            key for key in self._share_file_cache
            if key.startswith(f"{share_url}|")
        ]
        for key in keys:
            self._share_file_cache.delete(key)
        return len(keys)

    def clear_share_cache(self) -> Dict[str, int]:
        """清空分享解析、状态和不可变文件列表缓存。"""
        with self._share_cache_lock:
            counts = {
                "share_info": len(self._share_info_cache),
                "share_status": len(self._share_status_cache),
                "share_files": len(self._share_file_cache),
            }
            self._share_info_cache.clear()
            self._share_status_cache.clear()
            self._share_file_cache.clear()
        return counts
