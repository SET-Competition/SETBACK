-- ---------------------------------------------------------------------------
-- 002 — Make the leaderboard public, and shut the anon key out of everything
--       except the two things the website actually needs
--
-- Steps 1 and 2 are two problems that only appear once a real person
-- registers, both of which schema.sql reintroduced every time it was re-run.
-- Steps 3 and 4 take away the grants and widen the policies that made the
-- second one reachable in the first place.
--
-- Fixed here AND in schema.sql; this file exists so a database already
-- created from the old schema can be corrected without a full re-run.
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
revoke all on public.participants from anon;

-- 3. Every other table kept Supabase's default grants.
--
-- RLS with no matching policy already returns zero rows, which is what was
-- standing between anon and these - one layer, on tables holding encrypted
-- exchange credentials and every participant's trade history. The grant is
-- what makes a policy reachable, so removing it means a future
-- `create policy ... using (true)` - the exact mistake fixed in step 2 -
-- cannot open anything by itself.
--
-- pending_signups is absent deliberately: anon MUST keep insert there.
revoke all on public.participant_api_keys from anon;
revoke all on public.trade_metrics        from anon;
revoke all on public.balance_snapshots    from anon;
revoke all on public.fetch_errors         from anon;
revoke all on public.cash_flows           from anon;
revoke all on public.participant_scores   from anon;

-- 4. Two policies that name no role, and so apply to PUBLIC including anon.
--
-- They do not leak today, because auth.uid() is NULL for anon and
-- `participant_id = null` is null rather than true - an accident of the
-- comparison, not a boundary, and one that stops holding the moment either
-- predicate gains an `or`. Same shape as the policy dropped in step 2.
drop policy if exists "participants read own trades"    on public.trade_metrics;
drop policy if exists "participants read own snapshots" on public.balance_snapshots;

create policy "participants read own trades"
  on public.trade_metrics for select
  to authenticated
  using (participant_id = auth.uid());

create policy "participants read own snapshots"
  on public.balance_snapshots for select
  to authenticated
  using (participant_id = auth.uid());

commit;

-- Confirmation: the SQL editor shows the last statement's result.
--
--   leaderboard_public        t   anon can read the board
--   participants_readable     f   anon cannot read emails
--   participants_policies     0   no policy left to re-open it
--
-- participants_policies is 0 today because participants needs no policy at
-- all. If a legitimate `authenticated`-scoped one is ever added, that number
-- becomes 1 and this note is what says so was expected.
--   views_owned_by            one row per object, all the same owner
--
-- has_table_privilege rather than information_schema.role_table_grants:
-- that view only returns rows whose grantor or grantee is a currently
-- enabled role, so a pre-existing Supabase default grant can read as absent
-- BEFORE the revoke has run - a confirmation that says "already fine" when
-- nothing has happened yet is worse than none. has_table_privilege answers
-- the real question and cannot be filtered out.
--
-- views_owned_by is here because the fix above rests on views running with
-- their OWNER's privileges, bypassing RLS. That holds when the views and
-- participants share an owner, which they do when schema.sql created them
-- all. If participants were owned by a different role, leaderboard would
-- return zero rows instead of the board - an empty live table rather than a
-- permission error, so the site would show an empty board, not sample data.
-- Check the owners match; they should all read the same.
select
  has_table_privilege('anon', 'public.leaderboard',   'select') as leaderboard_public,
  has_table_privilege('anon', 'public.participants',  'select') as participants_readable,
  (select count(*) from pg_policies
    where schemaname = 'public' and tablename = 'participants')  as participants_policies,
  (select string_agg(relname || '=' || relowner::regrole::text, ', ' order by relname)
     from pg_class
    where relnamespace = 'public'::regnamespace
      and relname in ('participants', 'leaderboard', 'latest_balances'))
                                                                 as views_owned_by;
