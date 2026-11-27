-- Additive, service-private GCRA state. No credentials, request bodies or raw identities.
create table gateway_external_limits (
    integration text not null check (integration ~ '^[a-z][a-z0-9-]{0,63}$'),
    bucket text not null check (bucket = 'global' or bucket ~ '^user:[a-f0-9]{64}$'),
    tat timestamptz not null,
    blocked_until timestamptz,
    updated_at timestamptz not null default clock_timestamp(),
    primary key (integration,bucket),
    check (bucket = 'global' or blocked_until is null)
);
create index gateway_external_limits_retention on gateway_external_limits(integration,updated_at);
