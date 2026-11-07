from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from gateway_mcp.services.auth import (
    auth_enabled,
    current_actor,
    token_ttl_seconds,
    verify_gateway_token_claims,
)
from gateway_mcp.services.policy import GatewayActor, actor_from_claims
from gateway_mcp.config import public_url


def web_actor(request: Request) -> GatewayActor | None:
    if not auth_enabled():
        return current_actor()

    authorization = request.headers.get("authorization", "")
    token = ""
    from gateway_mcp.services import bitrix_embed
    frame_cookie = ''
    if authorization.casefold().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        frame_cookie = request.cookies.get(bitrix_embed.COOKIE,'') if bitrix_embed.enabled() else ''
        token = frame_cookie or request.cookies.get("gateway_token", "")
    if not token:
        return None
    try:
        claims = verify_gateway_token_claims(token)
        if claims.get('browser_channel') != (bitrix_embed.CHANNEL if frame_cookie else None):
            return None
        actor = actor_from_claims(claims)
        # A constrained MCP credential is never a browser/admin session, even
        # when its identity is the same as the human owner.
        if actor.tool_allowlist is not None:
            return None
    except Exception:
        return None
    from gateway_mcp.services.platforma_onboarding import check_web_path, enabled
    if enabled() and not check_web_path(actor, request.url.path):
        from starlette.exceptions import HTTPException
        raise HTTPException(403, "Доступен только личный вход и статус заявки: /join")
    return actor


def with_gateway_cookie(response: Response, token: str) -> Response:
    response.set_cookie(
        "gateway_token",
        token,
        max_age=token_ttl_seconds(),
        httponly=True,
        secure=public_url().startswith("https://"),
        samesite="lax",
        path="/",
    )
    return response


def clear_gateway_cookie(response: Response) -> Response:
    response.delete_cookie(
        "gateway_token",
        path="/",
        secure=public_url().startswith("https://"),
        httponly=True,
        samesite="lax",
    )
    from gateway_mcp.services import bitrix_embed
    return bitrix_embed.clear_session(response) if bitrix_embed.enabled() else response


def login_redirect(next_url: str = "/credentials") -> RedirectResponse:
    from gateway_mcp.services import bitrix_identity
    if bitrix_identity.enabled():
        return RedirectResponse('/auth/bitrix24/login?next='+quote(bitrix_identity.safe_next(next_url),safe=''),status_code=303)
    return RedirectResponse(f"/auth/yandex/login?next={quote(next_url, safe='/?:=&')}", status_code=303)
