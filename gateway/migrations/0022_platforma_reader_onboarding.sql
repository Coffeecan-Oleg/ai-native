-- APP11 self-service reader approval. No existing user or grant is changed.
create table platforma_reader_requests (
    id uuid primary key,
    subject text not null unique,
    email text not null,
    login text not null default '',
    status text not null default 'pending'
        check (status in ('pending', 'approved', 'rejected', 'revoked')),
    revision integer not null default 1 check (revision > 0),
    requested_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    decided_at timestamptz,
    decided_by text,
    decision_reason text not null default '',
    approved_until timestamptz,
    scope_grant_ids bigint[] not null default '{}',
    resource_grant_id bigint,
    check (subject like 'yandex:%'),
    check (status <> 'approved' or
        (approved_until is not null and cardinality(scope_grant_ids) = 6
         and resource_grant_id is not null))
);
create index platforma_reader_requests_status_idx
    on platforma_reader_requests(status, requested_at desc);

create table platforma_reader_events (
    id bigserial primary key,
    request_id uuid not null references platforma_reader_requests(id),
    revision integer not null,
    actor_subject text not null,
    action text not null check (action in ('requested','approved','rejected','revoked')),
    reason text not null default '',
    created_at timestamptz not null default now()
);
