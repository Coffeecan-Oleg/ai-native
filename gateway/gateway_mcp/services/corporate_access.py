"""Finite automatic baseline for verified internal employees of one Bitrix portal."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

from gateway_mcp.services import bitrix_identity as identity

SPACE = 'space:organizational-awareness'
READ_TOOLS = frozenset({'gateway_access_profile','gateway_search_tools','gateway_call_tool',
    'gateway_corporate_skill_list','gateway_corporate_skill_read','gateway_knowledge_space_list',
    'gateway_knowledge_space_get','gateway_knowledge_document_get','gateway_knowledge_search',
    'gateway_telemetry_skill_stats'})
EVENT_TOOLS = frozenset({'gateway_telemetry_skill_started','gateway_telemetry_skill_completed','gateway_telemetry_skill_failed'})
# Corporate entry exposes no fictional backend, including to the owner.
READ_ROUTES = frozenset()
WRITE_ROUTES = {}
BASE_SCOPES = frozenset({'skills:read','tools:read','tools:call','telemetry:read','telemetry:write','knowledge:read'})
WEB_PATHS = frozenset({'/admin','/admin/audit','/admin/telemetry/skills',
    '/auth/bitrix24/frame-session',
    '/platforma/skills','/platforma/skills/upload',
    '/platforma/skills/view','/platforma/skills/rename','/platforma/how-it-works',
    '/platforma/skills/manage','/platforma/skills/manage/action','/platforma/skills/manage/edit',
    '/platforma/skills/activity',
    '/platforma/how-it-works/download','/platforma/connection','/join','/auth/logout'})


def enabled():
    return identity.enabled()


def is_owner(actor):
    if not enabled() or not identity.is_subject(actor.subject):
        return False
    member, user = identity.parse_subject(actor.subject)
    configured = os.getenv('GATEWAY_BITRIX_OWNER_USER_ID','')
    pin = os.getenv('GATEWAY_BITRIX_MEMBER_ID','')
    return bool(configured) and user == configured and (not pin or member == pin)


def _scope_set(employee, actor):
    from gateway_mcp.services.storage import list_active_scope_grants
    if employee['user_id'] in os.getenv('GATEWAY_BITRIX_DENIED_USER_IDS','').split(','):
        raise PermissionError('corporate_access_revoked')
    scopes = set(BASE_SCOPES)
    if is_owner(actor):
        scopes |= {'access:admin','access:read'}
    # Real-service access is an explicit user-ID list, never automatic membership.
    if employee['user_id'] in os.getenv('GATEWAY_BITRIX_UNISENDER_USER_IDS','').split(','):
        scopes.add('unisender:read')
    # No email/login aliases, old Yandex groups or historic allow-grants inherited.
    grants = list_active_scope_grants([('user', actor.subject.casefold())])
    for grant in grants:
        if grant.get('effect') == 'deny':
            if grant.get('scope') == '*':
                raise PermissionError('corporate_access_revoked')
            scopes.discard(grant.get('scope'))
    return scopes


def fresh_actor(subject, *, ceiling=None, tools=None):
    from gateway_mcp.services.policy import GatewayActor
    employee = identity.current_employee(subject)
    actor = GatewayActor(subject=subject, login=employee['name'])
    scopes = _scope_set(employee, actor)
    if ceiling is not None:
        scopes.intersection_update(ceiling)
    allowed = allowed_tools(scopes)
    if tools is not None:
        allowed.intersection_update(tools)
    return replace(actor, scopes=tuple(sorted(scopes)),
        scope_ceiling=tuple(sorted(scopes)) if ceiling is not None else None,
        tool_allowlist=tuple(sorted(allowed)) if tools is not None else None)


def from_claims(claims):
    from gateway_mcp.services.policy import _optional_token_list
    return fresh_actor(str(claims.get('sub','')),
        ceiling=_optional_token_list(claims,'scope_ceiling'), tools=_optional_token_list(claims,'tool_allowlist'))


def allowed_tools(scopes):
    result = set(READ_TOOLS | EVENT_TOOLS | READ_ROUTES | set(WRITE_ROUTES))
    if 'unisender:read' in scopes:
        from gateway_mcp.services.unisender_readonly import ROUTES
        result.update(ROUTES)
    return result


def delegated(actor):
    fresh = fresh_actor(actor.subject, ceiling=actor.scope_ceiling, tools=actor.tool_allowlist)
    scopes = set(fresh.scopes) & (set(BASE_SCOPES) | {'unisender:read'})
    allowed = allowed_tools(scopes)
    if actor.tool_allowlist is not None:
        allowed.intersection_update(actor.tool_allowlist)
    return replace(fresh, groups=(), scopes=tuple(sorted(scopes)),
        scope_ceiling=tuple(sorted(scopes)), tool_allowlist=tuple(sorted(allowed)))


def active_row(subject):
    employee = identity.current_employee(subject)
    # Recheck independent gateway bans as well as employment.
    actor = fresh_actor(subject)
    if not BASE_SCOPES.intersection(actor.scopes):
        return None
    checked = datetime.fromtimestamp(employee['verified_at'],timezone.utc)
    return {'id':uuid5(NAMESPACE_URL,subject), 'subject':subject, 'email':'', 'login':employee['name'],
        'status':'approved','corporate':True,'revision':1,'requested_at':checked,
        'approved_until':datetime.fromtimestamp(employee['verified_at']+60,timezone.utc),
        'access_condition':'active internal employee; automatically rechecked'}


def profile(actor):
    fresh = fresh_actor(actor.subject, ceiling=actor.scope_ceiling, tools=actor.tool_allowlist)
    config = json.loads(Path(os.environ['GATEWAY_ADDON_PROFILES_FILE']).read_text(encoding='utf-8'))
    # This is only a shared-document catalogue template, not an identity alias.
    template = config['profiles'][os.environ['GATEWAY_PLATFORMA_OWNER_SUBJECT']]
    from gateway_mcp.services.platforma_onboarding import DOCUMENTS
    available = template.get('documents',{}).get(SPACE,{})
    documents = {path:available[path] for path in sorted(DOCUMENTS) if path in available}
    if not DOCUMENTS <= documents.keys():
        raise PermissionError('corporate_shared_catalogue_unavailable')
    from gateway_mcp.services.platforma_clients import telemetry_agents, telemetry_clients
    tools = allowed_tools(set(fresh.scopes))
    resources = [{'id':'mcp:'+t,'kind':'tool','operations':['write' if t in EVENT_TOOLS else 'read']}
                 for t in sorted((READ_TOOLS|EVENT_TOOLS)&tools)]
    resources += [{'id':r,'kind':'backend_route','operations':['read']} for r in sorted(READ_ROUTES)]
    resources += [{'id':r,'kind':'backend_route','operations':['write']} for r in sorted(WRITE_ROUTES)]
    if 'unisender:read' in fresh.scopes:
        from gateway_mcp.services.unisender_readonly import ROUTES
        resources += [{'id':r,'kind':'backend_route','operations':['read']} for r in sorted(ROUTES)]
    limits = dict(template.get('skill_telemetry',{}))
    limits.update(agents=list(telemetry_agents()),clients=list(telemetry_clients()))
    # Persisted security namespace must match addon_guard; branding is independent.
    return {'version':'2.0.0','status':'active','subject':actor.subject,'project':'APP11',
        'reader_only':True,'participant_role':'corporate-employee','spaces':[SPACE],
        'usage_projects':[], 'forbidden_operations':['delete','admin','grant','export'],
        'documents':{SPACE:documents},'resources':resources,'skill_telemetry':limits,
        'authentication':'bitrix24','access_condition':'active internal employee'}


def knowledge_reader(actor,path):
    expected = Path(os.environ.get('GATEWAY_KNOWLEDGE_VAULT_PATH','/data/company-knowledge-vault'))/'spaces'/'organizational-awareness'
    if Path(path) != expected:
        return False
    return active_row(actor.subject) is not None


def web_path_allowed(actor,path):
    fresh = fresh_actor(actor.subject)
    if fresh.tool_allowlist is not None:
        return False
    if is_owner(fresh):
        return True
    return path in WEB_PATHS


def require_owner(actor):
    fresh = fresh_actor(actor.subject)
    if actor.tool_allowlist is not None or not is_owner(fresh) or 'access:admin' not in fresh.scopes:
        raise PermissionError('corporate_owner_required')


def require_route_access(actor,route,arguments):
    from gateway_mcp.services.policy import has_scope
    from gateway_mcp.services.access_inference import infer_route_access
    from gateway_mcp.services.access_policy import _grant_matches
    from gateway_mcp.services.storage import list_active_resource_grants
    fresh = fresh_actor(actor.subject,ceiling=actor.scope_ceiling,tools=actor.tool_allowlist)
    name = route.get('name')
    if name in READ_ROUTES:
        expected = ('platforma','platforma-demo','platforma:read','GET')
    elif name in WRITE_ROUTES:
        expected = ('platforma','platforma-demo','platforma:write',WRITE_ROUTES[name])
    elif name in {'unisender.getLists','unisender.getContactCount'}:
        expected = ('unisender','unisender-readonly','unisender:read','GET')
    else:
        raise PermissionError('corporate_route_denied')
    if tuple(route.get(k) for k in ('backend','transport','scope','http_method')) != expected:
        raise PermissionError('corporate_route_denied')
    if not has_scope(fresh,expected[2]) or fresh.tool_allowlist is not None and name not in fresh.tool_allowlist:
        raise PermissionError('corporate_route_denied')
    inferred = infer_route_access(route,arguments)
    grants = list_active_resource_grants(subjects=[('user',actor.subject.casefold())],system=inferred['system'])
    for grant in grants:
        if grant.get('effect') == 'deny' and _grant_matches(grant=grant,action=inferred['action'],
                resource=inferred['resource'],resource_type=inferred['resource_type']):
            raise PermissionError('corporate_resource_denied')
    return {'decision':'allow','reason':'verified_corporate_baseline','mode':'strict',
            'matched':[],**inferred}
