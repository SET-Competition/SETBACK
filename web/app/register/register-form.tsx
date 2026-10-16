"use client";

import { useEffect, useState } from "react";
import { buildRows, venueLabel } from "@/lib/signup";

/**
 * Participant signup.
 *
 * ONE submission registers every venue a participant trades. The page POSTs an
 * ARRAY of rows to pending_signups - one per venue filled in - which PostgREST
 * accepts in a single request.
 *
 * That shape is deliberate. A participant on Coinbase spot and Lighter would
 * otherwise have to submit twice, and the likely outcome of asking that is
 * people registering one venue and quietly missing half their equity from the
 * leaderboard. Keeping one ROW per venue (rather than one wide row) means each
 * credential is verified independently: a bad Coinbase key does not cost
 * someone their valid Lighter registration.
 *
 * sign_ups.py then verifies each row against the exchange, re-encrypts it
 * under FERNET_KEY into participant_api_keys, and wipes the plaintext.
 *
 * Credentials are in PLAINTEXT in pending_signups until the importer runs. Run
 * sign_ups.py often; that window is the whole exposure. See the warning at the
 * top of section 8 in migrations/schema.sql.
 *
 * The anon key is compiled into the bundle, which is fine and is now fine for
 * a stronger reason than it used to be: anon can SELECT the leaderboard view
 * and nothing else at all. It cannot write here either. Submissions go to the
 * register Edge Function, which holds the service role inside Supabase.
 */

// Rendered on the client, but the submission no longer goes to the database
// from here - it goes to an Edge Function (see the handler below).
//
// An earlier version of this comment said there was no server step to add
// "because there is nothing a server could hide". That was true and beside
// the point: the reason to add one was never secrecy, it was having somewhere
// to count requests. A write that goes straight from a visitor's browser to
// PostgREST cannot be rate-limited by anything we control.
//
// Both must be referenced as a full `process.env.NAME` expression: Next inlines
// the value by literal substitution at build time, so destructuring or indexing
// process.env would leave them undefined in the browser.
const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL;
const SUPABASE_ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

type Status =
  | { kind: "idle" }
  | { kind: "sending"; message: string }
  | { kind: "error"; message: string }
  | { kind: "done"; message: string };

const NOTE = "rounded-lg border px-4 py-3 text-sm leading-relaxed";
const FIELD =
  "w-full rounded-md border border-white/12 bg-white/[0.03] px-3 py-2 text-sm " +
  "text-white placeholder:text-white/20 outline-none transition-colors " +
  "focus:border-white/25 focus:bg-white/[0.05] disabled:opacity-50";

/**
 * Props are listed explicitly rather than extending InputHTMLAttributes.
 *
 * The earlier version spread an input-shaped type onto the <input> branch and
 * hand-copied two attributes onto the <textarea> one, so `<Field textarea
 * required />` type-checked and compiled while rendering a textarea with no
 * `required` - a browser-level guard vanishing with no error, on a form whose
 * job is validating credentials. Naming the props means `rest` below is
 * exactly the set BOTH elements accept, and neither branch can silently drop
 * one.
 */
type FieldProps = {
  name: string;
  label: string;
  hint?: React.ReactNode;
  textarea?: boolean;
  required?: boolean;
  maxLength?: number;
  disabled?: boolean;
  className?: string;
  /** Input only - a textarea has no `type`, so it is not forwarded there. */
  type?: React.HTMLInputTypeAttribute;
};

function Field({
  name,
  label,
  hint,
  textarea = false,
  className = "",
  type,
  ...rest
}: FieldProps) {
  // required | maxLength | disabled - valid on <input> and <textarea> alike.
  const shared = { name, ...rest };

  return (
    <label className="mb-5 block last:mb-0">
      <span className="mb-1 block text-sm font-medium text-white/90">{label}</span>
      {hint && <span className="mb-1.5 block text-xs text-white/40">{hint}</span>}
      {textarea ? (
        <textarea
          {...shared}
          className={`${FIELD} min-h-[5.5rem] resize-y font-mono text-xs ${className}`}
        />
      ) : (
        <input {...shared} type={type} className={`${FIELD} ${className}`} />
      )}
    </label>
  );
}

function Section({
  title,
  optional = false,
  lead,
  children,
}: {
  title: string;
  optional?: boolean;
  lead?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <fieldset className="mb-5 rounded-lg border border-white/10 bg-white/[0.02] px-4 py-4 sm:px-5">
      <legend className="px-1.5 text-[11px] font-semibold uppercase tracking-wider text-white/45">
        {title}
        {optional && (
          <span className="font-normal normal-case tracking-normal text-white/25">
            {" "}
            — optional
          </span>
        )}
      </legend>
      {lead && <p className="mb-4 mt-1 text-xs leading-relaxed text-white/40">{lead}</p>}
      <div className="mt-2">{children}</div>
    </fieldset>
  );
}

export default function RegisterForm() {
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  // Typing an API key into a page served over http:// sends it in the clear.
  // Checked after mount, because `window` does not exist while this renders on
  // the server.
  const [insecure, setInsecure] = useState(false);
  useEffect(() => setInsecure(!window.isSecureContext), []);

  const unconfigured = !SUPABASE_URL || !SUPABASE_ANON_KEY;
  const locked = insecure || unconfigured || status.kind === "sending";
  const submitted = status.kind === "done";

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (unconfigured) return;

    const form = event.currentTarget;
    const values = Object.fromEntries(
      Array.from(new FormData(form).entries(), ([k, v]) => [k, String(v)]),
    );
    values.display_name = (values.display_name || "").trim();
    values.email = (values.email || "").trim();

    const { rows, problem } = buildRows(values);
    if (problem) {
      setStatus({ kind: "error", message: problem });
      return;
    }

    setStatus({
      kind: "sending",
      message: `Submitting ${rows.length} credential${rows.length > 1 ? "s" : ""}…`,
    });

    try {
      // Posted to the `register` Edge Function, NOT to PostgREST.
      //
      // The browser used to insert into pending_signups directly, which is
      // why nothing rate-limited this form: the write never touched the
      // website, so there was no edge to put a limit at, and the anon key
      // needed to do it is readable in this bundle by design. The function
      // runs inside Supabase with the service role, sees the caller's
      // address, and is the only thing that can write to that table once
      // migrations/004 has run. See supabase/functions/register/index.ts.
      //
      // Still one request carrying one row per venue, and still a single
      // insert statement on the far side - so either every venue is
      // registered or none is, and nobody ends up half-submitted.
      const response = await fetch(`${SUPABASE_URL}/functions/v1/register`, {
        method: "POST",
        headers: {
          // The function itself requires no auth (verify_jwt = false), but
          // Supabase's gateway routes on this header.
          apikey: SUPABASE_ANON_KEY,
          Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(rows),
      });

      if (!response.ok) {
        // Unlike the PostgREST version this replaced, the body here is SAFE
        // to show, and that is a deliberate property of the function rather
        // than an assumption about it.
        //
        // PostgREST returned PostgreSQL's DETAIL, and for a check-constraint
        // violation that is `Failing row contains (...)` - the entire row,
        // api_key and api_secret included. Rendering it printed the
        // participant's private key on screen, where it lands in any
        // screenshot they send the organiser.
        //
        // The function never passes a PostgREST body back. It logs those and
        // returns a message it composed itself, so the text below is ours.
        // It is still read defensively: a string, length-capped, and only
        // when the shape is what we expect - because the alternative is
        // trusting a response to decide what to paint on a page that has
        // credentials on it.
        let detail = "";
        try {
          const body = await response.json();
          if (body && typeof body.error === "string") {
            detail = ` ${body.error.slice(0, 200)}`;
          }
        } catch {
          // No JSON body - a gateway error, or the function never ran.
        }

        setStatus({
          kind: "error",
          message:
            response.status === 429
              ? `Too many submissions from your network.${detail} ` +
                "Wait an hour and try again, or contact the organiser."
              : `Could not submit — the server rejected the request ` +
                `(HTTP ${response.status}).${detail} Check your entries and ` +
                `try again, or contact the organiser with that code.`,
        });
        return;
      }

      setStatus({ kind: "done", message: rows.map(venueLabel).join(", ") });
    } catch (error) {
      // Only a REJECTED fetch reaches here - a transport failure: offline,
      // DNS, TLS, CORS. Those messages are generated by the browser and carry
      // no response body, so showing them is safe and genuinely diagnostic.
      setStatus({
        kind: "error",
        message:
          `Could not submit: ${error instanceof Error ? error.message : String(error)}. ` +
          "Please try again, or contact the organiser.",
      });
    }
  }

  return (
    <main className="mx-auto w-full max-w-2xl px-4 py-10 sm:px-6 sm:py-16">
      <header className="mb-7">
        <h1 className="text-2xl font-semibold tracking-tight text-white">Register</h1>
        <p className="mt-2 text-sm leading-relaxed text-white/45">
          Submit read-only exchange credentials to be ranked on the leaderboard.
          You only need to do this once, even if you trade several venues.
        </p>
      </header>

      <div className={`${NOTE} mb-5 border-amber-500/25 bg-amber-500/[0.07] text-amber-200/85`}>
        <strong className="font-semibold">Read-only keys only.</strong> Credentials
        that can trade or withdraw are rejected automatically — this competition
        never needs the ability to move your funds.
        <ul className="mt-2 list-disc space-y-1 pl-5 text-amber-200/70">
          <li>
            <strong className="font-medium text-amber-200/90">Coinbase</strong> —
            create the key with <em>View</em> permission only. Leave <em>Trade</em>{" "}
            and <em>Transfer</em> unchecked.
          </li>
          <li>
            <strong className="font-medium text-amber-200/90">Lighter</strong> —
            submit a read-only token, which starts with{" "}
            <code className="font-mono">ro:</code>. Never submit your wallet private
            key; it cannot be revoked and controls every asset in the wallet.
          </li>
        </ul>
      </div>

      {insecure && (
        <div className={`${NOTE} mb-5 border-rose-500/30 bg-rose-500/[0.08] text-rose-200/90`}>
          This page is not being served over HTTPS, so anything you type here would
          be sent unencrypted. Do not enter credentials.
        </div>
      )}

      {unconfigured && (
        <div className={`${NOTE} mb-5 border-rose-500/30 bg-rose-500/[0.08] text-rose-200/90`}>
          <strong className="font-semibold">Not configured yet.</strong> This
          deployment has no Supabase credentials, so the form cannot accept
          submissions. The organiser needs to set{" "}
          <code className="font-mono text-xs">NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
          <code className="font-mono text-xs">NEXT_PUBLIC_SUPABASE_ANON_KEY</code>.
        </div>
      )}

      {submitted ? (
        <div className={`${NOTE} border-emerald-500/25 bg-emerald-500/[0.07] text-emerald-200/85`}>
          <strong className="font-semibold">Submitted — {status.message}.</strong> Your
          credentials will be checked against each exchange shortly. If one turns out
          not to be read-only you will be emailed about that venue, and can submit a
          replacement; the others are unaffected.
        </div>
      ) : (
        <form onSubmit={onSubmit} autoComplete="off">
          <Section title="You">
            <Field
              name="display_name"
              label="Display name"
              hint="Shown on the public leaderboard."
              required
              maxLength={40}
              disabled={locked}
            />
            <Field
              name="email"
              label="Email"
              hint="Used to contact you."
              type="email"
              required
              maxLength={320}
              disabled={locked}
            />
          </Section>

          <p className="mb-5 text-xs leading-relaxed text-white/40">
            Fill in every venue you trade on — you can register all three at once.
            Leave a section blank if you do not trade there.
          </p>

          <Section title="Coinbase spot" optional>
            <Field
              name="coinbase_spot_key"
              label="API key name"
              hint={
                <>
                  Looks like <code className="font-mono">organizations/…/apiKeys/…</code>
                </>
              }
              className="font-mono text-xs"
              maxLength={2000}
              disabled={locked}
            />
            <Field
              name="coinbase_spot_secret"
              label="API private key"
              hint="The whole block, including the BEGIN and END lines."
              textarea
              maxLength={4000}
              disabled={locked}
            />
          </Section>

          <Section
            title="Coinbase perpetuals"
            optional
            lead={
              <>
                Coinbase issues a <em>separate</em> key for perpetuals. If you trade
                both, you need both keys.
              </>
            }
          >
            <Field
              name="coinbase_perp_key"
              label="API key name"
              className="font-mono text-xs"
              maxLength={2000}
              disabled={locked}
            />
            <Field
              name="coinbase_perp_secret"
              label="API private key"
              textarea
              maxLength={4000}
              disabled={locked}
            />
          </Section>

          <Section title="Lighter" optional>
            <Field
              name="lighter_token"
              label="Read-only token"
              hint={
                <>
                  Starts with <code className="font-mono">ro:</code>. Not your wallet
                  private key. Lighter is perpetuals only, so there is nothing else to
                  enter.
                </>
              }
              textarea
              maxLength={2000}
              disabled={locked}
            />
          </Section>

          <button
            type="submit"
            disabled={locked}
            className="w-full rounded-md bg-white px-4 py-2.5 text-sm font-semibold text-[#08090b] transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {status.kind === "sending" ? "Submitting…" : "Submit"}
          </button>

          {status.kind === "error" && (
            <div className={`${NOTE} mt-5 border-rose-500/30 bg-rose-500/[0.08] text-rose-200/90`}>
              {status.message}
            </div>
          )}
          {status.kind === "sending" && (
            <div className={`${NOTE} mt-5 border-white/12 bg-white/[0.03] text-white/60`}>
              {status.message}
            </div>
          )}
        </form>
      )}

      <footer className="mt-8 text-xs leading-relaxed text-white/25">
        Your credentials are checked against the exchange and stored encrypted. Only
        read-only keys are accepted.
      </footer>
    </main>
  );
}
