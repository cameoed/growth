"""Local native/browser API boundary, without an account or authentication layer."""

import ipaddress
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

DESKTOP_API_VERSION = "1"


def require_local(request: Request):
    try:
        local = ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        local = False
    if not local or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(403, "This API is available only on this computer")
    origin = request.headers.get("origin")
    if origin and (
        urlsplit(origin).netloc != request.url.netloc
        or urlsplit(origin).scheme != request.url.scheme
    ):
        raise HTTPException(403, "Use this computer's application to make this change")
