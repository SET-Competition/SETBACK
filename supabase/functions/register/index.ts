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
 * Vercel variable, not in the bundle, not in this repository. It identifies
 * the caller, asks the database whether that caller has submitted too often,
 * and inserts only if the answer is yes.
 *
 * migrations/004 then revokes anon's insert. Deploy this and point the site
 * at it BEFORE running 004 - until then the old path is still open and this
 * is a door beside an open doorway.
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

/**
 * Config is validated, not merely defaulted.
 *
 * `Number(Deno.env.get(x) ?? 5)` looks equivalent and is not: `??` catches
 * only UNSET, so a typo'd value becomes NaN, JSON.stringify writes NaN as
 * null, PostgREST passes an explicit null as SQL NULL, and `recent >= NULL`
 * is NULL - so the branch is not taken and THE LIMIT IS SILENTLY OFF. The
 * mirror case is as bad: "" becomes 0, and 0 >= 0 blocks everyone forever.
 * A misconfigured limiter must fall back to a working one and say so.
 */
export function positiveInt(name: string, fallback: number): number {
  const raw = Deno.env.get(name);
  if (raw === undefined || raw.trim() === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    console.error(`${name}=${JSON.stringify(raw)} is not a positive integer; using ${fallback}`);
    return fallback;
  }
  return value;
}

/** Submissions allowed per caller per window. Tunable without a redeploy. */
const MAX_ATTEMPTS = positiveInt("SIGNUP_MAX_ATTEMPTS", 20);
/**
 * Seconds, not an interval string.
 *
 * An interval taken from the environment is a string Postgres parses, and the
 * failure modes are invisible: malformed means every request 503s, and
 * `-1 hour` moves the window into the future so the count is always zero and
 * the limit is off with nothing to see.
 */
const WINDOW_SECONDS = positiveInt("SIGNUP_WINDOW_SECONDS", 3600);

/** A submission covers at most the three venues the form offers. */
const MAX_ROWS = 3;
/** Bounded before parsing: 4000-char secrets, three of them, plus slack. */
const MAX_BODY_BYTES = 32_768;

/**
 * Must stay in step with VENUES in web/lib/signup.js.
 * tests/test_signup_form.py asserts they agree.
 */
const VENUES = new Set(["coinbase:spot", "coinbase:perp", "lighter:perp"]);

/**
 * CORS must name every header the form sends, or no browser ever gets here.
 *
 * `register-form.tsx` sends apikey and Authorization alongside Content-Type.
 * Those are not safelisted, so the browser preflights, and a reply that omits
 * them blocks the POST before it is sent - while curl, which ignores CORS
 * entirely, keeps working. That combination makes it look like a network
 * fault to the participant and like a healthy endpoint to whoever is testing.
 * `*` would not help: it is not honoured for Authorization.
 *
 * The origin is `*` deliberately. It is not a control here and pretending
 * otherwise would be worse than leaving it open: this endpoint is public by
 * necessity, and CORS governs what a BROWSER hands to a script on another
 * origin, not what a script anywhere can send. The rate limit is the control.
 */
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Max-Age": "86400",
};

function reply(status: number, body: Record<string, unknown> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

/** A bare address: no netmask, no port, no brackets, no zone. */
export function isAddress(value: string): boolean {
  // A netmask would widen the rate-limit key - `1.2.3.4/24` is a different
  // inet from `1.2.3.4/32`, so varying it buys 33 independent windows.
  if (value.includes("/") || value.includes("%") || value.includes("[")) return false;
  if (/^(\d{1,3}\.){3}\d{1,3}$/.test(value)) {
    return value.split(".").every((octet) => Number(octet) <= 255);
  }
  // At least TWO colons. One colon and dots is `1.2.3.4:5678` - an address
  // with a port, which would otherwise pass this branch and then be rejected
  // by Postgres, turning a caller behind a port-appending proxy into a 503.
  // Real IPv6 always has two or more, IPv4-mapped forms included.
  const colons = value.length - value.replaceAll(":", "").length;
  return colons >= 2 && value.length <= 45 && /^[0-9a-f:.]+$/i.test(value);
}

/**
 * Who is calling, as far as anything here can tell.
 *
 * cf-connecting-ip is written by the platform edge and cannot be set by the
 * caller, so it is preferred. Failing that, the LAST valid element of
 * x-forwarded-for: the platform APPENDS the peer it saw, so the last entry is
 * the one it wrote and the first is whatever the caller sent.
 *
 * An earlier version read `split(",")[0]` and argued that forging it "evades
 * the limit without affecting anyone else". That was wrong twice over. Five
 * requests carrying a victim's address exhaust that address's window, which
 * is precisely the permanent ban for everyone behind a NAT that this design
 * avoids elsewhere by not recording blocked attempts; and rotating the forged
 * value gives unlimited rows in the table this exists to bound.
 */
export function clientAddress(headers: Headers): string | null {
  const direct = (headers.get("cf-connecting-ip") ?? "").trim();
  if (isAddress(direct)) return direct;

  const chain = (headers.get("x-forwarded-for") ?? "").split(",");
  for (let i = chain.length - 1; i >= 0; i--) {
    const candidate = chain[i].trim();
    if (isAddress(candidate)) return candidate;
  }
  return null;
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

  // Control characters are rejected here rather than left to Postgres.
  //
  // A NUL survives trim() and every length check below, and then text cannot
  // store it - so the insert fails, and the failure is the one path in this
  // function that handles a PostgREST error body containing the credential.
  // Refusing it early keeps that path unreachable by submission.
  for (const field of [display_name, email, exchange, account_type, api_key, api_secret]) {
    // deno-lint-ignore no-control-regex
    if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(field)) {
      return "entries for unusual characters";
    }
  }

  // Bounds mirror pending_signups_size_check in migrations/schema.sql. They
  // are re-checked rather than trusted because the browser is not the only
  // thing that can call this.
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

async function postgrest(path: string, body: unknown, prefer?: string): Promise<Response> {
  const headers: Record<string, string> = {
    apikey: SERVICE_ROLE,
    Authorization: `Bearer ${SERVICE_ROLE}`,
    "Content-Type": "application/json",
  };
  if (prefer) headers.Prefer = prefer;
  return await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

/**
 * What went wrong, without what it went wrong ON.
 *
 * PostgREST returns PostgreSQL's DETAIL, and for a constraint violation on
 * this table that is `Failing row contains (...)` with api_key in it. Logging
 * the body verbatim moves the plaintext from a row that gets wiped on import
 * into a log with no wipe, no constraint, a wider audience and any configured
 * drain. `code` and `message` are what an operator acts on; `details` and
 * `hint` are what carry the credential.
 */
async function safeDetail(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const code = typeof body?.code === "string" ? body.code : "?";
    const message = typeof body?.message === "string" ? body.message.slice(0, 200) : "?";
    return `${code}: ${message}`;
  } catch {
    return "(unparseable body, withheld)";
  }
}

export async function handle(request: Request): Promise<Response> {
  if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (request.method !== "POST") return reply(405, { error: "POST only" });

  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_BODY_BYTES) return reply(413, { error: "Submission too large" });

  const raw = await request.text();
  // Bytes, not string length: `raw.length` counts UTF-16 code units, so a body
  // of astral characters is more than twice the size this is meant to bound.
  if (new TextEncoder().encode(raw).length > MAX_BODY_BYTES) {
    return reply(413, { error: "Submission too large" });
  }

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
    if (typeof candidate !== "object" || candidate === null || Array.isArray(candidate)) {
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
  // Keyed with JSON rather than a separator character, because any separator
  // can appear in a submitted string and make two different identities read
  // as one.
  const identities = new Set(rows.map((r) => JSON.stringify([r.display_name, r.email])));
  if (identities.size > 1) {
    return reply(400, { error: "Every venue must carry the same name and email" });
  }
  if (new Set(rows.map((r) => `${r.exchange}:${r.account_type}`)).size !== rows.length) {
    return reply(400, { error: "Each venue may be submitted once" });
  }

  const address = clientAddress(request.headers);
  if (!address) {
    // Fail CLOSED. An unidentifiable caller is the shape of the abuse this
    // exists to stop, and refusing one legitimate submission is cheaper than
    // a limit that is bypassed by deleting a header. 400 rather than 503:
    // this is something about the request, and saying "temporarily
    // unavailable" would send the operator looking for an outage.
    return reply(400, { error: "Could not identify the request origin" });
  }

  const gate = await postgrest("rpc/record_signup_attempt", {
    p_ip: address,
    p_max_attempts: MAX_ATTEMPTS,
    p_window_seconds: WINDOW_SECONDS,
  });

  if (!gate.ok) {
    console.error("rate limiter failed", gate.status, await safeDetail(gate));
    return reply(503, { error: "Registration is temporarily unavailable" });
  }

  // `=== false` rather than `!== true`, so an unexpected body is an OUTAGE
  // and not a rate limit. PostgREST returns a bare `true` for a scalar
  // function today; if that framing ever changes, `!== true` would turn the
  // endpoint into a permanent, convincing-looking "too many submissions" wall
  // that nobody would think to debug.
  const allowed = await gate.json();
  if (allowed === false) {
    return reply(429, {
      error: "Too many submissions from this network. Please try again later.",
    });
  }
  if (allowed !== true) {
    console.error("rate limiter returned an unexpected body", JSON.stringify(allowed));
    return reply(503, { error: "Registration is temporarily unavailable" });
  }

  const inserted = await postgrest("pending_signups", rows, "return=minimal");

  if (!inserted.ok) {
    console.error("insert failed", inserted.status, await safeDetail(inserted));
    return reply(502, { error: "Could not record the submission" });
  }

  return reply(201, { registered: rows.length });
}

// Supabase runs this file as the entrypoint, so the server starts in
// production. Under `deno test` it does not, which is what lets the tests
// import `handle` and call it directly instead of binding a port.
if (import.meta.main) Deno.serve(handle);
