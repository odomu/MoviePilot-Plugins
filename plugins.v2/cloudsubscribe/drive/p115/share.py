"""115 分享链接校验与文件遍历。"""

import copy
import threading
import time
from typing import Any, Dict, List, Tuple

from app.log import logger

from ...core.cloud import ShareLinkStatus
from ...core.delegation import OwnerDelegator

try:
    from p115client.tool.iterdir import share_iterdir
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

        cached = self._share_status_cache.get(share_url)
        if isinstance(cached, ShareLinkStatus):
            logger.debug(f"复用115分享状态缓存：{share_url}")
            return copy.deepcopy(cached)

        if not self.client:
            status.error_message = "客户端未初始化"
            return status

        # 解析分享链接
        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code:
            status.error_message = "无效的分享链接格式"
            return status

        try:
            # 使用 share_snap 接口检查分享状态
            self.rate_limiter.wait()
            payload = {
                "share_code": share_code,
                "receive_code": receive_code or "",
                "cid": 0,
                "limit": 1,  # 只获取1条记录，用于验证
                "offset": 0,
            }
            resp = self.client.share_snap(payload, **self._ios_request_kwargs(app=False))

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
            self._share_status_cache[share_url] = copy.deepcopy(status)
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

    def list_share_files(
            self,
            share_url: str,
            cid: int = 0,
            max_depth: int = 3,
            target_season: int = None
    ) -> List[dict]:
        """
        列出分享链接内的文件

        :param share_url: 115 分享链接
        :param cid: 目录 ID，0 为根目录
        :param max_depth: 最大递归深度
        :param target_season: 目标季数，用于优化递归（跳过明显不匹配的目录）
        :return: 文件列表
        """
        if self.is_ed2k_url(share_url):
            file_info = self.parse_ed2k_link(share_url)
            if not file_info:
                logger.error("无效的 ED2K 文件链接")
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
            logger.debug("Magnet 文件清单需等待115离线下载完成后读取")
            return []

        cache_key = f"{share_url}|{cid}|{max_depth}|{target_season}"
        cached = self._share_file_cache.get(cache_key)
        if cached is not None:
            logger.debug(f"复用115分享文件缓存：{share_url}")
            return copy.deepcopy(cached)

        if not self.client:
            return []

        info = self.extract_share_info(share_url)
        share_code = info.get("share_code")
        receive_code = info.get("receive_code")

        if not share_code or not receive_code:
            logger.error("无效的分享链接或解析失败")
            return []

        files = self._list_share_files_recursive(
            share_code=share_code,
            receive_code=receive_code,
            cid=cid,
            depth=1,
            max_depth=max_depth,
            target_season=target_season
        )
        if files:
            self._share_file_cache[cache_key] = copy.deepcopy(files)
        return files

    def _list_share_files_recursive(
            self,
            share_code: str,
            receive_code: str,
            cid: int = 0,
            depth: int = 1,
            max_depth: int = 3,
            target_season: int = None
    ) -> List[dict]:
        """递归列出分享文件（带速率限制和季数过滤优化）"""
        if depth > max_depth:
            return []

        files = []
        try:
            # 速率限制
            self.rate_limiter.wait()

            iterator = share_iterdir(
                self.client,
                share_code=share_code,
                receive_code=receive_code,
                cid=cid,
                app="web",
            )

            for item in iterator:
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

                    # 优化：如果指定了目标季数，跳过明显不匹配的季目录
                    if target_season is not None:
                        skip_dir = self._should_skip_season_dir(dir_name, target_season)
                        if skip_dir:
                            logger.info(f"跳过非目标季目录: {dir_name} (目标: S{target_season})")
                            continue

                    sub_cid = int(item.get("id", 0))
                    children = self._list_share_files_recursive(
                        share_code=share_code,
                        receive_code=receive_code,
                        cid=sub_cid,
                        depth=depth + 1,
                        max_depth=max_depth,
                        target_season=target_season
                    )
                    files.extend(children)
                    continue

                files.append(file_info)

        except Exception as e:
            logger.error(f"列出分享文件失败: {e}")

        return files

    def _should_skip_season_dir(self, dir_name: str, target_season: int) -> bool:
        """
        判断是否应该跳过该目录（明显是其他季的目录）

        :param dir_name: 目录名
        :param target_season: 目标季数
        :return: True 表示应跳过，False 表示需要递归
        """
        import re

        # 常见的季数目录命名模式
        patterns = [
            r'[Ss]eason\s*(\d+)',  # Season 1, season1
            r'[Ss](\d+)',  # S1, s01
            r'第(\d+)季',  # 第1季
            r'第([一二三四五六七八九十]+)季',  # 第一季
        ]

        # 中文数字映射
        cn_num_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                      '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}

        for pattern in patterns:
            match = re.search(pattern, dir_name)
            if match:
                season_str = match.group(1)
                # 转换中文数字
                if season_str in cn_num_map:
                    found_season = cn_num_map[season_str]
                else:
                    try:
                        found_season = int(season_str)
                    except ValueError:
                        continue

                # 如果目录明确是其他季，跳过
                if found_season != target_season:
                    return True
                else:
                    # 明确是目标季，不跳过
                    return False

        # 目录名没有明显的季数标识，不跳过（可能包含多季或其他内容）
        return False

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

        logger.info(f"转存分享到目录 ID: {parent_id} ({save_path})")

        # 执行转存 (file_id=0 表示转存所有内容)
        return self._do_transfer(
            share_code=share_code,
            receive_code=receive_code,
            file_id="0",
            parent_id=parent_id,
            save_path=save_path
        )

    def transfer_file(
            self,
            share_url: str,
            file_id: str,
            save_path: str,
            target_name: str = None,
            source_sha1: str = None,
    ) -> bool:
        """转存单个文件，并按源 SHA1 应用平台文件名。"""
        if self.is_offline_url(share_url):
            return self.add_offline_download(
                share_url, save_path, target_name=target_name
            )
        if not self.client:
            return False
        if target_name:
            existing, _ = self.rename_files_by_sha1_batch(
                save_path,
                {
                    str(file_id): {
                        "sha1": source_sha1,
                        "target_name": target_name,
                    }
                },
                [str(file_id)],
                log_unresolved=False,
            )
            if str(file_id) in existing:
                logger.debug(
                    f"115 暂存目录已存在目标文件，跳过重复转存：{target_name}"
                )
                return True
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
        success = self._do_transfer(
            share_code=share_code, receive_code=receive_code, file_id=file_id,
            parent_id=parent_id, save_path=save_path
        )
        if success is None:
            if target_name and self.rename_file_by_sha1(
                    save_path, source_sha1, target_name
            ):
                return True
            source_hash = self._normalize_hash(source_sha1)
            if target_name and len(source_hash) == 40:
                recovered = self._recover_duplicate_transfer(
                    share_code=share_code,
                    receive_code=receive_code,
                    file_ids=[str(file_id)],
                    save_path=save_path,
                    rename_items={
                        str(file_id): {
                            "sha1": source_hash,
                            "target_name": target_name,
                        }
                    },
                )
                if str(file_id) in recovered:
                    logger.info(
                        f"115重复文件已通过独立目录恢复：{target_name}"
                    )
                    return True
                logger.info(
                    f"115目标文件已存在或仍在系统处理，"
                    f"已转入文件后处理：{target_name}"
                )
                return True
            return False
        if success and target_name:
            if not self.rename_file_by_sha1(save_path, source_sha1, target_name):
                logger.info(f"转存已完成，文件重命名进入后处理队列：{target_name}")
        return success

    def transfer_files_batch(
            self,
            share_url: str,
            file_ids: List[str],
            save_path: str,
            batch_size: int = 20,
            batch_interval: float = 3.0,
            rename_items: Dict[str, Dict[str, str]] = None,
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

        if rename_items:
            precheck_ids = [
                file_id
                for file_id in file_ids
                if str(
                    (rename_items.get(str(file_id)) or {}).get("target_name")
                    or ""
                ).strip()
            ]
            existing, unresolved = self.rename_files_by_sha1_batch(
                save_path, rename_items, precheck_ids, log_unresolved=False
            )
            if existing:
                success_ids.extend(
                    file_id for file_id in file_ids if str(file_id) in existing
                )
                logger.debug(
                    f"115 暂存目录已存在 {len(existing)} 个目标文件，"
                    "本轮直接复用并跳过重复转存"
                )
            unresolved_set = set(unresolved)
            precheck_set = {str(file_id) for file_id in precheck_ids}
            file_ids = [
                file_id
                for file_id in file_ids
                if str(file_id) not in precheck_set
                   or str(file_id) in unresolved_set
            ]
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
        logger.debug(
            f"115 批量转存：共 {len(file_ids)} 个文件，"
            f"分 {total_pages} 页（每页最多 {page_size} 个）"
        )

        for page_index in range(0, len(file_ids), page_size):
            page_ids = file_ids[page_index:page_index + page_size]
            page_num = page_index // page_size + 1

            # 使用逗号分隔多个文件 ID
            file_id_str = ",".join(page_ids)

            logger.debug(
                f"处理第 {page_num}/{total_pages} 页，包含 {len(page_ids)} 个文件"
            )

            success = self._do_transfer(
                share_code=share_code,
                receive_code=receive_code,
                file_id=file_id_str,
                parent_id=parent_id,
                save_path=save_path
            )

            if success is True:
                success_ids.extend(page_ids)
                logger.debug(f"第 {page_num} 页转存成功")
            elif success is None:
                ready, unresolved = self.rename_files_by_sha1_batch(
                    save_path, rename_items or {}, page_ids
                )
                recovered = {}
                if unresolved:
                    recovered = self._recover_duplicate_transfer(
                        share_code=share_code,
                        receive_code=receive_code,
                        file_ids=unresolved,
                        save_path=save_path,
                        rename_items=rename_items or {},
                    )
                    ready.update(recovered)
                verified_ids = [fid for fid in page_ids if str(fid) in ready]
                success_ids.extend(verified_ids)
                failed_ids.extend(fid for fid in page_ids if fid not in verified_ids)
                logger.warning(
                    f"第 {page_num} 页返回文件已存在，目录复核后确认 "
                    f"{len(verified_ids) - len(recovered)} 个，独立目录恢复 "
                    f"{len(recovered)} 个，仍失败 {len(page_ids) - len(verified_ids)} 个"
                )
            else:
                logger.warning(
                    f"第 {page_num} 页批量转存失败，停止逐文件重试以避免放大风控"
                )
                failed_ids.extend(page_ids)
            if page_index + len(page_ids) < len(file_ids) and batch_interval:
                time.sleep(max(0.0, min(float(batch_interval), 60.0)))
        if rename_items and any(
                str(item.get("target_name") or "").strip()
                for item in rename_items.values()
        ):
            self.rename_files_by_sha1_batch(save_path, rename_items, success_ids)

        logger.debug(f"批量转存完成: 成功 {len(success_ids)} 个，失败 {len(failed_ids)} 个")
        return success_ids, failed_ids

    def _recover_duplicate_transfer(
            self,
            share_code: str,
            receive_code: str,
            file_ids: List[str],
            save_path: str,
            rename_items: Dict[str, Dict[str, str]],
    ) -> Dict[str, dict]:
        """目标目录不存在重复文件时，使用独立目录重新接收并移回目标目录。"""
        if not file_ids:
            return {}
        retry_name = (
            f"CloudSubscribe接收重试-{int(time.time() * 1000)}-"
            f"{threading.get_ident()}"
        )
        retry_path = f"{str(save_path).rstrip('/')}/{retry_name}"
        retry_cid = self.get_pid_by_path(retry_path, mkdir=True)
        if retry_cid == -1:
            logger.warning("115重复文件恢复失败：无法创建独立接收目录")
            return {}

        logger.info(
            f"115目标目录未找到已接收文件，改用独立目录重试："
            f"{retry_path}，文件 {len(file_ids)} 个"
        )
        retry_result = self._do_transfer(
            share_code=share_code,
            receive_code=receive_code,
            file_id=",".join(str(file_id) for file_id in file_ids),
            parent_id=retry_cid,
            save_path=retry_path,
            max_retries=1,
        )
        if retry_result is False:
            logger.warning("115独立目录重试失败，保留目录供后续复核")
            self._cleanup_empty_retry_directory(retry_path, retry_cid)
            return {}

        files = []
        for attempt in range(3):
            files = self.list_files_by_cid(retry_cid)
            if files:
                break
            if attempt < 2:
                time.sleep(1)

        files_by_sha1 = {
            self._normalize_hash(item.get("sha1") or item.get("sha")): item
            for item in files
            if len(self._normalize_hash(item.get("sha1") or item.get("sha"))) == 40
        }
        files_by_name = {
            str(item.get("name") or item.get("n") or "").strip(): item
            for item in files
            if str(item.get("name") or item.get("n") or "").strip()
        }
        pending = {}
        for file_id in file_ids:
            key = str(file_id)
            rename_item = rename_items.get(key) or {}
            target_name = str(rename_item.get("target_name") or "").strip()
            source_sha1 = self._normalize_hash(rename_item.get("sha1"))
            item = files_by_sha1.get(source_sha1) if len(source_sha1) == 40 else None
            if not item and target_name:
                item = files_by_name.get(target_name)
            if not item or not target_name:
                continue
            pending[key] = {
                "item": dict(item),
                "target_name": target_name,
            }

        renamed = self.rename_file_items_batch(retry_path, pending)
        moved = self.move_file_items_batch(renamed, save_path)
        if moved:
            logger.info(
                f"115独立目录恢复完成：{len(moved)}/{len(file_ids)} 个文件已移回 "
                f"{save_path}"
            )
        self._cleanup_empty_retry_directory(retry_path, retry_cid)
        return moved

    def _cleanup_empty_retry_directory(self, retry_path: str, retry_cid: Any) -> None:
        """仅在独立接收目录确认为空时移入回收站，避免残留额外文件夹。"""
        checked = False
        items = []
        for attempt in range(3):
            checked, items = self.list_files_by_cid_checked(retry_cid)
            if not checked or not items:
                break
            if attempt < 2:
                time.sleep(1)
        if not checked or items:
            return
        if self.delete_file(retry_cid):
            self.path_cache.invalidate(retry_path)
            logger.info(f"115空接收重试目录已清理：{retry_path}")

    def _do_transfer(
            self,
            share_code: str,
            receive_code: str,
            file_id: str,
            parent_id: int,
            save_path: str,
            max_retries: int = None
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
                self.rate_limiter.wait()
                resp = self.client.share_receive(
                    payload,
                    **self._ios_request_kwargs(app=False),
                )

                if resp.get("state"):
                    if file_id == "0":
                        logger.info(f"115 转存完成：{save_path}")
                    else:
                        file_count = len([item for item in str(file_id).split(",") if item])
                        logger.debug(f"115 文件转存完成：{file_count} 个，目录 {save_path}")
                    return True
                else:
                    error_msg = resp.get("error", "未知错误")
                    error_code = resp.get("errno", resp.get("errcode", 0))

                    # 检查是否是重复文件
                    if "重复" in error_msg or "已存在" in error_msg:
                        logger.warning(
                            f"115返回文件已存在，等待目标目录复核: {file_id}，"
                            f"错误码: {error_code}"
                        )
                        return None

                    # 检查是否是可重试的错误（如限流）
                    if error_code in (990001, 990002, 990009):  # 常见的限流错误码
                        if attempt < max_retries:
                            wait_time = (attempt + 1) * 2  # 递增等待时间
                            logger.warning(f"遇到限流，{wait_time}秒后重试 (尝试 {attempt + 1}/{max_retries + 1})")
                            time.sleep(wait_time)
                            continue

                    logger.error(f"转存失败: {error_msg} (错误码: {error_code})")
                    return False

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    wait_time = (attempt + 1) * 1.5
                    logger.warning(f"转存异常: {e}, {wait_time:.1f}秒后重试 (尝试 {attempt + 1}/{max_retries + 1})")
                    time.sleep(wait_time)
                else:
                    logger.error(f"转存过程中发生异常: {e}")
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
