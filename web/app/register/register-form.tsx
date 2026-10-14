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
 * The anon key is compiled into the bundle, which is fine: RLS grants anon
 * INSERT on this one table and nothing else, so the key can add rows and can
 * neither read one back nor reach any other table.
 */

// Rendered on the client, where fetch() talks to Supabase directly. There is no
// server step to add, because there is nothing a server could hide - the anon
// key is public by design.
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

function Field({
  name,
  label,
  hint,
  textarea = false,
  ...rest
}: {
  name: string;
  label: string;
  hint?: React.ReactNode;
  textarea?: boolean;
} & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="mb-5 block last:mb-0">
      <span className="mb-1 block text-sm font-medium text-white/90">{label}</span>
      {hint && <span className="mb-1.5 block text-xs text-white/40">{hint}</span>}
      {textarea ? (
        <textarea
          name={name}
          maxLength={rest.maxLength}
          disabled={rest.disabled}
          className={`${FIELD} min-h-[5.5rem] resize-y font-mono text-xs`}
        />
      ) : (
        <input name={name} {...rest} className={`${FIELD} ${rest.className ?? ""}`} />
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
      // One request, one row per venue. PostgREST inserts a JSON array as
      // multiple rows, and it is a single statement - so either every venue is
      // registered or none is, and a participant never ends up half-submitted.
      const response = await fetch(`${SUPABASE_URL}/rest/v1/pending_signups`, {
        method: "POST",
        headers: {
          apikey: SUPABASE_ANON_KEY,
          Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
          "Content-Type": "application/json",
          Prefer: "return=minimal",
        },
        body: JSON.stringify(rows),
      });

      if (!response.ok) {
        throw new Error(`${response.status} ${await response.text()}`);
      }

      setStatus({ kind: "done", message: rows.map(venueLabel).join(", ") });
    } catch (error) {
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
