import type { Metadata } from "next";

import { getLeaderboard, type LeaderboardRow } from "@/lib/leaderboard";

export const metadata: Metadata = {
  title: "SET — Leaderboard",
  description:
    "Crypto trading competition ranked on a flow-adjusted Sharpe ratio.",
};

// The board only changes when the sync runs, and the sync is a Railway cron
// service whose schedule lives in Railway's UI, not here - so any interval
// written down in this repository is a claim the code cannot keep true. An
// earlier version of this comment said "hourly" and was wrong within a week,
// which is why this one names no number.
//
// Five minutes is well inside any plausible schedule. It is not chosen to
// match the cron: it bounds how long a MANUAL run, or a change to the
// schedule, stays invisible on the board, which is the case that actually
// matters. Between syncs it costs a query against a view with one row per
// participant.
export const revalidate = 300;

/**
 * The competition window, as shown in the header.
 *
 * Read from the environment because the BACKEND already is: `venue_common.py`
 * takes COMPETITION_START/COMPETITION_END from env, and `metrics.py` scores
 * against those. With the dates hardcoded here, extending the run on Railway
 * left the board advertising the old close date while the scorer used the new
 * one - and the board is what participants believe.
 *
 * TWO THINGS THIS DOES NOT DO, both of which have bitten already:
 *
 * 1. It does not make the board self-updating on Vercel. NEXT_PUBLIC_* values
 *    are inlined at BUILD time and this route is prerendered, so editing the
 *    variable changes nothing until a redeploy. The backend picks its values
 *    up on restart; the frontend needs a new build. Changing the window means
 *    changing it in both places AND redeploying this one.
 *
 * 2. It does not make the unset case agree with the backend. These fallbacks
 *    are the dates this page shipped with, matching the root .env.example -
 *    but venue_common.py defaults COMPETITION_START to 2026-01-01 and
 *    COMPETITION_END to null (open-ended). So with nothing configured
 *    anywhere, the board still advertises a window the scorer is not using.
 *    The fallbacks exist so an unconfigured deployment renders something
 *    rather than nothing; agreement comes from setting both sides.
 */
const COMPETITION_START = competitionDate(
  process.env.NEXT_PUBLIC_COMPETITION_START,
  "2026-09-01",
);
const COMPETITION_END = competitionDate(
  process.env.NEXT_PUBLIC_COMPETITION_END,
  "2026-10-26",
);

/**
 * The backend wants the last INSTANT that counts, so its values are full
 * timestamps - `COMPETITION_END=2026-10-26T23:59:59Z`. The header wants a
 * date. Take the leading date and ignore the rest.
 *
 * Sliced rather than parsed through `Date`: `new Date("2026-09-01")` is UTC
 * midnight, and formatting that in a negative-offset timezone renders the
 * day before. A string that is already the date the organiser typed should
 * not acquire a timezone on the way to the screen.
 */
function competitionDate(value: string | undefined, fallback: string): string {
  const raw = (value ?? "").trim();
  const match = /^(\d{4}-\d{2}-\d{2})/.exec(raw);
  if (match) return match[1];

  // A value that was SET but cannot be read is not the same as an unset one,
  // and silently falling back makes them identical - which hides exactly the
  // mistakes that matter: NEXT_PUBLIC_ omitted from the variable name, a
  // locale format like 10/26/2026, a typo'd year. The board would keep
  // showing the old date with nothing anywhere to say why.
  //
  // This runs at build time on Vercel, so the warning lands in the build log.
  if (raw) {
    console.warn(
      `[leaderboard] ignoring unparseable competition date ${JSON.stringify(raw)} ` +
      `- expected YYYY-MM-DD or a timestamp starting with it. Falling back to ${fallback}.`,
    );
  }
  return fallback;
}

/**
 * `reliable` is `periods >= 20`, set in metrics.py - a Python constant, not
 * an environment variable, so this is NOT read from env: that would add a
 * second place to change it and a new way for the two to disagree. Named
 * here so the coupling is visible, and so the number appears once.
 *
 * Pinned to that expression by tests/test_leaderboard_page.py, which is the
 * durable link - a line number here would rot on the next edit to metrics.py.
 */
const RELIABLE_MIN_PERIODS = 20;

function usd(value: number | null): string {
  if (value === null) return "—";
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

function sharpe(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

/**
 * Absolute UTC, never "3 hours ago". A relative string rendered on the server
 * freezes at build time and then quietly lies for the rest of the day.
 */
function stamp(ms: number | null): string {
  if (ms === null) return "never";
  return new Date(ms).toISOString().replace("T", " ").slice(0, 16) + "Z";
}

function Row({ row }: { row: LeaderboardRow }) {
  const provisional = row.reliable === false;

  return (
    <tr className="border-b border-white/5 transition-colors hover:bg-white/[0.03]">
      <td className="py-3 pl-4 pr-2 tabular-nums text-white/40">{row.rank}</td>

      <td className="px-2 py-3">
        <span className="font-medium text-white">{row.display_name}</span>
        {provisional && (
          <span
            className="ml-2 rounded border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-400/90"
            title={
              row.unreliable_reason ??
              `fewer than ${RELIABLE_MIN_PERIODS} return periods`
            }
          >
            provisional
          </span>
        )}
      </td>

      <td
        className={`px-2 py-3 text-right font-mono tabular-nums ${
          row.sharpe === null
            ? "text-white/30"
            : row.sharpe >= 0
              ? "text-emerald-400"
              : "text-rose-400"
        }`}
      >
        {sharpe(row.sharpe)}
      </td>

      <td className="px-2 py-3 text-right font-mono tabular-nums text-white/50">
        {row.periods ?? "—"}
        {row.gap_periods ? (
          <span className="ml-1 text-white/25" title={`${row.gap_periods} gap periods`}>
            /{row.gap_periods}
          </span>
        ) : null}
      </td>

      <td className="px-2 py-3 text-right font-mono tabular-nums text-white/80">
        {usd(row.total_usdc)}
      </td>
      <td className="hidden px-2 py-3 text-right font-mono tabular-nums text-white/45 sm:table-cell">
        {usd(row.spot_usdc)}
      </td>
      <td className="hidden px-2 py-3 text-right font-mono tabular-nums text-white/45 sm:table-cell">
        {usd(row.perp_usdc)}
      </td>
      <td className="hidden py-3 pl-2 pr-4 text-right font-mono text-xs text-white/30 lg:table-cell">
        {stamp(row.as_of)}
      </td>
    </tr>
  );
}

export default async function Page() {
  const { rows, source, notice } = await getLeaderboard();

  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-6 sm:py-16">
      <header className="mb-8">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-2xl font-semibold tracking-tight text-white">
            Leaderboard
          </h1>
          <span className="font-mono text-xs text-white/35">
            {COMPETITION_START} → {COMPETITION_END}
          </span>
        </div>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-white/45">
          Ranked on a flow-adjusted Sharpe ratio: deposits and withdrawals are
          divided out, so this measures trading rather than deposit size.
          Entries with fewer than {RELIABLE_MIN_PERIODS} return periods are
          marked{" "}
          <span className="text-amber-400/80">provisional</span> and sort below
          established ones.
        </p>
      </header>

      {notice && (
        <div className="mb-6 rounded-md border border-amber-500/25 bg-amber-500/[0.07] px-4 py-3 text-sm text-amber-200/85">
          <span className="font-medium">Sample data.</span> {notice}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-white/10 bg-white/[0.02]">
        <table className="w-full min-w-[560px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-white/10 text-left text-[11px] uppercase tracking-wider text-white/35">
              <th className="py-2.5 pl-4 pr-2 font-medium">#</th>
              <th className="px-2 py-2.5 font-medium">Participant</th>
              <th className="px-2 py-2.5 text-right font-medium">Sharpe</th>
              <th className="px-2 py-2.5 text-right font-medium" title="Return periods / gap periods">
                Periods
              </th>
              <th className="px-2 py-2.5 text-right font-medium">Equity</th>
              <th className="hidden px-2 py-2.5 text-right font-medium sm:table-cell">Spot</th>
              <th className="hidden px-2 py-2.5 text-right font-medium sm:table-cell">Perp</th>
              <th className="hidden py-2.5 pl-2 pr-4 text-right font-medium lg:table-cell">
                As of
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-12 text-center text-white/35">
                  Nobody has registered yet.
                </td>
              </tr>
            ) : (
              rows.map((row) => <Row key={row.participant_id} row={row} />)
            )}
          </tbody>
        </table>
      </div>

      <footer className="mt-6 flex flex-wrap items-center justify-between gap-2 font-mono text-xs text-white/25">
        <span>
          {rows.length} participant{rows.length === 1 ? "" : "s"}
        </span>
        <span>{source === "live" ? "live · supabase" : "sample data"}</span>
      </footer>
    </main>
  );
}
