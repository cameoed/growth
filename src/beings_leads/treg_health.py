"""Bounded, free credential checks; no research endpoints or secret metadata."""

import asyncio
import hashlib
import time

import httpx

from .treg_people import PEOPLE_SEARCH_ENDPOINT, resolve_treg_credentials

_cache = {}
_lock = asyncio.Lock()


async def check_health():
    credentials = resolve_treg_credentials()
    if credentials is None:
        return {"configured": False, "ready": False, "status": "not_configured"}
    key = hashlib.sha256(
        (credentials.token + credentials.org + credentials.base_url).encode()
    ).hexdigest()
    async with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            return dict(cached[1])
        headers = {"X-Treg-Token": credentials.token}
        if credentials.org:
            headers["X-Treg-Org"] = credentials.org
        result = {"configured": True, "ready": False, "status": "unavailable"}
        try:
            async with httpx.AsyncClient(
                timeout=5, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.get(credentials.base_url + "/auth/me", headers=headers)
                response.raise_for_status()
                me = response.json()
                result.update(ready=True, status="authenticated", role=me.get("role"))
                org = me.get("org_id")
                if isinstance(org, int):
                    balance = await client.get(
                        f"{credentials.base_url}/orgs/{org}/balance",
                        headers=headers, params={"limit": 1},
                    )
                    if balance.status_code == 200:
                        amount = balance.json().get("balance_micro")
                        if isinstance(amount, int):
                            result["balance_microusd"] = amount
                            if amount <= 0:
                                result.update(ready=False, status="balance_empty")
                # Authentication does not grant catalog use. Read the existing
                # provider audit without starting research or spending credits.
                calls = await client.get(
                    credentials.base_url + "/calls", headers=headers, params={"limit": 10},
                )
                if calls.status_code == 200:
                    history = calls.json()
                    if isinstance(history, list):
                        latest = next((call for call in history
                                       if isinstance(call, dict)
                                       and call.get("tool_name") == PEOPLE_SEARCH_ENDPOINT), None)
                        if latest and latest.get("status_code") in {401, 403}:
                            result.update(ready=False, status="access_denied")

        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        _cache.clear()
        _cache[key] = (time.monotonic(), result)
        return dict(result)
