"""Finite telemetry labels, independent of user identity and authorization.

These are self-reported application labels, not attested client identities.
Adding a label grants no OAuth scope, backend operation or private data access.
"""

_CLIENTS = (
    ('codex', 'codex-desktop'),
    ('claude', 'claude-desktop'),
    ('claude', 'claude-code'),
    ('chatgpt', 'chatgpt'),
    ('opencode', 'opencode'),
    ('openclaw', 'openclaw'),
)


def telemetry_agents():
    return tuple(sorted({agent for agent, _ in _CLIENTS}))


def telemetry_clients():
    return tuple(sorted({client for _, client in _CLIENTS}))


def client_identifiers():
    return [{'agent': agent, 'client': client} for agent, client in _CLIENTS]


def extend_owner_telemetry_labels(limits):
    """Build an additive private-config update; no I/O or permission changes."""
    result = dict(limits)
    result['agents'] = sorted(set(limits.get('agents', [])) | set(telemetry_agents()))
    result['clients'] = sorted(set(limits.get('clients', [])) | set(telemetry_clients()))
    return result
