"""Own-actor MCP probes. Correlation marks are not credentials or permissions."""
import re
import secrets
import time
from uuid import uuid4

from gateway_mcp.services.storage_audit import append_audit_event
from gateway_mcp.services.storage_core import _connect, ensure_schema, postgres_enabled

TTL = 900
MARK = re.compile(r"diag-([0-9]{10})-([0-9a-f]{24})\Z")
SCOPES = re.compile(r"[A-Za-z0-9_*:.-]{1,80}\Z")
CLIENTS = ('codex', 'claude', 'chatgpt', 'cursor', 'opencode', 'openclaw')
TOOL_SCOPES = {
    'gateway_access_profile':'skills:read', 'gateway_search_tools':'tools:read',
    'gateway_call_tool':'tools:call', 'gateway_corporate_skill_list':'skills:read',
    'gateway_corporate_skill_read':'skills:read', 'gateway_knowledge_space_list':'knowledge:read',
    'gateway_knowledge_space_get':'knowledge:read', 'gateway_knowledge_document_get':'knowledge:read',
    'gateway_knowledge_search':'knowledge:read', 'gateway_telemetry_skill_stats':'telemetry:read',
    'gateway_telemetry_skill_started':'telemetry:write', 'gateway_telemetry_skill_completed':'telemetry:write',
    'gateway_telemetry_skill_failed':'telemetry:write',
}


def new_mark():
    return f'diag-{int(time.time())}-{secrets.token_hex(12)}'


def validate_mark(value, *, now=None):
    match = MARK.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError('invalid_diagnostic_id')
    age = int(time.time() if now is None else now)-int(match[1])
    if age < -30 or age > TTL:
        raise ValueError('expired_diagnostic_id')
    return value


def client_info(mcp):
    """Only bounded known self-reported labels, never arbitrary client strings."""
    try:
        info = mcp.get_context().session.client_params.clientInfo
        raw = str(info.name).strip().casefold()
        client = next((name for name in CLIENTS if raw == name or raw.startswith(name+'-')
                       or raw.startswith(name+' ')), 'unknown')
        version = str(info.version)
        version = version if re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}',version) else ''
        return {'name':client, 'version':version if client != 'unknown' else ''}
    except (AttributeError, LookupError, ValueError):
        return {'name':'unknown', 'version':''}


def _strings(values, expression, maximum=64):
    if not isinstance(values, (tuple, list)):
        return []
    return sorted({v for v in values[:maximum] if isinstance(v,str) and expression.fullmatch(v)})


def record_probe(actor, mark, mcp):
    validate_mark(mark)
    if not actor.subject or actor.subject == 'anonymous':
        raise PermissionError('authenticated_actor_required')
    scopes = _strings(list(actor.scopes), SCOPES)
    tools = [name for name,scope in TOOL_SCOPES.items()
             if (scope in scopes or '*' in scopes)
             and (actor.tool_allowlist is None or name in actor.tool_allowlist)]
    request_id = 'gwc-'+uuid4().hex
    details = {'diagnostic_id':mark,'gateway_request_id':request_id,'effective_scopes':scopes,
               'allowed_tools':sorted(tools),'client':client_info(mcp)}
    payload = {'ts_ms':int(time.time()*1000),'event':'connection_check',
               'actor':{'subject':actor.subject},'tool':'gateway_access_profile','system':'access',
               'decision':'allow','status':'ok','scope':'skills:read','channel':'mcp','arguments':details}
    try:
        recorded = bool(append_audit_event(payload))
    except Exception:
        recorded = False
    return {'diagnostic_id':mark,'gateway_request_id':request_id,'channel':'mcp',
            'recorded':recorded,'reason_code':'recorded' if recorded else 'diagnostic_storage_unavailable'}


def read_probe(actor, mark):
    validate_mark(mark)
    if not actor.subject or actor.subject == 'anonymous':
        raise PermissionError('authenticated_actor_required')
    if not postgres_enabled():
        raise RuntimeError('diagnostic_storage_unavailable')
    ensure_schema()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                select created_at, payload -> 'arguments' as details
                from audit_events
                where actor_subject=%s and event='connection_check'
                  and tool='gateway_access_profile' and status='ok' and decision='allow'
                  and payload ->> 'channel'='mcp'
                  and payload #>> '{arguments,diagnostic_id}'=%s
                  and created_at >= now() - interval '15 minutes'
                order by created_at desc, id desc limit 1
            """, (actor.subject,mark))
            row = cur.fetchone()
    if not row:
        return None
    details = row.get('details')
    if not isinstance(details,dict):
        return None
    request_id = details.get('gateway_request_id','')
    request_id = request_id if isinstance(request_id,str) and re.fullmatch(r'gwc-[0-9a-f]{32}',request_id) else ''
    client = details.get('client',{})
    client = client if isinstance(client,dict) else {}
    name = client.get('name') if client.get('name') in CLIENTS else 'unknown'
    version = client.get('version','')
    version = version if isinstance(version,str) and re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}',version) and name != 'unknown' else ''
    return {'checked_at':row['created_at'],'gateway_request_id':request_id,
            'effective_scopes':_strings(details.get('effective_scopes'),SCOPES),
            'allowed_tools':sorted(t for t in details.get('allowed_tools',[]) if isinstance(t,str) and t in TOOL_SCOPES)
                            if isinstance(details.get('allowed_tools'),list) else [],
            'client':{'name':name,'version':version}}
