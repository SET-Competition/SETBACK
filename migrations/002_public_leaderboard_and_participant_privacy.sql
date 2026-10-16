-- ---------------------------------------------------------------------------
-- 002 — Make the leaderboard public, and stop anon reading participant email
--
-- Two problems that only appear once a real person registers, both of which
-- schema.sql reintroduces every time it is re-run. Fixed here AND in
-- schema.sql; this file exists so a database already created from the old
-- schema can be corrected without a full re-run.
--
-- Safe to run more than once.
-- ---------------------------------------------------------------------------

begin;

-- 1. The leaderboard is a public web page.
--
-- schema.sql ended with `revoke all on public.leaderboard from anon`, which
-- made web/lib/leaderboard.ts fall back to MOCK_ROWS: the live site served
-- five invented traders under a "sample data" banner. A public competition
-- ranking that silently shows fabricated standings is worse than one that
-- shows an error, and the only consumer of this view is a browser holding the
-- anon key, so anon is exactly the role that needs it.
--
-- The view carries display_name, scores and balances - no email, no key
-- material, no per-trade detail - which is why it, and only it, is public.
grant select on public.leaderboard to anon;

-- 2. participants.email was world-readable.
--
-- The policy below was named "leaderboard is readable" and written
-- `for select using (true)` with no `to` clause, so it applied to anon, and
-- anon's default grant on the public schema did the rest: every participant's
-- email address was readable over PostgREST by anyone who opened the site and
-- copied the key out of the bundle.
--
-- It never did what its name claimed. `leaderboard` is a VIEW, and a view
-- runs with its owner's privileges rather than the caller's, so it reads
-- participants regardless of RLS - as the comment in section 12 of schema.sql
-- says. Dropping this policy does not affect the leaderboard; it only stops
-- the direct table read.
drop policy if exists "leaderboard is readable" on public.participants;

-- Belt and braces: RLS with no policy already returns zero rows, but the
-- table grant is what made the policy reachable, so remove that too.
revoke select on public.participants from anon;

commit;

-- Confirmation: the SQL editor shows the last statement's result.
--   leaderboard_public        should be 1
--   participants_anon_select  should be 0
--   participants_anon_policy  should be 0
select
  (select count(*) from information_schema.role_table_grants
    where table_schema = 'public' and table_name = 'leaderboard'
      and grantee = 'anon' and privilege_type = 'SELECT')   as leaderboard_public,
  (select count(*) from information_schema.role_table_grants
    where table_schema = 'public' and table_name = 'participants'
      and grantee = 'anon' and privilege_type = 'SELECT')   as participants_anon_select,
  (select count(*) from pg_policies
    where schemaname = 'public' and tablename = 'participants'
      and policyname = 'leaderboard is readable')           as participants_anon_policy;
