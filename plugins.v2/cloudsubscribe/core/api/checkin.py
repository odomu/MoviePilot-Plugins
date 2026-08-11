"""通用签到 API。"""

from typing import Any, Dict, Optional

from .. import OwnerDelegator


class CheckinApi(OwnerDelegator):
    def api_vue_checkin_overview(self, days: int = 7) -> dict:
        return {
            "success": True,
            "data": self.get_checkin_overview(days=days),
        }

    def api_vue_checkin(
            self,
            provider: str,
            payload: Optional[Dict[str, Any]] = None,
    ) -> dict:
        request = payload or {}
        mode = str(request.get("mode") or "").strip().lower()
        if "is_gambler" in request:
            mode = "gambler" if bool(request.get("is_gambler")) else "normal"
        adapter = self._resolve_provider(provider)
        if adapter is not None:
            effective_mode = mode or str(
                getattr(self, f"_{adapter.key}_checkin_mode", "normal")
            ).strip().lower()
            if (
                    effective_mode in adapter.risky_modes
                    and not bool(
                request.get("confirm_risky")
                or request.get("confirm_gambler")
            )
            ):
                return {
                    "success": False,
                    "message": (
                        f"{adapter.name} {effective_mode} 模式需要先确认风险"
                    ),
                    "data": {"confirmation_required": True},
                }
        return self.start_manual_checkin(
            provider=provider,
            mode=mode,
        )

    def api_vue_checkin_history(
            self,
            provider: str,
            limit: int = 20,
    ) -> dict:
        history = self.get_checkin_history(provider=provider, limit=limit)
        if history is None:
            return {"success": False, "message": "不支持的签到提供方"}
        return {"success": True, "data": history}
