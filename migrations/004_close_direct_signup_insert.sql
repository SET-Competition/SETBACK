-- ---------------------------------------------------------------------------
-- 004 — Close the direct path into pending_signups
--
-- RUN THIS LAST, and only once BOTH of these are true:
--
--   1. the `register` Edge Function is deployed
--        supabase functions deploy register
--   2. the site is redeployed and its form posts to /functions/v1/register
--
-- Between 003 and this file, both paths work and nothing breaks. After this
-- file, the old one is gone: anon cannot insert, so a browser that still
-- posts to /rest/v1/pending_signups gets 401 and the form stops working.
--
-- That is the point. The rate limit in 003 is only a limit if this is the
-- only entrance - otherwise it is a speed bump beside an open door, and the
-- door is the one documented in the bundle every visitor downloads.
--
-- TO UNDO, two things are needed and the SQL alone is not enough.
--
-- The deployed site posts to /functions/v1/register, so re-opening the table
-- without reverting the frontend leaves the form pointing at a function that
-- may be the thing you are rolling back from. Revert
-- web/app/register/register-form.tsx to post to /rest/v1/pending_signups and
-- redeploy, THEN run:
--
--   create policy "anon submits a signup"
--     on public.pending_signups
--     for insert
--     to anon
--     with check (true);
--
--   grant insert on public.pending_signups to anon;
--   revoke select, update, delete on public.pending_signups from anon;
--
-- Written out in full because this PR deleted them from schema.sql, and a
-- rollback instruction that says "re-run the statements this replaces" is
-- useless once the statements are only in git history.
--
-- Safe to run more than once.
-- ---------------------------------------------------------------------------

begin;

drop policy if exists "anon submits a signup" on public.pending_signups;
revoke all on public.pending_signups from anon;

commit;

-- Confirmation: the SQL editor shows the last statement's result.
--   anon_can_insert   f   the direct path is closed
--   anon_policies     0   nothing left to re-open it
--
-- If anon_can_insert is still true, something re-granted it - check for a
-- later re-run of an older schema.sql.
select
  has_table_privilege('anon', 'public.pending_signups', 'insert') as anon_can_insert,
  (select count(*) from pg_policies
    where schemaname = 'public' and tablename = 'pending_signups')
                                                                  as anon_policies;
