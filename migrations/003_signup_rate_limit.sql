-- ---------------------------------------------------------------------------
-- 003 — Rate-limit signups
--
-- README has said since the form was written that nothing rate-limits
-- submissions and that this must be fixed "before the form is advertised
-- anywhere public". This is that fix, and it has to go where the WRITE
-- actually happens.
--
-- The form POSTed from the browser straight to PostgREST, so the request
-- never touched the website. Rate limiting at Cloudflare or Vercel would
-- throttle page loads while the flood went to supabase.co directly - anyone
-- can read the anon key out of the JS bundle, which is by design and not a
-- leak. The only place that sees every insert is the database.
--
-- So: a Supabase Edge Function becomes the single entrance. It runs inside
-- the project with the service role, identifies the caller, calls the
-- function below, and inserts only if allowed. 004 then revokes anon's direct
-- insert, which is what actually closes the old path - this file alone
-- changes nothing about what anon can do.
--
-- These objects are also in schema.sql (section 8a), so a fresh project gets
-- them without running this file. This exists for a database that already
-- predates them.
--
-- RUN THIS FIRST, then deploy the Edge Function, then point the site at it,
-- and only then 004. In between, both paths work and nothing is down.
--
-- Safe to run more than once.
-- ---------------------------------------------------------------------------

begin;

-- One row per allowed submission.
--
-- client_ip holds a NETWORK, not the exact address the caller presented - see
-- record_signup_attempt for why. It is personal data, kept on the narrow
-- basis of preventing abuse of an open form, pruned after a day. Hashing it
-- would buy little: IPv4 is small enough to reverse a bare hash by brute
-- force, so it would read as a protection without being one. The table is
-- service-role only - no grant, and RLS with no policy - so nothing reachable
-- from a browser can read it.
create table if not exists public.signup_attempts (
  id           bigint generated always as identity primary key,
  client_ip    inet        not null,
  attempted_at timestamptz not null default now()
);

-- For the count: the window is scanned within one network.
create index if not exists signup_attempts_ip_time
  on public.signup_attempts (client_ip, attempted_at desc);

-- For the prune. Without it the delete below is a sequential scan on every
-- request, so the cost of each submission grows with the size of the table -
-- which is exactly backwards for something whose job is to survive a flood.
create index if not exists signup_attempts_age
  on public.signup_attempts (attempted_at);

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
  p_ip             inet,
  p_max_attempts   integer default 20,
  p_window_seconds integer default 3600
) returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $fn$
declare
  v_key    inet;
  v_window interval;
  recent   integer;
begin
  -- A null argument must not read as "no limit".
  --
  -- The caller passes these from its environment, and a misconfigured value
  -- arrives as JSON null rather than as an absent argument - so the DEFAULT
  -- above does not apply. `recent >= null` is null, the branch is not taken,
  -- and the limiter silently stops limiting. Fail closed instead.
  if p_ip is null or p_max_attempts is null or p_window_seconds is null then
    return false;
  end if;

  -- Count per NETWORK, not per address.
  --
  -- inet equality compares the address AND the netmask, so without this:
  --
  --   * an IPv6 client gets a fresh window per source address, free and
  --     without forging anything - a residential allocation is a /64 and
  --     every device can rotate through many /128s within it;
  --   * `1.2.3.4/24` is a different inet from `1.2.3.4/32`, so varying the
  --     mask alone buys 33 independent windows on one IPv4 address.
  --
  -- network() zeroes the host bits, so every address inside the /64 (or the
  -- exact IPv4 address) collapses to one key. The Edge Function also refuses
  -- a value carrying a mask, but this is the half that has to hold.
  v_key := network(set_masklen(p_ip, case when family(p_ip) = 6 then 64 else 32 end));
  v_window := make_interval(secs => p_window_seconds);

  -- Retention, not correctness: the count below is bounded by v_window
  -- regardless. Run on roughly one call in fifty rather than every one,
  -- because a delete on every request serialises concurrent callers on the
  -- row locks of the same old rows - turning a limiter into a queue under
  -- the load it exists for.
  if random() < 0.02 then
    delete from public.signup_attempts
     where attempted_at < now() - interval '1 day';
  end if;

  select count(*) into recent
    from public.signup_attempts
   where client_ip = v_key
     and attempted_at > now() - v_window;

  if recent >= p_max_attempts then
    -- Deliberately NOT recorded. Recording blocked attempts would let a
    -- flood hold its own window open indefinitely, turning a rate limit into
    -- a permanent ban for whoever shares that network - a NAT, a campus, a
    -- phone network.
    return false;
  end if;

  insert into public.signup_attempts (client_ip) values (v_key);
  return true;
end;
$fn$;

-- security definer means this runs as its owner, so EXECUTE has to be taken
-- away from everyone PUBLIC gave it to...
revoke all on function public.record_signup_attempt(inet, integer, integer) from public;
revoke all on function public.record_signup_attempt(inet, integer, integer) from anon;
revoke all on function public.record_signup_attempt(inet, integer, integer) from authenticated;

-- ...and given back, explicitly, to the one role that calls it. Supabase's
-- default privileges would usually cover this, but the entire signup flow
-- gates on this one call and "it works because of a platform default" is not
-- something to leave implicit.
grant execute on function public.record_signup_attempt(inet, integer, integer) to service_role;

commit;

-- PostgREST caches the schema. A new function is not reachable at
-- /rest/v1/rpc/... until that cache reloads, and Supabase's DDL event trigger
-- normally does it within seconds. If submissions come back 503 and the logs
-- say "rate limiter failed 404", this is why - run `notify pgrst, 'reload schema';`
notify pgrst, 'reload schema';

-- Confirmation: the SQL editor shows the last statement's result.
--   attempts_table          t
--   limiter_function        t
--   service_role_executes   t   the caller can actually call it
--   anon_executes           f
--   anon_reads_attempts     f
select
  to_regclass('public.signup_attempts') is not null                as attempts_table,
  to_regprocedure('public.record_signup_attempt(inet, integer, integer)')
    is not null                                                    as limiter_function,
  has_function_privilege('service_role',
    'public.record_signup_attempt(inet, integer, integer)', 'execute')
                                                                   as service_role_executes,
  has_function_privilege('anon',
    'public.record_signup_attempt(inet, integer, integer)', 'execute')
                                                                   as anon_executes,
  has_table_privilege('anon', 'public.signup_attempts', 'select')   as anon_reads_attempts;
