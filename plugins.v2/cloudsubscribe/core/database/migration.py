"""从 MoviePilot PluginData 自动迁移到插件私有数据库。"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock, RLock
from typing import Any, Dict, Iterable, List, Tuple

from app.db import SessionFactory
from app.db.models.plugindata import PluginData
from app.log import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .manager import CloudSubscribeDatabaseManager
from .repositories import CloudSubscribeRepositories


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class LegacySnapshot:
    """一次完整、可校验的旧 PluginData 数据快照。"""

    history: List[Dict[str, Any]] = field(default_factory=list)
    offline: Dict[str, Any] = field(default_factory=dict)
    checkin: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    schedule: Dict[str, Any] = field(default_factory=dict)
    budgets: Dict[str, Dict[str, int]] = field(default_factory=dict)
    accounts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    auth_sessions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    source_keys: List[str] = field(default_factory=list)
    runtime_keys: List[str] = field(default_factory=list)
    history_format: str = "none"
    offline_format: str = "none"
    duplicate_keys: int = 0
    plugin_data_count: int = 0

    @property
    def required(self) -> bool:
        return bool(self.source_keys or self.runtime_keys)

    def normalized(
            self, repositories: CloudSubscribeRepositories
    ) -> "LegacySnapshot":
        history_rows = repositories.history.normalize_records(self.history)
        checkin = {
            str(provider).strip().lower(): repositories.checkin.normalize_records(
                str(provider).strip().lower(), records
            )
            for provider, records in self.checkin.items()
            if str(provider).strip()
        }
        return LegacySnapshot(
            history=[copy.deepcopy(item["payload"]) for item in history_rows],
            offline=repositories.offline.normalize_values(self.offline),
            checkin=checkin,
            schedule=(
                repositories.schedule.normalize(self.schedule)
                if self.schedule else {}
            ),
            budgets={
                str(provider).strip().lower(): repositories.budget.normalize_values(
                    values
                )
                for provider, values in self.budgets.items()
                if str(provider).strip()
            },
            accounts=repositories.account.normalize_values(self.accounts),
            auth_sessions={
                str(provider).strip().lower(): repositories.auth.normalize_value(
                    value
                )
                for provider, value in self.auth_sessions.items()
                if str(provider).strip()
                if repositories.auth.normalize_value(value)
            },
            source_keys=list(self.source_keys),
            runtime_keys=list(self.runtime_keys),
            history_format=self.history_format,
            offline_format=self.offline_format,
            duplicate_keys=self.duplicate_keys,
            plugin_data_count=self.plugin_data_count,
        )

    def payload(self) -> Dict[str, Any]:
        return {
            "history": self.history,
            "offline": self.offline,
            "checkin": self.checkin,
            "schedule": self.schedule,
            "budgets": self.budgets,
            "accounts": self.accounts,
            "auth_sessions": self.auth_sessions,
        }

    def counts(self) -> Dict[str, int]:
        counts = {
            "history": len(self.history),
            "offline_pending": len(self.offline),
            "checkin_history": sum(len(items) for items in self.checkin.values()),
            "checkin_schedule": 1 if self.schedule else 0,
            "point_budgets": sum(len(items) for items in self.budgets.values()),
            "account_snapshots": len(self.accounts),
            "auth_sessions": len(self.auth_sessions),
        }
        for provider, items in sorted(self.checkin.items()):
            counts[f"checkin:{provider}"] = len(items)
        for provider, items in sorted(self.budgets.items()):
            counts[f"budget:{provider}"] = len(items)
        return counts

    def checksum(self) -> str:
        return _canonical_digest(self.payload())


class LegacyPluginDataStore:
    """只读旧 PluginData，并在自动迁移完成后清空插件数据。"""

    HISTORY_KEY = "history"
    OFFLINE_PENDING_KEY = "pending_offline_strm_v1"
    SCHEDULE_KEY = "checkin_schedule_state"
    SCHEMA_VERSION_KEY = "cloudsubscribe:storage_schema_version"
    HISTORY_PREFIX = "cloudsubscribe:history:item:"
    OFFLINE_PREFIX = "cloudsubscribe:offline:item:"
    RUNTIME_KEYS = {
        "account_info_cache",
        "dian115_auth_session",
        "juying_auth_session",
        "pansou_auth_session",
        "pinglian_auth_session",
    }

    def __init__(self, plugin_id: str):
        self.plugin_id = str(plugin_id or "CloudSubscribe")
        self._lock = RLock()

    @classmethod
    def is_persistent_key(cls, key: str) -> bool:
        normalized = str(key or "")
        return bool(
            normalized in {
                cls.HISTORY_KEY,
                cls.OFFLINE_PENDING_KEY,
                cls.SCHEDULE_KEY,
                cls.SCHEMA_VERSION_KEY,
            }
            or normalized.startswith(cls.HISTORY_PREFIX)
            or normalized.startswith(cls.OFFLINE_PREFIX)
            or normalized.endswith("_checkin_history")
            or normalized.endswith("_sub_points_history")
        )

    @classmethod
    def is_runtime_key(cls, key: str) -> bool:
        normalized = str(key or "")
        return normalized in cls.RUNTIME_KEYS or normalized.endswith("_auth_session")

    def _rows(self, db: Session) -> List[PluginData]:
        return list(db.scalars(
            select(PluginData)
            .where(PluginData.plugin_id == self.plugin_id)
            .order_by(PluginData.id)
        ).all())

    @staticmethod
    def _latest_rows(
            rows: Iterable[PluginData]
    ) -> Tuple[Dict[str, PluginData], int]:
        grouped: Dict[str, List[PluginData]] = {}
        for row in rows:
            grouped.setdefault(str(row.key), []).append(row)
        duplicate_count = sum(max(0, len(items) - 1) for items in grouped.values())
        return {key: items[-1] for key, items in grouped.items()}, duplicate_count

    @staticmethod
    def _split_collection(
            rows: Dict[str, PluginData], prefix: str
    ) -> List[Dict[str, Any]]:
        values = []
        for position, (key, row) in enumerate(rows.items()):
            if not key.startswith(prefix):
                continue
            value = copy.deepcopy(row.value)
            if isinstance(value, dict) and isinstance(value.get("value"), dict):
                values.append((int(value.get("order") or 0), value["value"]))
            elif isinstance(value, dict):
                values.append((position, value))
        values.sort(key=lambda item: item[0])
        return [copy.deepcopy(value) for _, value in values]

    @staticmethod
    def _split_mapping(
            rows: Dict[str, PluginData], prefix: str
    ) -> Dict[str, Any]:
        values = {}
        for key, row in rows.items():
            if not key.startswith(prefix):
                continue
            value = copy.deepcopy(row.value)
            if isinstance(value, dict) and value.get("key") is not None:
                values[str(value["key"])] = copy.deepcopy(value.get("value"))
        return values

    def read_snapshot(self) -> LegacySnapshot:
        with self._lock, SessionFactory() as db:
            all_rows = self._rows(db)
            rows, duplicate_count = self._latest_rows(all_rows)
        marker = rows.get(self.SCHEMA_VERSION_KEY)
        try:
            split_marker = int(marker.value or 0) >= 1 if marker else False
        except (TypeError, ValueError):
            split_marker = False

        split_history = self._split_collection(rows, self.HISTORY_PREFIX)
        legacy_history = rows.get(self.HISTORY_KEY)
        if split_history:
            history = split_history
            history_format = "split"
        elif legacy_history:
            value = copy.deepcopy(legacy_history.value) if legacy_history else []
            history = [item for item in value if isinstance(item, dict)] \
                if isinstance(value, list) else []
            history_format = "json"
        else:
            history = []
            history_format = "split" if split_marker else "none"

        split_offline = self._split_mapping(rows, self.OFFLINE_PREFIX)
        legacy_offline = rows.get(self.OFFLINE_PENDING_KEY)
        if split_offline:
            offline = split_offline
            offline_format = "split"
        elif legacy_offline:
            value = copy.deepcopy(legacy_offline.value) if legacy_offline else {}
            offline = value if isinstance(value, dict) else {}
            offline_format = "json"
        else:
            offline = {}
            offline_format = "split" if split_marker else "none"

        checkin = {}
        budgets = {}
        for key, row in rows.items():
            if key.endswith("_checkin_history"):
                provider = key[:-len("_checkin_history")]
                value = copy.deepcopy(row.value)
                checkin[provider] = [
                    item for item in value if isinstance(item, dict)
                ] if isinstance(value, list) else []
            elif key.endswith("_sub_points_history"):
                provider = key[:-len("_sub_points_history")]
                value = copy.deepcopy(row.value)
                budgets[provider] = value if isinstance(value, dict) else {}

        schedule_row = rows.get(self.SCHEDULE_KEY)
        schedule_value = copy.deepcopy(schedule_row.value) if schedule_row else {}
        account_row = rows.get("account_info_cache")
        account_value = copy.deepcopy(account_row.value) if account_row else {}
        auth_sessions = {}
        for key, row in rows.items():
            if not key.endswith("_auth_session"):
                continue
            provider = key[:-len("_auth_session")]
            value = copy.deepcopy(row.value)
            if provider and isinstance(value, dict):
                auth_sessions[provider] = value
        source_keys = sorted(
            key for key in rows
            if key != self.SCHEMA_VERSION_KEY and self.is_persistent_key(key)
        )
        runtime_keys = sorted(
            key for key in rows if self.is_runtime_key(key)
        )
        return LegacySnapshot(
            history=history,
            offline=offline,
            checkin=checkin,
            schedule=schedule_value if isinstance(schedule_value, dict) else {},
            budgets=budgets,
            accounts=account_value if isinstance(account_value, dict) else {},
            auth_sessions=auth_sessions,
            source_keys=source_keys,
            runtime_keys=runtime_keys,
            history_format=history_format,
            offline_format=offline_format,
            duplicate_keys=duplicate_count,
            plugin_data_count=len(all_rows),
        )

    def cleanup(self) -> int:
        """仅删除已经识别并完成迁移的 PluginData 数据键。"""
        with self._lock, SessionFactory() as db:
            targets = [
                row.id for row in self._rows(db)
                if self.is_persistent_key(str(row.key))
                   or self.is_runtime_key(str(row.key))
            ]
            if targets:
                db.execute(delete(PluginData).where(PluginData.id.in_(targets)))
            db.commit()
            return len(targets)


class CloudSubscribeDataMigration:
    """启动时将旧 PluginData 一次性迁入插件独立数据库。"""

    VERSION = 1

    def __init__(
            self,
            manager: CloudSubscribeDatabaseManager,
            repositories: CloudSubscribeRepositories,
            legacy: LegacyPluginDataStore,
    ):
        self.manager = manager
        self.repositories = repositories
        self.legacy = legacy
        self._execute_lock = Lock()

    def _target_snapshot(
            self,
            db: Session,
            checkin_providers: Iterable[str] = (),
            budget_providers: Iterable[str] = (),
    ) -> LegacySnapshot:
        checkin = self.repositories.checkin.load_all(db=db)
        for provider in checkin_providers:
            checkin.setdefault(str(provider).strip().lower(), [])
        budgets = self.repositories.budget.load_all(db=db)
        for provider in budget_providers:
            budgets.setdefault(str(provider).strip().lower(), {})
        return LegacySnapshot(
            history=self.repositories.history.list_all(db=db),
            offline=self.repositories.offline.load_all(db=db),
            checkin=checkin,
            schedule=self.repositories.schedule.load(db=db),
            budgets=budgets,
            accounts=self.repositories.account.load_all(db=db),
            auth_sessions=self.repositories.auth.load_all(db=db),
        )

    def _write_target(self, db: Session, source: LegacySnapshot) -> None:
        self.repositories.snapshot.replace_all(source.payload(), db=db)

    def _cleanup_source(self, source: LegacySnapshot) -> int:
        return self.legacy.cleanup() if source.plugin_data_count else 0

    def execute(self) -> Dict[str, Any]:
        if not self._execute_lock.acquire(blocking=False):
            raise RuntimeError("CloudSubscribe 数据迁移正在执行")
        try:
            source = self.legacy.read_snapshot().normalized(self.repositories)
            existing = self.repositories.migration.load()
            source_counts = source.counts()
            source_checksum = source.checksum()

            if not source.required:
                with self.manager.session() as db:
                    current_target = self._target_snapshot(db)
                current_counts = current_target.counts()
                current_checksum = current_target.checksum()
                cleaned = self._cleanup_source(source)
                if not (existing and existing.status == "completed"):
                    # 旧数据为空且没有已完成状态时，以当前目标建立基线；
                    # 后续运行时新增数据不再受这份迁移快照约束。
                    self.repositories.migration.save({
                        "version": self.VERSION,
                        "status": "completed",
                        "source_counts": current_counts,
                        "target_counts": current_counts,
                        "source_checksum": current_checksum,
                        "target_checksum": current_checksum,
                        "migrated_at": datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        ),
                        "source_cleaned": True,
                        "error": "",
                    })
                elif not bool(existing.source_cleaned) or existing.error:
                    self.repositories.migration.save({
                        "version": self.VERSION,
                        "source_cleaned": True,
                        "error": "",
                    })
                return {
                    "counts": current_counts,
                    "checksum": current_checksum,
                    "cleaned": cleaned,
                    "migrated": False,
                }

            can_reuse_target = bool(
                existing
                and existing.status == "completed"
                and int(existing.version or 0) >= self.VERSION
                and str(existing.source_checksum or "") == source_checksum
            )
            if can_reuse_target:
                with self.manager.session() as db:
                    current_target = self._target_snapshot(
                        db,
                        checkin_providers=source.checkin.keys(),
                        budget_providers=source.budgets.keys(),
                    )
                current_counts = current_target.counts()
                current_checksum = current_target.checksum()
                if (
                        current_counts == source_counts
                        and current_checksum == source_checksum
                ):
                    cleaned = self._cleanup_source(source)
                    self.repositories.migration.save({
                        "source_cleaned": True,
                        "error": "",
                    })
                    return {
                        "counts": current_counts,
                        "checksum": current_checksum,
                        "cleaned": cleaned,
                        "migrated": False,
                    }

            self.repositories.migration.save({
                "version": self.VERSION,
                "status": "running",
                "source_counts": source_counts,
                "target_counts": {},
                "source_checksum": source_checksum,
                "target_checksum": "",
                "migrated_at": "",
                "source_cleaned": False,
                "error": "",
            })
            try:
                with self.manager.session(write=True) as db:
                    self._write_target(db, source)
                    db.flush()
                    target = self._target_snapshot(
                        db,
                        checkin_providers=source.checkin.keys(),
                        budget_providers=source.budgets.keys(),
                    )
                    target_counts = target.counts()
                    target_checksum = target.checksum()
                    if target_counts != source_counts:
                        raise RuntimeError(
                            f"迁移数量校验失败：source={source_counts}, "
                            f"target={target_counts}"
                        )
                    if target_checksum != source_checksum:
                        raise RuntimeError(
                            "迁移内容校验失败：源数据与目标数据 checksum 不一致"
                        )
                    self.repositories.migration.save({
                        "version": self.VERSION,
                        "status": "completed",
                        "source_counts": source_counts,
                        "target_counts": target_counts,
                        "source_checksum": source_checksum,
                        "target_checksum": target_checksum,
                        "migrated_at": datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        ),
                        "source_cleaned": False,
                        "error": "",
                    }, db=db)
            except Exception as error:
                self.repositories.migration.save({
                    "version": self.VERSION,
                    "status": "failed",
                    "source_counts": source_counts,
                    "source_checksum": source_checksum,
                    "source_cleaned": False,
                    "error": str(error),
                })
                raise

            try:
                cleaned = self._cleanup_source(source)
            except Exception as error:
                self.repositories.migration.save({
                    "source_cleaned": False,
                    "error": f"旧 PluginData 清理失败：{error}",
                })
                raise
            self.repositories.migration.save({
                "source_cleaned": True,
                "error": "",
            })
            logger.info(
                f"CloudSubscribe 数据自动迁移完成：{target_counts}，"
                f"清理 PluginData {cleaned} 行"
            )
            return {
                "counts": target_counts,
                "checksum": target_checksum,
                "cleaned": cleaned,
                "migrated": True,
            }
        finally:
            self._execute_lock.release()
