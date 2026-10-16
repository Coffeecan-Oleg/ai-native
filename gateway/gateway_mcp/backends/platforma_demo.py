"""Bounded read-only routes for a bundled fictional Platforma snapshot."""
import hashlib
import inspect
from functools import lru_cache
from typing import Any

from gateway_mcp.backends.common import BackendRouteError
from gateway_mcp.backends.platforma_demo_store import DATA_FILE, DemoStore, QueryError

ROUTES = {
    "platforma.context.get": "context",
    "platforma.services.list": "list_services",
    "platforma.services.get": "get_service",
    "platforma.requests.list": "list_requests",
    "platforma.requests.get": "get_request",
}


@lru_cache(maxsize=1)
def _store() -> DemoStore:
    return DemoStore()


def _context() -> dict[str, Any]:
    store = _store()
    return {
        **store.meta(),
        "dataset_sha256": hashlib.sha256(DATA_FILE.read_bytes()).hexdigest(),
        "visible_services": len(store.services),
        "visible_requests": len(store.requests),
        "units": [{"id": row["id"], "name": row["name"]} for row in store.units.values()],
        "periods": store.periods,
        "default_period": store.as_of[:7],
        "source": "platforma-training:synthetic.json",
        "authentication": "GatewayMCP personal OAuth; no local demo identity",
        "overdue_definition": "Unfinished requests with due_at before snapshot_as_of.",
    }


def _validate_arguments(arguments: dict[str, Any]) -> None:
    for key, value in arguments.items():
        if value is None:
            continue
        if key in {"limit", "offset"}:
            if type(value) is not int:
                raise BackendRouteError("limit and offset must be integers")
        elif key == "overdue_only":
            if type(value) is not bool:
                raise BackendRouteError("overdue_only must be boolean")
        elif key == "query":
            if not isinstance(value, str) or len(value) > 2000:
                raise BackendRouteError("query must be a bounded string")
        elif not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise BackendRouteError("Filters and identifiers must be nonempty bounded strings")
    if arguments.get("status") not in (None, "active", "completed", "early", "exact", "late"):
        raise BackendRouteError("Unknown request status")
    if arguments.get("sort") not in (None, "due_asc", "due_desc", "created_desc", "id"):
        raise BackendRouteError("Unknown request ordering")
    if arguments.get("priority") not in (None, "Обычный", "Высокий", "Критичный"):
        raise BackendRouteError("Unknown request priority")


async def _call_platforma_demo(route: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    name = str(route.get("name") or "")
    if name not in ROUTES or route.get("scope") != "platforma:read" or route.get("http_method") != "GET":
        raise BackendRouteError("Only declared read-only Platforma routes are supported")
    if not isinstance(arguments, dict):
        raise BackendRouteError("arguments must be a JSON object")
    operation = ROUTES[name]
    method = _context if operation == "context" else getattr(_store(), operation)
    try:
        inspect.signature(method).bind(**arguments)
    except TypeError:
        raise BackendRouteError("Unexpected or missing route arguments") from None
    _validate_arguments(arguments)
    if any(arguments.get(key) is None for key in ("service_id", "request_id") if key in arguments):
        raise BackendRouteError("Identifier cannot be null")
    try:
        data = method(**arguments)
    except QueryError as error:
        raise BackendRouteError(str(error)) from None
    return {"ok": True, "status": 200, "data": data}
