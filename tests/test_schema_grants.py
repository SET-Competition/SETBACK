"""
The two privileges on which the public site's correctness and privacy rest.

Both were wrong at once on the live database, and neither failed loudly:

  - `leaderboard` was revoked from anon, so web/lib/leaderboard.ts took its
    fallback and the public ranking served five invented traders under a
    "sample data" banner. Fabricated standings on a competition board are a
    worse failure than an error page, and nothing in CI noticed.

  - `participants` carried a policy named "leaderboard is readable", written
    `for select using (true)` with no `to` clause. Naming no role means
    PUBLIC, which includes anon, so every participant's EMAIL was readable
    over PostgREST by anyone who lifted the anon key out of the JS bundle -
    and the leaderboard, being a view that runs as its owner, never needed it.

schema.sql is re-run by hand whenever the schema changes, so a re-run is how
these came back the first time.

WHAT THIS FILE LEARNED THE HARD WAY
-----------------------------------
Version 1 grepped for the presence of statements. That holds while the bug is
one line away: appending `grant select on public.participants to anon;`
re-opened the leak with a green suite.

Version 2 modelled privileges in order - correct, because grants are
last-write-wins - but kept an unsafe default. When a regex failed to RECOGNISE
a statement, the statement was skipped and the previous state survived. Review
found six ways to exploit that, every one of them green: a column grant
(`grant select (email) …`), a trailing clause (`… to anon with grant option`,
`… from anon cascade`), `alter table … disable row level security`, a `--`
inside a string literal swallowing the next statement, and a stray `$$` in a
comment re-pairing with the real dollar-quoted blocks and deleting 40% of the
file from the analysis.

So version 3 inverts the default. Statements are split by a scanner that
understands string literals, quoted identifiers, line and block comments, and
dollar quoting with tags - not by stripping lines and splitting on `;`. RLS is
modelled in order exactly as grants are. And
`test_the_schema_uses_only_sql_this_file_models` fails on any grant, revoke,
policy alteration or view option this file cannot parse, so SQL it does not
understand is a red test rather than a silent pass.

The remaining assertions are deliberately overstrict in the safe direction: a
column-level grant counts as a table grant, because the property wanted here
is "anon holds nothing on participants", and the file's own idiom is
`revoke all`.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "migrations" / "schema.sql"

_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")


def _statements(path: Path) -> list[str]:
    """
    Split SQL into statements with a scanner, not line-wise regexes.

    Comments are skipped only when they start outside a string, and dollar
    quoting is matched tag to tag, so neither a `--` inside a literal nor a
    stray `$$` in prose can consume real SQL. Both were demonstrated holes in
    the previous version: the `--` merged the following statement into an
    unparsable fragment, and the `$$` re-paired with the file's five `do`
    blocks and dropped ~22,000 characters from the analysis - in both cases
    silently, with the suite green.
    """
    source = path.read_text(encoding="utf-8")
    statements: list[str] = []
    current: list[str] = []
    i, end = 0, len(source)

    while i < end:
        char, pair = source[i], source[i : i + 2]

        if char in "'\"":  # literal or quoted identifier; doubling escapes
            j = i + 1
            while j < end:
                if source[j] == char:
                    if source[j + 1 : j + 2] == char:
                        j += 2
                        continue
                    break
                j += 1
            current.append(source[i : j + 1])
            i = j + 1
        elif pair == "--":
            newline = source.find("\n", i)
            i = end if newline < 0 else newline
        elif pair == "/*":
            depth, j = 1, i + 2
            while j < end and depth:
                if source[j : j + 2] == "/*":
                    depth, j = depth + 1, j + 2
                elif source[j : j + 2] == "*/":
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            i = j
        elif char == "$" and (tag := _DOLLAR_TAG.match(source, i)):
            closing = source.find(tag.group(0), tag.end())
            i = end if closing < 0 else closing + len(tag.group(0))
            current.append(" ")  # the body is procedural code, never a grant
        elif char == ";":
            statements.append("".join(current))
            current = []
            i += 1
        else:
            current.append(char)
            i += 1

    statements.append("".join(current))
    return [
        re.sub(r"\s+", " ", statement).strip().lower()
        for statement in statements
        if statement.strip()
    ]


def _table(name: str) -> str:
    """`participants`, `"public"."participants"` -> `public.participants`."""
    name = name.replace('"', "").strip()
    return name if "." in name else f"public.{name}"


def _reaches_anon(roles: str) -> bool:
    """
    True if this role list includes anon.

    PUBLIC is the pseudo-role every role belongs to, anon included - the
    subtlety that made a policy with no `to` clause the bug that shipped.

    Trailing clauses are stripped first. `to anon with grant option` and
    `from anon cascade` both used to leave no element equal to `anon`, so a
    hostile grant and a board-breaking revoke were each one keyword from
    invisible.
    """
    roles = re.sub(r"\bwith grant option\b|\bgranted by\s+\S+|\b(cascade|restrict)\b", " ", roles)
    named = {
        role.strip().strip('"').removeprefix("group ").strip()
        for role in roles.split(",")
    }
    return "anon" in named or "public" in named


def _covers(privileges: str, wanted: str) -> bool:
    """
    `select (email)` counts as select. A column grant is narrower than a table
    grant, but the property asserted here is that anon holds NOTHING on these
    tables, so treating it as a grant is overstrict only in the safe direction
    - and `grant select (email) on public.participants to anon` was a live
    leak an earlier version waved through.
    """
    privileges = re.sub(r"\s*\([^)]*\)", "", privileges)
    privileges = privileges.replace("privileges", "").strip()
    if privileges in ("all", ""):
        return True
    return wanted in {p.strip() for p in privileges.split(",")}


def _targets(target: str, table: str) -> bool:
    """Does this grant/revoke name `table`? Handles lists and ALL TABLES."""
    target = target.strip()
    schema_wide = re.match(r"all tables in schema\s+(?P<schemas>.+)$", target)
    if schema_wide:
        schemas = {s.strip().strip('"') for s in schema_wide.group("schemas").split(",")}
        return _table(table).split(".")[0] in schemas
    if re.match(r"all (sequences|functions|routines|procedures) in schema", target):
        return False
    return _table(table) in {_table(t) for t in target.replace("table ", "").split(",")}


_GRANT = re.compile(
    r"^(?P<verb>grant|revoke)\s+(?P<option_only>grant option for\s+)?"
    r"(?P<privileges>.+?)\s+on\s+(?P<target>.+?)\s+(?:to|from)\s+(?P<roles>.+)$"
)


def _anon_effective(
    statements: list[str], table: str, privilege: str, *, default: bool
) -> bool:
    """Whether anon holds `privilege` on `table` once the whole file has run."""
    state = default
    for statement in statements:
        match = _GRANT.match(statement)
        if not match or match.group("option_only"):
            continue  # revoking the grant OPTION leaves the privilege itself
        if not _targets(match.group("target"), table):
            continue
        if not _covers(match.group("privileges"), privilege):
            continue
        if not _reaches_anon(match.group("roles")):
            continue
        state = match.group("verb") == "grant"
    return state


_RLS = re.compile(
    r"^alter table\s+(?:if exists\s+)?(?:only\s+)?(?P<table>[\w.\"]+)\s+"
    r"(?P<action>enable|disable) row level security$"
)


def _rls_enabled(statements: list[str], table: str) -> bool:
    """
    Modelled in order, like the grants.

    The previous version asserted only that an `enable` existed somewhere, so
    appending `alter table public.participants disable row level security;`
    passed - and composed with a column grant into a complete live email leak
    with a green suite.
    """
    state = False
    for statement in statements:
        match = _RLS.match(statement)
        if match and _table(match.group("table")) == _table(table):
            state = match.group("action") == "enable"
    return state


_CREATE_POLICY = re.compile(
    r'^create policy\s+(?P<name>"[^"]+"|\S+)\s+on\s+(?:table\s+)?'
    r'(?P<table>"?[\w."]+"?)\s*(?P<body>.*)$'
)
_DROP_POLICY = re.compile(
    r'^drop policy\s+(?:if exists\s+)?(?P<name>"[^"]+"|\S+)\s+on\s+'
    r'(?:table\s+)?(?P<table>"?[\w."]+"?)'
)


def _surviving_policies(statements: list[str], table: str, command: str) -> list[str]:
    """
    Policy names that let anon run `command` on `table` once the file has
    finished.

    The body is cut at `using` / `with check` before anything is read from it.
    Without that, the first `for` or `to` ANYWHERE won - including inside a
    predicate - so `using (note = 'send to anon')` on a policy with no `to`
    clause scanned as "addressed to someone", and
    `using (note = 'for insert')` on a policy with no `for` clause scanned as
    insert-only. Both are the exact shape of the bug that shipped, and both
    passed.
    """
    live: dict[str, bool] = {}
    for statement in statements:
        dropped = _DROP_POLICY.match(statement)
        if dropped and _table(dropped.group("table")) == _table(table):
            live.pop(dropped.group("name").strip('"'), None)
            continue

        created = _CREATE_POLICY.match(statement)
        if not created or _table(created.group("table")) != _table(table):
            continue

        head = re.split(r"\busing\b|\bwith check\b", created.group("body"), maxsplit=1)[0]
        if "as restrictive" in head:
            continue  # restrictive policies narrow access, they never grant it

        # No FOR clause means FOR ALL, which includes every command.
        declared = re.search(r"\bfor\s+(all|select|insert|update|delete)\b", head)
        if declared and declared.group(1) not in ("all", command):
            continue

        # No TO clause means PUBLIC.
        roles = re.search(r"\bto\s+(.+)$", head)
        if _reaches_anon(roles.group(1) if roles else "public"):
            live[created.group("name").strip('"')] = True

    return sorted(live)


def test_the_schema_uses_only_sql_this_file_models():
    """
    Anything this file cannot parse must be a RED test, not a silent pass.

    Every hole review found had the same shape: an unrecognised statement was
    skipped and the previous state survived, so the model answered a question
    it had not actually read. Inverting that default is worth more than any
    individual pattern below it, because it also covers the shapes nobody has
    thought of yet.
    """
    guarded = ("public.participants", "public.leaderboard")
    unmodelled: list[str] = []

    for statement in _statements(SCHEMA):
        if re.match(r"^(grant|revoke)\b", statement) and not _GRANT.match(statement):
            unmodelled.append(statement)
        if re.match(r"^alter policy\b", statement) and any(
            t.split(".")[-1] in statement for t in guarded
        ):
            unmodelled.append(statement)
        if re.match(r"^alter table\b", statement) and "row level security" in statement:
            if not _RLS.match(statement) and "force row level security" not in statement:
                unmodelled.append(statement)

    assert not unmodelled, (
        "schema.sql contains privilege SQL this test cannot model, so its "
        "other assertions are answering a question they have not read:\n  "
        + "\n  ".join(unmodelled)
        + "\n\nEither express it in a form the model handles (`alter policy` "
        "as drop + create, for instance) or teach the model the new shape."
    )


def test_the_leaderboard_view_runs_as_its_owner():
    """
    The whole design rests on this view bypassing RLS.

    `leaderboard` reads `participants`, which has RLS on and no policy. That
    is fine only because a view runs with its OWNER's privileges - so the view
    sees the rows and anon, querying the view, does not see the table. Turn on
    security_invoker and the view runs as the caller instead: RLS yields
    nothing, and the site renders an EMPTY live board. Not sample data, not a
    permission error - no banner fires at all, because nothing failed.

    Supabase's own database linter nudges toward security_invoker for views
    owned by a privileged role, so this is a plausible future edit rather than
    a hypothetical one.
    """
    offenders = [
        statement
        for statement in _statements(SCHEMA)
        if "security_invoker" in statement and "leaderboard" in statement
    ]
    assert not offenders, (
        "security_invoker is set on public.leaderboard:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe view would then run as the caller, hit RLS on participants, "
        "and return nothing - an empty board with no error anywhere."
    )


def test_the_leaderboard_view_is_readable_by_anon():
    """
    The browser reads this view with the anon key and no auth step, so anon is
    the one role that MUST hold select on it when the file finishes.

    Starts from `default=False`: a grant on this view has to be explicit, not
    inherited from whatever Supabase did to the public schema.
    """
    assert _anon_effective(
        _statements(SCHEMA), "public.leaderboard", "select", default=False
    ), (
        "After schema.sql runs, anon cannot select public.leaderboard.\n"
        "The live board then falls back to MOCK_ROWS and serves invented "
        "traders to the public. Look for a later revoke undoing the grant - "
        "including `revoke ... on all tables in schema public from anon`, and "
        "`revoke ... from anon cascade`, which also strip it.\n"
        "Closing the board is a legitimate decision, but a deliberate one: "
        "change this test in the same commit."
    )


def test_anon_cannot_read_participant_email():
    """
    participants holds email. Two independent things keep anon out - RLS with
    no permissive select policy, and no table grant - and either alone returns
    zero rows. Both are pinned, and both are modelled in order, because review
    showed they can be taken down one statement each.

    Starts from `default=True`: Supabase default-grants select on public-schema
    tables, which is the grant the original policy rode in on, so the file has
    to revoke it rather than merely not mention it.
    """
    statements = _statements(SCHEMA)

    assert _rls_enabled(statements, "public.participants"), (
        "RLS is not enabled on public.participants when schema.sql finishes. "
        "Check for a later `disable row level security`."
    )

    leaks = _surviving_policies(statements, "public.participants", "select")
    assert not leaks, (
        f"Policies {leaks} let anon select public.participants, which "
        "publishes every participant's email address.\n"
        "A policy with no FOR clause covers SELECT, and one with no TO clause "
        "- or `to public` - reaches anon.\n"
        "The leaderboard is a VIEW and runs with its owner's privileges, so it "
        "reads this table regardless of RLS and needs no policy here. That is "
        'what the original "leaderboard is readable" policy got wrong.'
    )

    assert not _anon_effective(statements, "public.participants", "select", default=True), (
        "After schema.sql runs, anon holds select on public.participants, so "
        "every participant's email is readable over PostgREST.\n"
        "Supabase grants this by default, so the revoke must be explicit - and "
        "must not be undone by a later grant, including a column grant such as "
        "`grant select (email) on public.participants to anon`."
    )


def test_anon_cannot_write_signups_directly():
    """
    The rate limit is only a limit if the Edge Function is the only entrance.

    anon used to hold `insert` on pending_signups, under a policy named
    "anon submits a signup", and that is precisely why nothing rate-limited
    the form: the write went from the visitor's browser to PostgREST without
    passing through anything we run. Cloudflare and Vercel sit in front of the
    WEBSITE, and the website was not on the path - so there was no point at
    which to count requests, whatever was bought or configured.

    Submissions now go through supabase/functions/register/index.ts, which
    holds the service role inside Supabase and checks record_signup_attempt
    first. Re-granting insert here would not break anything visibly: the form
    would keep working, and the limit would quietly become optional for anyone
    who reads the bundle. That is the regression this pins.

    Starts from `default=True`, like participants: Supabase default-grants on
    the public schema, so the revoke has to be explicit.
    """
    statements = _statements(SCHEMA)

    assert not _anon_effective(
        statements, "public.pending_signups", "insert", default=True
    ), (
        "anon can insert into public.pending_signups, so the browser can reach "
        "the table directly and the rate limit in migrations/003 is bypassable "
        "by anyone who reads the anon key out of the JS bundle.\n"
        "Submissions are meant to go through the register Edge Function."
    )

    leaks = _surviving_policies(statements, "public.pending_signups", "insert")
    assert not leaks, (
        f"Policies {leaks} let anon insert into public.pending_signups, "
        "re-opening the unrated path the Edge Function replaced."
    )

    # The table still holds plaintext credentials until the importer runs, so
    # the read side matters as much as it ever did.
    assert not _anon_effective(
        statements, "public.pending_signups", "select", default=True
    ), "anon can select public.pending_signups, which holds plaintext credentials."
