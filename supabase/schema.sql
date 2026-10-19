-- myNews schema — personalized multi-user digest.
-- Phase 1 uses `profiles` and `digests`; `push_subscriptions` is created now so
-- Phase 3 (web push) is purely additive.

create extension if not exists "uuid-ossp";

-- ---------------------------------------------------------------------------
-- profiles: one row per user. Replaces the RECIPIENT env var and the hardcoded
-- PROMPT. `compiled_profile` is the contract between the compile step (Phase 2)
-- and the worker.
-- ---------------------------------------------------------------------------
create table if not exists profiles (
  id             uuid primary key references auth.users on delete cascade,
  email          text,
  interests_text text,                                   -- raw text box content
  compiled_profile jsonb,                                -- {persona, edge_for, sections[]}
  compiled_at    timestamptz,
  delivery_hour  int  default 8,                         -- 0-23, user's local time
  timezone       text default 'America/Los_Angeles',
  wants_push     boolean default true,
  wants_email    boolean default true,
  active         boolean default true,
  active_version int,                                    -- -> profile_versions.version
  created_at     timestamptz default now()
);

-- ---------------------------------------------------------------------------
-- push_subscriptions: Web Push endpoints (Phase 3). One user may have many.
-- ---------------------------------------------------------------------------
create table if not exists push_subscriptions (
  id            bigserial primary key,
  user_id       uuid references profiles(id) on delete cascade,
  endpoint      text unique,
  p256dh        text,
  auth          text,
  failure_count int default 0,
  created_at    timestamptz default now()
);

-- ---------------------------------------------------------------------------
-- digests: one generated brief per user per local date. The UNIQUE constraint
-- is load-bearing: hourly cron re-runs and delayed schedules must never
-- double-deliver.
-- ---------------------------------------------------------------------------
-- The attribution columns (profile_version, model, prompt_version) exist so a
-- thumbs-up can be traced to what actually produced the brief — a silent model
-- fallback would otherwise look like a profile effect. They cannot be backfilled.
create table if not exists digests (
  id              bigserial primary key,
  user_id         uuid references profiles(id) on delete cascade,
  local_date      date not null,
  teaser          text,
  html            text,
  profile_version int,
  model           text,
  prompt_version  text,
  created_at      timestamptz default now(),
  unique (user_id, local_date)
);

create index if not exists digests_user_date_idx on digests (user_id, local_date);

-- ---------------------------------------------------------------------------
-- digest_feedback: the user's verdict on one brief. `notes` is the comment
-- /api/feedback applies to the profile; `rating` is a quick thumb kept for the
-- record. Either may be absent. UNIQUE (user_id, digest_id) makes it an
-- upsert, so changing your mind replaces the verdict instead of adding a
-- contradictory row.
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
-- profile_versions: the user's last 5 compiled_profiles — 'user' (from the
-- compile step) or 'learned' (from applying a feedback comment).
-- profiles.compiled_profile stays the ACTIVE copy so the worker is unchanged;
-- this table is what makes a bad edit undoable.
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

-- ---------------------------------------------------------------------------
-- Row-Level Security: each user reads/writes only their own rows. The worker
-- uses the service-role key and bypasses RLS entirely.
-- ---------------------------------------------------------------------------
alter table profiles           enable row level security;
alter table push_subscriptions enable row level security;
alter table digests            enable row level security;
alter table digest_feedback    enable row level security;
alter table profile_versions   enable row level security;

create policy "own profile"        on profiles
  for all using (id = auth.uid()) with check (id = auth.uid());

create policy "own subscriptions"  on push_subscriptions
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy "own digests"        on digests
  for select using (user_id = auth.uid());

create policy "own feedback"       on digest_feedback
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

-- Insert as well as select: the compile endpoint writes a 'user' version while
-- acting as the caller (their token), so RLS still applies there.
create policy "own profile versions read"  on profile_versions
  for select using (user_id = auth.uid());

create policy "own profile versions write" on profile_versions
  for insert with check (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Inserting a profile_versions row IS activation. The trigger assigns the
-- version number server-side (so two clients can't race on max(version)+1)
-- and points the profile at the new row.
--
-- Undo is deliberately NOT an insert — it updates profiles to point at an
-- existing version, so undoing doesn't manufacture history.
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
-- Auto-provision a profile whenever someone signs in for the first time, so no
-- profile row (and no uuid) ever has to be created by hand. The row starts with
-- a null compiled_profile; the user (Phase 2) or the seed fills it in. Runs as
-- SECURITY DEFINER because the new auth user has no session yet.
-- ---------------------------------------------------------------------------
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, email, active)
  values (new.id, new.email, true)
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
