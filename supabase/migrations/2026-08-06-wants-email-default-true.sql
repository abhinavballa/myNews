-- Email should be the default channel: opt-out, not opt-in.
-- Run once in the Supabase SQL editor (schema.sql already ran, so its updated
-- DEFAULT does not retroactively change the live column or existing rows).

-- 1. New rows (incl. the auto-create trigger) get wants_email = true.
alter table profiles alter column wants_email set default true;

-- 2. Backfill everyone currently opted out by the old default.
--    (Anyone who explicitly turns email off later will set it false again.)
update profiles set wants_email = true where wants_email is distinct from true;
