-- Shared sandbox skill materials; deliberately separate from credentials/knowledge.
create table platforma_shared_skills (
    skill_id text primary key,
    origin text not null check (origin in ('uploaded','bundled')),
    title text not null check (char_length(title) between 2 and 120),
    title_key text not null unique,
    description text not null default '',
    version text not null,
    author_subject text not null,
    author_label text not null,
    manifest jsonb not null default '{}' check (jsonb_typeof(manifest)='object'),
    upload_key uuid unique,
    fingerprint text not null default '',
    revision integer not null default 1 check (revision > 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create table platforma_shared_skill_files (
    skill_id text not null references platforma_shared_skills(skill_id),
    path text not null,
    content bytea not null check (octet_length(content) <= 131072),
    sha256 text not null,
    primary key(skill_id,path)
);
create table platforma_shared_skill_events (
    id bigserial primary key,
    skill_id text not null references platforma_shared_skills(skill_id),
    revision integer not null,
    actor_subject text not null,
    action text not null check (action in ('uploaded','renamed')),
    created_at timestamptz not null default now()
);
create index platforma_shared_skills_author on platforma_shared_skills(author_subject);
