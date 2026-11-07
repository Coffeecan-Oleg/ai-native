import math
import time
from typing import Any

from gateway_mcp.services.auth import current_actor
from gateway_mcp.services.observability import audit_event, observe_tool_call, record_policy_deny


def _timed_arguments(arguments: dict[str, Any], started_at: float) -> dict[str, Any]:
    """Server measurement only; old/unmeasurable events remain explicitly untimed."""
    result = dict(arguments)
    for name in ("gateway_duration_ms", "gateway_timing_kind"):
        result.pop(name, None)
    try:
        elapsed = time.perf_counter() - started_at
        if math.isfinite(elapsed) and 0 <= elapsed <= 86400:
            result.update(gateway_duration_ms=round(elapsed * 1000),
                          gateway_timing_kind="tool_run_v1")
    except (TypeError, ValueError, OverflowError):
        pass
    return result


def finish_tool(
    *,
    tool: str,
    system: str,
    started_at: float,
    status: str,
    decision: str,
    scope: str,
    arguments: dict[str, Any],
    error: str = "",
) -> None:
    measured = _timed_arguments(arguments, started_at) if tool == "gateway_call_tool" else arguments
    observe_tool_call(
        tool=tool,
        system=system,
        status=status,
        decision=decision,
        started_at=started_at,
    )
    audit_event(
        event="tool_call",
        actor=current_actor(),
        tool=tool,
        system=system,
        decision=decision,
        status=status,
        scope=scope,
        arguments=measured,
        error=error,
        channel="mcp",
    )


def deny(tool: str, scope: str, reason: str) -> None:
    record_policy_deny(tool, scope, reason)
