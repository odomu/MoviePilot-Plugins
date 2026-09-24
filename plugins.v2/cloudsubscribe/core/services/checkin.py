"""通用每日签到执行、通知与历史持久化。

服务层只负责编排：渠道差异（客户端获取、私有动作、提示语、积分口径）全部由
CheckinDefinition 及其 executor 承载，新增渠道无需修改本模块。
"""

import copy
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Type

import pytz
from app.core.config import settings
from app.log import logger

from .. import OwnerDelegator
from ..checkin_manager import get_checkin_definitions
from ..definitions import CheckinDefinition
from ..search import SearchCapability


class CheckinService(OwnerDelegator):
    """统一编排各提供方的签到、通知和历史。"""

    _HISTORY_LIMIT = 60
    _DEFAULT_RETRY_COUNT = 2
    _MAX_RETRY_COUNT = 10
    _SCHEDULE_STATE_KEY = "checkin_schedule_state"
    # 重试时段由调度注册（core/api/registration.py）读取，勿删。
    _RETRY_START_HOUR = 9
    _RETRY_END_HOUR = 23
    #: 对外暴露的签到记录字段白名单（不含验证码、HTTP 与错误码等诊断字段）。
    _PUBLIC_FIELDS = (
        "id", "provider", "provider_name", "executed_at", "trigger",
        "mode", "success", "status", "points_change",
        "points_before", "points_after", "signin_days",
        "signin_points", "message",
        "lottery_target_count", "lottery_executed",
        "lottery_cost_points", "lottery_award_points",
        "lottery_vip_days",
    )

    def __init__(self, owner):
        super().__init__(owner)
        object.__setattr__(self, "_run_lock", threading.Lock())
        object.__setattr__(self, "_history_lock", threading.RLock())
        object.__setattr__(self, "_schedule_lock", threading.Lock())

    @staticmethod
    def _now() -> datetime:
        return datetime.now(pytz.timezone(settings.TZ))

    @staticmethod
    def _now_text() -> str:
        return datetime.now(
            pytz.timezone(settings.TZ)
        ).isoformat(timespec="seconds")

    @staticmethod
    def _parse_executed_at(record: Dict[str, Any]) -> datetime:
        """把记录时间解析为本地时区时间；格式非法时抛出 ValueError。"""
        executed_at = datetime.fromisoformat(
            str(record.get("executed_at") or "")
        )
        timezone = pytz.timezone(settings.TZ)
        if executed_at.tzinfo is None:
            executed_at = timezone.localize(executed_at)
        return executed_at.astimezone(timezone)

    @classmethod
    def _record_date_key(cls, record: Dict[str, Any]) -> str:
        value = str(record.get("executed_at") or "").strip()
        if not value:
            return ""
        try:
            return cls._parse_executed_at(record).strftime("%Y-%m-%d")
        except ValueError:
            return value[:10]

    @staticmethod
    def _number(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _calculate_signin_days(
            cls,
            history: List[Dict[str, Any]],
            current_date_key: str = "",
            current_success: bool = False,
            raw_signin_days: Any = None,
            provider_key: str = "",
    ) -> Optional[int]:
        """综合本地打卡历史与渠道返回，计算累计签到天数。"""
        signed_dates = {
            cls._record_date_key(item)
            for item in (history or [])
            if item.get("success") and cls._record_date_key(item)
        }
        if current_success and current_date_key:
            signed_dates.add(current_date_key)
        local_days = len(signed_dates)

        remote_days = None
        if raw_signin_days is not None and str(raw_signin_days).strip() != "":
            try:
                val = int(raw_signin_days)
                if val > 0:
                    if str(provider_key).lower() == "hdhaven" and val == 1 and local_days > 1:
                        val = None
                    remote_days = val
            except (TypeError, ValueError):
                remote_days = None

        if remote_days is None:
            for item in reversed(history or []):
                item_days = item.get("signin_days")
                if item_days is not None and str(item_days).strip() != "":
                    try:
                        val = int(item_days)
                        if val > 0:
                            if str(provider_key).lower() == "hdhaven" and val == 1 and local_days > 1:
                                continue
                            remote_days = val
                            break
                    except (TypeError, ValueError):
                        continue

        if remote_days is not None and local_days > 0:
            return max(remote_days, local_days)
        if remote_days is not None:
            return remote_days
        return local_days if local_days > 0 else None

    def get_checkin_providers(self) -> Tuple[CheckinDefinition, ...]:
        """向配置校验与调度注册暴露全部签到提供方契约。"""
        return tuple(get_checkin_definitions().values())

    def _resolve_provider(self, provider: str) -> Optional[CheckinDefinition]:
        return get_checkin_definitions().get(str(provider or "").strip().lower())

    def _checkin_enabled(self, provider: CheckinDefinition) -> bool:
        return bool(getattr(self, provider.enabled_attr, False))

    def _checkin_mode(self, provider: CheckinDefinition) -> str:
        """读取渠道配置的签到模式；未配置时回落到渠道声明的默认模式。"""
        return str(
            getattr(self, provider.mode_attr, "") or provider.default_mode
        ).strip().lower()

    def is_checkin_ready(self, provider: CheckinDefinition) -> bool:
        """渠道凭据是否齐备：优先用渠道声明的判定，否则检查 credential_attrs。"""
        if provider.ready is not None:
            return bool(provider.ready(self))
        return all(
            bool(getattr(self, attr, None))
            for attr in provider.credential_attrs
        )

    def _configuration_message(self, provider: CheckinDefinition) -> str:
        """凭据缺失时的修复提示，支持渠道按模式动态给出（如 HDHive）。"""
        hint = provider.hint(self) if callable(provider.hint) else provider.hint
        return str(hint or "") or f"请先配置并保存 {provider.name} 账号和密码"

    @staticmethod
    def _error_type(provider: CheckinDefinition) -> Type[Exception]:
        return provider.error_types[0] if provider.error_types else RuntimeError

    def _resolve_client(self, provider: CheckinDefinition) -> Any:
        """取签到客户端：网盘渠道走驱动管理器，搜索渠道走搜索注册表。"""
        if provider.drive_key:
            manager = getattr(self, "_drive_manager", None)
            client = manager.get_client(provider.drive_key) if manager else None
            if client is None:
                raise self._error_type(provider)(
                    f"{provider.name} 客户端未初始化"
                )
            return client
        registry = getattr(
            getattr(self, "_search_handler", None), "_search_registry", None
        )
        search_provider = registry.get(provider.key) if registry is not None else None
        if (
                search_provider is not None
                and search_provider.supports(SearchCapability.CHECKIN)
        ):
            return search_provider.require(SearchCapability.CHECKIN)
        raise self._error_type(provider)(
            f"{provider.name} 账号未配置，请先保存账号和密码"
        )

    def _execute_checkin(
            self, provider: CheckinDefinition, mode: str
    ) -> Dict[str, Any]:
        """统一执行入口：渠道客户端实现 checkin(mode)。"""
        return self._resolve_client(provider).checkin(mode)

    def _refresh_account(
            self, provider: CheckinDefinition, record: Dict[str, Any]
    ) -> None:
        """签到成功后刷新搜索渠道账户快照；网盘渠道无此缓存。"""
        if provider.drive_key:
            return
        try:
            from ..api.page import clear_ui_options_cache
        except Exception as error:  # pragma: no cover - 宿主环境缺失时降级
            logger.debug(f"加载 UI 选项缓存清理入口失败：{error}")
            return
        try:
            self.update_search_account_points(
                provider.key,
                record.get("points_after"),
                record.get("signin_days"),
            )
            clear_ui_options_cache()
        except Exception as error:
            logger.debug(f"刷新 {provider.name} 搜索账户积分失败：{error}")

    def _enabled_providers(self) -> List[CheckinDefinition]:
        """配置中已启用的渠道。"""
        return [
            provider
            for provider in get_checkin_definitions().values()
            if self._checkin_enabled(provider)
        ]

    def _ready_providers(self) -> List[CheckinDefinition]:
        """已启用且凭据完整的渠道。"""
        return [
            provider
            for provider in self._enabled_providers()
            if self.is_checkin_ready(provider)
        ]

    @staticmethod
    def _normalize_provider_key(provider: str) -> str:
        """归一化渠道参数：空值与 all/全部 都表示全部渠道。"""
        value = str(provider or "").strip().lower()
        return "" if value in {"all", "全部"} else value

    @classmethod
    def _normalize_history(cls, stored: Any) -> List[Dict[str, Any]]:
        """裁剪到窗口上限并丢弃非法条目，避免脏数据影响统计。"""
        if not isinstance(stored, list):
            return []
        return [
            copy.deepcopy(item)
            for item in stored[-cls._HISTORY_LIMIT:]
            if isinstance(item, dict)
        ]

    def _load_history(self, provider: CheckinDefinition) -> List[Dict[str, Any]]:
        return self._normalize_history(self.get_data(provider.history_key))

    def _save_history(
            self,
            provider: CheckinDefinition,
            record: Dict[str, Any],
    ) -> None:
        with self._history_lock:
            history = self._load_history(provider)
            history.append(copy.deepcopy(record))
            self.save_data(
                provider.history_key,
                history[-self._HISTORY_LIMIT:],
            )

    def _checkin_histories(self) -> Dict[str, Any]:
        """一次性读取全部渠道签到历史，避免逐渠道查询。"""
        snapshot = self._get_data_store().load_checkin_snapshot()
        histories = snapshot.get("histories") if isinstance(snapshot, dict) else None
        return histories if isinstance(histories, dict) else {}

    def _get_provider_current_points(
            self, provider: CheckinDefinition
    ) -> Optional[int]:
        """纯从本地账户缓存读取可用积分，严禁发起网络请求或触发登录。"""
        try:
            account = self._cached_account_info(f"search:{provider.key}", {})
            points = account.get("points") if isinstance(account, dict) else None
            if isinstance(points, dict):
                points = points.get("available")
            return int(points) if points is not None else None
        except (TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def _latest_points(history: List[Dict[str, Any]]) -> Any:
        """取最近一条记录里的余额；非数字时原样返回。"""
        for item in reversed(history):
            points = item.get("points_after")
            if points is None or str(points).strip() == "":
                continue
            try:
                return int(points)
            except (TypeError, ValueError):
                return points
        return None

    def get_checkin_history(
            self,
            provider: str,
            limit: int = 20,
    ) -> Optional[Dict[str, Any]]:
        adapter = self._resolve_provider(provider)
        if adapter is None:
            return None
        with self._history_lock:
            history = self._load_history(adapter)
        current_points = self._latest_points(history)
        if current_points is None and self.is_checkin_ready(adapter):
            current_points = self._get_provider_current_points(adapter)
        normalized_limit = max(1, min(int(limit or 20), self._HISTORY_LIMIT))
        return {
            "total": len(history),
            "limit": normalized_limit,
            "current_points": current_points,
            "signin_days": self._calculate_signin_days(history, provider_key=adapter.key),
            "items": [
                self._public_record(record)
                for record in list(reversed(history))[:normalized_limit]
            ],
        }

    @classmethod
    def _public_record(cls, record: Dict[str, Any]) -> Dict[str, Any]:
        """移除验证码、HTTP 与错误码等仅供内部诊断的字段。"""
        return {
            key: copy.deepcopy(record.get(key))
            for key in cls._PUBLIC_FIELDS
        }

    def list_checkin_details(
            self,
            provider: str = "",
            limit: int = 10,
    ) -> Dict[str, Any]:
        """按渠道集中返回供智能体与远程命令展示的签到详情。"""
        provider_key = self._normalize_provider_key(provider)
        if provider_key:
            adapter = self._resolve_provider(provider_key)
            if adapter is None:
                return {"success": False, "message": "不支持的签到提供方"}
            providers = [adapter] if self._checkin_enabled(adapter) else []
        else:
            providers = self._enabled_providers()
        normalized_limit = max(1, min(int(limit or 10), self._HISTORY_LIMIT))
        histories = self._checkin_histories()
        channels = []
        for item in providers:
            history = self._normalize_history(histories.get(item.key))
            records = [
                self._public_record(record)
                for record in reversed(history)
            ][:normalized_limit]
            for record in records:
                # 保留真实触发来源，其余历史（含旧版本缺省值）统一按手动展示。
                if record.get("trigger") not in {"scheduled", "retry"}:
                    record["trigger"] = "manual"
            channels.append({
                "provider": item.key,
                "provider_name": item.name,
                "points_label": item.points_label,
                "current_points": self._latest_points(history),
                "signin_days": self._calculate_signin_days(history, provider_key=item.key),
                "total": len(history),
                "items": records,
            })
        total = sum(item["total"] for item in channels)
        return {
            "success": True,
            "message": f"共查询到 {total} 条签到记录",
            "data": {"channels": channels, "total": total},
        }

    def _overview_status(
            self,
            record: Optional[Dict[str, Any]],
            *,
            enabled: bool,
            configured: bool,
            retry_pending: bool,
    ) -> Dict[str, str]:
        if not enabled:
            return {"key": "disabled", "label": "未启用", "tone": "disabled"}
        if not configured:
            return {"key": "unconfigured", "label": "未配置", "tone": "warning"}
        if not record:
            return {"key": "pending", "label": "待签到", "tone": "pending"}
        if record.get("success"):
            already = any(
                "已签到" in str(record.get(key) or "")
                for key in ("status", "message")
            )
            return {
                "key": "already" if already else "success",
                "label": "已签到" if already else "签到成功",
                "tone": "success",
            }
        if retry_pending and self._checkin_auto_retry:
            return {"key": "retry", "label": "等待重试", "tone": "warning"}
        return {"key": "failed", "label": "签到失败", "tone": "error"}

    def _channel_overview(
            self,
            provider: CheckinDefinition,
            records: List[Dict[str, Any]],
            date_keys: List[str],
            retry_pending: bool,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """单渠道概览卡片；同时返回今日全部记录（含重试）供汇总统计。"""
        today = date_keys[0]
        records_by_date: Dict[str, Dict[str, Any]] = {}
        today_records: List[Dict[str, Any]] = []
        for record in records:
            date_key = self._record_date_key(record)
            if date_key:
                records_by_date[date_key] = record
            if date_key == today:
                today_records.append(record)

        enabled = self._checkin_enabled(provider)
        configured = self.is_checkin_ready(provider)
        today_record = records_by_date.get(today)
        status = self._overview_status(
            today_record,
            enabled=enabled,
            configured=configured,
            retry_pending=retry_pending,
        )
        # 无记录的日期共用同一种状态；有记录的日期按记录本身判定成败。
        empty_status = self._overview_status(
            None,
            enabled=enabled,
            configured=configured,
            retry_pending=False,
        )
        timeline = []
        for date_key in date_keys:
            record = records_by_date.get(date_key)
            day_status = empty_status if record is None else self._overview_status(
                record,
                enabled=True,
                configured=True,
                retry_pending=date_key == today and retry_pending,
            )
            timeline.append({
                "date": date_key,
                "status": day_status["key"],
                "label": day_status["label"],
                "success": bool(record.get("success")) if record else False,
            })
        return {
            "provider": provider.key,
            "provider_name": provider.name,
            "icon": provider.icon,
            "points_label": provider.points_label,
            "enabled": enabled,
            "configured": configured,
            "mode": self._checkin_mode(provider),
            "status": status,
            "today": self._public_record(today_record) if today_record else None,
            "latest": self._public_record(records[-1]) if records else None,
            "timeline": timeline,
            "total": len(records),
        }, today_records

    def get_checkin_overview(self, days: int = 7) -> Dict[str, Any]:
        """聚合多渠道签到状态，供平台仪表盘一次读取。"""
        normalized_days = max(3, min(int(days or 7), 14))
        snapshot = self._get_data_store().load_checkin_snapshot()
        histories = snapshot.get("histories") or {}
        schedule = snapshot.get("schedule") or {}
        now = self._now()
        date_keys = [
            (now - timedelta(days=index)).strftime("%Y-%m-%d")
            for index in range(normalized_days)
        ]
        retry_providers = {
            str(value or "").strip().lower()
            for value in (schedule.get("pending_providers") or [])
            if str(value or "").strip()
        }
        channels = []
        today_points = 0
        lottery_executed = 0
        lottery_net_points = 0

        for provider in get_checkin_definitions().values():
            channel, today_records = self._channel_overview(
                provider,
                self._normalize_history(histories.get(provider.key)),
                date_keys,
                provider.key in retry_providers,
            )
            channels.append(channel)
            for record in today_records:
                today_points += self._number(record.get("points_change"))
                lottery_executed += self._number(record.get("lottery_executed"))
                lottery_net_points += (
                        self._number(record.get("lottery_award_points"))
                        - self._number(record.get("lottery_cost_points"))
                )

        ready_channels = [
            item for item in channels
            if item["enabled"] and item["configured"]
        ]
        today_status_keys = [item["status"]["key"] for item in ready_channels]
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "running": self._run_lock.locked(),
            "days": date_keys,
            "summary": {
                "enabled": sum(item["enabled"] for item in channels),
                "ready": len(ready_channels),
                "today_success": sum(
                    key in {"success", "already"} for key in today_status_keys
                ),
                "today_failed": sum(
                    key in {"failed", "retry"} for key in today_status_keys
                ),
                "today_pending": sum(
                    key == "pending" for key in today_status_keys
                ),
                "today_points": today_points,
                "lottery_executed": lottery_executed,
                "lottery_net_points": lottery_net_points,
            },
            "schedule": {
                "cron": str(self._checkin_cron or ""),
                "auto_retry": bool(self._checkin_auto_retry),
                "retry_count": self._configured_retry_count(),
                "state": copy.deepcopy(schedule),
            },
            "channels": channels,
        }

    def _notify_checkin(
            self,
            provider: CheckinDefinition,
            record: Dict[str, Any],
    ) -> None:
        if not self._notify:
            return
        balance = record.get("points_after")
        signin_days = record.get("signin_days")
        points_change = record.get("points_change")
        delta_text = (
            f"{points_change:+d}" if points_change is not None else "未知"
        )
        mode = {
            "gambler": "赌狗签到",
            "lucky": "运气签到",
        }.get(record.get("mode"), "普通签到")
        lines = [
            f"模式：{mode}",
            f"状态：{record.get('status') or '未知'}",
            f"{provider.points_label}：{delta_text}，"
            f"余额 {balance if balance is not None else '未知'}",
            f"累计：{signin_days if signin_days is not None else '未知'} 天",
        ]
        if record.get("lottery_target_count"):
            net_points = (
                    self._number(record.get("lottery_award_points"))
                    - self._number(record.get("lottery_cost_points"))
            )
            lines.append(
                f"转盘：{record.get('lottery_executed') or 0}/"
                f"{record.get('lottery_target_count')} 次，净积分 "
                f"{net_points:+d}"
            )
        if not record.get("success") and record.get("message"):
            lines.append(f"原因：{record.get('message')}")
        self.post_message(
            mtype=self._notification_type,
            title=(
                f"【网盘订阅助手】{provider.name} 签到完成"
                if record.get("success")
                else f"【网盘订阅助手】{provider.name} 签到失败"
            ),
            text="\n".join(lines),
        )

    @staticmethod
    def _summary_line(item: Dict[str, Any]) -> str:
        """把单个渠道结果渲染为一行汇总文本。"""
        record = item.get("data") if isinstance(item.get("data"), dict) else {}
        provider_name = str(
            item.get("provider_name") or record.get("provider_name")
            or item.get("provider") or record.get("provider") or "未知渠道"
        )
        success = bool(item.get("success") or record.get("success"))
        status = str(
            record.get("status") or item.get("message") or
            ("签到成功" if success else "签到失败")
        )
        details = ["成功" if success else "失败", status]
        points_change = record.get("points_change")
        if points_change is not None:
            points_label = str(item.get("points_label") or "积分")
            details.append(f"{points_label} {points_change:+d}")
        message = str(record.get("message") or "")
        if not success and message and message != status:
            details.append(message[:36])
        return f"{provider_name}：{'，'.join(details)}"

    def _notify_checkin_summary(
            self,
            results: List[Dict[str, Any]],
            title: str,
    ) -> None:
        """批量签到完成后发送一条短汇总，避免每个渠道各发一条。"""
        if not self._notify or not results:
            return
        self.post_message(
            mtype=self._notification_type,
            title=f"【网盘订阅】{title}",
            text="\n".join(self._summary_line(item) for item in results),
        )

    def _build_record(
            self,
            provider: CheckinDefinition,
            trigger: str,
            mode: str,
            result: Optional[Dict[str, Any]] = None,
            error: Optional[Exception] = None,
    ) -> Dict[str, Any]:
        """把渠道返回或异常归一为一条可持久化的签到记录。"""
        data = result if isinstance(result, dict) else {}
        lottery = data.get("lottery")
        lottery = lottery if isinstance(lottery, dict) else {}
        success = bool(data.get("success")) if result is not None else False
        points_before = data.get("points_before")
        points_after = data.get("points_after")
        # 积分字段统一归一为整数或空值：通知与汇总可直接按 +N/-N 展示。
        points_change = data.get("points_change")
        if points_change is not None:
            points_change = self._number(points_change)
        elif points_before is not None and points_after is not None:
            points_change = self._number(points_after) - self._number(points_before)
        signin_points = data.get("signin_points")
        if signin_points is not None:
            signin_points = self._number(signin_points)
        elif points_change is not None:
            # 未单独声明签到收益时，用总变化扣除转盘净收支（无转盘渠道即总变化）。
            signin_points = points_change - (
                    self._number(lottery.get("award_points"))
                    - self._number(lottery.get("cost_points"))
            )
        status_code = self._number(
            data.get("status_code")
            or getattr(error, "status_code", 0)
            or getattr(error, "status", 0)
        )
        error_code = str(
            data.get("error_code")
            or getattr(error, "code", "")
            or ("unexpected_error" if error is not None else "")
        )
        executed_at_text = self._now_text()
        current_date = executed_at_text[:10]
        history = self._load_history(provider)
        signin_days = self._calculate_signin_days(
            history=history,
            current_date_key=current_date,
            current_success=success,
            raw_signin_days=data.get("signin_days"),
            provider_key=provider.key,
        )
        return {
            "id": f"{provider.key}-{uuid.uuid4().hex}",
            "provider": provider.key,
            "provider_name": provider.name,
            "points_label": provider.points_label,
            "executed_at": executed_at_text,
            "trigger": str(trigger or "manual"),
            "mode": mode,
            "success": success,
            "status": str(data.get("status") or (
                "签到成功" if success else "签到失败"
            )),
            "message": str(
                data.get("message")
                or ("" if result is not None else error or "签到失败")
            ),
            "points_change": points_change,
            "points_before": points_before,
            "points_after": points_after,
            "signin_days": signin_days,
            "signin_points": signin_points,
            "lottery_target_count": lottery.get("target_count"),
            "lottery_executed": lottery.get(
                "used_after", lottery.get("executed")
            ),
            "lottery_cost_points": lottery.get("cost_points"),
            "lottery_award_points": lottery.get("award_points"),
            "lottery_vip_days": lottery.get("vip_days"),
            "http_status": status_code,
            "error_code": error_code,
            "captcha_verified": bool(data.get("captcha_verified")),
        }

    @staticmethod
    def _result_item(
            provider: CheckinDefinition,
            result: Dict[str, Any],
            *,
            public: bool = False,
    ) -> Dict[str, Any]:
        """在渠道结果上补齐展示元数据，供智能体与通知复用。"""
        payload = dict(result)
        if public and isinstance(payload.get("data"), dict):
            payload["data"] = CheckinService._public_record(payload["data"])
        return {
            "provider": provider.key,
            "provider_name": provider.name,
            "points_label": provider.points_label,
            **payload,
        }

    def _prepare_checkin(
            self, provider: str, mode: str
    ) -> Tuple[Optional[CheckinDefinition], str, str]:
        """校验渠道与模式；返回 (渠道, 生效模式, 失败原因)，原因空串表示可执行。"""
        adapter = self._resolve_provider(provider)
        if adapter is None:
            return None, "", "不支持的签到提供方"
        if not self._checkin_enabled(adapter):
            return adapter, "", f"{adapter.name} 每日签到未启用"
        if not self.is_checkin_ready(adapter):
            return adapter, "", self._configuration_message(adapter)
        normalized_mode = str(mode or self._checkin_mode(adapter)).strip().lower()
        if normalized_mode not in adapter.modes:
            return adapter, normalized_mode, f"{adapter.name} 签到模式无效"
        return adapter, normalized_mode, ""

    def _acquire_run_slot(
            self, adapter: CheckinDefinition, lock_acquired: bool
    ) -> Optional[Dict[str, Any]]:
        """占用签到运行槽；返回非空表示已有任务在执行。"""
        if lock_acquired or self._run_lock.acquire(blocking=False):
            return None
        return {
            "success": False,
            "message": f"{adapter.name} 签到正在执行，请稍后重试",
        }

    def start_manual_checkin(
            self, provider: str, mode: str = ""
    ) -> Dict[str, Any]:
        """预占签到锁并后台执行，避免长耗时渠道阻塞 HTTP 请求。"""
        adapter, normalized_mode, error = self._prepare_checkin(provider, mode)
        if error:
            return {"success": False, "message": error}
        busy = self._acquire_run_slot(adapter, False)
        if busy:
            return busy
        try:
            threading.Thread(
                target=self.run_checkin,
                kwargs={
                    "provider": adapter.key,
                    "trigger": "manual",
                    "mode": normalized_mode,
                    # 锁已由本方法持有，交由后台线程释放。
                    "lock_acquired": True,
                },
                daemon=True,
                name=f"cloudsubscribe-checkin-{adapter.key}",
            ).start()
        except Exception:
            self._run_lock.release()
            raise
        return {
            "success": True,
            "message": f"{adapter.name} 签到任务已提交",
            "data": {
                "provider": adapter.key,
                "mode": normalized_mode,
                "running": True,
            },
        }

    def run_checkin(
            self,
            provider: str,
            trigger: str = "manual",
            mode: str = "",
            lock_acquired: bool = False,
            notify: bool = True,
    ) -> Dict[str, Any]:
        """执行一次提供方签到；同一插件实例不允许签到并发。"""
        adapter, normalized_mode, error = self._prepare_checkin(provider, mode)
        if error:
            if lock_acquired:
                self._run_lock.release()
            return {"success": False, "message": error}
        busy = self._acquire_run_slot(adapter, lock_acquired)
        if busy:
            return busy

        try:
            try:
                result = self._execute_checkin(adapter, normalized_mode)
                record = self._build_record(
                    adapter, trigger, normalized_mode, result=result
                )
            except Exception as error:
                if not isinstance(error, adapter.error_types):
                    logger.error(
                        f"{adapter.name} 签到异常："
                        f"{type(error).__name__}: {error}"
                    )
                record = self._build_record(
                    adapter, trigger, normalized_mode, error=error
                )
            if record["success"]:
                self._refresh_account(adapter, record)
            self._save_history(adapter, record)
            if notify:
                self._notify_checkin(adapter, record)
            log_func = logger.info if record["success"] else logger.warning
            log_func(
                f"{adapter.name} 签到结果："
                f"模式={normalized_mode}，状态={record['status']}，"
                f"积分变化={record['points_change']}，消息={record['message']}"
            )
            return {
                "success": bool(record["success"]),
                "message": record["message"],
                "data": copy.deepcopy(record),
            }
        finally:
            self._run_lock.release()

    def run_quick_checkin(
            self,
            provider: str = "",
            mode: str = "",
    ) -> Dict[str, Any]:
        """供智能体和远程命令复用的签到入口。"""
        provider_key = self._normalize_provider_key(provider)
        if provider_key:
            adapter = self._resolve_provider(provider_key)
            if adapter is None:
                return {"success": False, "message": "不支持的签到提供方"}
            providers = [adapter]
        else:
            providers = self._ready_providers()
        if not providers:
            return {"success": False, "message": "没有已启用且配置完整的签到渠道"}

        requested_mode = str(mode or "").strip().lower()
        if requested_mode and not all(
                requested_mode in item.modes for item in providers
        ):
            supported = sorted({mode for item in providers for mode in item.modes})
            return {
                "success": False,
                "message": f"所选渠道签到模式仅支持 {', '.join(supported)}",
            }
        aggregate = not provider_key
        items = [
            self._result_item(
                item,
                self.run_checkin(
                    provider=item.key,
                    trigger="manual",
                    mode=requested_mode,
                    notify=not aggregate,
                ),
                public=True,
            )
            for item in providers
        ]
        if aggregate:
            self._notify_checkin_summary(items, "签到汇总")
        success = all(item.get("success") for item in items)
        return {
            "success": success,
            "message": (
                f"已完成 {len(items)} 个渠道签到"
                if success else f"已执行 {len(items)} 个渠道，存在签到失败"
            ),
            "data": {"items": items},
        }

    def _configured_retry_count(self) -> int:
        if not bool(getattr(self, "_checkin_auto_retry", True)):
            return 0
        try:
            value = int(getattr(
                self, "_checkin_retry_count", self._DEFAULT_RETRY_COUNT
            ))
        except (TypeError, ValueError):
            value = self._DEFAULT_RETRY_COUNT
        return max(1, min(value, self._MAX_RETRY_COUNT))

    def _today_records_map(
            self,
            providers: List[CheckinDefinition],
            today: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """一次快照读取全部渠道的今日签到记录，避免逐渠道查询。"""
        histories = self._checkin_histories()
        return {
            provider.key: [
                record
                for record in self._normalize_history(histories.get(provider.key))
                if self._record_date_key(record) == today
            ]
            for provider in providers
        }

    @staticmethod
    def _signed_today(records: List[Dict[str, Any]]) -> bool:
        """今日是否已有成功记录：手动签到与定时签到同等对待。"""
        return any(record.get("success") for record in records)

    def _load_schedule_state(
            self,
            today: str,
            providers: List[CheckinDefinition],
            today_records: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """读取当日调度状态；状态失效时按当天签到记录重建。"""
        provider_keys = sorted(provider.key for provider in providers)
        retry_count = self._configured_retry_count()
        stored = self.get_data(self._SCHEDULE_STATE_KEY)
        if (
                isinstance(stored, dict)
                and stored.get("date") == today
                and stored.get("full_completed") is True
                and stored.get("retry_count") == retry_count
                and stored.get("providers") == provider_keys
        ):
            return copy.deepcopy(stored)

        full_completed = any(
            record.get("trigger") == "scheduled"
            for records in today_records.values()
            for record in records
        )
        return {
            "date": today,
            "providers": provider_keys,
            "full_completed": full_completed,
            "retry_count": retry_count,
            "pending_providers": [
                provider.key
                for provider in providers
                if full_completed
                   and retry_count
                   and not self._signed_today(today_records[provider.key])
            ],
            "completed_retry_count": 0,
        }

    def _save_schedule_state(
            self,
            today: str,
            providers: List[CheckinDefinition],
            pending_keys: List[str],
            completed_retry_count: int,
    ) -> None:
        """记录当日调度进度：已执行、待重试渠道与已完成重试次数。"""
        self.save_data(self._SCHEDULE_STATE_KEY, {
            "date": today,
            "providers": sorted(provider.key for provider in providers),
            "full_completed": True,
            "retry_count": self._configured_retry_count(),
            "pending_providers": [str(key) for key in pending_keys],
            "completed_retry_count": max(0, int(completed_retry_count or 0)),
        })

    def _execute_scheduled_providers(
            self,
            providers: List[CheckinDefinition],
            trigger: str,
    ) -> List[Dict[str, Any]]:
        return [
            self._result_item(
                provider,
                self.run_checkin(
                    provider=provider.key,
                    trigger=trigger,
                    notify=False,
                ),
            )
            for provider in providers
        ]

    @staticmethod
    def _scheduled_result(
            results: List[Dict[str, Any]],
            trigger: str,
    ) -> Dict[str, Any]:
        success = bool(results) and all(
            item.get("success") for item in results
        )
        label = "首次签到" if trigger == "scheduled" else "异常重试"
        return {
            "success": success,
            "message": (
                f"{label}已执行 {len(results)} 个渠道"
                if success
                else f"{label}存在失败渠道"
                if results
                else "没有需要执行的签到渠道"
            ),
            "data": {"trigger": trigger, "items": results},
        }

    @staticmethod
    def _skipped_result(
            message: str, *, success: bool = True
    ) -> Dict[str, Any]:
        return {
            "success": success,
            "message": message,
            "data": {"skipped": True, "items": []},
        }

    def run_scheduled_checkins(self) -> Dict[str, Any]:
        """单任务入口：每天首次只签到尚未成功的渠道，随后仅重试失败渠道。"""
        if not self._schedule_lock.acquire(blocking=False):
            return self._skipped_result("签到调度正在执行", success=False)
        try:
            today = self._now().date().isoformat()
            providers = self._ready_providers()
            if not providers:
                return self._scheduled_result([], "scheduled")

            retry_count = self._configured_retry_count()
            today_records = self._today_records_map(providers, today)
            # 手动签到与定时签到同等对待：今日已成功的渠道不再重复执行。
            pending = [
                provider
                for provider in providers
                if not self._signed_today(today_records[provider.key])
            ]
            if not pending:
                self._save_schedule_state(today, providers, [], retry_count)
                return self._skipped_result("今日签到已全部完成")

            state = self._load_schedule_state(
                today=today,
                providers=providers,
                today_records=today_records,
            )
            if not state["full_completed"]:
                results = self._execute_scheduled_providers(
                    pending, trigger="scheduled"
                )
                self._save_schedule_state(
                    today,
                    providers,
                    [
                        item["provider"]
                        for item in results
                        if retry_count and not item.get("success")
                    ],
                    0,
                )
                self._notify_checkin_summary(results, "签到汇总")
                return self._scheduled_result(results, "scheduled")

            completed_retry_count = self._number(
                state.get("completed_retry_count")
            )
            if not retry_count or completed_retry_count >= retry_count:
                return self._skipped_result("签到异常重试已关闭或已完成")

            planned = {str(key) for key in (state.get("pending_providers") or [])}
            retry_targets = [
                provider for provider in pending if provider.key in planned
            ]
            if not retry_targets:
                self._save_schedule_state(today, providers, [], retry_count)
                return self._skipped_result("没有需要重试的签到渠道")

            results = self._execute_scheduled_providers(
                retry_targets, trigger="retry"
            )
            self._save_schedule_state(
                today,
                providers,
                [
                    item["provider"]
                    for item in results
                    if not item.get("success")
                ],
                completed_retry_count + 1,
            )
            self._notify_checkin_summary(results, "签到重试汇总")
            return self._scheduled_result(results, "retry")
        finally:
            self._schedule_lock.release()
