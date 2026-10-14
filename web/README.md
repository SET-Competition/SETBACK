# web

Frontend for the SET trading competition. Next.js App Router, deployed to
Vercel with **Root Directory set to `web/`** — the repository root stays a
Python project and keeps building on Railway.

```bash
npm install
cp .env.example .env.local   # optional; without it the board shows sample data
npm run dev
```

## Routes

| Route | What it is |
|---|---|
| `/` | Redirects to `/leaderboard`. There is no landing page yet; when there is, `app/page.tsx` becomes it. |
| `/leaderboard` | The ranking. Reads the `leaderboard` view, revalidating every 5 minutes. |
| `/register` | Participant signup. POSTs to `pending_signups` from the browser. |

`/register` is a thin server component wrapping `register-form.tsx`, which is a
client component — it holds submit state and talks to Supabase directly. The
split exists because a `"use client"` module cannot export `metadata`, and each
route needs its own `<title>`.

The validation that runs before a credential leaves the browser lives in
[`lib/signup.js`](lib/signup.js). That file is `.js` rather than `.ts` on
purpose: `tests/test_signup_form.py` extracts the block between its PORTABLE
LOGIC markers and runs it under Node against the repository's schema, so the
venue list, the POSTed columns and `pending_signups` cannot drift apart. Its
types are JSDoc, which TypeScript honours in a `.js` file.

## Environment

| Variable | Notes |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | The **anon** key. Public by design; RLS constrains it. |

Never set `SUPABASE_KEY` (service_role) or `FERNET_KEY` here. They decrypt
every participant's exchange credentials and belong only to the backend.

`NEXT_PUBLIC_*` values are inlined **at build time**, not read at runtime. Set
them in Vercel before the build that should carry them; changing one afterwards
needs a redeploy, not a restart.

Without them the leaderboard renders sample rows and says so in a banner — see
`getLeaderboard()` in `lib/leaderboard.ts` — and `/register` renders but refuses
to submit, for the same reason: a page that silently does nothing is worse than
one that says what is missing.

## Before the live board works

`migrations/schema.sql` ends with `revoke all on public.leaderboard from anon`,
so the anon key cannot read the view yet. Either grant it —

```sql
grant select on public.leaderboard to anon;
```

— which the schema's own comments call safe, since the view carries no email
and no key material, or add Supabase Auth and keep it to `authenticated`.

`/register` needs no such grant: RLS already gives anon `insert` on
`pending_signups`, and deliberately no `select`.

## Deploying to set-global.com

1. Import the repository into Vercel and set **Root Directory** to `web/`.
2. Add both `NEXT_PUBLIC_*` variables in the project's Settings → Environment
   Variables, for every environment you build.
3. Settings → Domains, add `set-global.com`, and point the DNS records Vercel
   gives you at it from wherever the domain is registered.

`/register` must be served over HTTPS, which Vercel does automatically. The page
disables itself outside a secure context, because it asks people to type
exchange credentials.
