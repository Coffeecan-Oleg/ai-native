import os
import hmac
import re
import stat
from pathlib import Path

from prometheus_client import CONTENT_TYPE_LATEST
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway_mcp.services.auth import auth_enabled
from gateway_mcp.services.observability import metrics_response
from gateway_mcp.services.storage import health as storage_health
from gateway_mcp.config import release_sha


def _metrics_token() -> str | None:
    """A dedicated file credential; never accept MCP tokens, cookies or query keys."""
    name = os.getenv("GATEWAY_METRICS_TOKEN_FILE", "")
    if not name or not Path(name).is_absolute():
        return None
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 257:
                return None
            if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
                return None
            token = source.read(258).decode("ascii").strip()
    except (OSError, UnicodeError, ValueError):
        return None
    return token if re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) else None


def register_health_routes(mcp):
    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> Response:
        db = storage_health()
        ok = not db.get("enabled") or bool(db.get("ok"))
        return JSONResponse(
            {
                "ok": ok,
                "service": "gateway-mcp",
                "release_sha": release_sha(),
                "auth_enabled": auth_enabled(),
                "postgres": db,
            },
            status_code=200 if ok else 503,
        )

    @mcp.custom_route("/metrics", methods=["GET"], include_in_schema=False)
    async def metrics(request: Request) -> Response:
        # Closed by default, also when the main Gateway authentication is disabled.
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if os.getenv("GATEWAY_METRICS_ENABLED", "").strip().casefold() != "true":
            return Response(status_code=404, headers=headers)
        token = _metrics_token()
        if token is None:
            return Response(status_code=503, headers=headers)
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + token).encode("ascii")):
            return Response(status_code=401, headers={**headers, "WWW-Authenticate": 'Bearer realm="gateway-metrics"'})
        return Response(metrics_response(), media_type=CONTENT_TYPE_LATEST, headers=headers)
