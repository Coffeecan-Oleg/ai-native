from importlib import import_module
from asyncio import to_thread
from typing import Any

from gateway_mcp.backends.common import BackendRouteError, _apply_argument_aliases


BACKEND_DISPATCH: dict[str, tuple[str, str]] = {
    "unisender-readonly": ("gateway_mcp.backends.unisender_readonly", "_call_unisender_readonly"),
    "platforma-demo": ("gateway_mcp.backends.platforma_demo", "_call_platforma_demo"),
    "bitrix24-rest": ("gateway_mcp.backends.bitrix24", "_call_bitrix24"),
    "caldav": ("gateway_mcp.backends.caldav", "_call_caldav"),
    "gitlab-rest": ("gateway_mcp.backends.gitlab", "_call_gitlab"),
    "google-rest": ("gateway_mcp.backends.google", "_call_google"),
    "infra": ("gateway_mcp.backends.infra", "_call_infra"),
    "metrika-rest": ("gateway_mcp.backends.metrika", "_call_metrika"),
    "notification": ("gateway_mcp.backends.notifications", "_call_notifications"),
    "openrouter-audio": ("gateway_mcp.backends.openrouter", "_call_openrouter_audio"),
    "telegram": ("gateway_mcp.backends.telegram", "_call_telegram"),
    "tracker-rest": ("gateway_mcp.backends.tracker", "_call_tracker"),
    "webmaster-rest": ("gateway_mcp.backends.webmaster", "_call_webmaster"),
    "wordstat-rest": ("gateway_mcp.backends.wordstat", "_call_wordstat"),
    "yandex-disk-rest": ("gateway_mcp.backends.yandex_disk", "_call_yandex_disk"),
    "yandex-mail": ("gateway_mcp.backends.yandex_mail", "_call_yandex_mail"),
    "yonote-rpc": ("gateway_mcp.backends.yonote", "_call_yonote"),
}


def _backend_callable(transport: str):
    module_name, function_name = BACKEND_DISPATCH[transport]
    return getattr(import_module(module_name), function_name)


async def call_backend(route: dict[str, Any], arguments: dict[str, Any], *, admission=None) -> dict[str, Any]:
    arguments = _apply_argument_aliases(route, arguments)
    transport = route.get("transport")
    if transport in BACKEND_DISPATCH:
        from gateway_mcp.services import external_limits
        if external_limits.enabled(route):
            from gateway_mcp.services.auth import current_actor
            actor=current_actor()
            if admission is None:admission=await to_thread(external_limits.require,route,actor)
            external_limits.consume(admission,route,actor)
        try:
            from gateway_mcp.services import external_safety
            if external_safety.enabled(route):
                return await external_safety.invoke(admission,lambda:_backend_callable(str(transport))(route,arguments))
            return await _backend_callable(str(transport))(route, arguments)
        except external_limits.LimitError as exc:
            if exc.contacted and exc.code=='EXTERNAL_UPSTREAM_RATE_LIMITED':
                await to_thread(external_limits.cooldown,route,exc.retry)
            raise
    raise BackendRouteError(f"Unsupported backend transport: {transport or '<missing>'}")
