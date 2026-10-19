-- Feedback-driven profile edits: a comment on a brief is applied to the
-- user's compiled_profile by /api/feedback, and the last few profiles are kept
-- so a bad edit can be undone.
-- Run once in the Supabase SQL editor. Safe to re-run (everything is guarded).
--
-- Adds:
--   digest_feedback   one editable verdict per user per brief
--   profile_versions  the last 5 compiled_profiles per user, so an edit can be undone
--   attribution columns on digests so a thumb can be traced to what produced it

-- ---------------------------------------------------------------------------
-- digest_feedback: the user's verdict on one brief. `notes` is the comment
-- /api/feedback applies to the profile; `rating` is a quick thumb kept for the
-- record. Either may be absent.
--
-- UNIQUE (user_id, digest_id) makes this an upsert: re-reading a brief and
-- changing your mind replaces the verdict instead of adding a contradictory row.
-- ---------------------------------------------------------------------------
create table if not exists digest_feedback (
  id         bigserial primary key,
  user_id    uuid   references profiles(id) on delete cascade,
  digest_id  bigint references digests(id)  on delete cascade,
  rating     smallint check (rating in (-1, 1)),
  notes      text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (user_id, digest_id)
);

create index if not exists digest_feedback_user_idx on digest_feedback (user_id, id);

-- ---------------------------------------------------------------------------
-- profile_versions: the user's recent compiled_profiles.
--
-- profiles.compiled_profile stays the ACTIVE profile (so the worker and
-- fetch_active_profiles are unchanged); this table is what makes a bad edit
-- undoable. 'user' rows come from compiling interests_text, 'learned' rows from
-- applying a feedback comment. Capped at 5 per user by a trigger below.
-- ---------------------------------------------------------------------------
create table if not exists profile_versions (
  id               bigserial primary key,
  user_id          uuid  references profiles(id) on delete cascade,
  version          int   not null,
  compiled_profile jsonb not null,
  source           text  not null check (source in ('user', 'learned')),
  rationale        text,
  created_at       timestamptz default now(),
  unique (user_id, version)
);

create index if not exists profile_versions_user_idx
  on profile_versions (user_id, version desc);

-- Earlier drafts of this migration had a batch-refiner watermark column.
alter table profile_versions drop column if exists feedback_through;

-- ---------------------------------------------------------------------------
-- Attribution on digests. Without these, a thumb cannot be traced to what
-- produced the brief: a silent gemini-3.5-flash -> gemini-3.6-flash fallback
-- would look like a profile effect. None of this can be backfilled.
-- ---------------------------------------------------------------------------
alter table digests  add column if not exists profile_version int;
alter table digests  add column if not exists model           text;
alter table digests  add column if not exists prompt_version  text;
alter table profiles add column if not exists active_version  int;

-- ---------------------------------------------------------------------------
-- Inserting a profile_versions row IS activation. The trigger assigns the
-- version number server-side (no read-then-write race from two clients) and
-- points profiles at the new row.
--
-- Undo is deliberately NOT an insert — it updates profiles directly to point
-- at an existing version, so undoing doesn't manufacture history.
-- ---------------------------------------------------------------------------
create or replace function public.activate_profile_version()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.version is null then
    select coalesce(max(version), 0) + 1 into new.version
      from profile_versions where user_id = new.user_id;
  end if;

  update profiles
     set compiled_profile = new.compiled_profile,
         active_version   = new.version
   where id = new.user_id;

  return new;
end;
$$;

drop trigger if exists on_profile_version_insert on profile_versions;
create trigger on_profile_version_insert
  before insert on profile_versions
  for each row execute function public.activate_profile_version();

-- Keep only the newest 5 versions per user — enough to undo a few bad edits,
-- without the table growing forever. The just-inserted row is always the
-- newest (and active), so it is never pruned.
create or replace function public.prune_profile_versions()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  delete from profile_versions
   where user_id = new.user_id
     and id not in (select id from profile_versions
                     where user_id = new.user_id
                     order by version desc limit 5);
  return null;
end;
$$;

drop trigger if exists on_profile_version_prune on profile_versions;
create trigger on_profile_version_prune
  after insert on profile_versions
  for each row execute function public.prune_profile_versions();

-- ---------------------------------------------------------------------------
-- Seed a version 1 for anyone who already has a compiled_profile, so existing
-- users have a 'user' baseline to undo back to. Skips users who already have rows.
-- ---------------------------------------------------------------------------
insert into profile_versions (user_id, compiled_profile, source, rationale)
select p.id, p.compiled_profile, 'user', 'baseline (pre-versioning)'
  from profiles p
 where p.compiled_profile is not null
   and not exists (select 1 from profile_versions v where v.user_id = p.id);

-- ---------------------------------------------------------------------------
-- RLS — same shape as the existing policies: each user sees only their own.
-- The worker uses the service-role key and bypass RLS entirely.
-- ---------------------------------------------------------------------------
alter table digest_feedback  enable row level security;
alter table profile_versions enable row level security;

drop policy if exists "own feedback" on digest_feedback;
create policy "own feedback" on digest_feedback
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

-- Insert as well as select: the compile endpoint writes a 'user' version while
-- acting as the user (their token), so RLS still applies there.
drop policy if exists "own profile versions read" on profile_versions;
create policy "own profile versions read" on profile_versions
  for select using (user_id = auth.uid());

drop policy if exists "own profile versions write" on profile_versions;
create policy "own profile versions write" on profile_versions
  for insert with check (user_id = auth.uid());
