"""电影订阅搜索、匹配与转存流程。"""

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...drive.common import format_size


class MovieSyncProcessor(OwnerDelegator):
    """处理电影订阅同步。"""

    def process_movie_subscribe(
            self,
            subscribe,
            history: List[dict],
            transfer_details: List[Dict[str, Any]],
            transferred_count: int,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            manual_upgrade: bool = False,
            transient_target: bool = False,
    ) -> int:
        """
        处理单个电影订阅

        :param subscribe: 订阅对象
        :param history: 历史记录列表
        :param transfer_details: 转存详情列表
        :param transferred_count: 当前已转存数量
        :return: 更新后的转存数量
        """
        try:
            if self._stop_requested():
                return transferred_count
            logger.debug(f"处理电影订阅：{subscribe.name} ({subscribe.year})")

            # 加载该订阅的历史积分花费（用 tmdb_id 作为唯一标识）
            sub_key = self.subscription_budget_key(subscribe, MediaType.MOVIE)
            if hasattr(self._search_handler, 'reset_sub_spent_points'):
                self._search_handler.reset_sub_spent_points(sub_key)

            # 检查历史记录是否已成功转存
            movie_history_score = -1  # -1 表示未转存过
            movie_history_size = 0
            subscribe_tmdb_id = str(getattr(subscribe, "tmdbid", None) or "").strip()
            subscribe_year = str(getattr(subscribe, "year", None) or "").strip()
            for h in history:
                if h.get("type") != "电影" or h.get("status") != "成功":
                    continue
                history_tmdb_id = str(h.get("tmdb_id") or "").strip()
                if subscribe_tmdb_id and history_tmdb_id:
                    same_movie = subscribe_tmdb_id == history_tmdb_id
                else:
                    history_year = str(h.get("year") or "").strip()
                    same_movie = (
                            h.get("title") == subscribe.name
                            and bool(subscribe_year and history_year)
                            and subscribe_year == history_year
                    )
                if not same_movie:
                    continue
                score = int(h.get("rule_score") or 0)
                if score > movie_history_score:
                    movie_history_score = score
                    movie_history_size = self._resource_size_bytes(
                        h.get("file_size") or h.get("size")
                    )

            # 原生 best_version 决定是否为洗版订阅，插件范围进一步限制处理对象。
            is_best_version = manual_upgrade or self._is_cloud_upgrade_subscribe(subscribe)

            # 生成元数据
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.type = MediaType.MOVIE

            # 识别媒体信息
            mediainfo: MediaInfo = self._recognize_media_once(
                (
                    "subscribe", MediaType.MOVIE.value, subscribe.tmdbid,
                    subscribe.doubanid, subscribe.name, subscribe.year, 0, True,
                ),
                meta=meta,
                mtype=MediaType.MOVIE,
                tmdbid=subscribe.tmdbid,
                doubanid=subscribe.doubanid,
                cache=True,
            )
            if not mediainfo:
                logger.warning(f"无法识别媒体信息：{subscribe.name}")
                return transferred_count

            # 洗版电影需要先建立现有版本基线，否则同名文件只能被当作普通订阅完成。
            existing_movie = None
            upgrade_target_exists = False
            if is_best_version:
                emby_has_size = False
                manual_movie = (
                        (getattr(subscribe, "_manual_media_baseline", {}) or {}).get("movie")
                        or {}
                )
                if manual_movie:
                    manual_name = str(manual_movie.get("file_name") or "").strip()
                    manual_size = self._resource_size_bytes(manual_movie.get("file_size"))
                    manual_score = self._get_mp_rule_score(
                        manual_name, manual_size, subscribe, 0, mediainfo
                    )
                    movie_history_score = max(movie_history_score, manual_score)
                    if manual_size:
                        movie_history_size = manual_size
                    logger.info(
                        f"电影 {subscribe.name} 洗版基线采用所选媒体库内容："
                        f"{manual_name}，评分 {manual_score}，{format_size(manual_size)}"
                    )
                for media_item in self._emby_media_resolver.movie_media(
                        chain=self._chain, mediainfo=mediainfo
                ):
                    media_file = Path(str(media_item.get("path") or ""))
                    media_size = self._resource_size_bytes(media_item.get("size"))
                    emby_has_size = emby_has_size or media_size > 0
                    emby_score = self._get_mp_rule_score(
                        media_file.name, media_size, subscribe, 0, mediainfo
                    )
                    if media_size:
                        movie_history_size = media_size
                    if emby_score > movie_history_score:
                        movie_history_score = emby_score
                    if media_size or emby_score > 0:
                        logger.info(
                            f"电影 {subscribe.name} 洗版基线采用 Emby 媒体："
                            f"{media_file.name}，评分 {emby_score}，{format_size(media_size)}"
                        )
                existing_movie = self._timed_sync_call(
                    "cloud_scan",
                    self._find_cloud_movie_file,
                    subscribe,
                    mediainfo,
                )
                if existing_movie:
                    upgrade_target_exists = True
                    existing_dir, existing_name, existing_file = existing_movie
                    if self._strm_generate_enabled:
                        existing_strm = self._generate_strm(
                            existing_dir,
                            existing_name,
                            target_file=existing_file,
                        )
                        if not existing_strm:
                            logger.warning(
                                f"电影 {subscribe.name} 真实网盘文件已存在，"
                                "但 STRM 修复尚未完成"
                            )
                    if not emby_has_size:
                        file_name, target_file = existing_name, existing_file
                        existing_size = int(getattr(target_file, "size", 0) or 0)
                        existing_score = self._get_mp_rule_score(
                            file_name, existing_size, subscribe, 0, mediainfo
                        )
                        if existing_score >= movie_history_score:
                            movie_history_score = existing_score
                            movie_history_size = existing_size
                        logger.info(
                            f"电影 {subscribe.name} Emby 未提供有效大小，"
                            f"网盘回退基线：{movie_history_score} "
                            f"（{file_name}，{format_size(existing_size)}）"
                        )
                    logger.info(
                        f"电影 {subscribe.name} 洗版中，"
                        f"现有平台优先级 {movie_history_score}"
                    )
                else:
                    movie_history_score = -1
                    movie_history_size = 0
                    logger.info(
                        f"电影 {subscribe.name} 未找到真实网盘旧文件，"
                        "本轮无法建立洗版基线"
                    )
                    if manual_upgrade:
                        return transferred_count

            if movie_history_score >= 0 and not is_best_version:
                if transient_target:
                    logger.debug(f"电影 {subscribe.name} 已有成功转存历史，无需重复处理")
                    return transferred_count
                self._set_task_phase(subscribe, "同步历史完成状态", 95)
                self._subscribe_handler.check_and_finish_subscribe(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=[1],
                )
                if hasattr(self._search_handler, "clear_sub_points"):
                    self._search_handler.clear_sub_points(sub_key)
                logger.debug(
                    f"电影 {subscribe.name} 已有成功转存历史，"
                    f"已同步订阅进度与完成状态"
                )
                return transferred_count
            if not manual_resources:
                release_date = self._calendar_date(mediainfo.release_date)
                if release_date and release_date > datetime.date.today():
                    self.defer_subscribe_until(
                        subscribe,
                        release_date,
                        f"电影上映日期为 {release_date.isoformat()}",
                    )
                    logger.debug(
                        f"电影 {mediainfo.title_year} 尚未上映，"
                        f"延期至 {release_date.isoformat()} 后检查"
                    )
                    return transferred_count
            self._set_task_phase(subscribe, "检查网盘内容", 25)

            if not is_best_version:
                existing_movie = self._timed_sync_call(
                    "cloud_scan",
                    self._find_cloud_movie_file,
                    subscribe,
                    mediainfo,
                )
                if existing_movie:
                    cloud_dir, file_name, _ = existing_movie
                    logger.info(
                        f"目标电影已存在，结束订阅：{cloud_dir.rstrip('/')}/{file_name}"
                    )
                    self._generate_strm(cloud_dir, file_name)
                    self._scrape_metadata(cloud_dir, file_name, mediainfo)
                    if not transient_target:
                        self._subscribe_handler.check_and_finish_subscribe(
                            subscribe=subscribe,
                            mediainfo=mediainfo,
                            success_episodes=[1],
                        )
                    if hasattr(self._search_handler, "clear_sub_points"):
                        self._search_handler.clear_sub_points(sub_key)
                    return transferred_count

            # 手动资源直接进入现有匹配转存链，否则查询搜索源。
            self._set_task_phase(subscribe, "搜索候选资源", 45)
            if manual_resources:
                p115_results = [dict(resource) for resource in manual_resources]
                logger.info(
                    f"手动处理电影 {mediainfo.title}：收到 {len(p115_results)} 个资源链接"
                )
            else:
                p115_results = self._search_handler.search_resources(
                    mediainfo=mediainfo,
                    media_type=MediaType.MOVIE,
                    subscribe=subscribe,
                )

            if not p115_results:
                if self._stop_requested():
                    return transferred_count
                logger.info(f"未找到电影 {mediainfo.title} 的可处理资源")
                return transferred_count

            self._set_task_phase(subscribe, "筛选候选资源", 60)
            search_label = self._search_handler._search_label(
                mediainfo, MediaType.MOVIE
            )
            result_sources = "/".join(dict.fromkeys(
                str(resource.get("source") or "unknown").upper()
                for resource in p115_results
            ))
            logger.debug(
                f"[{search_label}][{result_sources}] 找到候选资源："
                f"{self._format_resource_summary(p115_results)}"
            )

            # 遍历搜索结果，尝试找到并转存电影
            movie_transferred = False
            for resource_index, resource in enumerate(p115_results):
                if movie_transferred or self._stop_requested():
                    break
                self._set_task_phase(
                    subscribe,
                    f"检查候选资源 {resource_index + 1}/{len(p115_results)}",
                    60 + int((resource_index + 1) / len(p115_results) * 22),
                )

                share_url = resource.get("url", "")
                resource_title = resource.get("title", "")

                # 检查是否是刚搜索出尚未真正解锁的延期解锁 HDHive 资源
                if (resource.get("need_unlock") or resource.get("need_access")) and not share_url:
                    slug = resource.get("slug")
                    if slug and upgrade_target_exists and movie_history_score >= 0:
                        preview_name = str(
                            getattr(self._search_handler, "_resource_filter_title", lambda value: "")(
                                resource
                            ) or resource_title
                        ).strip()
                        preview_matched, preview_score = self._search_handler.select_file_candidate(
                            [{
                                "name": preview_name,
                                "size": self._resource_size_bytes(resource.get("size")),
                            }],
                            mediainfo,
                            subscribe,
                        )
                        preview_size = self._resource_size_bytes(resource.get("size"))
                        preview_decision = self._should_upgrade_candidate(
                            movie_history_score,
                            preview_score,
                            movie_history_size,
                            preview_size,
                        )
                        if preview_matched and preview_size and not preview_decision[0]:
                            logger.info(
                                f"电影 {mediainfo.title} 候选解锁前跳过："
                                f"{preview_decision[1]}"
                            )
                            continue
                share_url = self._resolve_candidate_resource_url(
                    p115_results,
                    resource_index,
                    resource,
                    search_label,
                    log_prefix=f"[{search_label}][HDHIVE]",
                )
                if self._stop_requested():
                    break

                if not share_url:
                    continue

                if not self._is_supported_resource(resource, share_url):
                    logger.warning(
                        f"跳过当前同步链不支持的资源类型 "
                        f"{self._supported_resource_type(resource, share_url)}：{resource_title}"
                    )
                    continue

                action = "检查离线资源" if self._is_offline_url(share_url) else "检查分享"
                logger.info(
                    f"{action}：{resource_title} - "
                    f"{self._resource_log_reference(share_url)}"
                )

                try:
                    if self._is_magnet_url(share_url):
                        if not self._validate_resource_url(
                                share_url, resource_label="Magnet 链接"
                        ):
                            continue
                        provider_name = str(
                            (resource.get("magnet_metadata") or {}).get("display_name")
                            or resource_title
                        ).strip()
                        matched, current_score = self._search_handler.select_file_candidate(
                            [{"name": provider_name, "size": resource.get("size") or 0}],
                            mediainfo,
                            subscribe,
                        )
                        if not matched:
                            logger.debug(f"Magnet 元数据未通过平台优先级规则：{provider_name}")
                            continue
                        magnet_size = self._resource_size_bytes(
                            resource.get("size")
                            or (resource.get("magnet_metadata") or {}).get("size")
                        )
                        magnet_upgrade = False
                        if upgrade_target_exists and movie_history_score >= 0:
                            magnet_upgrade, magnet_reason = self._should_upgrade_candidate(
                                movie_history_score,
                                current_score,
                                movie_history_size,
                                magnet_size,
                            )
                            if not magnet_upgrade:
                                logger.info(
                                    f"电影 {mediainfo.title} Magnet 洗版候选跳过：{magnet_reason}"
                                )
                                continue
                        self._set_task_phase(subscribe, "提交离线下载", 90)
                        if not self._reserve_transfer_slots(1):
                            break
                        pending_key = self._queue_magnet_package(
                            resource, share_url, subscribe, mediainfo, sub_key=sub_key,
                            upgrade=magnet_upgrade,
                            upgrade_mode=self._upgrade_mode,
                            upgrade_baseline={
                                "movie": {
                                    "score": movie_history_score,
                                    "size": movie_history_size,
                                }
                            } if magnet_upgrade else {},
                            transient_target=transient_target,
                        )
                        if not pending_key:
                            self._release_transfer_slots(1)
                            continue
                        history.append(self._build_transfer_history_item(
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            status="下载中",
                            share_url=share_url,
                            file_name=provider_name,
                            source_file_name=provider_name,
                            cloud_dir=self._cloud_transfer_path.rstrip('/') or "/",
                            resource=resource,
                            rule_score=current_score,
                            upgrade=magnet_upgrade,
                            finalize_key=pending_key,
                        ))
                        movie_transferred = True
                        logger.info(f"Magnet 已进入下载后真实文件匹配：{provider_name}")
                        continue

                    share_files = self._validated_resource_files(
                        share_url,
                        resource_title=resource_title,
                    )
                    if not share_files:
                        continue

                    matched_file, current_score = self._match_movie_file(
                        share_files, mediainfo, subscribe
                    )

                    if matched_file:
                        file_name = matched_file.get('name', '')
                        logger.debug(f"找到匹配文件：{file_name}")

                        is_upgrade = False
                        # 洗版模式下检查是否需要升级资源
                        upgrade_old_size = movie_history_size
                        if upgrade_target_exists and movie_history_score >= 0:
                            candidate_size = self._resource_size_bytes(
                                matched_file.get("size")
                            )
                            should_upgrade, reason = self._should_upgrade_candidate(
                                movie_history_score,
                                current_score,
                                movie_history_size,
                                candidate_size,
                            )
                            if not should_upgrade:
                                logger.info(
                                    f"电影 {mediainfo.title} 洗版候选跳过：{reason}"
                                )
                                continue
                            is_upgrade = True
                            logger.info(
                                f"电影 {mediainfo.title} 洗版：{reason}"
                            )

                        save_dir, target_name = self._platform_target(
                            self._CLOUD_MEDIA_ROOT, subscribe, mediainfo, file_name
                        )
                        if is_upgrade and self._upgrade_mode == "coexist":
                            target_name = self._coexist_target_name(
                                target_name,
                                file_name,
                                self._resource_size_bytes(matched_file.get("size")),
                                matched_file.get("sha1") or "",
                            )
                        logger.info(
                            f"网盘转存暂存: {self._cloud_transfer_path}/{file_name}，"
                            f"完成后移动到: {save_dir}/{target_name}"
                        )

                        if self._stop_requested():
                            break
                        self._set_task_phase(subscribe, "转存匹配文件", 90)
                        if not self._reserve_transfer_slots(1):
                            logger.info(
                                f"已达单次同步上限 {self._max_transfer_per_sync}，"
                                f"跳过电影转存：{mediainfo.title_year}"
                            )
                            break
                        try:
                            success = self._timed_sync_call(
                                "share_transfer",
                                self._share_transfer.transfer_file,
                                share_url=share_url,
                                file_id=matched_file.get("id"),
                                save_path=self._cloud_transfer_path,
                                target_name=(
                                    None if self._is_offline_url(share_url)
                                    else target_name
                                ),
                                source_sha1=matched_file.get("sha1"),
                            )
                        except Exception:
                            self._release_transfer_slots(1)
                            raise
                        if not success:
                            self._release_transfer_slots(1)

                        # 记录历史
                        history_item = self._build_transfer_history_item(
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            status=self._transfer_history_status(success, share_url),
                            share_url=share_url,
                            file_name=target_name,
                            source_file_name=file_name,
                            cloud_dir=save_dir,
                            resource=resource,
                            file_size=self._resource_size_bytes(matched_file.get("size")),
                            source_sha1=matched_file.get("sha1") or "",
                            rule_score=current_score,
                            upgrade=is_upgrade,
                        )
                        history.append(history_item)

                        if success:
                            transferred_count += 1
                            movie_transferred = True
                            movie_history_score = current_score
                            self._set_task_phase(subscribe, "登记文件后处理", 95)
                            strm_path, pending_key = self._generate_or_queue_strm(
                                share_url,
                                save_dir,
                                target_name,
                                mediainfo,
                                source_sha1=matched_file.get("sha1"),
                                file_size=self._resource_size_bytes(matched_file.get("size")),
                                subscribe_id=(
                                    None if transient_target else getattr(subscribe, "id", None)
                                ),
                                success_episodes=[] if transient_target else [1],
                                sub_key=sub_key,
                                staging_dir=self._cloud_transfer_path,
                                staging_name=file_name,
                                upgrade=is_upgrade,
                                upgrade_mode=self._upgrade_mode,
                                upgrade_old_cloud_dir=(
                                    existing_movie[0] if existing_movie else ""
                                ),
                                upgrade_old_file_name=(
                                    existing_movie[1] if existing_movie else ""
                                ),
                                upgrade_old_file_id=(
                                    getattr(existing_movie[2], "id", "")
                                    if existing_movie else ""
                                ),
                                upgrade_old_size=upgrade_old_size,
                            )
                            if pending_key:
                                history_item["finalize_key"] = pending_key
                                history_item["status"] = (
                                    "下载中" if self._is_offline_url(share_url)
                                    else "处理中"
                                )
                            if strm_path:
                                self._media_server_notifier.notify(
                                    path=strm_path,
                                    mediainfo=mediainfo,
                                    file_name=target_name,
                                )
                            logger.info(
                                f"成功转存电影：{mediainfo.title} "
                                f"(平台优先级:{current_score})"
                            )

                            # 收集转存详情用于通知
                            if not pending_key:
                                transfer_details.append({
                                    "type": "电影",
                                    "title": mediainfo.title,
                                    "year": mediainfo.year,
                                    "image": mediainfo.get_poster_image(),
                                    "file_name": target_name,
                                })

                            self._record_download_history(
                                mediainfo=mediainfo,
                                subscribe=subscribe,
                                path=save_dir,
                                download_hash=matched_file.get("id"),
                                torrent_name=resource_title,
                                share_url=share_url,
                                torrent_description=file_name,
                            )

                            if not pending_key and not transient_target:
                                self._subscribe_handler.check_and_finish_subscribe(
                                    subscribe=subscribe,
                                    mediainfo=mediainfo,
                                    success_episodes=[1],
                                )
                                if hasattr(self._search_handler, "clear_sub_points"):
                                    self._search_handler.clear_sub_points(sub_key)
                        else:
                            logger.error(f"转存失败：{mediainfo.title}")

                except Exception as e:
                    logger.error(
                        f"处理分享链接出错：{self._resource_log_reference(share_url)}，"
                        f"错误：{str(e)}"
                    )
                    continue

        except Exception as e:
            logger.error(f"处理电影订阅 {subscribe.name} 出错：{str(e)}")
        return transferred_count
