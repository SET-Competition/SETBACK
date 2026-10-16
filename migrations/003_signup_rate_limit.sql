-- ---------------------------------------------------------------------------
-- 003 — Rate-limit signups
--
-- README has said since the form was written that nothing rate-limits
-- submissions and that this must be fixed "before the form is advertised
-- anywhere public". This is that fix, and it has to go where the WRITE
-- actually happens.
--
-- The form POSTs from the browser straight to PostgREST, so the request never
-- touches the website. Rate limiting at Cloudflare or Vercel would throttle
-- page loads while the flood went to supabase.co directly - anyone can read
-- the anon key out of the JS bundle, which is by design and not a leak. The
-- only place that sees every insert is the database.
--
-- So: a Supabase Edge Function becomes the single entrance. It runs inside
-- the project with the service role, sees the caller's IP, calls the function
-- below, and inserts only if allowed. 004 then revokes anon's direct insert,
-- which is what actually closes the old path - this file alone changes
-- nothing about what anon can do.
--
-- RUN THIS FIRST, and 004 only once the Edge Function is deployed and the
-- site is redeployed against it. In between, both paths work.
--
-- Safe to run more than once.
-- ---------------------------------------------------------------------------

begin;

-- One row per submission attempt, allowed or not.
--
-- The IP is stored in the clear and pruned after a day. It is personal data,
-- kept on the narrow basis of preventing abuse of an open form, for as long
-- as that takes and no longer. Hashing it would buy little: the space of IPv4
-- is small enough to reverse a bare hash by brute force, so it would read as
-- a protection without being one. The table is service-role only - no grant,
-- and RLS with no policy - so nothing reachable from a browser can read it.
create table if not exists public.signup_attempts (
  id           bigint generated always as identity primary key,
  client_ip    inet        not null,
  attempted_at timestamptz not null default now()
);

create index if not exists signup_attempts_ip_time
  on public.signup_attempts (client_ip, attempted_at desc);

alter table public.signup_attempts enable row level security;
revoke all on public.signup_attempts from anon;
revoke all on public.signup_attempts from authenticated;

-- Count and record in one statement.
--
-- Counting and then inserting from the Edge Function would race: two requests
-- that both read "4 so far" would both insert, and the limit would be one
-- higher than it says under exactly the load it exists to handle. Doing both
-- inside one function call makes that impossible.
--
-- Returns true if the caller may proceed.
create or replace function public.record_signup_attempt(
  p_ip           inet,
  p_max_attempts integer  default 5,
  p_window       interval default interval '1 hour'
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  recent integer;
begin
  -- Retention, not correctness: the count below is already bounded by
  -- p_window. Done here so the table cannot grow without a scheduled job.
  delete from public.signup_attempts
   where attempted_at < now() - interval '1 day';

  select count(*) into recent
    from public.signup_attempts
   where client_ip = p_ip
     and attempted_at > now() - p_window;

  if recent >= p_max_attempts then
    -- Deliberately NOT recorded. Recording blocked attempts would let a
    -- flood hold its own window open indefinitely, turning a rate limit into
    -- a permanent ban for whoever shares that address - a NAT, a campus, a
    -- phone network.
    return false;
  end if;

  insert into public.signup_attempts (client_ip) values (p_ip);
  return true;
end;
$fn$;

-- security definer means this runs as its owner, so execute has to be taken
-- away from everyone it was granted to by default. The Edge Function calls it
-- with the service role, which is unaffected.
revoke all on function public.record_signup_attempt(inet, integer, interval) from public;
revoke all on function public.record_signup_attempt(inet, integer, interval) from anon;
revoke all on function public.record_signup_attempt(inet, integer, interval) from authenticated;

commit;

-- Confirmation: the SQL editor shows the last statement's result.
--   attempts_table      t
--   limiter_function    t
--   anon_can_execute    f
--   anon_reads_attempts f
select
  to_regclass('public.signup_attempts') is not null            as attempts_table,
  to_regprocedure('public.record_signup_attempt(inet, integer, interval)')
    is not null                                                as limiter_function,
  has_function_privilege('anon',
    'public.record_signup_attempt(inet, integer, interval)', 'execute')
                                                               as anon_can_execute,
  has_table_privilege('anon', 'public.signup_attempts', 'select')
                                                               as anon_reads_attempts;
