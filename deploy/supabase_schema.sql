-- Brain mirror schema. Apply once per brand (Supabase SQL editor, or Claude's Supabase connector: apply_migration).
create table if not exists public.posts (
  platform text not null, post_id text not null, text text, posted_at timestamptz, source text, origin_id text,
  queue_id text, permalink text, synced_at timestamptz default now(), primary key (platform, post_id));
create table if not exists public.obs (
  platform text not null, post_id text not null, at timestamptz not null, age_h double precision,
  views bigint, likes bigint, replies bigint, reposts bigint, quotes bigint, shares bigint, bookmarks bigint, src text,
  primary key (platform, post_id, at));
create table if not exists public.followers (
  platform text not null, at timestamptz not null, followers bigint, src text, primary key (platform, at));
create table if not exists public.labels (
  platform text not null, post_id text not null, pillar text, topic_group text, topic text, angle text, proof text,
  length text, number_open text, ends_question text, list_format text, first_person text, labeled_at timestamptz,
  primary key (platform, post_id));
create table if not exists public.attribution (
  platform text not null, post_id text not null, follows double precision, follows_24h double precision,
  primary key (platform, post_id));
create table if not exists public.scores (
  platform text not null, post_id text not null, checkpoint text not null, estimated boolean,
  views double precision, likes double precision, replies double precision, reposts double precision, quotes double precision,
  shares double precision, bookmarks double precision, follows double precision, points double precision,
  quality double precision, reach double precision, score double precision, computed_at timestamptz,
  primary key (platform, post_id, checkpoint));
create table if not exists public.research (
  platform text not null, ext_id text not null, author text, author_followers bigint, text text, created_at timestamptz,
  views bigint, likes bigint, replies bigint, reposts bigint, quotes bigint, bookmarks bigint, query text, collected_at timestamptz,
  primary key (platform, ext_id));
create table if not exists public.research_themes (
  day date not null, platform text not null, text text not null, primary key (day, platform, text));
create table if not exists public.feedback (
  platform text not null, queue_id text not null, kind text not null, text text, note text, at timestamptz,
  primary key (platform, queue_id, kind));
create table if not exists public.sync_runs (
  id bigint generated always as identity primary key, at timestamptz default now(), rows_sent bigint, detail jsonb);
create index if not exists obs_post_idx on public.obs (platform, post_id);
create index if not exists posts_time_idx on public.posts (platform, posted_at desc);
create index if not exists research_time_idx on public.research (platform, collected_at desc);
-- Locked down: RLS on with no policies. Only the secret key used by brain/supa_sync.py can read or write.
alter table public.posts enable row level security;
alter table public.obs enable row level security;
alter table public.followers enable row level security;
alter table public.labels enable row level security;
alter table public.attribution enable row level security;
alter table public.scores enable row level security;
alter table public.research enable row level security;
alter table public.research_themes enable row level security;
alter table public.feedback enable row level security;
alter table public.sync_runs enable row level security;
