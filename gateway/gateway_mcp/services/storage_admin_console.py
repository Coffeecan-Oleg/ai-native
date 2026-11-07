"""Read-only projections for the admin console; never select credential payloads."""

from typing import Any
import os
import re

from gateway_mcp.services.storage_core import _connect, ensure_schema, postgres_enabled

_USERS = """
with identities as (
    select actor_subject as subject, email, login, yandex_id, updated_at, 0 as rank
    from user_oauth_tokens where provider in ('yandex','bitrix24-login')
    union all
    select requester_subject, requester_email, '', '', updated_at, 1
    from access_requests
), known_users as (
    select distinct on (subject) subject, email, login, yandex_id, updated_at
    from identities where subject <> ''
    order by subject, rank, updated_at desc
)
"""


def _users_cte() -> str:
    """Only successful corporate logins, never an import of the staff directory.

    Token updated_at may be a refresh, so it is not used as a login timestamp.
    Unknown historic OAuth channels stay unknown. No credential payload selected.
    """
    from gateway_mcp.services import corporate_access
    if not corporate_access.enabled():
        return _USERS
    member = os.getenv('GATEWAY_BITRIX_MEMBER_ID', '')
    # Strict hex binding makes the fixed SQL literal safe; missing pin fails closed.
    member = member if re.fullmatch(r'[0-9a-f]{32}', member) else 'unbound'
    return """
with successful_logins as (
    select actor_subject as subject,
        min(created_at) as first_login_at, max(created_at) as last_login_at,
        max(created_at) filter (where event = 'corporate_frame_login'
            or payload->'arguments'->>'channel' = 'web') as last_panel_login_at,
        max(created_at) filter (where event = 'corporate_login'
            and payload->'arguments'->>'channel' = 'mcp') as last_mcp_authorization_at
    from audit_events
    where event in ('corporate_login', 'corporate_frame_login')
        and decision = 'allow' and status = 'ok'
    group by actor_subject
), mcp_activity as (
    select actor_subject as subject, max(created_at) as last_mcp_call_at
    from audit_events where event = 'tool_call' and payload->>'channel' = 'mcp'
        and decision = 'allow' and status = 'ok'
    group by actor_subject
), known_users as (
    select u.actor_subject as subject, u.email, u.login, ''::text as yandex_id,
        u.updated_at, u.metadata->>'user_id' as bitrix_user_id,
        l.first_login_at, l.last_login_at, l.last_panel_login_at,
        l.last_mcp_authorization_at, m.last_mcp_call_at, u.revoked_at
    from user_oauth_tokens u
    join successful_logins l on l.subject = u.actor_subject
    left join mcp_activity m on m.subject = u.actor_subject
    where u.provider = 'bitrix24-login'
        and u.metadata->>'portal' = 'akmetron.bitrix24.ru'
        and u.metadata->>'member_id' = '__MEMBER__'
        and u.metadata->>'user_id' ~ '^[1-9][0-9]*$'
        and u.actor_subject = 'bitrix24:__MEMBER__:' || (u.metadata->>'user_id')
)
""".replace('__MEMBER__', member)


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    if not postgres_enabled():
        raise RuntimeError("admin console requires PostgreSQL")
    ensure_schema()
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in row.items()
        }
        for row in rows
    ]


def admin_overview_counts() -> dict[str, Any]:
    return _rows(
        _users_cte()
        + """
        select (select count(*) from known_users) as users,
            (select count(*) from access_requests where status = 'pending') as pending,
            (select count(*) from access_requests where status = 'processing') as processing,
            (select count(*) from audit_events
             where created_at >= now() - interval '24 hours'
             and status = 'error') as errors,
            (select count(*) from audit_events
             where created_at >= now() - interval '24 hours'
             and decision = 'deny') as denied
    """
    )[0]


def admin_users_page(
    *, query: str = "", offset: int = 0, limit: int = 26
) -> list[dict[str, Any]]:
    return _rows(
        _users_cte()
        + "select " + _user_columns() + """ from known_users
        where strpos(lower(subject || ' ' || email || ' ' || login), lower(%s)) > 0
        order by lower(coalesce(nullif(email, ''), subject)), subject
        limit %s offset %s
    """,
        (query, min(max(limit, 1), 101), max(offset, 0)),
    )


def admin_user_get(subject: str) -> dict[str, Any] | None:
    rows = _rows(
        _users_cte()
        + "select " + _user_columns() + """ from known_users where subject = %s
    """,
        (subject,),
    )
    return rows[0] if rows else None


def admin_audit_user_names(subjects: list[str]) -> dict[str, str]:
    """Bounded display lookup for already visible events, without token decryption."""
    from gateway_mcp.services import corporate_access
    if not corporate_access.enabled():
        return {}
    member = os.getenv('GATEWAY_BITRIX_MEMBER_ID', '')
    if not re.fullmatch(r'[a-f0-9]{32}', member):
        return {}
    pattern = re.compile(r'bitrix24:' + member + r':[1-9][0-9]{0,18}')
    selected = sorted({value for value in subjects[:50]
                       if isinstance(value, str) and pattern.fullmatch(value)})
    if not selected:
        return {}
    rows = _rows(_users_cte() +
                 'select subject, login from known_users where subject = any(%s)', (selected,))
    return {row['subject']: str(row.get('login') or '').strip()[:160]
            for row in rows if row.get('subject') in selected and str(row.get('login') or '').strip()}


def _user_columns():
    from gateway_mcp.services import corporate_access
    columns = 'subject, email, login, yandex_id, updated_at'
    if corporate_access.enabled():
        columns += ', bitrix_user_id, first_login_at, last_login_at, last_panel_login_at, last_mcp_authorization_at, last_mcp_call_at, revoked_at'
    return columns


def admin_user_grants(
    user: dict[str, Any], *, offset: int = 0, limit: int = 26
) -> dict[str, list]:
    from gateway_mcp.services import corporate_access
    identity_keys = ("subject",) if corporate_access.enabled() else ("subject", "email", "login", "yandex_id")
    keys = sorted(
        {
            str(user.get(key) or "").strip().casefold()
            for key in identity_keys
        }
        - {""}
    )
    tables = {
        "packages": ("access_grant_bundles", "id, title, package_key, package_version"),
        "scopes": ("access_scope_grants", "id, scope, effect"),
        "resources": (
            "access_resource_grants",
            "id, system, resource_type, resource_pattern, actions, effect",
        ),
    }
    result = {}
    for name, (table, columns) in tables.items():
        result[name] = _rows(
            f"""
            select {columns}, created_at, expires_at, revoked_at
            from {table}
            where subject_type = 'user' and lower(subject_key) = any(%s)
            order by created_at desc, id desc limit %s offset %s
        """,
            (keys, min(max(limit, 1), 101), max(offset, 0)),
        )
    return result
