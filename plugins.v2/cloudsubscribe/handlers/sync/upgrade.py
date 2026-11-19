"""洗版基线、评分与自动升级。"""

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from app.db import SessionFactory
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType
from app.utils.string import StringUtils

from .utils import extract_ed2k_filename
from ...core import OwnerDelegator


class UpgradeService(OwnerDelegator):
    def _set_upgrade_phase(
            self, subscribe, phase: str, progress: int, clear_search: bool = False, **extra_kwargs
    ) -> None:
        self._set_task_phase(subscribe, phase, progress, clear_search=clear_search, **extra_kwargs)

    def _process_tv_subscribe_upgrade(
            self,
            subscribe,
            history: List[dict],
            transfer_details: List[Dict[str, Any]],
            transferred_count: int,
            exclude_ids: Set[int],
            target_episodes: Optional[Set[int]] = None,
            manual_upgrade: bool = False,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """
        洗版模式专用转存逻辑（独立于普通转存）

        流程：
        1. 合并整理历史、插件转存历史和 Emby 路径建立基线
        2. 搜索全部集数（含已存在的）
        3. 仅转存画质提升达到层级的集数
        4. 不调用 check_and_finish_subscribe，保持订阅活跃

        :param subscribe: 订阅对象
        :param history: 历史记录列表
        :param transfer_details: 转存详情列表
        :param transferred_count: 当前已转存数量
        :param exclude_ids: 排除的订阅ID集合
        :return: 更新后的转存数量
        """
        from app.db.subscribe_oper import SubscribeOper

        try:
            transient_target = bool(getattr(subscribe, "_transient_target", False))
            season = subscribe.season or 1
            sub_key = self.subscription_budget_key(subscribe, MediaType.TV)
            if self._search_handler:
                self._search_handler.reset_subscription_budgets(sub_key)
            logger.debug(f"开始洗版：{subscribe.name} S{season:02d}")

            mediainfo: MediaInfo = self._subscribe_mediainfo(
                subscribe, MediaType.TV
            )
            if not mediainfo:
                logger.warning(f"【洗版转存】无法识别媒体信息 {subscribe.name}")
                return transferred_count

            # 目标网盘为空时不存在洗版语义：按全新转存，避免误显示“建立洗版基线”
            cloud_valid, cloud_episode_files, cloud_dir = self._timed_sync_call(
                "cloud_scan",
                self._scan_cloud_resource_episode_files,
                subscribe=subscribe,
                mediainfo=mediainfo,
                season=season,
                start_episode=max(1, int(subscribe.start_episode or 1)),
                total_episode=max(0, int(subscribe.total_episode or 0)),
                cleanup_backups=True,
            )
            has_unparsed = bool(getattr(self, "_season_unparsed_videos", None))
            if not cloud_valid:
                logger.warning(
                    f"【洗版转存】{mediainfo.title} S{season:02d} "
                    "无法确认真实网盘文件，本轮跳过洗版"
                )
                return transferred_count
            if not cloud_episode_files:
                upgrade_title = f"【洗版转存】{mediainfo.title} S{season:02d}"
                if manual_upgrade:
                    logger.warning(f"{upgrade_title} 目标网盘无既有文件，洗版无对象，已跳过")
                    return transferred_count
                logger.info(f"{upgrade_title} 目标网盘无既有文件，按全新转存处理")
                return self.process_tv_subscribe(
                    subscribe=subscribe, history=history,
                    transfer_details=transfer_details,
                    transferred_count=transferred_count,
                    exclude_ids=exclude_ids,
                    allow_upgrade=False,
                    manual_resources=manual_resources,
                    manual_upgrade=False,
                    target_episodes=target_episodes,
                )

            # 读取已有评分，并以整季整理历史、插件历史和 Emby 构建统一基线。
            self._set_upgrade_phase(subscribe, "建立洗版基线", 20)
            existing_ep_pri = self._read_ep_priority(subscribe)
            baseline = self._build_episode_baseline(
                subscribe, mediainfo, season, include_saved=True
            )

            verified_baseline = {}
            for episode, baseline_item in baseline.items():
                item_data = dict(baseline_item or {})
                cloud_file = cloud_episode_files.get(episode)
                if cloud_file:
                    cloud_name = str(getattr(cloud_file, "name", "") or "")
                    cloud_size = int(getattr(cloud_file, "size", 0) or 0)
                    if not int(item_data.get("file_size") or 0):
                        item_data["file_size"] = cloud_size
                        item_data["size_source"] = "真实网盘文件"
                    item_data.update({
                        "cloud_dir": cloud_dir,
                        "target_file_name": cloud_name,
                        "cloud_file_id": str(getattr(cloud_file, "id", "") or ""),
                    })
                    verified_baseline[int(episode)] = item_data
            for episode, cloud_file in cloud_episode_files.items():
                if int(episode) not in verified_baseline:
                    cloud_name = str(getattr(cloud_file, "name", "") or "")
                    cloud_size = int(getattr(cloud_file, "size", 0) or 0)
                    score = self._get_mp_rule_score(
                        cloud_name, cloud_size, subscribe, season, mediainfo
                    )
                    verified_baseline[int(episode)] = {
                        "file_name": cloud_name,
                        "file_size": cloud_size,
                        "source": "真实网盘文件",
                        "score": score,
                        "rule_score": score,
                        "cloud_dir": cloud_dir,
                        "target_file_name": cloud_name,
                        "cloud_file_id": str(getattr(cloud_file, "id", "") or ""),
                    }
            baseline = verified_baseline
            if target_episodes is not None:
                normalized_targets = {
                    int(episode) for episode in target_episodes if int(episode) > 0
                }
                baseline = {
                    episode: item for episode, item in baseline.items()
                    if episode in normalized_targets
                }
            local_scores = {
                episode: int(item.get("score") or 0)
                for episode, item in baseline.items()
            }
            upgrade_log_prefix = f"【洗版转存】{mediainfo.title} S{season:02d}"

            if not local_scores:
                if manual_upgrade:
                    logger.warning(
                        f"{upgrade_log_prefix} 未找到所选内容对应的真实网盘旧文件，"
                        "无法执行洗版"
                    )
                    return transferred_count
                logger.info(
                    f"{upgrade_log_prefix} 未找到可用的现有版本基线，"
                    "回退到普通转存逻辑"
                )
                return self.process_tv_subscribe(
                    subscribe=subscribe, history=history,
                    transfer_details=transfer_details,
                    transferred_count=transferred_count,
                    exclude_ids=exclude_ids,
                    allow_upgrade=False,
                    manual_resources=manual_resources,
                    manual_upgrade=False,
                    target_episodes=target_episodes,
                )

            logger.info(f"{upgrade_log_prefix} 已建立 {len(local_scores)} 集基线")

            # 构造待搜索的集数列表
            total_ep = subscribe.total_episode or 0
            start_ep = subscribe.start_episode or 1
            all_expected_episodes = set(range(start_ep, total_ep + 1)) if total_ep > 0 else set()

            # pri_order 没有固定满分；所有已有版本均通过候选比较决定是否升级。
            episodes_to_search = set(local_scores)

            if all_expected_episodes and target_episodes is None:
                missing_eps = sorted(all_expected_episodes - set(local_scores.keys()))
                if missing_eps:
                    logger.info(
                        f"{upgrade_log_prefix} 缺失 {len(missing_eps)} 集："
                        f"{self._format_episode_ranges(set(missing_eps))}"
                    )
                    episodes_to_search |= set(missing_eps)

            episodes_to_search = sorted(episodes_to_search)

            target_episode_air_dates: Dict[int, str] = {}
            if not episodes_to_search:
                logger.info(f"{upgrade_log_prefix} 无可搜索的集数")
                return transferred_count

            logger.info(
                f"{upgrade_log_prefix} 待搜索 {len(episodes_to_search)} 集："
                f"{self._format_episode_ranges(set(episodes_to_search))}"
            )

            # 搜索并转存匹配资源
            enabled_sources = (
                ["manual"]
                if manual_resources
                else self._search_handler.get_enabled_sources()
            )
            if not enabled_sources:
                logger.warning(f"{upgrade_log_prefix} 没有可用的搜索源")
                return transferred_count

            self._set_upgrade_phase(
                subscribe,
                "处理手动洗版资源" if manual_resources else "搜索候选资源",
                40,
            )
            prefetched_results = (
                {"manual": [dict(resource) for resource in manual_resources]}
                if manual_resources else {}
            )
            if not manual_resources:
                def on_upgrade_search_progress(data: Dict[str, Any]) -> None:
                    completed = data.get("completed", 0)
                    total = max(1, data.get("total", 1))
                    current_prog = 40 + int((completed / total) * 20)  # 40% -> 60%
                    self._set_upgrade_phase(
                        subscribe,
                        data.get("summary") or "搜索候选资源",
                        current_prog,
                        search_active=data.get("active", False),
                        search_channels=data.get("channels", []),
                        search_total_results=data.get("total_results", 0),
                    )

                prefetched_results = self._search_handler.search_sources(
                    sources=enabled_sources,
                    mediainfo=mediainfo,
                    media_type=MediaType.TV,
                    season=season,
                    target_episodes=episodes_to_search,
                    target_episode_air_dates=target_episode_air_dates,
                    subscribe=subscribe,
                    progress_callback=on_upgrade_search_progress,
                )
            resource_batches = self._build_transfer_resource_batches(
                enabled_sources, prefetched_results
            )
            new_priority = dict(existing_ep_pri)
            upgrade_downloaded = 0
            upgrade_queued = 0  # 已提交但需等下载完成的离线洗版集数
            upgrade_episodes = set()  # 记录已升级的集号，用于更新 note

            for source, candidate_resources, is_cross_batch in resource_batches:
                if self._stop_requested():
                    break
                if not episodes_to_search:
                    break
                logger.info(
                    f"{upgrade_log_prefix} 使用 {source.upper()} 处理"
                    f"{'跨盘' if is_cross_batch else '目标网盘'}候选"
                    f"（{len(candidate_resources)} 个，待比较 {len(episodes_to_search)} 集）"
                )

                for resource_index, resource in enumerate(candidate_resources):
                    if self._stop_requested():
                        break
                    if not episodes_to_search:
                        break
                    share_url = resource.get("url", "")
                    resource_title = resource.get("title", "")
                    pending_episodes = tuple(episodes_to_search)

                    if (resource.get("need_unlock") or resource.get("need_access")) and not share_url:
                        resource_ref = resource.get("resource_ref")
                        if resource_ref:
                            preview_files = resource.get("preview_files") or []
                            preview_matches = self._match_episode_files(
                                preview_files,
                                mediainfo,
                                subscribe,
                                season,
                                pending_episodes,
                                require_media_match=is_cross_batch,
                            ) if preview_files else {}
                            preview_candidates = [
                                (episode, file_item, score)
                                for episode, (file_item, score) in preview_matches.items()
                                if file_item
                            ]
                            if preview_candidates:
                                preview_worthwhile = any(
                                    self._should_upgrade_candidate(
                                        local_scores.get(episode, 0),
                                        score,
                                        int(baseline.get(episode, {}).get("file_size") or 0),
                                        self._resource_size_bytes(file_item.get("size")),
                                        has_existing=episode in baseline,
                                        suppress_when_unparsed=has_unparsed,
                                    )[0]
                                    for episode, file_item, score in preview_candidates
                                )
                                if not preview_worthwhile:
                                    logger.info(
                                        f"{upgrade_log_prefix} 预览中的目标集均不满足"
                                        f"{self._upgrade_mode}洗版条件，解锁前跳过：{resource_title}"
                                    )
                                    continue
                    share_url = self._resolve_candidate_resource_url(
                        candidate_resources,
                        resource_index,
                        resource,
                        self._search_handler._search_label(
                            mediainfo, MediaType.TV, season
                        ),
                        log_prefix=upgrade_log_prefix,
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

                    is_offline_resource = self._is_offline_url(share_url) or self._is_magnet_url(share_url)
                    if is_offline_resource:
                        if self._is_offline_blacklisted(resource, share_url):
                            logger.debug(
                                f"洗版离线资源命中黑名单跳过：{resource_title}"
                            )
                            continue

                    if is_offline_resource:
                        provider_name = self._prepare_magnet_resource(
                            resource, share_url
                        )
                        if not self._validate_resource_url(
                                share_url,
                                resource_label="Magnet 链接",
                                log_prefix=upgrade_log_prefix,
                        ):
                            continue
                        provider_name = provider_name or resource_title
                        title_seasons = self._magnet_title_seasons(resource)
                        if title_seasons and season not in title_seasons:
                            logger.debug(
                                f"{upgrade_log_prefix} Magnet 标题预过滤排除："
                                f"标题季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                                f"目标季数=S{season:02d}，标题={provider_name}"
                            )
                            continue
                        target_episodes = []
                        title_episodes = self._magnet_title_episodes(resource, season)
                        if self._is_ed2k_url(share_url):
                            # ED2K 链接是单个文件：只用该文件名的集数
                            ed2k_episodes = self._resource_title_episodes(
                                Path(extract_ed2k_filename(share_url)).stem, season
                            )
                            if ed2k_episodes:
                                title_episodes = set(ed2k_episodes)
                        if title_episodes:
                            target_episodes = sorted(
                                set(episodes_to_search) & title_episodes
                            )
                            if not target_episodes:
                                logger.debug(
                                    f"{upgrade_log_prefix} Magnet 标题预过滤排除："
                                    f"标题集数={self._format_episode_ranges(title_episodes)}，"
                                    f"目标集数={self._format_episode_ranges(set(episodes_to_search))}，"
                                    f"标题={provider_name}"
                                )
                                continue
                        _, provider_score = self._search_handler.select_file_candidate(
                            [{"name": provider_name, "size": 0}],
                            mediainfo,
                            subscribe,
                        )
                        target_episodes = target_episodes or sorted(
                            set(episodes_to_search)
                            & self._resource_preview_episodes(resource, season)
                        ) or sorted(episodes_to_search)
                        target_episode_set = set(target_episodes)
                        known_candidate_size = self._offline_link_size(share_url)
                        upgrade_baseline = {}
                        worthwhile = False
                        for episode in target_episodes:
                            baseline_item = baseline.get(episode, {})
                            old_score = local_scores.get(episode, 0)
                            episode_worthwhile = False
                            if episode not in baseline:
                                episode_worthwhile = True
                            elif provider_score > old_score:
                                episode_worthwhile = True
                            elif provider_score == old_score and self._upgrade_mode in {"largest", "smallest"}:
                                if known_candidate_size:
                                    episode_worthwhile = self._should_upgrade_candidate(
                                        old_score,
                                        provider_score,
                                        int(baseline_item.get("file_size") or 0),
                                        known_candidate_size,
                                    )[0]
                                else:
                                    # 逐集大小留到下载完成后判断
                                    episode_worthwhile = True
                            worthwhile = worthwhile or episode_worthwhile
                            if episode_worthwhile and episode in baseline:
                                upgrade_baseline[str(episode)] = {
                                    "score": old_score,
                                    "size": int(baseline_item.get("file_size") or 0),
                                    "cloud_dir": baseline_item.get("cloud_dir") or "",
                                    "file_name": baseline_item.get("target_file_name")
                                                 or baseline_item.get("file_name") or "",
                                    "file_id": baseline_item.get("cloud_file_id") or "",
                                }
                        if not worthwhile:
                            continue
                        pending_key = self._queue_magnet_package(
                            resource,
                            share_url,
                            subscribe,
                            mediainfo,
                            season=season,
                            target_episodes=target_episodes,
                            sub_key=sub_key,
                            upgrade=bool(upgrade_baseline),
                            upgrade_mode=self._upgrade_mode,
                            upgrade_baseline=upgrade_baseline,
                            transient_target=transient_target,
                        )
                        if not pending_key:
                            continue
                        upgrade_queued += sum(
                            1 for episode in target_episodes
                            if str(episode) in upgrade_baseline
                        )
                        self._clear_task_search_state(subscribe)
                        self._append_magnet_pending_history(
                            history=history,
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            share_url=share_url,
                            cloud_dir=self._cloud_transfer_path.rstrip("/") or "/",
                            resource=resource,
                            season=season,
                            target_episodes=target_episodes,
                            upgrade=bool(upgrade_baseline),
                            finalize_key=pending_key,
                        )
                        episodes_to_search = [
                            episode for episode in episodes_to_search
                            if episode not in target_episode_set
                        ]
                        continue

                    share_files = self._validated_resource_files(
                        share_url,
                        resource_title=resource_title,
                        target_season=(season if self._skip_other_season_dirs else None),
                        log_prefix=upgrade_log_prefix,
                    )
                    if not share_files:
                        continue

                    if manual_resources:
                        # 手动提交的分享以实际包含的集数为准：已存在集数按洗版规则比较后替换或跳过，
                        # 新增集数正常转存，避免只处理已有基线集数而漏掉新集。
                        _, share_episodes = self._summarize_share_episodes(
                            share_files,
                            season,
                            mediainfo if is_cross_batch else None,
                        )
                        pending_episodes = tuple(
                            sorted(set(pending_episodes) | share_episodes)
                        )

                    # 匹配需要升级的集数
                    mode_label = {
                        "largest": "保留最大文件",
                        "smallest": "保留最小文件",
                        "replace": "直接替换",
                        "coexist": "新旧共存",
                    }.get(self._upgrade_mode, self._upgrade_mode)
                    self._set_upgrade_phase(
                        subscribe,
                        f"比较版本：{len(pending_episodes)} 集待比对"
                        f"（{mode_label}，基线 {len(baseline)} 集）",
                        65,
                    )
                    matched_items = []
                    matched_files = self._match_episode_files(
                        share_files,
                        mediainfo,
                        subscribe,
                        season,
                        pending_episodes,
                        require_media_match=is_cross_batch,
                    )
                    upgrade_skip_episodes: List[int] = []
                    upgrade_skip_reason = ""
                    upgrade_prepared = self._prepare_share_files(share_files)
                    upgrade_parsed_cache: Dict[int, Any] = {}
                    for episode in pending_episodes:
                        matched_file, cand_pri = matched_files.get(
                            episode, (None, 0)
                        )
                        if not matched_file:
                            continue

                        file_name = matched_file.get('name', '')
                        # 候选文件大小（搜索接口已自带元数据）
                        candidate_size = self._resource_size_bytes(matched_file.get("size"))

                        # 现有文件信息（MoviePilot 规则优先级）
                        old_score = local_scores.get(episode, 0)

                        new_score = cand_pri

                        score_gap = new_score - old_score

                        should_upgrade, reason = self._should_upgrade_candidate(
                            old_score,
                            new_score,
                            int(baseline.get(episode, {}).get("file_size") or 0),
                            candidate_size,
                            has_existing=episode in baseline,
                            suppress_when_unparsed=has_unparsed,
                        )
                        if not should_upgrade:
                            upgrade_skip_episodes.append(episode)
                            upgrade_skip_reason = reason
                            continue

                        logger.info(
                            f"{upgrade_log_prefix} E{episode:02d}：{reason}"
                        )

                        target_dir, target_name = self._platform_target(
                            self._CLOUD_MEDIA_ROOT, subscribe, mediainfo,
                            file_name, season, episode
                        )
                        if episode in baseline and self._upgrade_mode == "coexist":
                            target_name = self._coexist_target_name(
                                target_name,
                                file_name,
                                candidate_size,
                                matched_file.get("sha1") or "",
                            )
                        baseline_item = baseline.get(episode, {})
                        old_dir = str(baseline_item.get("cloud_dir") or "").strip()
                        old_name = str(
                            baseline_item.get("target_file_name")
                            or baseline_item.get("file_name") or ""
                        ).strip()
                        old_file_id = str(baseline_item.get("cloud_file_id") or "")
                        if episode in baseline and self._upgrade_mode != "coexist" and not old_file_id:
                            if not old_dir or not old_name:
                                old_dir, old_name = self._platform_target(
                                    self._CLOUD_MEDIA_ROOT,
                                    subscribe,
                                    mediainfo,
                                    old_name or file_name,
                                    season,
                                    episode,
                                )
                            old_file = self._cloud_query.get_cached_file(old_dir, old_name)
                            if old_file:
                                old_file_id = str(getattr(old_file, "id", "") or "")
                                old_name = str(getattr(old_file, "name", "") or old_name)
                        matched_items.append({
                            "file": matched_file,
                            "resource": resource,
                            "episode": episode,
                            "new_score": new_score,
                            "old_score": old_score,
                            "score_gap": score_gap,
                            "file_name": file_name,
                            "candidate_size": candidate_size,
                            "target_dir": target_dir,
                            "target_name": target_name,
                            "is_upgrade": bool(episode in baseline),
                            "success_episodes": [],
                            "upgrade_old_cloud_dir": str(
                                old_dir
                            ),
                            "upgrade_old_file_name": str(
                                old_name
                            ),
                            "upgrade_old_file_id": str(
                                old_file_id
                            ),
                            "upgrade_old_size": int(
                                baseline.get(episode, {}).get("file_size") or 0
                            ),
                            "subtitle_files": self._companion_subtitle_files(
                                share_files,
                                matched_file,
                                season=season,
                                episode=episode,
                                prepared=upgrade_prepared,
                                parsed_cache=upgrade_parsed_cache,
                            ),
                        })

                    if upgrade_skip_episodes:
                        episodes_text = ", ".join(
                            str(value) for value in upgrade_skip_episodes[:20]
                        )
                        if len(upgrade_skip_episodes) > 20:
                            episodes_text += f", …(+{len(upgrade_skip_episodes) - 20})"
                        logger.info(
                            f"{upgrade_log_prefix} 洗版候选跳过 "
                            f"[{episodes_text}]：{upgrade_skip_reason}"
                        )

                    if not matched_items:
                        continue

                    upgrade_replace_count = sum(
                        1 for item in matched_items if item.get("is_upgrade")
                    )
                    self._set_upgrade_phase(
                        subscribe,
                        f"提交替换：{len(matched_items)} 集"
                        f"（替换 {upgrade_replace_count} / "
                        f"新增 {len(matched_items) - upgrade_replace_count}，"
                        f"跳过 {len(upgrade_skip_episodes)}）",
                        80,
                        clear_search=True,
                    )
                    transfer_results = self._transfer_episode_items(
                        matched_items,
                        share_url,
                        mediainfo,
                        subscribe,
                        season,
                        sub_key,
                        track_subscription=not manual_upgrade and not transient_target,
                        transient_target=transient_target,
                    )
                    if not transfer_results:
                        break
                    batch_detail_episodes = []
                    batch_success_episodes = []
                    for transfer_result in transfer_results:
                        item = transfer_result["item"]
                        episode = item["episode"]
                        new_score = item["new_score"]
                        old_score = item["old_score"]
                        file_name = item["file_name"]
                        success = transfer_result["success"]
                        pending_key = transfer_result["pending_key"]

                        history_item = self._build_transfer_history_item(
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            status=self._transfer_history_status(success, share_url),
                            share_url=share_url,
                            file_name=item["target_name"],
                            source_file_name=file_name,
                            cloud_dir=item["target_dir"],
                            resource=item["resource"],
                            season=season,
                            episode=episode,
                            file_size=int(item.get("candidate_size") or 0),
                            source_sha1=item["file"].get("sha1") or "",
                            source_md5=item["file"].get("md5") or "",
                            rule_score=new_score,
                            upgrade=item.get("is_upgrade", False),
                        )
                        history.append(history_item)

                        if success:
                            transferred_count += 1
                            upgrade_downloaded += 1
                            upgrade_episodes.add(episode)
                            new_priority[str(episode)] = new_score

                            if pending_key:
                                history_item["finalize_key"] = pending_key
                                history_item["status"] = (
                                    "下载中" if self._is_ed2k_url(share_url)
                                    else "处理中"
                                )
                            if not pending_key:
                                batch_detail_episodes.append(episode)
                            batch_success_episodes.append(episode)

                            logger.info(
                                f"{upgrade_log_prefix} 转存成功 E{episode:02d}"
                                f" {old_score}→{new_score}（{file_name}）"
                            )

                    if batch_success_episodes:
                        completed_episode_set = set(batch_success_episodes)
                        episodes_to_search = [
                            episode for episode in episodes_to_search
                            if episode not in completed_episode_set
                        ]
                    self._append_tv_transfer_detail(
                        transfer_details, mediainfo, season, batch_detail_episodes,
                        notification_kind="upgrade",
                    )
                    if batch_success_episodes:
                        self._record_download_history(
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            path=transfer_results[0]["item"]["target_dir"],
                            download_hash=share_url,
                            torrent_name=resource_title,
                            share_url=share_url,
                            seasons=f"S{season:02d}",
                            episodes=StringUtils.format_ep(batch_success_episodes),
                        )

            # 洗版转存的集数同样计入已入库进度。
            if upgrade_episodes and not manual_upgrade and not transient_target:
                remaining_lack = self._subscribe_handler.update_subscribe_progress(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=sorted(upgrade_episodes),
                )
                if remaining_lack is not None:
                    logger.debug(f"{upgrade_log_prefix} 已更新订阅进度（{len(upgrade_episodes)} 集）")
                else:
                    logger.warning(f"{upgrade_log_prefix} 更新订阅进度失败")

            # 更新单集优先级
            if (
                    new_priority != existing_ep_pri
                    and not manual_upgrade
                    and not transient_target
            ):
                try:
                    SubscribeOper().update(subscribe.id, {"episode_priority": new_priority})
                    logger.debug(f"{upgrade_log_prefix} 已更新 {len(new_priority)} 集评分")
                except Exception as e:
                    logger.warning(f"{upgrade_log_prefix} 更新 episode_priority 失败：{e}")

            # 不调用 check_and_finish_subscribe——保持订阅活跃以持续搜索更优资源
            if upgrade_downloaded:
                self._set_upgrade_phase(subscribe, "洗版完成", 100)
                logger.info(f"{upgrade_log_prefix} 洗版转存完成，共升级 {upgrade_downloaded} 集")
            elif upgrade_queued:
                self._set_upgrade_phase(subscribe, "洗版任务已提交", 100)
                logger.info(
                    f"{upgrade_log_prefix} 已提交 {upgrade_queued} 集离线洗版任务，"
                    "等待下载完成后自动替换"
                )
            else:
                self._set_upgrade_phase(subscribe, "无需替换", 100)
                logger.info(f"{upgrade_log_prefix} 洗版转存完成，未发现可升级资源")

        except Exception as e:
            logger.error(f"洗版失败：{subscribe.name}，{e}")
        return transferred_count

    def auto_upgrade_scan(self):
        """网盘转存后刷新已开启洗版订阅的整季评分。"""
        from app.db.subscribe_oper import SubscribeOper

        with SessionFactory() as db:
            all_subs = SubscribeOper(db=db).list("N,R") or []

        targets = [
            subscribe for subscribe in all_subs
            if subscribe.type == MediaType.TV.value
               and self._is_cloud_upgrade_subscribe(subscribe)
        ]
        if not targets:
            logger.debug("网盘洗版没有待刷新订阅")
            return

        updated = 0
        failed = 0
        for subscribe in targets:
            try:
                updated += self._upgrade_scan_single_sub(subscribe)
            except Exception as error:
                failed += 1
                logger.error(
                    f"网盘洗版评分刷新失败：{subscribe.name} "
                    f"S{subscribe.season or 1:02d}，{error}"
                )
        logger.info(
            f"网盘洗版评分刷新完成：{len(targets) - failed}/"
            f"{len(targets)} 个订阅，更新 {updated} 集"
        )

    def _upgrade_scan_single_sub(self, subscribe):
        """按统一基线刷新单个订阅的 episode_priority。"""
        from app.db.subscribe_oper import SubscribeOper

        season = int(subscribe.season or 1)
        mediainfo = self._subscribe_mediainfo(subscribe, MediaType.TV)
        if not mediainfo:
            logger.warning(f"洗版评分刷新失败：无法识别 {subscribe.name}")
            return 0

        baseline = self._build_episode_baseline(
            subscribe, mediainfo, season, include_saved=False
        )
        scores = {
            str(episode): int(item.get("score") or 0)
            for episode, item in baseline.items()
            if int(item.get("score") or 0) > 0
        }
        if not scores:
            logger.debug(f"洗版评分无可用基线：{subscribe.name} S{season:02d}")
            return 0
        if scores != self._read_ep_priority(subscribe):
            SubscribeOper().update(subscribe.id, {"episode_priority": scores})
            return len(scores)
        return 0

    def _self_heal_cleanup(self):
        """按真实整理/转存/媒体库基线刷新评分，避免依赖 STRM 文件名误删。"""
        from app.db.subscribe_oper import SubscribeOper

        try:
            with SessionFactory() as db:
                subscribes = SubscribeOper(db=db).list("N,R") or []
                subscribes = [
                    item for item in subscribes
                    if getattr(item, "episode_priority", None)
                ]
        except Exception as error:
            logger.warning(f"洗版评分自愈查询失败：{error}")
            return

        updated_count = 0
        cleaned_count = 0
        for subscribe in subscribes:
            try:
                season = int(subscribe.season or 1)
                mediainfo = self._subscribe_mediainfo(
                    subscribe, MediaType.TV
                )
                if not mediainfo:
                    continue
                baseline = self._build_episode_baseline(
                    subscribe, mediainfo, season, include_saved=False
                )
                if not baseline:
                    continue
                scores = {
                    str(episode): int(item.get("score") or 0)
                    for episode, item in baseline.items()
                    if int(item.get("score") or 0) > 0
                }
                old_scores = self._read_ep_priority(subscribe)
                if scores and scores != old_scores:
                    cleaned_count += len(set(old_scores) - set(scores))
                    SubscribeOper().update(
                        subscribe.id, {"episode_priority": scores}
                    )
                    updated_count += len(scores)
            except Exception as error:
                logger.warning(
                    f"洗版评分自愈失败：{subscribe.name} "
                    f"S{subscribe.season or 1:02d}，{error}"
                )

        if updated_count or cleaned_count:
            logger.info(
                f"洗版评分自愈完成：更新 {updated_count} 集，"
                f"清理 {cleaned_count} 条旧评分"
            )

    @staticmethod
    def _transient_target_defaults() -> Dict[str, Any]:
        return {
            "id": 0,
            "doubanid": None,
            "bangumiid": None,
            "anilistid": None,
            "media_source": None,
            "media_id": None,
            "media_category": None,
            "media_category_id": None,
            "episode_group": None,
            "filter_groups": None,
            "sites": None,
            "include": None,
            "exclude": None,
            "quality": None,
            "resolution": None,
            "effect": None,
            "audio_quality": None,
            "audio_format": None,
            "min_bitrate": None,
            "min_bit_depth": None,
            "min_sample_rate": None,
            "filter": None,
            "custom_words": None,
            "save_path": None,
            "downloader": None,
            "keyword": None,
            "note": [],
            "episode_priority": {},
            "best_version": False,
            "best_version_full": None,
            "current_priority": None,
            "search_imdbid": None,
            "search_interval": None,
            "manual_total_episode": None,
            "total_tracks": None,
            "music_type": None,
            "poster": None,
            "date": None,
            "username": None,
            "state": "N",
        }

    def build_transient_media_target(
            self,
            media: Dict[str, Any],
            target_id: int = -1,
            episodes: Optional[Set[int]] = None,
            manual_upgrade: bool = False,
            manual_baseline: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """将订阅外媒体转换为现有同步链可消费的临时目标。"""
        from ...drive.common import positive_int
        from .utils import normalize_season
        media_type = (
            MediaType.TV
            if str(media.get("media_type") or media.get("type") or "").strip().lower()
               in {"tv", MediaType.TV.value.lower()}
            else MediaType.MOVIE
        )
        title = str(media.get("title") or media.get("name") or "").strip()
        tmdb_id = positive_int(media.get("tmdb_id") or media.get("tmdbid"))
        if not title or not tmdb_id:
            raise ValueError("媒体目标缺少标题或 TMDB ID")

        season = normalize_season(media.get("season")) if media_type == MediaType.TV else None
        selected_episodes = sorted({
            int(value)
            for value in (
                episodes if episodes is not None else media.get("episodes") or []
            )
            if int(value) > 0
        })

        target_data = self._transient_target_defaults()
        target_data.update({
            "id": int(target_id),
            "name": title,
            "year": media.get("year"),
            "type": media_type.value,
            "tmdbid": tmdb_id,
            "doubanid": media.get("douban_id") or media.get("doubanid"),
            "bangumiid": media.get("bangumi_id") or media.get("bangumiid"),
            "season": season,
            "start_episode": min(selected_episodes) if selected_episodes else 1,
            "total_episode": max(selected_episodes) if selected_episodes else 0,
            "lack_episode": len(selected_episodes) if media_type == MediaType.TV else 0,
            "episode_group": media.get("episode_group"),
            "_transient_target": True,
            "_manual_upgrade": bool(manual_upgrade),
            "_target_episodes": list(selected_episodes),
            "_manual_media_baseline": dict(manual_baseline or {}),
        })
        target = SimpleNamespace(**target_data)
        target.to_dict = lambda: {
            k: list(v) if isinstance(v, (set, frozenset)) else v
            for k, v in target.__dict__.items()
        }
        return target

    def _history_upgrade_records(
            self, identities: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if not self._get_data:
            raise RuntimeError("历史记录存储未初始化")
        records = self._get_data("history") or []
        target_ids = {
            str(identity.get("record_id") or "").strip()
            for identity in identities
            if str(identity.get("record_id") or "").strip()
        }
        selected = []
        for record in records:
            record_id = self._ensure_history_record_id(record)
            if record_id in target_ids or any(
                    self._history_record_matches(record, identity)
                    for identity in identities
            ):
                if str(record.get("status") or "") == "成功" and not record.get("finalize_key"):
                    selected.append(copy.deepcopy(record))
        if not selected:
            raise LookupError("所选历史记录不存在或没有可洗版的成功内容")
        return selected

    def resolve_history_upgrade_targets(
            self, identities: List[Dict[str, Any]]
    ) -> List[Any]:
        from .utils import normalize_season
        records = self._history_upgrade_records(identities)
        grouped: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = {}
        for record in records:
            media_type = str(record.get("type") or "")
            if media_type not in {MediaType.TV.value, MediaType.MOVIE.value}:
                continue
            media_key = str(record.get("tmdb_id") or "").strip() or (
                f"{record.get('title') or ''}|{record.get('year') or ''}"
            )
            season = normalize_season(record.get("season")) if media_type == MediaType.TV.value else 0
            grouped.setdefault((media_type, media_key, season), []).append(record)

        targets = []
        for index, ((media_type, _, season), group) in enumerate(grouped.items(), start=1):
            sample = group[0]
            episodes = {
                episode
                for record in group
                for episode in self._platform_episode_numbers(record)
            }
            target = self.build_transient_media_target(
                {
                    "title": sample.get("title"),
                    "year": sample.get("year"),
                    "media_type": media_type,
                    "tmdb_id": sample.get("tmdb_id"),
                    "douban_id": sample.get("douban_id"),
                    "season": season or None,
                    "episode_group": sample.get("episode_group"),
                },
                target_id=-index,
                episodes=episodes,
                manual_upgrade=True,
            )
            targets.append(target)
        if not targets:
            raise ValueError("所选记录缺少可识别的电影或电视剧媒体信息")
        return targets

    def _media_server_services(self) -> Dict[str, Any]:
        from app.helper.mediaserver import MediaServerHelper

        services = MediaServerHelper().get_services() or {}
        return {
            name: service for name, service in services.items()
            if service and not service.instance.is_inactive()
        }

    @staticmethod
    def _media_server_storage_name(name: str, service: Any) -> str:
        """返回媒体库同步表使用的服务器类型标识。"""
        return str(getattr(service, "type", "") or name or "").strip().casefold()

    def _configured_media_path(self, value: Any) -> Tuple[Path, str]:
        """校验媒体服务器实际路径属于插件配置的 STRM 媒体根目录。"""
        if not self._local_resource_path:
            raise RuntimeError("未配置本地或挂载媒体根路径")
        root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        if not root.is_dir():
            raise RuntimeError("插件配置的媒体根路径不存在或不可访问")
        path = Path(str(value or "").strip()).expanduser().resolve(strict=False)
        if not str(value or "").strip() or (path != root and root not in path.parents):
            raise ValueError("媒体服务器条目路径不在插件配置的媒体根路径中")
        if not path.exists():
            raise LookupError(f"媒体服务器条目路径不存在或不可访问：{path}")
        return path, path.relative_to(root).as_posix()

    @staticmethod
    def _media_server_item_type(value: Any) -> Optional[MediaType]:
        text = str(value or "").strip().lower()
        if text in {"电影", "movie"}:
            return MediaType.MOVIE
        if text in {"电视剧", "tv", "series", "show"}:
            return MediaType.TV
        return None

    def list_media_server_content(
            self,
            server: str = "",
            keyword: str = "",
            tmdb_id: Any = None,
            media_type: str = "",
            limit: int = 500,
    ) -> Dict[str, Any]:
        """在指定媒体服务器内按标题或 TMDB ID 搜索可洗版内容。"""
        from app.db.models.mediaserver import MediaServerItem
        from sqlalchemy import func, or_

        from ...core.media import media_server_tmdb_filters, tmdb_id_of
        from ...drive.common import positive_int
        services = self._media_server_services()
        server_options = [
            {
                "title": f"{name}（{str(getattr(service, 'type', '') or '媒体服务器').upper()}）",
                "value": name,
            }
            for name, service in sorted(services.items(), key=lambda item: item[0].casefold())
        ]
        selected_server = str(server or "").strip()
        query_text = str(keyword or "").strip()
        target_tmdb_id = positive_int(tmdb_id)
        target_media_type = str(media_type or "").strip().lower()
        if not selected_server:
            return {"servers": server_options, "items": []}
        if selected_server not in services:
            raise ValueError("所选媒体服务器不存在或未启用")
        if not query_text and not target_tmdb_id:
            return {"servers": server_options, "items": []}

        storage_server = self._media_server_storage_name(
            selected_server, services[selected_server]
        )
        row_limit = max(1, min(int(limit or 500), 1000))
        with SessionFactory() as db:
            query = db.query(MediaServerItem).filter(
                func.lower(MediaServerItem.server) == storage_server
            )
            if target_tmdb_id:
                query = query.filter(*media_server_tmdb_filters(
                    MediaServerItem, [target_tmdb_id]
                ))
            else:
                query = query.filter(or_(
                    MediaServerItem.title.ilike(f"%{query_text}%"),
                    MediaServerItem.original_title.ilike(f"%{query_text}%"),
                ))
            rows = query.order_by(
                MediaServerItem.title, MediaServerItem.year
            ).limit(100).all()

            items = []
            for row in rows:
                media_type = self._media_server_item_type(row.item_type)
                row_tmdb_id = tmdb_id_of(row)
                if not media_type or not row_tmdb_id or not row.path:
                    continue
                if target_media_type in {"movie", "tv"} and (
                        (target_media_type == "movie") != (media_type == MediaType.MOVIE)
                ):
                    continue
                try:
                    _, relative = self._configured_media_path(row.path)
                except (ValueError, LookupError, RuntimeError):
                    continue
                base = {
                    "server": selected_server,
                    "item_id": str(row.item_id),
                    "title": str(row.title or "未知媒体"),
                    "year": row.year,
                    "tmdb_id": row_tmdb_id,
                    "media_type": "movie" if media_type == MediaType.MOVIE else "tv",
                    "path": relative,
                }
                if media_type == MediaType.MOVIE:
                    items.append({
                        **base,
                        "kind": "movie",
                        "label": f"{base['title']}{f' ({row.year})' if row.year else ''} · 电影 · {relative}",
                    })
                    continue
                seasoninfo = row.seasoninfo or {}
                for season_value, episode_values in seasoninfo.items():
                    try:
                        season = int(season_value)
                    except (TypeError, ValueError):
                        continue
                    episodes = sorted({
                        episode for value in (episode_values or [])
                        if (episode := positive_int(value))
                    })
                    if not episodes:
                        continue
                    items.append({
                        **base,
                        "kind": "season",
                        "season": season,
                        "label": f"{base['title']} S{season:02d} · 整季（{len(episodes)} 集）· {relative}",
                    })
                    for episode in episodes:
                        items.append({
                            **base,
                            "kind": "episode",
                            "season": season,
                            "episode": episode,
                            "label": f"{base['title']} S{season:02d}E{episode:02d} · {relative}",
                        })
                    if len(items) >= row_limit:
                        break
                if len(items) >= row_limit:
                    break
        return {"servers": server_options, "items": items[:row_limit]}

    def _media_server_episode_files(
            self, server: str, item_id: str, season: int
    ) -> Dict[int, Dict[str, Any]]:
        from app.chain.mediaserver import MediaServerChain
        chain = MediaServerChain()
        episode_ids = chain.get_season_episode_ids(
            server=server, item_id=item_id, season=season
        )
        result = {}
        for episode, episode_id in (episode_ids or {}).items():
            info = chain.iteminfo(server=server, item_id=episode_id)
            if not info:
                logger.debug(
                    f"媒体服务器剧集详情缺失，跳过该集：{server} "
                    f"S{season:02d}E{int(episode):02d}"
                )
                continue
            path, relative = self._configured_media_path(getattr(info, "path", ""))
            result[int(episode)] = {
                "file_name": path.name,
                "file_size": path.stat().st_size if path.is_file() and path.suffix.lower() != ".strm" else 0,
                "path": relative,
                "source": f"媒体服务器 {server}",
            }
        return result

    def resolve_media_server_upgrade_targets(
            self, selections: List[Dict[str, Any]]
    ) -> List[Any]:
        """二次校验媒体服务器条目和实际路径，并按媒体、季聚合洗版目标。"""
        from app.db.models.mediaserver import MediaServerItem
        from sqlalchemy import func

        from ...core.media import tmdb_id_of
        services = self._media_server_services()
        grouped: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
        episode_cache: Dict[Tuple[str, str, int], Dict[int, Dict[str, Any]]] = {}
        seen = set()
        with SessionFactory() as db:
            normalized_selections = []
            rows_by_server: Dict[str, set[str]] = {}
            for selection in selections[:200]:
                server = str(selection.get("server") or "").strip()
                item_id = str(selection.get("item_id") or "").strip()
                kind = str(selection.get("kind") or "").strip().lower()
                identity = (server, item_id, kind, selection.get("season"), selection.get("episode"))
                if identity in seen:
                    continue
                seen.add(identity)
                if server not in services or not item_id:
                    raise ValueError("所选媒体服务器不存在、未启用或条目无效")
                storage_server = self._media_server_storage_name(
                    server, services[server]
                )
                normalized_selections.append((
                    selection, server, item_id, kind, storage_server,
                ))
                rows_by_server.setdefault(storage_server, set()).add(item_id)

            rows_by_key = {}
            for storage_server, item_ids in rows_by_server.items():
                rows = db.query(MediaServerItem).filter(
                    func.lower(MediaServerItem.server) == storage_server,
                    MediaServerItem.item_id.in_(item_ids),
                ).all()
                rows_by_key.update({
                    (storage_server, str(row.item_id)): row for row in rows
                })

            for selection, server, item_id, kind, storage_server in normalized_selections:
                row = rows_by_key.get((storage_server, item_id))
                row_tmdb_id = tmdb_id_of(row)
                if not row or not row_tmdb_id:
                    raise LookupError("所选媒体库内容已不存在或缺少 TMDB ID")
                media_type = self._media_server_item_type(row.item_type)
                if not media_type:
                    raise ValueError("所选媒体库内容不是电影或电视剧")
                media_path, relative = self._configured_media_path(row.path)
                if media_type == MediaType.MOVIE:
                    if kind != "movie":
                        raise ValueError("电影媒体库条目类型错误")
                    if not media_path.is_file():
                        raise LookupError("所选电影的媒体服务器路径不是可访问文件")
                    key = (media_type.value, row_tmdb_id, 0)
                    grouped.setdefault(key, {
                        "row": row,
                        "season": 0,
                        "episodes": set(),
                        "baseline": {"movie": {
                            "file_name": media_path.name,
                            "file_size": media_path.stat().st_size if media_path.is_file() and media_path.suffix.lower() != ".strm" else 0,
                            "path": relative,
                            "source": f"媒体服务器 {server}",
                        }},
                    })
                    continue

                try:
                    season = max(1, int(selection.get("season") or 0))
                except (TypeError, ValueError):
                    raise ValueError("电视剧季号无效")
                cache_key = (server, item_id, season)
                if cache_key not in episode_cache:
                    try:
                        episode_cache[cache_key] = self._media_server_episode_files(
                            server, item_id, season
                        )
                    except Exception as error:
                        logger.warning(
                            f"读取媒体服务器剧集失败，回退插件历史："
                            f"{server} {row.title} S{season:02d}，{error}"
                        )
                        episode_cache[cache_key] = {}
                available = episode_cache[cache_key]
                if not available:
                    # 媒体库未返回剧集时不再硬失败：统一基线（历史）→ 网盘真实文件
                    available = self._manual_episode_baseline(
                        row, row_tmdb_id, media_type, season, relative
                    )
                    episode_cache[cache_key] = available
                    if not available:
                        logger.warning(
                            f"媒体库、历史与网盘均无 {row.title} "
                            f"S{season:02d} 剧集文件"
                        )
                if kind == "season":
                    selected_episodes = set(available)
                elif kind == "episode":
                    episode = int(selection.get("episode") or 0)
                    if episode <= 0 or episode not in available:
                        raise LookupError("所选媒体库剧集已不存在")
                    selected_episodes = {episode}
                else:
                    raise ValueError("电视剧媒体库条目类型错误")
                if not selected_episodes:
                    raise LookupError("所选媒体库季没有可洗版剧集")
                key = (media_type.value, row_tmdb_id, season)
                group = grouped.setdefault(key, {
                    "row": row,
                    "season": season,
                    "episodes": set(),
                    "baseline": {"episodes": {}},
                })
                group["episodes"].update(selected_episodes)
                group["baseline"]["episodes"].update({
                    episode: available[episode] for episode in selected_episodes
                })

        targets = []
        for index, group in enumerate(grouped.values(), start=1):
            row = group["row"]
            media_type = self._media_server_item_type(row.item_type)
            targets.append(self.build_transient_media_target(
                {
                    "title": row.title,
                    "year": row.year,
                    "media_type": media_type.value,
                    "tmdb_id": tmdb_id_of(row),
                    "season": group["season"] or None,
                },
                target_id=-index,
                episodes=group["episodes"],
                manual_upgrade=True,
                manual_baseline=group["baseline"],
            ))
        if not targets:
            raise ValueError("请选择至少一个符合插件媒体路径配置的媒体库内容")
        return targets
