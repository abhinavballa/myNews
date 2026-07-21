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
  wants_email    boolean default false,
  active         boolean default true,
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
create table if not exists digests (
  id         bigserial primary key,
  user_id    uuid references profiles(id) on delete cascade,
  local_date date not null,
  teaser     text,
  html       text,
  created_at timestamptz default now(),
  unique (user_id, local_date)
);

create index if not exists digests_user_date_idx on digests (user_id, local_date);

-- ---------------------------------------------------------------------------
-- Row-Level Security: each user reads/writes only their own rows. The worker
-- uses the service-role key and bypasses RLS entirely.
-- ---------------------------------------------------------------------------
alter table profiles           enable row level security;
alter table push_subscriptions enable row level security;
alter table digests            enable row level security;

create policy "own profile"        on profiles
  for all using (id = auth.uid()) with check (id = auth.uid());

create policy "own subscriptions"  on push_subscriptions
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

create policy "own digests"        on digests
  for select using (user_id = auth.uid());

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
