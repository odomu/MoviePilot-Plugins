"""通用每日签到执行、通知与历史持久化。"""

import copy
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, Type

import pytz
from app.core.config import settings
from app.log import logger

from .. import OwnerDelegator
from ...search.hdhive import HDHiveWebError


@dataclass(frozen=True)
class CheckinProvider:
    """签到提供方与插件配置、客户端之间的最小适配契约。"""

    key: str
    name: str
    client_getter: str
    credential_attrs: Tuple[str, ...]
    error_type: Type[Exception]

    @property
    def history_key(self) -> str:
        return f"{self.key}_checkin_history"


class CheckinService(OwnerDelegator):
    """统一编排各提供方的签到、通知和历史。"""

    _PROVIDERS = {
        "hdhive": CheckinProvider(
            key="hdhive",
            name="HDHive",
            client_getter="get_hdhive_web_client",
            credential_attrs=("_hdhive_username", "_hdhive_password"),
            error_type=HDHiveWebError,
        ),
    }
    _HISTORY_LIMIT = 60
    _RETRY_START_HOUR = 9
    _RETRY_END_HOUR = 23
    _DEFAULT_RETRY_COUNT = 2
    _MAX_RETRY_COUNT = 10
    _SCHEDULE_STATE_KEY = "checkin_schedule_state"

    def __init__(self, owner):
        super().__init__(owner)
        object.__setattr__(self, "_run_lock", threading.Lock())
        object.__setattr__(self, "_history_lock", threading.RLock())
        object.__setattr__(self, "_schedule_lock", threading.Lock())

    @staticmethod
    def _now_text() -> str:
        return datetime.now(
            pytz.timezone(settings.TZ)
        ).isoformat(timespec="seconds")

    @staticmethod
    def _now() -> datetime:
        return datetime.now(pytz.timezone(settings.TZ))

    @classmethod
    def _resolve_provider(cls, provider: str) -> Optional[CheckinProvider]:
        return cls._PROVIDERS.get(str(provider or "").strip().lower())

    def get_checkin_provider_specs(self) -> list[Dict[str, Any]]:
        """向配置校验与调度注册暴露稳定、无客户端对象的提供方信息。"""
        return [
            {
                "key": item.key,
                "name": item.name,
                "credential_attrs": item.credential_attrs,
            }
            for item in self._PROVIDERS.values()
        ]

    def _load_history(self, provider: CheckinProvider) -> list[Dict[str, Any]]:
        stored = self.get_data(provider.history_key) or []
        if not isinstance(stored, list):
            return []
        return [
            copy.deepcopy(item)
            for item in stored[-self._HISTORY_LIMIT:]
            if isinstance(item, dict)
        ]

    def _save_history(
            self,
            provider: CheckinProvider,
            record: Dict[str, Any],
    ) -> None:
        with self._history_lock:
            history = self._load_history(provider)
            history.append(copy.deepcopy(record))
            self.save_data(
                provider.history_key,
                history[-self._HISTORY_LIMIT:],
            )

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
        normalized_limit = max(1, min(int(limit or 20), self._HISTORY_LIMIT))
        return {
            "total": len(history),
            "limit": normalized_limit,
            "items": list(reversed(history))[:normalized_limit],
        }

    @staticmethod
    def _public_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """移除验证码、HTTP 与错误码等仅供内部诊断的字段。"""
        return {
            key: copy.deepcopy(record.get(key))
            for key in (
                "id", "provider", "provider_name", "executed_at", "trigger",
                "mode", "success", "status", "points_change",
                "points_before", "points_after", "signin_days",
            )
        }

    def list_checkin_details(
            self,
            provider: str = "",
            limit: int = 10,
    ) -> Dict[str, Any]:
        """按渠道集中返回供智能体与远程命令展示的签到详情。"""
        provider_key = str(provider or "").strip().lower()
        if provider_key in {"all", "全部"}:
            provider_key = ""
        if provider_key:
            adapter = self._resolve_provider(provider_key)
            if adapter is None:
                return {"success": False, "message": "不支持的签到提供方"}
            providers = [adapter]
        else:
            providers = list(self._PROVIDERS.values())
        normalized_limit = max(1, min(int(limit or 10), self._HISTORY_LIMIT))
        channels = []
        with self._history_lock:
            for item in providers:
                history = self._load_history(item)
                records = [
                    self._public_record(record)
                    for record in reversed(history)
                ][:normalized_limit]
                for record in records:
                    if record.get("trigger") not in {"scheduled", "retry"}:
                        record["trigger"] = "manual"
                channels.append({
                    "provider": item.key,
                    "provider_name": item.name,
                    "total": len(history),
                    "items": records,
                })
        total = sum(item["total"] for item in channels)
        return {
            "success": True,
            "message": f"共查询到 {total} 条签到记录",
            "data": {"channels": channels, "total": total},
        }

    def _notify_checkin(
            self,
            provider: CheckinProvider,
            record: Dict[str, Any],
    ) -> None:
        if not self._notify:
            return
        delta = record.get("points_change")
        delta_text = (
            f"{int(delta):+d}"
            if isinstance(delta, (int, float)) else "未知"
        )
        balance = record.get("points_after")
        mode = "赌狗签到" if record.get("mode") == "gambler" else "普通签到"
        text = (
            f"模式：{mode}\n"
            f"结果：{record.get('status') or '未知'}\n"
            f"积分变化：{delta_text}\n"
            f"当前积分：{balance if balance is not None else '未知'}\n"
            f"累计签到：{record.get('signin_days') or 0} 天\n"
            f"消息：{record.get('message') or '-'}"
        )
        self.post_message(
            mtype=self._notification_type,
            title=(
                f"【网盘订阅助手】{provider.name} 签到完成"
                if record.get("success")
                else f"【网盘订阅助手】{provider.name} 签到失败"
            ),
            text=text,
        )

    def _build_record(
            self,
            provider: CheckinProvider,
            trigger: str,
            mode: str,
            result: Optional[Dict[str, Any]] = None,
            error: Optional[Exception] = None,
    ) -> Dict[str, Any]:
        data = result or {}
        success = bool(data.get("success")) if result is not None else False
        default_message = "" if result is not None else str(error or "签到失败")
        return {
            "id": f"{provider.key}-{uuid.uuid4().hex}",
            "provider": provider.key,
            "provider_name": provider.name,
            "executed_at": self._now_text(),
            "trigger": str(trigger or "manual"),
            "mode": mode,
            "success": success,
            "status": str(data.get("status") or (
                "签到成功" if success else "签到失败"
            )),
            "message": str(data.get("message") or default_message),
            "points_change": data.get("points_change"),
            "points_before": data.get("points_before"),
            "points_after": data.get("points_after"),
            "signin_days": data.get("signin_days"),
            "http_status": int(
                data.get("status_code")
                or getattr(error, "status_code", 0)
                or 0
            ),
            "error_code": str(
                data.get("error_code")
                or getattr(error, "code", "")
                or ("unexpected_error" if error is not None else "")
            ),
            "captcha_verified": bool(data.get("captcha_verified")),
        }

    def run_checkin(
            self,
            provider: str,
            trigger: str = "manual",
            mode: str = "",
    ) -> Dict[str, Any]:
        """执行一次提供方签到；同一插件实例不允许签到并发。"""
        adapter = self._resolve_provider(provider)
        if adapter is None:
            return {"success": False, "message": "不支持的签到提供方"}
        if not bool(getattr(self, f"_{adapter.key}_checkin_enabled", False)):
            return {
                "success": False,
                "message": f"{adapter.name} 每日签到未启用",
            }
        if not all(getattr(self, name, None) for name in adapter.credential_attrs):
            return {
                "success": False,
                "message": f"请先配置并保存 {adapter.name} 账号和密码",
            }
        default_mode = getattr(
            self, f"_{adapter.key}_checkin_mode", "normal"
        )
        normalized_mode = str(mode or default_mode).strip().lower()
        if normalized_mode not in {"normal", "gambler"}:
            return {
                "success": False,
                "message": f"{adapter.name} 签到模式无效",
            }
        if not self._run_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": f"{adapter.name} 签到正在执行，请稍后重试",
            }

        try:
            try:
                client_getter = getattr(
                    self._search_handler, adapter.client_getter
                )
                result = client_getter().checkin(
                    is_gambler=normalized_mode == "gambler"
                )
                record = self._build_record(
                    adapter, trigger, normalized_mode, result=result
                )
            except Exception as error:
                if not isinstance(error, adapter.error_type):
                    logger.exception(f"{adapter.name} 签到异常：{error}")
                record = self._build_record(
                    adapter, trigger, normalized_mode, error=error
                )
            self._save_history(adapter, record)
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
            confirm_gambler: bool = False,
    ) -> Dict[str, Any]:
        """供智能体和远程命令复用的安全签到入口。"""
        provider_key = str(provider or "").strip().lower()
        if provider_key in {"all", "全部"}:
            provider_key = ""
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
        if requested_mode and requested_mode not in {"normal", "gambler"}:
            return {"success": False, "message": "签到模式仅支持 normal 或 gambler"}
        risky = [
            item.name
            for item in providers
            if (
                       requested_mode
                       or str(getattr(self, f"_{item.key}_checkin_mode", "normal"))
               ).lower() == "gambler"
        ]
        if risky and not confirm_gambler:
            return {
                "success": False,
                "message": (
                    f"{', '.join(risky)} 当前将执行赌狗签到，奖励会乘以 -1～3 的随机倍数，"
                    "最多扣除 3 积分；请明确确认后重试"
                ),
                "data": {"confirmation_required": True, "providers": risky},
            }

        items = []
        for item in providers:
            result = self.run_checkin(
                provider=item.key,
                trigger="manual",
                mode=requested_mode,
            )
            public_result = dict(result)
            if isinstance(result.get("data"), dict):
                public_result["data"] = self._public_record(result["data"])
            items.append({
                "provider": item.key,
                "provider_name": item.name,
                **public_result,
            })
        success = bool(items) and all(item.get("success") for item in items)
        return {
            "success": success,
            "message": (
                f"已完成 {len(items)} 个渠道签到"
                if success else f"已执行 {len(items)} 个渠道，存在签到失败"
            ),
            "data": {"items": items},
        }

    def _ready_providers(self) -> list[CheckinProvider]:
        return [
            provider
            for provider in self._PROVIDERS.values()
            if bool(getattr(
                self, f"_{provider.key}_checkin_enabled", False
            ))
               and all(
                getattr(self, name, None)
                for name in provider.credential_attrs
            )
        ]

    def _today_records(
            self,
            provider: CheckinProvider,
            today: str,
    ) -> list[Dict[str, Any]]:
        records = []
        for record in reversed(self._load_history(provider)):
            try:
                executed_at = datetime.fromisoformat(
                    str(record.get("executed_at") or "")
                )
            except ValueError:
                continue
            if executed_at.tzinfo is None:
                timezone = pytz.timezone(settings.TZ)
                executed_at = timezone.localize(executed_at)
            if executed_at.astimezone(
                    pytz.timezone(settings.TZ)
            ).date().isoformat() == today:
                records.append(record)
        return records

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

    def _load_schedule_state(
            self,
            today: str,
            providers: list[CheckinProvider],
    ) -> Dict[str, Any]:
        stored = self.get_data(self._SCHEDULE_STATE_KEY)
        if (
                isinstance(stored, dict)
                and stored.get("date") == today
                and stored.get("full_completed") is True
                and stored.get("retry_count") == self._configured_retry_count()
        ):
            return copy.deepcopy(stored)

        records_by_provider = {
            provider.key: self._today_records(provider, today)
            for provider in providers
        }
        full_completed = any(
            any(record.get("trigger") == "scheduled" for record in records)
            for records in records_by_provider.values()
        )
        retry_count = self._configured_retry_count()
        pending = []
        if full_completed and retry_count:
            for provider in providers:
                records = records_by_provider[provider.key]
                if not any(record.get("success") for record in records):
                    pending.append(provider.key)
        return {
            "date": today,
            "full_completed": full_completed,
            "retry_count": retry_count,
            "pending_providers": pending,
            "completed_retry_count": 0,
        }

    def _execute_scheduled_providers(
            self,
            providers: list[CheckinProvider],
            trigger: str,
    ) -> list[Dict[str, Any]]:
        results = []
        for provider in providers:
            result = self.run_checkin(
                provider=provider.key,
                trigger=trigger,
            )
            results.append({
                "provider": provider.key,
                "provider_name": provider.name,
                **result,
            })
        return results

    @staticmethod
    def _scheduled_result(
            results: list[Dict[str, Any]],
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

    def run_scheduled_checkins(self) -> Dict[str, Any]:
        """单任务入口：每天首次全量签到，随后仅重试失败渠道。"""
        if not self._schedule_lock.acquire(blocking=False):
            return {
                "success": False,
                "message": "签到调度正在执行",
                "data": {"skipped": True, "items": []},
            }
        try:
            now = self._now()
            today = now.date().isoformat()
            providers = self._ready_providers()
            if not providers:
                return self._scheduled_result([], "scheduled")

            state = self._load_schedule_state(
                today=today,
                providers=providers,
            )
            if not state["full_completed"]:
                results = self._execute_scheduled_providers(
                    providers, trigger="scheduled"
                )
                retry_count = self._configured_retry_count()
                state.update({
                    "full_completed": True,
                    "retry_count": retry_count,
                    "pending_providers": [
                        item["provider"]
                        for item in results
                        if retry_count and not item.get("success")
                    ],
                    "completed_retry_count": 0,
                })
                self.save_data(self._SCHEDULE_STATE_KEY, state)
                return self._scheduled_result(results, "scheduled")

            retry_count = self._configured_retry_count()
            completed_retry_count = int(
                state.get("completed_retry_count", 0) or 0
            )
            if not retry_count or completed_retry_count >= retry_count:
                return {
                    "success": True,
                    "message": "签到异常重试已关闭或已完成",
                    "data": {"skipped": True, "items": []},
                }

            provider_by_key = {provider.key: provider for provider in providers}
            pending = []
            for key in state.get("pending_providers", []):
                provider = provider_by_key.get(str(key))
                if provider is None:
                    continue
                if any(
                        record.get("success")
                        for record in self._today_records(provider, today)
                ):
                    continue
                pending.append(provider)

            if not pending:
                state["pending_providers"] = []
                state["completed_retry_count"] = retry_count
                self.save_data(self._SCHEDULE_STATE_KEY, state)
                return {
                    "success": True,
                    "message": "没有需要重试的签到渠道",
                    "data": {"skipped": True, "items": []},
                }

            results = self._execute_scheduled_providers(
                pending, trigger="retry"
            )
            state["pending_providers"] = [
                item["provider"]
                for item in results
                if not item.get("success")
            ]
            state["completed_retry_count"] = completed_retry_count + 1
            self.save_data(self._SCHEDULE_STATE_KEY, state)
            return self._scheduled_result(results, "retry")
        finally:
            self._schedule_lock.release()
