-- Additive only: existing published skills, files and historical events stay intact.
create table platforma_skill_workflows (
    skill_id text primary key check (skill_id ~ '^shared-[0-9a-f]{32}$'),
    author_subject text not null,
    author_label text not null,
    stage text not null check (stage in ('draft','review','published','archived')),
    revision integer not null default 1 check (revision > 0),
    candidate_revision integer not null check (candidate_revision > 0),
    published_revision integer,
    upload_key uuid unique,
    upload_fingerprint text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table platforma_skill_versions (
    skill_id text not null references platforma_skill_workflows(skill_id),
    revision integer not null check (revision > 0),
    title text not null check (char_length(title) between 2 and 120),
    title_key text not null,
    description text not null,
    version text not null,
    manifest jsonb not null check (jsonb_typeof(manifest)='object'),
    files jsonb not null check (jsonb_typeof(files)='object'),
    fingerprint text not null,
    created_by text not null,
    created_at timestamptz not null default now(),
    primary key(skill_id,revision)
);
alter table platforma_skill_workflows add constraint skill_candidate_version
    foreign key(skill_id,candidate_revision) references platforma_skill_versions(skill_id,revision)
    deferrable initially deferred;
alter table platforma_skill_workflows add constraint skill_published_version
    foreign key(skill_id,published_revision) references platforma_skill_versions(skill_id,revision)
    deferrable initially deferred;
create table platforma_skill_workflow_events (
    id bigserial primary key,
    skill_id text not null references platforma_skill_workflows(skill_id),
    revision integer not null,
    actor_subject text not null,
    action text not null check (action in ('draft_created','legacy_adopted','draft_updated','submitted','changes_requested','published','archived','restored')),
    created_at timestamptz not null default now()
);
create index platforma_skill_workflows_author on platforma_skill_workflows(author_subject);
create index platforma_skill_workflow_events_skill on platforma_skill_workflow_events(skill_id,id);
