"""Extra project boundaries for the training addon, before tool dispatch.

The private, read-only mounted profile is authoritative. Existing OAuth scopes,
tool allowlists, knowledge ACLs and backend resource policies still apply.
"""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from gateway_mcp.services.security_guard import authorize

READ_TOOLS = {
    "gateway_corporate_skill_list", "gateway_corporate_skill_read",
    "gateway_access_profile", "gateway_search_tools", "gateway_call_tool",
    "gateway_knowledge_space_list", "gateway_knowledge_space_get",
    "gateway_knowledge_document_get", "gateway_knowledge_search",
    "gateway_memory_search", "gateway_usage_dashboard", "gateway_telemetry_skill_stats",
}
WRITE_TOOLS = {
    "gateway_knowledge_space_create", "gateway_knowledge_document_put",
    "gateway_memory_write", "gateway_usage_import",
    "gateway_telemetry_skill_started", "gateway_telemetry_skill_completed",
    "gateway_telemetry_skill_failed",
}
ROUTES = {
    "platforma.context.get", "platforma.services.list", "platforma.services.get",
    "platforma.requests.list", "platforma.requests.get",
}
WRITE_ROUTES = {"platforma.requests.create": "POST", "platforma.requests.update": "PATCH"}


def profile_for(actor):
    configured = os.getenv("GATEWAY_ADDON_PROFILES_FILE", "")
    if not configured:
        return None
    # No default profile, owner fallback or cache after revocation.
    try:
        data = json.loads(Path(configured).read_text(encoding="utf-8"))
        if data.get("schema") != 1:
            raise ValueError("Invalid addon profile")
        profile = data["profiles"].get(actor.subject)
        if profile is None:
            from gateway_mcp.services.platforma_onboarding import enabled, reader_profile
            if not enabled():
                raise PermissionError("addon_profile_unavailable")
            try:
                profile = reader_profile(actor)
            except PermissionError:
                raise
            except Exception:
                raise PermissionError("addon_reader_status_unavailable") from None
        if profile.get("project") != "APP11":
            raise ValueError("Invalid addon profile")
        return profile
    except (OSError, ValueError, KeyError, TypeError):
        raise PermissionError("addon_profile_unavailable") from None


def _check(profile, actor, resource, kind, operation):
    decision = authorize(profile, subject=actor.subject, project="APP11",
                         resource=resource, resource_kind=kind, operation=operation)
    if not decision.allowed:
        raise PermissionError("addon_" + decision.reason)


def _space(profile, actor, space):
    from gateway_mcp.services.knowledge import _personal_slug
    own = "personal:" + _personal_slug(actor)
    canonical = "personal" if space == own else space
    if canonical not in profile.get("spaces", []):
        raise PermissionError("addon_space_denied")
    return canonical


def _document(profile, space, document):
    allowed = profile.get("documents", {}).get(space, {})
    if document not in allowed:
        raise PermissionError("addon_document_denied")
    return allowed[document]


def check_call(actor, name, arguments):
    profile = profile_for(actor)
    if profile is None:
        return None
    operation = "read" if name in READ_TOOLS else "write"
    if name not in READ_TOOLS | WRITE_TOOLS:
        raise PermissionError("addon_tool_denied")
    _check(profile, actor, "mcp:" + name, "tool", operation)
    if name == "gateway_call_tool":
        from gateway_mcp.config import read_json, tools_file
        route = next((r for r in read_json(tools_file(), {"tools":[]})["tools"]
                      if r.get("name") == arguments.get("tool_name")), None)
        if not route or route.get("transport") != "platforma-demo" or route.get("backend") != "platforma":
            raise PermissionError("addon_backend_route_denied")
        route_name = route["name"]
        if route_name in ROUTES and route.get("http_method") == "GET" and route.get("scope") == "platforma:read":
            _check(profile, actor, route_name, "backend_route", "read")
        elif route_name in WRITE_ROUTES and route.get("http_method") == WRITE_ROUTES[route_name] and route.get("scope") == "platforma:write":
            from gateway_mcp.services.platforma_onboarding import can_write_sandbox
            if not can_write_sandbox(actor):
                raise PermissionError("addon_sandbox_participant_required")
            _check(profile, actor, route_name, "backend_route", "write")
        else:
            raise PermissionError("addon_backend_route_denied")
    elif name.startswith("gateway_telemetry_skill_") and name != "gateway_telemetry_skill_stats":
        limits = profile.get("skill_telemetry", {})
        skill = arguments.get("skill_id", "")
        expected = limits.get("skills", {}).get(skill)
        if expected is None:
            from gateway_mcp.services.skill_library import telemetry_descriptor
            expected = telemetry_descriptor(actor, skill)
        if not expected or arguments.get("project", "") != limits.get("project"):
            raise PermissionError("addon_telemetry_skill_denied")
        if arguments.get("skill_pack", "") != expected["pack"] or arguments.get("skill_version", "") != expected["version"]:
            raise PermissionError("addon_telemetry_version_denied")
        if arguments.get("agent", "") not in limits.get("agents", []):
            raise PermissionError("addon_telemetry_agent_denied")
        if json.loads(arguments.get("metadata_json", "{}")) != {}:
            raise PermissionError("addon_telemetry_metadata_denied")
        if arguments.get("client", "") not in ["", *limits.get("clients", ["codex-desktop"])]:
            raise PermissionError("addon_telemetry_client_denied")
        routes = json.loads(arguments.get("mcp_routes_json", "[]"))
        missing = json.loads(arguments.get("missing_scopes_json", "[]"))
        if not isinstance(routes, list) or len(routes) > 7 or not all(isinstance(r, str) and r in ROUTES | set(WRITE_ROUTES) for r in routes):
            raise PermissionError("addon_telemetry_routes_denied")
        if not isinstance(missing, list) or len(missing) > 9 or not all(isinstance(r, str) and r in limits.get("scopes", []) for r in missing):
            raise PermissionError("addon_telemetry_scopes_denied")
        import re
        for key in ("session_id", "correlation_id", "error_class"):
            value = arguments.get(key, "")
            if not isinstance(value, str) or len(value) > 100 or (value and not re.fullmatch(r"[a-zA-Z0-9_.:-]+", value)):
                raise PermissionError("addon_telemetry_identifier_denied")
        duration = arguments.get("duration_ms", 0)
        if type(duration) is not int or not 0 <= duration <= 86400000:
            raise PermissionError("addon_telemetry_duration_denied")
    elif name == "gateway_telemetry_skill_stats":
        if arguments.get("actor_subject", "") not in ("", actor.subject):
            raise PermissionError("addon_foreign_telemetry_denied")
        if arguments.get("project", "") not in ("", profile.get("skill_telemetry", {}).get("project")):
            raise PermissionError("addon_telemetry_project_denied")
    elif name == "gateway_knowledge_space_create":
        if arguments.get("slug") != "organizational-awareness" or json.loads(arguments.get("members_json", "[]")) != []:
            raise PermissionError("addon_space_creation_denied")
        _check(profile, actor, "space:organizational-awareness", "container", "create")
    elif name in {"gateway_knowledge_space_get", "gateway_knowledge_document_get",
                  "gateway_knowledge_document_put", "gateway_knowledge_search"}:
        space = _space(profile, actor, arguments.get("space", ""))
        if name == "gateway_knowledge_document_get":
            _document(profile, space, arguments.get("document", ""))
        elif name == "gateway_knowledge_document_put":
            path = arguments.get("path", "")
            spec = _document(profile, space, path)
            if arguments.get("document_id") != spec["document_id"] or arguments.get("kind", "note") != spec["kind"]:
                raise PermissionError("addon_document_identity_denied")
            if path.startswith("skills/"):
                from gateway_mcp.services.knowledge import _resolve_space, _safe_child
                resolved = _resolve_space(actor, arguments["space"], required_role="writer")
                if _safe_child(resolved["path"], path).exists():
                    raise PermissionError("addon_existing_skill_protected")
    elif name in {"gateway_memory_write", "gateway_memory_search"}:
        if arguments.get("scope", "user" if name.endswith("write") else "") != "user":
            raise PermissionError("addon_user_memory_only")
        if arguments.get("subject", "") not in ("", actor.subject):
            raise PermissionError("addon_foreign_memory_denied")
        if name.endswith("search") and (arguments.get("include_sources", True) or arguments.get("include_yonote", True)):
            raise PermissionError("addon_external_memory_search_denied")
        if name.endswith("write") and (arguments.get("tier", "short") != "short" or
                arguments.get("source_type", "") != "knowledge" or
                arguments.get("ttl_days", 0) not in range(1, 8)):
            raise PermissionError("addon_installation_memory_only")
    elif name == "gateway_usage_import":
        if arguments.get("project") not in profile.get("usage_projects", []):
            raise PermissionError("addon_usage_project_denied")
    return profile


def filter_result(actor, name, result, profile):
    if not profile or name not in {"gateway_knowledge_space_list", "gateway_knowledge_search"}:
        return result
    from gateway_mcp.services.knowledge import _personal_slug
    allowed = set(profile.get("spaces", []))
    if not profile.get("reader_only"):
        allowed.add("personal:" + _personal_slug(actor))
    root = getattr(result, "root", result)
    for item in getattr(root, "content", []):
        if getattr(item, "type", "") != "text":
            continue
        payload = json.loads(item.text)
        if payload.get("ok"):
            if name == "gateway_knowledge_space_list":
                payload["spaces"] = [s for s in payload.get("spaces", [])
                                     if s.get("space") in allowed]
                payload["count"] = len(payload["spaces"])
            else:
                def permitted(document):
                    canonical = document.get("space", "")
                    if canonical == "personal:" + _personal_slug(actor):
                        canonical = "personal"
                    return document.get("path") in profile.get("documents", {}).get(canonical, {})
                payload["documents"] = [d for d in payload.get("documents", []) if permitted(d)]
                payload["count"] = len(payload["documents"])
            item.text = json.dumps(payload, ensure_ascii=False)
    # Knowledge tools return text, not structuredContent; never pass an unfiltered copy.
    if getattr(root, "structuredContent", None) is not None:
        root.structuredContent = None
    return result


@asynccontextmanager
async def write_lock(enabled):
    if not enabled:
        yield
        return
    import fcntl
    from gateway_mcp.services.knowledge import vault_root
    directory = vault_root() / ".addon-security"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "writes.lock").open("a") as handle:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise PermissionError("addon_write_busy")
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def register(server):
    from mcp.types import CallToolRequest, ServerResult, CallToolResult, TextContent, ListToolsRequest, ListToolsResult
    from gateway_mcp.services.auth import current_actor
    from gateway_mcp.services.observability import audit_event
    handler = server._mcp_server.request_handlers[CallToolRequest]
    list_handler = server._mcp_server.request_handlers.get(ListToolsRequest)

    async def reader_tools(request):
        from gateway_mcp.services.platforma_onboarding import enabled, is_owner, TOOLS
        actor = current_actor()
        if not enabled() or is_owner(actor):
            return await list_handler(request)
        try:
            profile = profile_for(actor)
            if not profile or not profile.get("reader_only"):
                raise PermissionError("Reader profile required")
        except Exception:
            return ServerResult(ListToolsResult(tools=[]))
        result = await list_handler(request)
        root = getattr(result, "root", result)
        allowed = {row['id'].removeprefix('mcp:') for row in profile.get('resources', [])
                   if row.get('kind') == 'tool' and row.get('id', '').startswith('mcp:')}
        if actor.tool_allowlist is not None:
            allowed.intersection_update(actor.tool_allowlist)
        root.tools = [t for t in root.tools if t.name in allowed]
        return result

    if list_handler is not None:
        server._mcp_server.request_handlers[ListToolsRequest] = reader_tools

    async def guarded(request):
        actor = current_actor()
        name = request.params.name
        args = request.params.arguments or {}
        try:
            async with write_lock(bool(os.getenv("GATEWAY_ADDON_PROFILES_FILE")) and name in WRITE_TOOLS):
                profile = check_call(actor, name, args)
                if profile and profile.get("reader_only") and name == "gateway_knowledge_space_list":
                    request.params.arguments = {**args, "include_personal": False}
                result = await handler(request)
                return filter_result(actor, name, result, profile)
        except PermissionError as exc:
            audit_event(event="addon_policy", actor=actor, tool=name,
                        system="addon-security", decision="deny",
                        scope="", status="denied",
                        arguments={}, error=str(exc))
            return ServerResult(CallToolResult(
                content=[TextContent(type="text", text=json.dumps({"ok":False,"error":str(exc)}))],
                isError=True))
    server._mcp_server.request_handlers[CallToolRequest] = guarded

