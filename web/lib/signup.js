/**
 * What the signup form submits, and the rules it enforces before submitting.
 *
 * This module is the single source of truth for three things that have to
 * agree and that no compiler checks against each other: the venues the form
 * offers, the columns each POSTed row carries, and the validation that runs
 * before a credential leaves the browser. `tests/test_signup_form.py` asserts
 * it agrees with `pending_signups` in migrations/schema.sql and with what
 * sign_ups.py selects.
 *
 * It was `signup_form.html` at the repository root until the form became a
 * page in this app. The logic is unchanged; only its home moved.
 *
 * WHY THIS IS .js AND NOT .ts
 * ---------------------------
 * The Python test extracts the block below verbatim and executes it with
 * `node`, so it has to be runnable JavaScript - which rules out type
 * annotations. Types are therefore written as JSDoc, and TypeScript only
 * honours JSDoc in a .js file. `allowJs` is already set in tsconfig.json, so
 * importers get the same types they would from a .ts module.
 */

// ===== BEGIN PORTABLE LOGIC ======================================================
// Extracted and run under Node by tests/test_signup_form.py, which pulls these
// lines out verbatim and executes them. So: no TypeScript syntax, no imports,
// no DOM references, and no `export` inside this block.

/**
 * One row of a submission - one venue the participant registered.
 *
 * @typedef {object} SignupRow
 * @property {string} display_name
 * @property {string} email
 * @property {string} exchange
 * @property {string} account_type
 * @property {string} api_key
 * @property {string | null} api_secret
 */

// The venues this form can register, and the inputs each one reads.
//
// `exchange` and `account_type` must be values migrations/schema.sql allows -
// there are check constraints on both - and `key`/`secret` name the form
// inputs, not the columns.
const VENUES = [
  { exchange: "coinbase", account_type: "spot", label: "Coinbase spot",
    key: "coinbase_spot_key", secret: "coinbase_spot_secret" },
  { exchange: "coinbase", account_type: "perp", label: "Coinbase perpetuals",
    key: "coinbase_perp_key", secret: "coinbase_perp_secret" },
  { exchange: "lighter",  account_type: "perp", label: "Lighter",
    key: "lighter_token",     secret: null },
];

// The columns each POSTed row carries. These must match pending_signups in
// migrations/schema.sql and what sign_ups.py selects; the test asserts all
// three agree. A typo here inserts a row with a NULL credential, and the
// participant is then told their API KEY was invalid - which sends everyone
// looking in the wrong place.
const SUBMIT_FIELDS = [
  "display_name",
  "email",
  "exchange",
  "account_type",
  "api_key",
  "api_secret",
];

/**
 * Turn the filled-in form into the rows to POST.
 *
 * Returns { rows, problem }. A venue is "filled in" if its key field has
 * anything in it; blank sections are simply skipped, so a participant who
 * only trades one venue submits one row.
 *
 * @param {Record<string, string>} values
 * @returns {{ rows: SignupRow[], problem: string | null }}
 */
function buildRows(values) {
  /** @type {SignupRow[]} */
  const rows = [];

  // Trimmed HERE rather than only at the call site, so this function is
  // self-contained and the checks below are what the tests exercise.
  //
  // `required` on the inputs does NOT cover this: the browser accepts "   "
  // as present, and a display_name that trims to empty then fails
  // pending_signups_size_check in Postgres instead - a 400 the participant
  // cannot act on, for a mistake the page can name precisely.
  const displayName = (values.display_name || "").trim();
  const email = (values.email || "").trim();

  if (!displayName) {
    return { rows: [], problem: "Enter a display name." };
  }

  if (!email) {
    return { rows: [], problem: "Enter an email address." };
  }

  for (const venue of VENUES) {
    const key = (values[venue.key] || "").trim();
    const secret = venue.secret ? (values[venue.secret] || "").trim() : "";

    if (!key && !secret) continue;          // section left blank

    if (!key) {
      return { rows: [], problem:
        `${venue.label}: you entered a private key but no API key name.` };
    }

    // A COURTESY, not a control - anyone can POST to the endpoint directly,
    // and verify_credential in the venue adapters is the real enforcement.
    // Worth having anyway for Lighter: it stops an irrevocable wallet private
    // key being transmitted at all, rather than rejecting it after receipt.
    if (venue.exchange === "lighter" && !key.startsWith("ro:")) {
      return { rows: [], problem:
        "Lighter needs a read-only token starting with \"ro:\". A wallet " +
        "private key cannot be revoked and will be rejected." };
    }

    if (venue.secret && !secret) {
      return { rows: [], problem:
        `${venue.label}: Coinbase needs both the API key name and the ` +
        `private key.` };
    }

    rows.push({
      display_name: displayName,
      email: email,
      exchange: venue.exchange,
      account_type: venue.account_type,
      api_key: key,
      api_secret: secret || null,
    });
  }

  if (rows.length === 0) {
    return { rows: [], problem:
      "Enter credentials for at least one venue - otherwise there is nothing " +
      "to rank." };
  }

  return { rows, problem: null };
}
// ===== END PORTABLE LOGIC ========================================================

/**
 * How a submitted row reads back to a human, for the confirmation message.
 * "Coinbase perp" is what the database calls it; "Coinbase perpetuals" is what
 * the participant typed it under.
 *
 * Outside the portable block: it is presentation, not validation, so the test
 * has no reason to run it.
 *
 * @param {SignupRow} row
 * @returns {string}
 */
function venueLabel(row) {
  const venue = VENUES.find(
    (v) => v.exchange === row.exchange && v.account_type === row.account_type,
  );
  return venue ? venue.label : `${row.exchange} ${row.account_type}`;
}

export { VENUES, SUBMIT_FIELDS, buildRows, venueLabel };
