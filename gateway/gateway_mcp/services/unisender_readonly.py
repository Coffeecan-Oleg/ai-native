"""Narrow service boundary for the owner-approved real UniSender reader."""
import os
from datetime import datetime, timezone

SCOPE = 'unisender:read'
ROUTES = frozenset({'unisender.getLists', 'unisender.getContactCount'})
PROVIDER = 'unisender-readonly'
SERVICE_SUBJECT = 'service:platforma-unisender-readonly'
METHODS = ('getLists', 'getContactCount')


def enabled():
    return os.getenv('GATEWAY_UNISENDER_READONLY_ENABLED', '').casefold() == 'true'


def require_actor(actor):
    from gateway_mcp.services import platforma_onboarding as onboarding
    from gateway_mcp.services.policy import has_scope
    if not enabled() or not has_scope(actor, SCOPE):
        raise PermissionError('unisender_read_scope_required')
    from gateway_mcp.services import corporate_access
    if corporate_access.enabled():
        if not has_scope(corporate_access.fresh_actor(actor.subject,ceiling=actor.scope_ceiling,tools=actor.tool_allowlist),SCOPE):
            raise PermissionError('unisender_read_scope_required')
        return
    cfg = onboarding.settings()
    if cfg['until'] <= datetime.now(timezone.utc):
        raise PermissionError('unisender_access_expired')
    if not onboarding.is_owner(actor) and onboarding.active_reader(actor.subject) is None:
        raise PermissionError('unisender_active_approval_required')


def grant_resource(cur, row, actor, until):
    """Called only inside a human-approved grant transaction; never on reads."""
    reason = 'APP11 UniSender reader request ' + str(row['id'])
    cur.execute('''insert into access_resource_grants
        (subject_type,subject_key,system,resource_pattern,actions,effect,reason,created_by,expires_at)
        values ('user',%s,'unisender','*','["read"]','allow',%s,%s,%s)''',
        (row['subject'].casefold(), reason, actor.subject, until))


def revoke_resource(cur, row):
    cur.execute('''update access_resource_grants set revoked_at=coalesce(revoked_at,now())
        where subject_type='user' and subject_key=%s and system='unisender'
        and reason=%s''', (row['subject'].casefold(), 'APP11 UniSender reader request ' + str(row['id'])))


def upgrade_approved_readers(actor):
    """Explicit deployment operation, not exposed as an MCP tool or web route."""
    from gateway_mcp.services import platforma_onboarding as onboarding
    onboarding.require_owner(actor)
    if not enabled():
        raise PermissionError('unisender_feature_disabled')
    cfg = onboarding.settings()
    if cfg['until'] <= datetime.now(timezone.utc):
        raise PermissionError('unisender_access_expired')
    upgraded = 0
    with onboarding._db() as conn:
        with conn.cursor() as cur:
            cur.execute("select * from platforma_reader_requests where status='approved' and approved_until>now() order by id for update")
            for row in cur.fetchall():
                if onboarding.active_reader(row['subject']) is None:
                    continue
                cur.execute('select scope from access_scope_grants where id=any(%s)', (row['scope_grant_ids'],))
                if SCOPE not in {r['scope'] for r in cur.fetchall()}:
                    cur.execute('''insert into access_scope_grants
                        (subject_type,subject_key,scope,effect,reason,created_by,expires_at)
                        values ('user',%s,%s,'allow',%s,%s,%s) returning id''',
                        (row['subject'].casefold(), SCOPE, 'APP11 UniSender reader request '+str(row['id']), actor.subject, row['approved_until']))
                    grant_id=cur.fetchone()['id']
                    cur.execute('update platforma_reader_requests set scope_grant_ids=%s,revision=revision+1 where id=%s',
                                (list(row['scope_grant_ids'])+[grant_id],row['id']))
                    grant_resource(cur,row,actor,row['approved_until'])
                    upgraded += 1
            # Explicit owner read resource, separate from all training data grants.
            owner_row={'id':'owner','subject':cfg['owner']}
            reason='APP11 UniSender reader request owner'
            cur.execute('''select id from access_resource_grants where subject_type='user' and subject_key=%s
                and system='unisender' and reason=%s and revoked_at is null and expires_at>now()''',
                (cfg['owner'].casefold(),reason))
            if cur.fetchone() is None:
                grant_resource(cur,owner_row,actor,cfg['until'])
    return {'upgraded_readers':upgraded,'owner_read_resource':True,'expires_at':cfg['until'].isoformat()}
