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

## The brand marks

The SET lockup and logomark are geometry and colour, held once in
[`lib/marks.ts`](lib/marks.ts). Everything that draws them imports it:
`app/brand.tsx` for the nav, `app/opengraph-image.tsx` for the share card.
The words that go with them live in [`lib/copy.ts`](lib/copy.ts) for the same
reason.

Three files cannot import anything, because they are consumed as files.
[`scripts/build-icons.mjs`](scripts/build-icons.mjs) generates all three from
`lib/marks.ts`:

| File | What reads it | Ground |
|---|---|---|
| `app/icon.svg` | The browser tab | transparent, adapts |
| `app/favicon.ico` | Browsers that will not take an SVG icon | transparent |
| `app/apple-icon.png` | iOS home screens, 180x180 | opaque |

Change the logo and you regenerate `lib/marks.ts` from the brand kit, then:

```bash
npm run icons        # rewrites all three from lib/marks.ts
```

Commit what it writes. It is deliberately not part of `npm run build`: Vercel
would otherwise regenerate committed binaries on every deploy, against
whatever libvips the builder ships, and a rendering change would reach
production without ever appearing in a diff.

### Why the tab icon has no background

It is drawn on the *browser's* chrome, not on the site, and this site has no
light mode whose colours would apply. A single fill is wrong half the time —
the brand's near-black vanishes into a dark tab strip, white vanishes into a
light one. So `icon.svg` carries both and lets a `prefers-color-scheme` rule
inside the SVG choose. Verified in Chrome: with the OS in dark mode it
fetches `/icon.svg` — never `/favicon.ico`, despite the ICO being listed
first — and renders the mark white.

`favicon.ico` is that same SVG flattened, and a raster cannot carry a media
query. It ships the light-chrome colour, because librsvg applies the default
CSS rule and ignores the unmatched `prefers-color-scheme` block. **So a
browser that both refuses SVG icons and runs dark chrome gets a near-black
mark on a dark toolbar.** That is the one gap, it is narrow — a browser new
enough to have dark chrome is almost always new enough to take the SVG — and
closing it would mean putting a tile back behind the mark.

`apple-icon.png` keeps its opaque brand tile on purpose: iOS composites a
transparent home-screen icon onto a ground of its own choosing, so Apple's
guidance is an opaque full-bleed icon. It is also padded where the favicon is
nearly full-bleed, because iOS draws it as a rounded tile and the rounding
needs margin — whereas a 16px tab icon has 256 pixels to work with and should
not spend them on air.

### What holds it together

`tests/test_brand_assets.py` checks that `icon.svg` draws `LOGOMARK_PATHS`,
that it is placed where the generator's constants say, that it carries both
mark colours, that each raster sits on the ground its surface needs, and that
the rasters place the mark where the generator's layout puts it.

That last check exists because they once did not — both rasters were made
from the brand kit's square PNG, whose padding is tighter, so the mark
shipped about 5% larger on the iOS tile and 12% wider at 32px than in the
browser tab. Its resolution is one pixel, which is the most a measurement off
a thresholded anti-aliased edge can honestly claim. That is ample at 180px
and 32px, and not enough at 16px, where a 12% error rounds to the same pixels
as a correct one; the ICO's *directory* is checked exactly instead.

## Environment

| Variable | Notes |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | The **anon** key (or `sb_publishable_…`). Public by design; RLS constrains it. |
| `NEXT_PUBLIC_COMPETITION_START` | *Optional.* Shown in the board header. |
| `NEXT_PUBLIC_COMPETITION_END` | *Optional.* Same. |

The two competition dates must match the backend's `COMPETITION_START` /
`COMPETITION_END`, which `venue_common.py` reads from the environment and
`metrics.py` scores against. Set them in one place and not the other and the
board advertises a window the scorer is not using. Full timestamps are
accepted and its leading date is used — `2026-10-26T23:59:59Z` renders as
`2026-10-26` — but set each to the date you want *displayed*, which is not
always the backend value verbatim: an end of `2026-10-27T00:00:00Z` scores
through the 26th and would display as the 27th.

Unset falls back to the dates the page shipped with; an unparseable value is
ignored and logged. Note that these are inlined at **build** time, so changing
them on Vercel needs a redeploy — and that the backend's own defaults differ
(`COMPETITION_START` defaults to 2026-01-01 and `COMPETITION_END` to
open-ended), so leaving both sides unset does *not* make them agree.

Never set `SUPABASE_KEY` (service_role) or `FERNET_KEY` here. They decrypt
every participant's exchange credentials and belong only to the backend.

`NEXT_PUBLIC_*` values are inlined **at build time**, not read at runtime. Set
them in Vercel before the build that should carry them; changing one afterwards
needs a redeploy, not a restart.

Without them the leaderboard renders sample rows and says so in a banner — see
`getLeaderboard()` in `lib/leaderboard.ts` — and `/register` renders but refuses
to submit, for the same reason: a page that silently does nothing is worse than
one that says what is missing.

## What the anon key reaches

`migrations/schema.sql` grants anon `select` on the `leaderboard` view and
nothing else. That is the whole surface the browser needs for reading.

`/register` does not write with the anon key at all. It posts to the
`register` Edge Function (`supabase/functions/register/index.ts`), which holds
the service role inside Supabase and rate-limits by IP — the browser never
touches `pending_signups` directly, which is what makes the limit meaningful.

If the board shows sample data under a banner saying **permission denied for
view leaderboard**, that grant is missing — the database predates
`migrations/002_public_leaderboard_and_participant_privacy.sql`, or a re-run
of an older `schema.sql` revoked it. Run 002; it is idempotent.

Treat that banner as a bug, not as setup. `tests/test_schema_grants.py` exists
to stop the grant being lost again, because the symptom is a public ranking
full of invented traders rather than an error anyone would notice.

## Where this is deployed

**https://app.set.trade** — Vercel, Root Directory `web/`.

The subdomain is not a style choice. `set.trade` and `www.set.trade` serve a
separate Framer site, so the apex is not available: pointing it here would take
that site down. Framer keeps the apex, this keeps `app.`, and neither touches
the other's DNS records.

To stand up another environment:

1. Import the repository into Vercel and set **Root Directory** to `web/`.
2. Add both `NEXT_PUBLIC_*` variables under Settings → Environment Variables,
   for every environment you build — they are inlined at build time, so a
   variable added afterwards needs a redeploy, not a restart.
3. Settings → Domains, add the hostname, and create the `CNAME` Vercel gives
   you at your DNS provider.

If that provider is Cloudflare, set the record to **DNS only (grey cloud)**.
Proxying it stacks Cloudflare in front of Vercel's own CDN and interferes with
certificate issuance.

`/register` must be served over HTTPS, which Vercel does automatically. The page
disables itself outside a secure context, because it asks people to type
exchange credentials.
