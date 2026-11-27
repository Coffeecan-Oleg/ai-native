CREATE TABLE gateway_external_circuits (
    integration text PRIMARY KEY,
    failures integer NOT NULL DEFAULT 0 CHECK (failures >= 0),
    last_failure_at timestamptz,
    opened_until timestamptz,
    probe_id text,
    generation bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL
);
CREATE TABLE gateway_external_calls (
    call_id text PRIMARY KEY,
    integration text NOT NULL REFERENCES gateway_external_circuits(integration),
    user_bucket text NOT NULL,
    generation bigint NOT NULL,
    probe boolean NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX gateway_external_calls_integration_expiry ON gateway_external_calls(integration, expires_at);
