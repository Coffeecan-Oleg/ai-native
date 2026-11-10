CREATE TABLE platforma_activity_settings (
    id integer PRIMARY KEY CHECK (id = 1),
    enabled boolean NOT NULL DEFAULT false,
    categories jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(categories) = 'array'),
    interval_seconds integer NOT NULL DEFAULT 60 CHECK (interval_seconds IN (30,60,300,900)),
    revision bigint NOT NULL DEFAULT 1,
    updated_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    next_send_at timestamptz NOT NULL DEFAULT now(),
    worker_seen_at timestamptz,
    worker_status text NOT NULL DEFAULT 'not_started',
    overflow_count bigint NOT NULL DEFAULT 0
);
INSERT INTO platforma_activity_settings(id) VALUES (1);

CREATE TABLE platforma_activity_outbox (
    id bigserial PRIMARY KEY,
    audit_id bigint NOT NULL UNIQUE,
    category text NOT NULL,
    details jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','sending','sent','failed','unknown','cancelled','expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    attempted_at timestamptz,
    completed_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    batch_id text,
    message_id bigint,
    error_code text NOT NULL DEFAULT ''
);
CREATE INDEX platforma_activity_pending ON platforma_activity_outbox(state,id);
CREATE INDEX platforma_activity_history ON platforma_activity_outbox(created_at DESC,id DESC);
