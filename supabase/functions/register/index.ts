/**
 * The only way into pending_signups.
 *
 * The form used to POST from the browser straight to PostgREST. That is why
 * nothing rate-limited it: the write never touched the website, so there was
 * no edge to put a limit at. Anyone could read the anon key out of the JS
 * bundle - public by design - and POST to /rest/v1/pending_signups as fast as
 * they liked.
 *
 * This function is the replacement entrance. It runs inside the Supabase
 * project with the service role, which never leaves the platform: not in a
 * Vercel variable, not in the bundle, not in this repository. It sees the
 * caller's address, asks the database whether that address has submitted too
 * often, and inserts only if the answer is yes.
 *
 * migrations/004 then revokes anon's insert. Until that is run, the old path
 * is still open and this is a door beside an doorway - deploy this first,
 * point the site at it, and only then run 004.
 *
 * NO DEPENDENCIES ON PURPOSE. This is the one piece of the system that
 * handles plaintext exchange credentials in transit, and PostgREST is a REST
 * API that `fetch` can speak. A client library here would be a supply chain
 * for nothing.
 */

// Injected by the platform. SERVICE_ROLE bypasses RLS and every grant, which
// is the entire reason this code is here and not in the browser.
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

/** Submissions allowed per address per window, overridable without a redeploy. */
const MAX_ATTEMPTS = Number(Deno.env.get("SIGNUP_MAX_ATTEMPTS") ?? 5);
const WINDOW = Deno.env.get("SIGNUP_WINDOW") ?? "01:00:00";

/** A submission covers at most the three venues the form offers. */
const MAX_ROWS = 3;
/** Bounded before parsing: 4000-char secrets, three of them, plus slack. */
const MAX_BODY_BYTES = 32_768;

const VENUES = new Set(["coinbase:spot", "coinbase:perp", "lighter:perp"]);

/**
 * CORS is `*` deliberately.
 *
 * It is not a control here and pretending otherwise would be worse than
 * leaving it open: this endpoint is public by necessity, and CORS governs
 * what a BROWSER will hand to a script on another origin, not what a script
 * anywhere can send. curl ignores it. The rate limit is the control.
 */
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Max-Age": "86400",
};

function reply(status: number, body: Record<string, unknown> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

/** The columns a submitter is allowed to set. Everything else is dropped. */
export function clean(raw: Record<string, unknown>): Record<string, string | null> | string {
  const text = (value: unknown) => (typeof value === "string" ? value.trim() : "");

  const display_name = text(raw.display_name);
  const email = text(raw.email);
  const exchange = text(raw.exchange).toLowerCase();
  const account_type = text(raw.account_type).toLowerCase();
  const api_key = text(raw.api_key);
  const api_secret = text(raw.api_secret);

  // Bounds mirror pending_signups_size_check in migrations/schema.sql. They
  // are re-checked rather than trusted because the browser is not the only
  // thing that can call this, and a constraint violation here would come back
  // from PostgREST carrying the whole failing row - credentials included.
  if (display_name.length < 1 || display_name.length > 40) return "display name";
  if (email.length < 3 || email.length > 320 || !email.includes("@")) return "email";
  if (!VENUES.has(`${exchange}:${account_type}`)) return "venue";
  if (api_key.length < 1 || api_key.length > 2000) return "API key";
  if (api_secret.length > 4000) return "API secret";

  // The backend refuses Lighter wallet keys outright; catching it here means
  // the participant is told now rather than after the importer runs.
  if (exchange === "lighter" && !api_key.startsWith("ro:")) {
    return "Lighter read-only token (it must start with ro:)";
  }

  // status, attempts, last_error and processed_at are deliberately absent.
  // They are the importer's to write, and the old direct-to-PostgREST path
  // let a submitter set them.
  return {
    display_name,
    email,
    exchange,
    account_type,
    api_key,
    api_secret: api_secret === "" ? null : api_secret,
  };
}

async function postgrest(path: string, body: unknown, prefer: string): Promise<Response> {
  return await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    method: "POST",
    headers: {
      apikey: SERVICE_ROLE,
      Authorization: `Bearer ${SERVICE_ROLE}`,
      "Content-Type": "application/json",
      Prefer: prefer,
    },
    body: JSON.stringify(body),
  });
}

export async function handle(request: Request): Promise<Response> {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (request.method !== "POST") return reply(405, { error: "POST only" });

  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_BODY_BYTES) return reply(413, { error: "Submission too large" });

  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) return reply(413, { error: "Submission too large" });

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return reply(400, { error: "Malformed submission" });
  }

  if (!Array.isArray(parsed) || parsed.length === 0 || parsed.length > MAX_ROWS) {
    return reply(400, { error: "Submit between one and three venues" });
  }

  const rows: Record<string, string | null>[] = [];
  for (const candidate of parsed) {
    if (typeof candidate !== "object" || candidate === null) {
      return reply(400, { error: "Malformed submission" });
    }
    const result = clean(candidate as Record<string, unknown>);
    if (typeof result === "string") {
      return reply(400, { error: `Check your ${result}.` });
    }
    rows.push(result);
  }

  // One submission is one participant. Rows disagreeing about who they belong
  // to would register several people from a single rate-limited request.
  if (new Set(rows.map((r) => `${r.display_name}\u0000${r.email}`)).size > 1) {
    return reply(400, { error: "Every venue must carry the same name and email" });
  }
  if (new Set(rows.map((r) => `${r.exchange}:${r.account_type}`)).size !== rows.length) {
    return reply(400, { error: "Each venue may be submitted once" });
  }

  // x-forwarded-for is a list, client first. It is attacker-controlled, but
  // the platform appends the real peer and the database only ever compares
  // this value to itself - a forged address evades the limit without
  // affecting anyone else's.
  const forwarded = request.headers.get("x-forwarded-for") ?? "";
  const ip = forwarded.split(",")[0].trim();
  if (!ip) {
    // Fail CLOSED. An unidentifiable caller is exactly the shape of the abuse
    // this exists to stop, and signups are rare enough that refusing one is
    // cheaper than leaving the limit bypassable by stripping a header.
    return reply(400, { error: "Could not identify the request origin" });
  }

  const gate = await postgrest("rpc/record_signup_attempt", {
    p_ip: ip,
    p_max_attempts: MAX_ATTEMPTS,
    p_window: WINDOW,
  }, "return=representation");

  if (!gate.ok) {
    console.error("rate limiter failed", gate.status, await gate.text());
    return reply(503, { error: "Registration is temporarily unavailable" });
  }
  if ((await gate.json()) !== true) {
    return reply(429, {
      error: "Too many submissions from this network. Try again in an hour.",
    });
  }

  const inserted = await postgrest("pending_signups", rows, "return=minimal");

  if (!inserted.ok) {
    // The body is logged and never returned. PostgREST hands back
    // PostgreSQL's DETAIL, and for a check-constraint violation that is
    // `Failing row contains (...)` - the entire row, api_key included.
    // Bounding how long that plaintext is visible is the property this whole
    // design turns on, and it would be absurd to undo that here.
    console.error("insert failed", inserted.status, await inserted.text());
    return reply(502, { error: "Could not record the submission" });
  }

  return reply(201, { registered: rows.length });
}

// Supabase runs this file as the entrypoint, so the server starts in
// production. Under `deno test` it does not, which is what lets the tests
// import `handle` and call it directly instead of binding a port.
if (import.meta.main) Deno.serve(handle);
