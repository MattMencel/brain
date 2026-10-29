create extension if not exists pg_search cascade;

create table if not exists records (
    id bigserial primary key,
    source text not null,
    ref text not null unique,
    ts timestamptz not null,
    project text,
    title text,
    summary text,
    questions text,
    atoms text,
    body text not null,
    content_hash text not null,
    source_mtime double precision,
    summarizer_version int,
    ingested_at timestamptz not null default now()
);

alter table records add column if not exists author text;

create index if not exists records_bm25 on records
    using bm25 (id, title, summary, questions, atoms, body);

create table if not exists passages (
    id bigserial primary key,
    ref text not null references records (ref) on delete cascade,
    n int not null,
    text text not null,
    unique (ref, n)
);

create index if not exists passages_bm25 on passages using bm25 (id, text);

create table if not exists topics (
    id bigserial primary key,
    ref text not null references records (ref) on delete cascade,
    n int not null,
    title text not null,
    summary text not null,
    questions text,
    atoms text,
    turn_from int not null,
    turn_to int not null,
    unique (ref, n)
);

create index if not exists topics_bm25 on topics using bm25 (id, title, summary, questions, atoms);

create table if not exists connector_runs (
    connector text primary key,
    last_success timestamptz,
    last_error text,
    last_error_at timestamptz
);

alter table records add column if not exists events_hash text;

alter table connector_runs add column if not exists config_hash text;

create table if not exists events (
    id bigserial primary key,
    ref text not null references records (ref) on delete cascade,
    ts timestamptz not null,
    actor text not null,
    kind text not null,
    detail text,
    bot boolean not null default false
);

create index if not exists events_ts on events (ts);
create index if not exists events_actor on events (actor);
create index if not exists events_ref on events (ref);
