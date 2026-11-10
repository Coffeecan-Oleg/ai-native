"""Finite, signed delegation limits for personal MCP OAuth credentials."""
import os
from dataclasses import replace

from gateway_mcp.services.policy import GatewayActor, has_scope


def _configured_list(name: str) -> set[str]:
    values = {part for part in os.getenv(name, "").replace(",", " ").split() if part}
    if any("*" in part for part in values):
        raise ValueError(f"{name} must contain only explicit values")
    return values


def limit_oauth_actor(
    actor: GatewayActor, requested_scopes: list[str] | None = None
) -> GatewayActor:
    from gateway_mcp.services.platforma_onboarding import oauth_reader_actor
    actor = oauth_reader_actor(actor)
    scopes = _configured_list("GATEWAY_MCP_OAUTH_SCOPES")
    tools = _configured_list("GATEWAY_MCP_OAUTH_TOOLS")
    if bool(scopes) != bool(tools):
        raise ValueError("MCP OAuth scopes and tools must be configured together")
    ceilings = [value for value in (scopes or None, actor.scope_ceiling,
                                  set(requested_scopes) if requested_scopes else None)
                if value is not None]
    if not ceilings:
        return actor
    ceiling = set(ceilings[0]).intersection(*ceilings[1:])
    if any("*" in part for part in ceiling):
        raise ValueError("Delegated OAuth scopes must be explicit")
    granted = {scope for scope in ceiling if has_scope(actor, scope)}
    if not granted:
        raise PermissionError("No permitted MCP OAuth scopes remain")
    tool_limits = [value for value in (tools or None, actor.tool_allowlist) if value is not None]
    allowlist = (tuple(sorted(set(tool_limits[0]).intersection(*tool_limits[1:])))
                 if tool_limits else None)
    return replace(actor, groups=(), scopes=tuple(sorted(granted)),
                   scope_ceiling=tuple(sorted(granted)), tool_allowlist=allowlist)
