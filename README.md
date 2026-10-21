# SET API

Backend for a crypto trading competition. Participants register **read-only**
exchange credentials for Coinbase and Lighter; a scheduled job records their
equity, orders and cash flows into Supabase; everyone is ranked on a
**flow-adjusted Sharpe ratio** — so the leaderboard measures trading, not
deposit size.

---

## How it works

```
/register (web/) ──► pending_signups ──► sign_ups (start of every sync):
                                          verify read-only, Fernet-encrypt
        ┌────────────────────────────────────────────────┘
        ▼
participant_api_keys  (Fernet-encrypted credentials in Supabase)
        │
        │  post_to_supabase.py, on Railway's cron
        ▼
  decrypt ──► Coinbase spot     Advanced Trade REST        (ccxt.coinbase)
          ──► Coinbase perps    derivatives JSON-RPC       (ccxt.deribit, adapted)
          ──► Lighter perps     read-only token            (ccxt.lighter)
        │
        ▼
balance_snapshots · trade_metrics · cash_flows · fetch_errors
        │
        │  score.py, immediately after each sync
        ▼
  metrics.py  ──►  participant_scores  ──►  leaderboard view
```

Three properties the design turns on:

- **Deposits must not look like profit.** A participant who wires in $50k on
  day 30 has not earned a 50k return. `cash_flows` is recorded so returns can
  be computed time-weighted, with external flows divided out.
- **Every credential is independent.** One participant's revoked key must not
  cost anyone else their run, so each is fetched and written under its own
  error boundary.
- **A snapshot missed is gone forever.** Orders and cash flows resume from
  their stored timestamps next run; the equity curve point for 12:15 cannot be
  backfilled. That asymmetry decides what counts as a failure and what the
  exit code means.

---

## Quick start

```bash
uv sync --dev                 # install, including test dependencies
cp .env.example .env          # then fill in SUPABASE_URL, SUPABASE_KEY, FERNET_KEY
uv run pytest -q              # 356 tests, no network needed
```

Apply the schema by pasting **`migrations/schema.sql`** into the Supabase SQL
editor. The file is idempotent and transactional — safe to re-run, and it
repairs an existing database rather than only creating a fresh one.

---

## Environment

Every variable is documented in [`.env.example`](.env.example), which
`tests/test_venues.py` asserts stays in sync with the code in both directions.
The three that must be set:

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Project URL |
| `SUPABASE_KEY` | The **secret** key (`sb_secret_…`), never the publishable one — the sync writes, and RLS blocks everything else |
| `FERNET_KEY` | Decrypts stored participant credentials — must be identical locally and on Railway |

One that must **never** be set on the deployed service:
`COINBASE_PERMISSIONS_OPTIONAL`, which turns a failure to read a Coinbase
key's permissions into a pass. See the warning in `.env.example`.

Locally these come from `.env` via `load_dotenv()`. On Railway there is no
`.env` file — Railway injects the variables into the process before Python
starts, `load_dotenv()` finds nothing, and `os.getenv()` reads what Railway
already put there.

---

## Database

| Object | What it holds |
|---|---|
| `participants` | One row per person |
| `participant_api_keys` | Encrypted credentials, one per venue |
| `balance_snapshots` | The equity curve — the irreplaceable table |
| `trade_metrics` | Closed orders |
| `cash_flows` | Deposits and withdrawals, so returns mean something |
| `fetch_errors` | Per-credential sync failures |
| `participant_scores` | Current Sharpe per participant, upserted each run |
| `pending_signups` | The signup form inbox — plaintext, wiped on import |
| `latest_balances` | *view* — current equity by venue |
| `daily_balances` | *view* — last snapshot per venue per day; what scoring reads |
| `leaderboard` | *view* — the ranking |

`leaderboard` sorts on `reliable desc, sharpe desc`. `reliable` means at least
20 return periods, so early in a competition nobody qualifies — provisional
entries sort below established ones rather than disappearing.

---

## Commands

```bash
uv run python post_to_supabase.py     # the scheduled job: import signups, sync, score
uv run python score.py                # rescore from stored history, no fetching
uv run python score.py --period daily
uv run python sign_ups.py             # import pending signups now, without syncing
uv run python sign_ups.py --csv       # legacy: import participant_signups.csv
uv run python metrics.py              # print Sharpe for everyone
uv run python rotate_credentials.py --audit   # report credential SHAPES only
uv run python coinbase.py             # sanity-check the adapter against your own keys
uv run python lighter.py
```

`post_to_supabase.py` exits non-zero only when the failure **rate** reaches
`SYNC_FAILURE_THRESHOLD` (default 1.0 — total failure). One participant's dead
key is not an outage, and an exit code that fires on any error stops meaning
anything. Scoring failures never change the exit code: the score recomputes
from complete history next run, so a failure there is stale ranks, not lost
data.

---

## Participant signup

Participants submit at **`/register`** in the frontend ([`web/`](web/)), which
POSTs to the `register` Edge Function. The importer then verifies each key
against the exchange, encrypts it under `FERNET_KEY`, and wipes the plaintext.
It runs at the start of every scheduled sync, so a new participant is
registered and gets their first snapshot within one cron interval, with
nobody having to remember to run it.

```
/register  →  register (Edge Function)   rate-limit, validate, service role
      →  pending_signups                 anon can reach NOTHING here
      →  sign_ups                        each sync, before syncing:
                                         verify read-only against the exchange
                                         → Fernet-encrypt → participant_api_keys
                                         → wipe the plaintext row
```

The function is the entrance because the browser used to write to the table
directly, which is why nothing could rate-limit the form — see **Rate
limiting** below.

**One submission registers every venue.** The page asks for Coinbase spot,
Coinbase perpetuals and Lighter together, and POSTs an array of one to three
rows — one per venue filled in. Each section is optional; at least one is
required. Keeping a row per venue rather than one wide row means each
credential is verified independently, so a bad Coinbase key does not cost
someone their valid Lighter registration.

| Venue | What the participant pastes |
|---|---|
| Coinbase spot | Key name (`organizations/…/apiKeys/…`) **and** the EC private key |
| Coinbase perps | A *separate* key — Coinbase issues different credentials for perps |
| Lighter | The `ro:` read-only token, and nothing else |

No passphrase is asked for: `ccxt.coinbase` requires only `apiKey` and
`secret`, and `ccxt.lighter` only `privateKey`. Coinbase's INTX portfolio UUID
is resolved automatically at verification, so participants never supply it.

**Setting it up**

1. Run [`migrations/schema.sql`](migrations/schema.sql) against the project.
   It is idempotent and includes everything below; the numbered migrations
   exist for databases that predate a change.
2. Deploy the signup endpoint — **nothing can register until this exists**:

   ```bash
   supabase link --project-ref <your-project-ref>
   supabase functions deploy register
   ```

   Confirm **Verify JWT is off** for it. If submissions come back
   `503 Registration is temporarily unavailable` and the function log says
   `rate limiter failed 404`, PostgREST has not reloaded its schema cache —
   run `notify pgrst, 'reload schema';`.
3. Set `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY` on the
   frontend — `web/.env.local` locally, the Vercel project's Variables tab in
   production. Neither is secret: the anon key is public by design, and the
   grants are what constrain it. Without them `/register` renders but refuses
   to submit and says so.
4. Deploy over **HTTPS**. The page disables itself outside a secure context,
   because it asks people to type exchange credentials.
5. Nothing to run by hand: the next scheduled sync imports whatever is
   waiting. `uv run python sign_ups.py` does it immediately, if you'd rather
   not wait.

On an existing database instead of step 1: run `migrations/003`, deploy the
function, redeploy the site, then `migrations/004` — in that order. Between
the redeploy and 004 both paths work, so the form is never down.

The validation that runs before a credential leaves the browser lives in
[`web/lib/signup.js`](web/lib/signup.js), and `tests/test_signup_form.py`
executes it under Node against this repository's schema — so the venue list,
the POSTed columns and `pending_signups` cannot drift apart.

**It is drained every sync, and that is load-bearing.** Credentials sit in
`pending_signups` **in plaintext** between submission and import — anyone who
can read that table, and every Supabase backup taken while a row is pending,
holds usable credentials. That window is the whole exposure. Each sync starts
by importing, so it lasts at most one cron interval while the job is running
— and as long as the job is down otherwise, which is one more reason to
watch its exit code.

The trade is deliberate and rests on two things. Every credential here is
read-only, so a leak exposes positions rather than funds; and the rows clear
on import. If either stops being true — a credential that can move money, or a
table left undrained for weeks — this design needs revisiting. The alternative
is a Supabase Edge Function holding `FERNET_KEY` and encrypting at insert, so
plaintext never lands at all.

**Why not encrypt in the browser?** Fernet is symmetric. The page would need
`FERNET_KEY` itself, and shipping that to every visitor would expose the key
protecting every credential already stored.

**What the anon key can do.** One thing: select the `leaderboard` view. The
public ranking is read straight from the browser; everything else is revoked.

Signups used to be the second thing — anon held `insert` on `pending_signups`
— and that is exactly why nothing rate-limited the form. The write went from
the visitor's browser to PostgREST without passing through anything we run, so
Cloudflare and Vercel, which sit in front of the *website*, were never on the
path. Submissions now go to the `register` Edge Function, which runs inside
Supabase with the service role, sees the caller's address and checks
`record_signup_attempt` before inserting. See `migrations/003` and `004`.

A submitter still cannot read anything back: not their own row, not anyone
else's, and not whether an email is already registered. The `leaderboard` view
carries display names, scores and balances; `participants`, which carries
email, is revoked and has no policy, so addresses are unreachable.
Verified against real PostgreSQL, along with a check constraint that refuses
to let a resolved row keep a credential — so the plaintext is destroyed on
import even if the importer has a bug.

**Rate limiting.** 20 submissions per network per hour by default, counted and
recorded in one statement by `record_signup_attempt` so two simultaneous
requests cannot both pass a limit of one.

Per **network**, not per address: `inet` equality compares the netmask too, so
counting raw addresses would give any IPv6 client a fresh window per request
(a residential allocation is a /64) and let anyone buy 33 windows on one IPv4
address by varying the mask. Keys are normalised to /64 and /32.

Blocked attempts are deliberately **not** recorded — a flood that extended its
own window would turn a rate limit into a permanent ban for everyone behind a
NAT, a campus or a phone network. That cuts both ways: 20/hour is shared by
everyone behind a single CGNAT or campus egress, so **raise it before a launch
event** where a cohort may sign up from one address.

Tune with these, set as function secrets
(`supabase secrets set SIGNUP_MAX_ATTEMPTS=50`), no redeploy needed:

| variable | default | notes |
|---|---|---|
| `SIGNUP_MAX_ATTEMPTS` | `20` | A non-integer or non-positive value logs and falls back rather than disabling the limit |
| `SIGNUP_WINDOW_SECONDS` | `3600` | Seconds, not an interval string — a malformed interval would 503 every request, and a negative one would move the window into the future and silently switch the limit off |

A determined attacker with addresses to spend still gets through; this bounds
casual abuse of an open form, which is what it is for. A captcha is the next
step if it ever needs one.

Verification is asynchronous, so someone who submits a trade-capable key finds
out later. The reason is stored in `pending_signups.last_error`, and transient
failures are retried up to `MAX_IMPORT_ATTEMPTS` before the row is rejected and
its credential wiped.

---

## Coinbase perpetuals

On **1 October 2026** Coinbase moved international perpetuals off INTX and onto
a Deribit-powered **JSON-RPC 2.0** gateway at `drb.coinbase.com/api/v2`, with
no parallel run. Spot is unchanged and stays on Advanced Trade REST.

ccxt has no support for the gateway, but the gateway *is* Deribit's API, so
[`coinbase_derivatives.py`](coinbase_derivatives.py) subclasses
`ccxt.deribit` and changes only two things: the host, and the auth — the
participant's existing CDP key is exchanged at `public/auth`
(`grant_type: coinbase_cdp`) for a 15-minute bearer token. Everything else is
ccxt's own Deribit parsing, so perp orders reach `trade_metrics` in the same
shape as spot ones. No new credentials, no new dependency.

Three things about the gateway that fail silently if handled naively:

- **Valuation** comes from `total_equity_usd`, an account-level figure.
  Summing per-currency `margin_balance` counts the same collateral once per
  listed currency — it turned a $19.95 account into $622.05.
- **Order history needs both modes.** `historical=false` returns only the last
  30 minutes of orders; `historical=true` excludes the most recent. The
  adapter fetches both and merges them.
- **Migration artifacts.** Each account's INTX balance arrived as a
  `coinbase_migration` correction, and each open position was recreated by a
  zero-fee block trade. Those recreations are dropped, so positions open on
  1 October are not recorded twice.

The old INTX REST endpoints still answer, as a mirror lagging the gateway by
about a minute. They are not used: the documented path is the gateway, and an
undocumented mirror of a product Coinbase says has ended can disappear without
notice.

---

## Deployment

Railway, `railpack.json` → `python post_to_supabase.py`, on a cron schedule.
Set the environment variables in the service's Variables tab.

The build runs `uv sync --locked`, which **refuses to resolve** and fails the
build when `uv.lock` disagrees with `pyproject.toml`. After changing
dependencies, run `uv lock` and commit the result. CI checks this.

Snapshot cadence matters to the metric: `build_portfolio_series` writes a
silent venue off after 3 stale periods, so the cron interval and that
threshold have to be read together.

---

## Rotating `FERNET_KEY`

The one procedure where wrong ordering is unrecoverable. `MultiFernet`
encrypts with the first key and decrypts with any, which is what makes this
survivable at all.

1. Generate a new key:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Put the **new** key in `FERNET_KEY`, and move the **old** one to
   `FERNET_KEYS_RETIRED`. Both must be set at once — this is the step that
   keeps existing rows readable.
3. Run `uv run python rotate_credentials.py` to re-encrypt every row.
4. Only when it reports every row on the current key, remove
   `FERNET_KEYS_RETIRED`.

Skipping step 2 orphans every stored credential permanently: nothing decrypts,
and every participant must issue new exchange keys.

---

## Read-only enforcement

Credentials are rejected at registration unless they are provably read-only.

- **Coinbase** — `v3PrivateGetBrokerageKeyPermissions` must report `can_view`
  without `can_trade` or `can_transfer`. An error reading permissions is a
  rejection, not a pass.
- **Lighter** — only `ro:` read-only tokens are accepted. An L1 wallet private
  key is unscoped and irrevocable, controlling every asset in the wallet on
  every chain; it is refused at signup and again every time an exchange is
  built, so one already stored stops working too. There is no override.

`rotate_credentials.py --audit` reports the shape of every stored credential —
`participant_id` and shape only, never a value, not even a prefix.

---

## Tests and CI

```bash
uv run pytest -q          # no network: conftest.py installs placeholder secrets
uv run ruff check .
```

CI runs both against a **fresh checkout**, plus a production-only install
(`uv sync --locked --no-dev`) that imports every module. That last job exists
because two failures reached `main` that a local `pytest` could not catch:
a module referencing a constant that was never committed, and a source file
that was never committed at all. Only a clean checkout reproduces either.
