/**
 * The register function is the only way into pending_signups, so it is the
 * only thing standing between an open form and a table of plaintext exchange
 * credentials. These tests cover the three jobs it has: refuse what should
 * not be stored, refuse more than the rate limit allows, and never hand a
 * PostgREST body back to the browser.
 *
 * `handle` is imported rather than served, so nothing binds a port. Outbound
 * fetch is stubbed, so nothing reaches a database.
 *
 *   deno test --allow-env supabase/functions/register/
 */

import { assertEquals, assertStringIncludes } from "@std/assert";

Deno.env.set("SUPABASE_URL", "https://example.supabase.co");
Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-for-tests");

const { clean, handle } = await import("./index.ts");

const VALID = {
  display_name: "tester",
  email: "tester@example.com",
  exchange: "lighter",
  account_type: "perp",
  api_key: "ro:1:all:9999999999:" + "a".repeat(64),
  api_secret: "",
};

/** Record every outbound call and answer them as the database would. */
function stubFetch(options: { allowed?: boolean; insertOk?: boolean } = {}) {
  const { allowed = true, insertOk = true } = options;
  const calls: { url: string; body: unknown }[] = [];
  globalThis.fetch = ((url: string | URL | Request, init?: RequestInit) => {
    const href = String(url);
    calls.push({ url: href, body: JSON.parse(String(init?.body ?? "null")) });
    if (href.includes("rpc/record_signup_attempt")) {
      return Promise.resolve(new Response(JSON.stringify(allowed), { status: 200 }));
    }
    return Promise.resolve(
      insertOk
        ? new Response(null, { status: 201 })
        // What PostgREST really returns on a constraint violation, and the
        // single most dangerous string in this system.
        : new Response(
          JSON.stringify({
            message: 'new row violates check constraint "pending_signups_size_check"',
            details: `Failing row contains (1, tester, tester@example.com, lighter, perp, ${VALID.api_key}, null, pending).`,
          }),
          { status: 400 },
        ),
    );
  }) as typeof fetch;
  return calls;
}

function post(body: unknown, ip: string | null = "198.51.100.7"): Request {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (ip) headers["x-forwarded-for"] = `${ip}, 10.0.0.1`;
  return new Request("https://example.supabase.co/functions/v1/register", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

Deno.test("a valid submission is inserted", async () => {
  const calls = stubFetch();
  const response = await handle(post([VALID]));
  assertEquals(response.status, 201);
  assertEquals(await response.json(), { registered: 1 });

  assertEquals(calls.length, 2);
  assertStringIncludes(calls[0].url, "rpc/record_signup_attempt");
  assertStringIncludes(calls[1].url, "pending_signups");
});

Deno.test("the rate limiter is consulted BEFORE the insert", async () => {
  // Ordering is the whole design: counting after writing would let every
  // blocked request still store a credential.
  const calls = stubFetch({ allowed: false });
  const response = await handle(post([VALID]));
  assertEquals(response.status, 429);
  assertEquals(calls.length, 1, "nothing may be inserted once the limit is hit");
  assertStringIncludes((await response.json()).error, "Too many submissions");
});

Deno.test("a PostgREST error body never reaches the caller", async () => {
  const calls = stubFetch({ insertOk: false });
  const response = await handle(post([VALID]));
  const text = await response.text();

  assertEquals(response.status, 502);
  // The credential appears in PostgREST's DETAIL. If it ever appears here it
  // lands on the participant's screen and in any screenshot they send on.
  assertEquals(text.includes(VALID.api_key), false);
  assertEquals(text.includes("Failing row contains"), false);
  assertEquals(text.includes("check constraint"), false);
  assertEquals(calls.length, 2);
});

Deno.test("a caller with no identifiable address is refused", async () => {
  const calls = stubFetch();
  const response = await handle(post([VALID], null));
  assertEquals(response.status, 400);
  assertEquals(calls.length, 0, "an unidentified caller must not be counted or stored");
});

Deno.test("columns the importer owns cannot be set by the submitter", async () => {
  const calls = stubFetch();
  const response = await handle(
    post([{ ...VALID, status: "imported", attempts: 99, last_error: "x", id: 1 }]),
  );
  assertEquals(response.status, 201);

  const [row] = calls[1].body as Record<string, unknown>[];
  assertEquals(Object.keys(row).sort(), [
    "account_type", "api_key", "api_secret", "display_name", "email", "exchange",
  ]);
});

Deno.test("rejects what the table's constraints would reject", async () => {
  const cases: [string, Record<string, unknown>][] = [
    ["blank display name", { display_name: "   " }],
    ["oversized display name", { display_name: "x".repeat(41) }],
    ["missing @", { email: "nope" }],
    ["oversized email", { email: "a".repeat(315) + "@b.com" }],
    ["unknown exchange", { exchange: "binance" }],
    ["venue combination the form does not offer", { exchange: "lighter", account_type: "spot" }],
    ["blank api key", { api_key: "" }],
    ["oversized api key", { api_key: "a".repeat(2001) }],
    ["oversized api secret", { api_secret: "a".repeat(4001) }],
    ["lighter wallet key rather than a ro: token", { api_key: "0x" + "a".repeat(64) }],
  ];
  for (const [name, override] of cases) {
    const calls = stubFetch();
    const response = await handle(post([{ ...VALID, ...override }]));
    assertEquals(response.status, 400, `${name} should be refused`);
    assertEquals(calls.length, 0, `${name} must not reach the database`);
    await response.body?.cancel();
  }
});

Deno.test("one submission is one participant", async () => {
  const calls = stubFetch();
  const response = await handle(post([
    VALID,
    { ...VALID, exchange: "coinbase", account_type: "spot", email: "someone@else.com" },
  ]));
  assertEquals(response.status, 400);
  assertStringIncludes((await response.json()).error, "same name and email");
  assertEquals(calls.length, 0);
});

Deno.test("a venue may be submitted once", async () => {
  const calls = stubFetch();
  const response = await handle(post([VALID, VALID]));
  assertEquals(response.status, 400);
  assertStringIncludes((await response.json()).error, "once");
  assertEquals(calls.length, 0);
});

Deno.test("shape and size are bounded before anything is parsed", async () => {
  for (const body of [[], [VALID, VALID, VALID, VALID], {}, "nope", [null]]) {
    const calls = stubFetch();
    const response = await handle(post(body));
    assertEquals(response.status, 400, `${JSON.stringify(body)} should be refused`);
    assertEquals(calls.length, 0);
    await response.body?.cancel();
  }
});

Deno.test("only POST, and preflight is answered", async () => {
  const url = "https://example.supabase.co/functions/v1/register";
  const preflight = await handle(new Request(url, { method: "OPTIONS" }));
  assertEquals(preflight.status, 204);
  assertEquals(preflight.headers.get("Access-Control-Allow-Origin"), "*");

  const wrong = await handle(new Request(url, { method: "GET" }));
  assertEquals(wrong.status, 405);
  await wrong.body?.cancel();
});

Deno.test("clean trims and normalises", () => {
  const row = clean({
    ...VALID,
    display_name: "  tester  ",
    email: "  Tester@Example.com  ",
    exchange: "LIGHTER",
    account_type: "PERP",
  });
  assertEquals(typeof row, "object");
  assertEquals((row as Record<string, unknown>).display_name, "tester");
  assertEquals((row as Record<string, unknown>).exchange, "lighter");
  assertEquals((row as Record<string, unknown>).account_type, "perp");
  // An empty secret is null, not "" - Lighter has no secret and the column
  // is nullable precisely for that.
  assertEquals((row as Record<string, unknown>).api_secret, null);
});
