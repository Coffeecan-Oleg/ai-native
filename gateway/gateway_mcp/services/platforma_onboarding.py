"""APP11 reader approval, with one DB commit as the authorization boundary.

The owner profile stays in the existing private read-only file. Reader scopes,
resource grant, dynamic knowledge membership and tool profile are usable only
while this exact subject's approval AND its linked grants remain active.
"""
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from gateway_mcp.services.storage_core import _connect, ensure_schema, postgres_enabled

SPACE = 'space:organizational-awareness'
SCOPES = frozenset({'skills:read', 'access:request', 'tools:read', 'tools:call',
                    'platforma:read', 'memory:read'})
TOOLS = frozenset({'gateway_corporate_skill_list', 'gateway_corporate_skill_read', 'gateway_access_profile', 'gateway_search_tools', 'gateway_call_tool',
                   'gateway_knowledge_space_list', 'gateway_knowledge_space_get',
                   'gateway_knowledge_document_get', 'gateway_knowledge_search'})
ROUTES = frozenset({'platforma.context.get', 'platforma.services.list',
                    'platforma.services.get', 'platforma.requests.list', 'platforma.requests.get'})
DOCUMENTS = frozenset({'awareness/start.md', 'awareness/projects.md', 'awareness/resources.md',
                       'awareness/refresh.md', 'awareness/project-profile.md',
                       'awareness/capabilities.md', 'awareness/install.md',
                       'decisions/awareness-installed.md',
                       'skills/organizational-awareness/SKILL.md',
                       'skills/organizational-awareness/references/repository-examples.md'})


def enabled():
    return os.getenv('GATEWAY_PLATFORMA_ONBOARDING_ENABLED', '').casefold() == 'true'


def is_owner(actor):
    subject = os.getenv('GATEWAY_PLATFORMA_OWNER_SUBJECT', '')
    return bool(subject) and actor.subject == subject


def _utc(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('timezone required')
    return dt.astimezone(timezone.utc)


def settings():
    if not enabled() or not postgres_enabled():
        raise RuntimeError('Reader onboarding is not configured')
    owner = os.getenv('GATEWAY_PLATFORMA_OWNER_SUBJECT', '')
    if not owner.startswith('yandex:') or len(owner) > 200:
        raise RuntimeError('Exact owner subject required')
    profiles = json.loads(Path(os.environ['GATEWAY_ADDON_PROFILES_FILE']).read_text())
    profile = profiles['profiles'][owner]
    if profile.get('subject') != owner or profile.get('status') != 'active' or profile.get('project') != 'APP11':
        raise RuntimeError('Active owner profile required')
    until = min(_utc(os.environ['GATEWAY_PLATFORMA_READER_UNTIL']), _utc(profile['expires_at']))
    documents = profile.get('documents', {}).get(SPACE, {})
    if not DOCUMENTS <= documents.keys():
        raise RuntimeError('Knowledge map catalogue incomplete')
    return {'owner': owner, 'until': until,
            'documents': {path: documents[path] for path in sorted(DOCUMENTS)}}


def require_owner(actor):
    if actor.tool_allowlist is not None or not is_owner(actor):
        raise PermissionError('Only the human owner can decide reader access')
    from gateway_mcp.services.admin_roles import require_current_admin
    require_current_admin(actor)
    settings()


def _identity(actor):
    if (not actor.subject.startswith('yandex:') or not actor.yandex_id or
            actor.subject != 'yandex:' + actor.yandex_id or not actor.email or
            len(actor.subject) > 200 or len(actor.email) > 320 or len(actor.login) > 200):
        raise PermissionError('Personal Yandex identity required')
    if is_owner(actor):
        raise PermissionError('Owner does not need a reader request')


def _db():
    if not postgres_enabled():
        raise RuntimeError('PostgreSQL required')
    ensure_schema()
    conn = _connect()
    conn.execute("set statement_timeout = '5s'")
    return conn


def effective_status(row):
    if row['status'] == 'approved' and row['approved_until'] <= datetime.now(timezone.utc):
        return 'expired'
    return row['status']


def _event(cur, row, actor, action, reason=''):
    cur.execute('''insert into platforma_reader_events
        (request_id,revision,actor_subject,action,reason) values (%s,%s,%s,%s,%s)''',
        (row['id'], row['revision'], actor.subject, action, reason))


def own_request(actor, *, create=False, resubmit=False):
    cfg = settings()
    _identity(actor)
    if cfg['until'] <= datetime.now(timezone.utc):
        raise PermissionError('Training access period has ended')
    with _db() as conn:
        with conn.cursor() as cur:
            # One stable identity key, independent of email aliases and concurrent visits.
            cur.execute('select pg_advisory_xact_lock(hashtextextended(%s,0))',
                        ('APP11-reader:' + actor.subject,))
            cur.execute('select * from platforma_reader_requests where subject=%s for update', (actor.subject,))
            row = cur.fetchone()
            if row is None and create:
                cur.execute('''insert into platforma_reader_requests(id,subject,email,login)
                    values (%s,%s,%s,%s) returning *''',
                    (uuid4(), actor.subject, actor.email, actor.login))
                row = cur.fetchone()
                _event(cur, row, actor, 'requested')
            elif row and resubmit and effective_status(row) in {'rejected', 'revoked', 'expired'}:
                _revoke_linked(cur, row)
                cur.execute('''update platforma_reader_requests set status='pending',revision=revision+1,
                    email=%s,login=%s,requested_at=now(),last_seen_at=now(),decided_at=null,
                    decided_by=null,decision_reason='',approved_until=null,
                    scope_grant_ids='{}',resource_grant_id=null where id=%s returning *''',
                    (actor.email, actor.login, row['id']))
                row = cur.fetchone()
                _event(cur, row, actor, 'requested')
            elif row and (row['email'], row['login']) != (actor.email, actor.login):
                cur.execute('''update platforma_reader_requests set email=%s,login=%s,
                    revision=revision+1,last_seen_at=now() where id=%s returning *''',
                    (actor.email, actor.login, row['id']))
                row = cur.fetchone()
    return row


def list_requests(actor, offset=0):
    require_owner(actor)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) as n from platforma_reader_requests")
            count = cur.fetchone()['n']
            cur.execute('''select * from platforma_reader_requests
                order by (status='pending') desc,requested_at desc limit 50 offset %s''',
                (max(0, min(int(offset), 100000)),))
            rows = cur.fetchall()
    return {'rows': rows, 'count': count}


def get_request(actor, request_id):
    require_owner(actor)
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute('select * from platforma_reader_requests where id=%s', (UUID(str(request_id)),))
            row = cur.fetchone()
    if row is None:
        raise ValueError('Request not found')
    return row


def preview(actor, request_id, decision, revision, reason):
    cfg = settings()
    row = get_request(actor, request_id)
    _validate_decision(row, decision, revision, reason)
    if decision == 'approved' and cfg['until'] <= datetime.now(timezone.utc):
        raise PermissionError('Training access period has ended')
    return {'request_id': str(row['id']), 'subject': row['subject'], 'email': row['email'],
            'revision': row['revision'], 'status': row['status'], 'decision': decision,
            'reason': reason.strip(), 'until': cfg['until'].isoformat() if decision == 'approved' else None}


def _validate_decision(row, decision, revision, reason):
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise ValueError('Decision reason required')
    if row['revision'] != int(revision):
        raise ValueError('Request changed; refresh the page')
    if (decision in {'approved', 'rejected'} and row['status'] != 'pending') or (
            decision == 'revoked' and row['status'] != 'approved'):
        raise ValueError('Decision no longer applicable')
    if decision not in {'approved', 'rejected', 'revoked'}:
        raise ValueError('Unknown decision')


def _revoke_linked(cur, row):
    cur.execute('update access_scope_grants set revoked_at=coalesce(revoked_at,now()) where id=any(%s)',
                (row['scope_grant_ids'],))
    if row['resource_grant_id']:
        cur.execute('update access_resource_grants set revoked_at=coalesce(revoked_at,now()) where id=%s',
                    (row['resource_grant_id'],))


def decide(actor, request_id, decision, revision, reason):
    require_owner(actor)
    cfg = settings()
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute('select * from platforma_reader_requests where id=%s for update', (UUID(str(request_id)),))
            row = cur.fetchone()
            if row is None:
                raise ValueError('Request not found')
            _validate_decision(row, decision, revision, reason)
            if row['subject'] == actor.subject:
                raise PermissionError('Cannot decide your own request')
            grant_ids = []
            resource_id = None
            until = None
            if decision == 'approved':
                until = cfg['until']
                if until <= datetime.now(timezone.utc):
                    raise PermissionError('Training access period has ended')
                grant_reason = 'APP11 reader request ' + str(row['id'])
                for scope in sorted(SCOPES):
                    cur.execute('''insert into access_scope_grants
                        (subject_type,subject_key,scope,effect,reason,created_by,expires_at)
                        values ('user',%s,%s,'allow',%s,%s,%s) returning id''',
                        (row['subject'].casefold(), scope, grant_reason, actor.subject, until))
                    grant_ids.append(cur.fetchone()['id'])
                cur.execute('''insert into access_resource_grants
                    (subject_type,subject_key,system,resource_pattern,actions,effect,reason,created_by,expires_at)
                    values ('user',%s,'platforma','*','["read"]','allow',%s,%s,%s) returning id''',
                    (row['subject'].casefold(), grant_reason, actor.subject, until))
                resource_id = cur.fetchone()['id']
            elif decision == 'revoked':
                _revoke_linked(cur, row)
            cur.execute('''update platforma_reader_requests set status=%s,revision=revision+1,
                decided_at=now(),decided_by=%s,decision_reason=%s,approved_until=%s,
                scope_grant_ids=%s,resource_grant_id=%s where id=%s returning *''',
                (decision, actor.subject, reason.strip(), until, grant_ids, resource_id, row['id']))
            result = cur.fetchone()
            _event(cur, result, actor, decision, reason.strip())
    return result


def active_reader(subject):
    if not enabled():
        return None
    cfg = settings()
    if subject == cfg['owner']:
        return None
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute('''select * from platforma_reader_requests where subject=%s
                and status='approved' and approved_until>now()''', (subject,))
            row = cur.fetchone()
            if row is None or row['approved_until'] > cfg['until']:
                return None
            cur.execute('''select scope from access_scope_grants where id=any(%s)
                and subject_type='user' and subject_key=%s and effect='allow'
                and revoked_at is null and expires_at>now()''',
                (row['scope_grant_ids'], subject.casefold()))
            scopes = cur.fetchall()
            cur.execute('''select id from access_resource_grants where id=%s
                and subject_type='user' and subject_key=%s and system='platforma'
                and effect='allow' and resource_pattern='*' and resource_type=''
                and actions='["read"]'::jsonb and revoked_at is null and expires_at>now()''',
                (row['resource_grant_id'], subject.casefold()))
            resource = cur.fetchone()
    if len(scopes) != len(SCOPES) or {r['scope'] for r in scopes} != SCOPES or not resource:
        return None
    return row


def oauth_reader_actor(actor):
    if not enabled() or is_owner(actor):
        return actor
    try:
        approved = active_reader(actor.subject)
    except Exception:
        raise PermissionError('Reader approval cannot be verified') from None
    if approved is None:
        raise PermissionError('Reader access awaits owner approval or is inactive; open /join')
    # The Yandex waiting-room cookie deliberately has no scopes. Resolve the
    # approved grants afresh for code exchange; never trust its cached groups.
    from gateway_mcp.services.policy import actor_from_user_info
    fresh = actor_from_user_info({'id': actor.yandex_id or actor.subject.removeprefix('yandex:'),
                                 'login': actor.login, 'default_email': actor.email})
    granted = set(fresh.scopes) & SCOPES
    previous = set(actor.scope_ceiling) if actor.scope_ceiling is not None else SCOPES
    allowed = TOOLS | ROUTES
    previous_tools = set(actor.tool_allowlist) if actor.tool_allowlist is not None else allowed
    return replace(actor, groups=(), scopes=tuple(sorted(granted)),
                   scope_ceiling=tuple(sorted(SCOPES & previous)),
                   tool_allowlist=tuple(sorted(allowed & previous_tools)))


def reader_profile(actor):
    row = active_reader(actor.subject)
    if row is None:
        raise PermissionError('addon_reader_not_approved')
    cfg = settings()
    return {'version': '1.0.0', 'status': 'active', 'subject': actor.subject,
            'project': 'APP11', 'expires_at': row['approved_until'].isoformat(),
            'reader_only': True, 'spaces': [SPACE], 'usage_projects': [],
            'forbidden_operations': ['write', 'create', 'delete', 'admin', 'grant', 'export'],
            'resources': [{'id': 'mcp:' + t, 'kind': 'tool', 'operations': ['read']} for t in sorted(TOOLS)] +
                         [{'id': r, 'kind': 'backend_route', 'operations': ['read']} for r in sorted(ROUTES)],
            'documents': {SPACE: cfg['documents']}}


def knowledge_reader(actor, path):
    if not enabled() or Path(path) != Path(os.environ.get('GATEWAY_KNOWLEDGE_VAULT_PATH', '/data/company-knowledge-vault')) / 'spaces' / 'organizational-awareness':
        return False
    try:
        return active_reader(actor.subject) is not None
    except Exception:
        return False


def check_web_path(actor, path):
    # Authentication/OAuth protocol routes do not use web_actor. All other web
    # handlers which accept browser sessions share this gate, including credentials.
    if not enabled() or is_owner(actor) or path in {'/join', '/join/request', '/auth/logout'}:
        return True
    if path in {'/platforma/requests', '/platforma/requests/view'}:
        try:
            return active_reader(actor.subject) is not None
        except Exception:
            return False
    return False


def web_login_actor(actor):
    if not enabled() or is_owner(actor):
        return actor
    _identity(actor)
    # A waiting-room cookie is identity, never an inherited policy/group grant.
    # Approved database grants are resolved freshly when that session is read.
    return replace(actor, groups=(), scopes=())


def waiting_room_actor(user_info):
    """Admit a verified non-owner identity only to the configured waiting room.

    The generic policy and provider/service binding rules stay strict. No policy
    groups, database grants or tool authority are inherited by this exception.
    """
    if not enabled():
        return None
    from gateway_mcp.services.policy import actor_from_user_info
    actor = actor_from_user_info(user_info, include_database_grants=False)
    if is_owner(actor):
        return None
    _identity(actor)
    try:
        cfg = settings()
    except (RuntimeError, ValueError, OSError, KeyError, TypeError):
        raise PermissionError('Reader entry is temporarily unavailable') from None
    if cfg['until'] <= datetime.now(timezone.utc):
        raise PermissionError('Training access period has ended')
    return replace(actor, groups=(), scopes=())
