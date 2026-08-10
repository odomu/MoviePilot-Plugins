"""通用签到 API。"""

from typing import Any, Dict, Optional

from .. import OwnerDelegator


class CheckinApi(OwnerDelegator):
    def api_vue_checkin(
            self,
            provider: str,
            payload: Optional[Dict[str, Any]] = None,
    ) -> dict:
        request = payload or {}
        mode = str(request.get("mode") or "").strip().lower()
        if "is_gambler" in request:
            mode = "gambler" if bool(request.get("is_gambler")) else "normal"
        return self.run_checkin(
            provider=provider,
            trigger="manual",
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
