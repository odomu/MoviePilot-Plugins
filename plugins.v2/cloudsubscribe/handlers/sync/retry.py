"""
历史记录重试与现场补偿执行服务。
"""
import copy
import re
from threading import Lock, Thread
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import CloudDriveCapability, CloudFile, OwnerDelegator
from ...core.media import (
    list_subscribes_by_tmdb_id,
    recognize_media,
    tmdb_id_of,
)

_retry_inflight_lock = Lock()
_retry_inflight_keys: set = set()


class HistoryRetryService(OwnerDelegator):
    """负责对历史失败或中断的记录进行就地重试与上下文还原。"""

    def _find_history_record(
            self, record_time: str, share_url: str, file_name: str
    ) -> Optional[Dict[str, Any]]:
        history = (self._get_data("history") or []) if self._get_data else []
        return next(
            (
                item for item in history
                if str(item.get("time") or "") == str(record_time or "")
                   and str(item.get("share_url") or "") == str(share_url or "")
                   and str(item.get("file_name") or "") == str(file_name or "")
            ),
            None,
        )

    @staticmethod
    def _history_retry_key(record_time: str, share_url: str, file_name: str) -> str:
        return "|".join(
            (str(record_time or ""), str(share_url or ""), str(file_name or ""))
        )

    def submit_history_retry(
            self, record_time: str, share_url: str, file_name: str
    ) -> Dict[str, Any]:
        """校验后立即返回，把耗时的重试放到后台线程执行，防止前端页面卡死。"""
        record = self._find_history_record(record_time, share_url, file_name)
        if not record:
            raise ValueError("未找到对应的转存历史记录")
        can_retry, retry_title = self._history_retry_state(record)
        if not can_retry:
            raise ValueError(retry_title)

        key = self._history_retry_key(record_time, share_url, file_name)
        with _retry_inflight_lock:
            if key in _retry_inflight_keys:
                raise ValueError("该记录正在重试中，请稍候")
            _retry_inflight_keys.add(key)

        try:
            processing = copy.deepcopy(record)
            processing["status"] = (
                "下载中"
                if self._is_ed2k_url(str(processing.get("share_url") or ""))
                else "处理中"
            )
            processing.pop("failure_reason", None)
            self.append_history_records([processing], reopen_terminal=True)
            if self._history_changed:
                self._history_changed()
        except Exception:
            with _retry_inflight_lock:
                _retry_inflight_keys.discard(key)
            raise

        Thread(
            target=self._run_history_retry,
            args=(key, str(record_time or ""), str(share_url or ""), str(file_name or "")),
            daemon=True,
            name="cloudsubscribe-history-retry",
        ).start()
        return {"status": "处理中"}

    def _run_history_retry(
            self, key: str, record_time: str, share_url: str, file_name: str
    ) -> None:
        try:
            self.retry_history_record(
                record_time, share_url, file_name, force=True
            )
        except Exception as error:
            logger.error(f"后台重试历史记录失败：{error}")
            self._mark_history_retry_failed(
                record_time, share_url, file_name, str(error) or "重试失败"
            )
        finally:
            with _retry_inflight_lock:
                _retry_inflight_keys.discard(key)
            if self._history_changed:
                self._history_changed()

    def _mark_history_retry_failed(
            self, record_time: str, share_url: str, file_name: str, reason: str
    ) -> None:
        """后台重试异常时把记录落回失败态，避免界面一直停在「处理中」。"""
        history = (self._get_data("history") or []) if self._get_data else []
        record = next(
            (
                item for item in history
                if str(item.get("time") or "") == str(record_time or "")
                   and str(item.get("share_url") or "") == str(share_url or "")
                   and str(item.get("file_name") or "") == str(file_name or "")
            ),
            None,
        )
        if not record or str(record.get("status") or "") == "成功":
            return
        record["status"] = "失败"
        record["failure_reason"] = reason
        record.pop("retrying", None)
        record.pop("finalize_key", None)
        if self._save_data:
            self._save_data("history", history)

    @staticmethod
    def _find_share_file_for_history(files: List[dict], source_sha1: str, source_name: str) -> Optional[dict]:
        source_hash = re.sub(r"[^0-9A-Fa-f]", "", str(source_sha1 or "")).upper()
        source_name = str(source_name or "").strip()
        leaf_files = []

        def collect(items: List[dict]) -> None:
            for item in items or []:
                if item.get("is_dir"):
                    collect(item.get("children") or [])
                else:
                    leaf_files.append(item)

        collect(files)
        for item in leaf_files:
            item_hash = re.sub(
                r"[^0-9A-Fa-f]", "", str(item.get("sha1") or "")
            ).upper()
            if source_hash and item_hash == source_hash:
                return item
        if source_name:
            matched = next(
                (
                    item
                    for item in leaf_files
                    if str(item.get("name") or "").strip() == source_name
                ),
                None,
            )
            if matched:
                return matched
        return leaf_files[0] if len(leaf_files) == 1 else None

    def retry_history_record(
            self,
            record_time: str,
            share_url: str,
            file_name: str,
            force: bool = False,
    ) -> Dict[str, Any]:
        """按持久化历史中的精确记录重新执行平台命名和完整后处理。"""
        history = (self._get_data("history") or []) if self._get_data else []
        record = next(
            (
                item for item in history
                if str(item.get("time") or "") == str(record_time or "")
                   and str(item.get("share_url") or "") == str(share_url or "")
                   and str(item.get("file_name") or "") == str(file_name or "")
            ),
            None,
        )
        if not record:
            raise ValueError("未找到对应的转存历史记录")
        if not force:
            can_retry, retry_title = self._history_retry_state(record)
            if not can_retry:
                raise ValueError(retry_title)
        else:
            record.pop("retrying", None)

        source_sha1 = str(record.get("source_sha1") or "").strip()
        source_name = str(
            record.get("source_file_name") or record.get("file_name") or ""
        ).strip()
        canonical_url = str(record.get("share_url") or "").strip()
        if not source_name or not canonical_url:
            raise ValueError("历史记录缺少文件名或资源链接，无法重试")

        retry_context = self._resolve_history_retry_context(record, source_name)
        subscribe = retry_context["subscribe"]
        mediainfo = retry_context["mediainfo"]
        season = retry_context["season"]
        episode = retry_context["episode"]
        cloud_dir = retry_context["cloud_dir"]
        target_name = retry_context["target_name"]

        final_file_exists = bool(
            self._cloud_query.find_file(cloud_dir, target_name, attempts=1)
        )
        success = final_file_exists
        retry_staging_name = source_name
        if not success:
            expected_size = int(record.get("file_size") or 0)
            expected_sha1 = source_sha1.upper()
            for candidate_name in dict.fromkeys((source_name, target_name)):
                staging_file = self._cloud_query.find_file(
                    self._cloud_transfer_path, candidate_name, attempts=1
                )
                if not staging_file:
                    continue
                actual_size = int(getattr(staging_file, "size", 0) or 0)
                actual_sha1 = str(
                    getattr(staging_file, "sha1", "") or ""
                ).upper()
                if expected_sha1 and actual_sha1 != expected_sha1:
                    continue
                if expected_size > 0 and actual_size != expected_size:
                    continue
                retry_staging_name = str(
                    getattr(staging_file, "name", "") or candidate_name
                )
                if not source_sha1 and actual_sha1:
                    source_sha1 = actual_sha1
                    record["source_sha1"] = actual_sha1
                success = True
                logger.info(
                    f"历史恢复复用目标盘暂存文件："
                    f"{self._cloud_transfer_path.rstrip('/')}/{retry_staging_name}"
                )
                break
        cross_source = None
        cached_source = None
        if record.get("transfer_mode") == "cross":
            source_key = str(record.get("source_drive_key") or "").strip()
            target_key = str(record.get("target_drive_key") or "").strip()
            if not source_key or not target_key:
                raise ValueError("跨盘历史缺少源网盘或目标网盘信息")
            if not self._cloud_drive or target_key != self._cloud_drive.key:
                raise ValueError("跨盘历史的目标网盘与当前转存网盘不一致")
            cached_source = CloudFile(
                id="",
                name=source_name,
                is_directory=False,
                size=int(record.get("file_size") or 0),
                sha1=source_sha1,
                md5=str(record.get("source_md5") or ""),
            )
            cache_info = self._cross_transfer_manager.cache_info(
                source_key, cached_source, verify_checksum=True
            ) if self._cross_transfer_manager else {}
            record.update(cache_info)
            if self._save_data:
                self._save_data("history", history)
            if not success and cache_info.get("cache_status") == "complete":
                task = self._cross_transfer_manager.create_from_cloud_file(
                    source_key,
                    cached_source,
                    target_key,
                    self._cloud_transfer_path,
                    source_name,
                )
                success = self._cross_transfer_manager.wait(task["id"])
                if not success:
                    record.update(self._cross_transfer_manager.cache_info(
                        source_key, cached_source, verify_checksum=False,
                    ))
                    record["failure_reason"] = "缓存恢复上传失败"
                    self._save_data("history", history)
                    raise RuntimeError("缓存完整，但恢复上传到目标网盘失败")
            if not success:
                try:
                    cross_source = self._cloud_drive_registry.get(source_key)
                except KeyError as error:
                    raise ValueError(
                        f"跨盘历史源网盘未就绪：{source_key}"
                    ) from error

        if not success:
            file_id = ""
            if not self._is_ed2k_url(canonical_url):
                share_transfer = (
                    cross_source.require(CloudDriveCapability.SHARE_TRANSFER)
                    if cross_source else self._share_transfer
                )
                status = share_transfer.check_share_status(canonical_url)
                if not status.is_valid:
                    provider_name = cross_source.name if cross_source else "网盘"
                    raise ValueError(f"{provider_name}分享链接无效：{status.status_text}")
                share_files = share_transfer.list_share_files(canonical_url)
                source_file = self._find_share_file_for_history(
                    share_files, source_sha1, source_name
                )
                if not source_file:
                    raise ValueError("原分享中未找到历史记录对应的源文件")
                file_id = str(source_file.get("id") or "")
                if not file_id:
                    raise ValueError("原分享文件缺少文件ID")
                if not source_sha1:
                    source_sha1 = str(source_file.get("sha1") or "")
                source_md5 = str(
                    source_file.get("md5") or record.get("source_md5") or ""
                )
                source_name = str(source_file.get("name") or source_name)
                record["source_file_name"] = source_name
                record["source_sha1"] = source_sha1
                record["source_md5"] = source_md5
                record["file_size"] = int(source_file.get("size") or 0)
                if cached_source:
                    cached_source = CloudFile(
                        id=file_id,
                        name=source_name,
                        is_directory=False,
                        size=int(record.get("file_size") or 0),
                        sha1=source_sha1,
                        md5=source_md5,
                    )
            success = self._transfer_file(
                canonical_url,
                {"id": file_id, "name": source_name,
                 "size": record.get("file_size") or 0,
                 "sha1": source_sha1,
                 "md5": record.get("source_md5") or ""},
                self._cloud_transfer_path, None, source_sha1,
            )

        if not success:
            if cached_source and self._cross_transfer_manager:
                record.update(self._cross_transfer_manager.cache_info(
                    str(record.get("source_drive_key") or ""),
                    cached_source,
                    verify_checksum=False,
                ))
            record["failure_reason"] = "重试转存失败"
            self._save_data("history", history)
            raise RuntimeError("重试转存失败")

        strm_path, pending_key = self._generate_or_queue_strm(
            canonical_url,
            cloud_dir,
            target_name,
            mediainfo,
            source_sha1=source_sha1,
            file_size=int(record.get("file_size") or 0),
            subscribe_id=getattr(subscribe, "id", None),
            success_episodes=(
                [episode] if mediainfo.type == MediaType.TV and episode else [1]
            ),
            season=season if mediainfo.type == MediaType.TV else None,
            notification_episodes=(
                [episode] if mediainfo.type == MediaType.TV and episode else None
            ),
            staging_dir="" if final_file_exists else self._cloud_transfer_path,
            staging_name=retry_staging_name,
        )
        if not strm_path and not pending_key:
            record["status"] = "失败"
            record["failure_reason"] = "文件已转存但后处理任务登记失败"
            self._save_data("history", history)
            raise RuntimeError("文件已转存，但无法登记后处理任务")
        record["file_name"] = target_name
        record["cloud_dir"] = cloud_dir
        record["source_file_name"] = source_name
        record["source_sha1"] = source_sha1
        record["tmdb_id"] = mediainfo.tmdb_id
        record.pop("failure_reason", None)
        if cached_source and self._cross_transfer_manager:
            record.update(self._cross_transfer_manager.cache_info(
                str(record.get("source_drive_key") or ""),
                cached_source,
                verify_checksum=False,
            ))
        # 终态关联先落定，再做装饰性字段更新：避免装饰性异常阻断后处理终态写入
        if pending_key:
            record["finalize_key"] = pending_key
            record["status"] = (
                "下载中" if self._is_ed2k_url(canonical_url) else "处理中"
            )
        else:
            record.pop("finalize_key", None)
            record["status"] = "成功"
            self._media_server_notifier.notify(
                path=strm_path,
                mediainfo=mediainfo,
                file_name=target_name,
            )
        try:
            target_sub = subscribe or SimpleNamespace(
                name=str(record.get("title") or getattr(mediainfo, "title", "") or ""),
                year=str(record.get("year") or getattr(mediainfo, "year", "") or ""),
            )
            effective_title = str(
                getattr(subscribe, "name", None)
                or getattr(target_sub, "name", None)
                or getattr(mediainfo, "title", None)
                or record.get("title")
                or ""
            ).strip()
            if effective_title:
                record["title"] = effective_title
            if getattr(target_sub, "year", None) or getattr(mediainfo, "year", None):
                record["year"] = str(
                    getattr(target_sub, "year", None) or mediainfo.year
                )
            if getattr(mediainfo, "get_poster_image", None) and mediainfo.get_poster_image():
                record["image"] = mediainfo.get_poster_image()
        except Exception as error:
            logger.debug(f"历史记录重试元数据更新失败：{error}")

        if subscribe and not pending_key:
            success_episodes = [1]
            if mediainfo.type == MediaType.TV:
                success_episodes = [episode] if episode else []
            if success_episodes:
                self._subscribe_handler.check_and_finish_subscribe(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=success_episodes,
                )
        if pending_key:
            self.append_history_records([record], reopen_terminal=True)
        else:
            self._save_data("history", history)
        if record["status"] == "成功":
            self._record_platform_transfer_histories([record])
        logger.info(
            f"历史记录后处理完成：{cloud_dir.rstrip('/')}/{target_name}，"
            f"状态：{record['status']}"
        )
        return {
            "status": record["status"],
            "pending_key": pending_key,
            "strm_path": str(strm_path or ""),
            "cloud_dir": cloud_dir,
            "file_name": target_name,
        }

    def _resolve_history_retry_context(
            self, record: Dict[str, Any], source_name: str
    ) -> Dict[str, Any]:
        """按当前订阅和规则还原历史记录的最终处理上下文。"""
        title = str(record.get("title") or "").strip()
        if not title or not source_name:
            raise ValueError("历史记录缺少媒体名称或源文件名")

        media_type = (
            MediaType.TV
            if str(record.get("type") or "") == "电视剧"
            else MediaType.MOVIE
        )
        season = int(record.get("season") or 1) if media_type == MediaType.TV else None
        episode = (
            int(record.get("episode") or 0) or None
            if media_type == MediaType.TV
            else None
        )
        meta = MetaInfo(title)
        meta.type = media_type
        meta.year = record.get("year")
        if season is not None:
            meta.begin_season = season
        if episode is not None:
            meta.begin_episode = episode
        mediainfo = recognize_media(
            self._chain,
            meta=meta,
            mtype=media_type,
            tmdb_id=record.get("tmdb_id"),
            cache=True,
        )
        if not mediainfo:
            raise ValueError(f"无法识别历史记录媒体：{title}")

        subscribe = None
        tmdb_id = mediainfo.tmdb_id or record.get("tmdb_id")
        if tmdb_id:
            try:
                candidates = list_subscribes_by_tmdb_id(
                    SubscribeOper(), tmdb_id, season
                )
                if not candidates and media_type == MediaType.MOVIE:
                    candidates = [
                        item
                        for item in (SubscribeOper().list() or [])
                        if tmdb_id_of(item) == int(tmdb_id)
                    ]
                subscribe = next(
                    (
                        item
                        for item in candidates
                        if str(getattr(item, "type", "")) == media_type.value
                           and (
                                   media_type != MediaType.TV
                                   or int(getattr(item, "season", 1) or 1) == season
                           )
                    ),
                    None,
                )
            except Exception as error:
                logger.warning(f"查询历史记录对应订阅失败：{title}，{error}")

        target_subscribe = subscribe or SimpleNamespace(
            name=title or (mediainfo and mediainfo.title),
            year=record.get("year") or (mediainfo and mediainfo.year),
            media_category=None,
            media_category_id=None,
            include=None,
            exclude=None,
            quality=None,
            resolution=None,
            effect=None,
            audio_quality=None,
            audio_format=None,
            min_bitrate=None,
            min_bit_depth=None,
            min_sample_rate=None,
            filter=None,
            filter_groups=None,
            sites=None,
            custom_words=None,
        )
        cloud_dir, target_name = self._platform_target(
            root_path=self._CLOUD_MEDIA_ROOT,
            subscribe=target_subscribe,
            mediainfo=mediainfo,
            source_name=source_name,
            season=season,
            episode=episode,
        )
        return {
            "subscribe": subscribe,
            "target_subscribe": target_subscribe,
            "mediainfo": mediainfo,
            "season": season,
            "episode": episode,
            "cloud_dir": cloud_dir,
            "target_name": target_name,
        }
