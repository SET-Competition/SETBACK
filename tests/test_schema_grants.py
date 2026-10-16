"""
The two grants on which the public site's correctness and privacy both rest.

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

schema.sql is re-run by hand against the project whenever the schema changes,
so a re-run is how these came back the first time, and this is what makes the
next re-run safe.

WHY THIS MODELS PRIVILEGES INSTEAD OF GREPPING
----------------------------------------------
The first version of this file asserted "a revoke for participants exists"
and "no revoke for leaderboard exists". Both held while the bug was one line
away: appending `grant select on public.participants to anon;` anywhere below
re-opened the email leak with a green suite, and
`revoke select on all tables in schema public from anon;` broke the board
with a green suite. Neither is exotic - the first is what Supabase's own
dashboard emits when you tick a box.

Privileges are order-dependent and last-write-wins, so the only honest check
is to evaluate the statements in order and assert on the FINAL state. That is
what _anon_select_effective does, and it is why a later grant or a
schema-wide revoke cannot slip past.

The starting state differs per object on purpose. Supabase default-grants on
the public schema, so `participants` starts readable and the file must take
that away; a view's grant is ours to make, so `leaderboard` starts closed and
the file must give it.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "migrations" / "schema.sql"


def _statements(path: Path) -> list[str]:
    """
    schema.sql as lowercased, whitespace-collapsed statements.

    Dollar-quoted bodies go first: they hold procedural code, never a grant or
    a policy, and their semicolons would otherwise split statements wrongly.
    Line comments go second, so a `--` inside a $$ body cannot eat real SQL.
    """
    source = re.sub(r"\$\$.*?\$\$", " ", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    source = "\n".join(re.sub(r"--.*$", "", line) for line in source.splitlines())
    return [
        re.sub(r"\s+", " ", statement).strip().lower()
        for statement in source.split(";")
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
    subtlety that made `to public` a leak the grep version waved through.
    """
    named = {
        role.strip().replace('"', "").removeprefix("group ").strip()
        for role in roles.split(",")
    }
    return "anon" in named or "public" in named


def _covers_select(privileges: str) -> bool:
    privileges = privileges.replace("privileges", "").strip()
    if privileges in ("all", ""):
        return True
    return "select" in {p.strip() for p in privileges.split(",")}


def _targets(target: str, table: str) -> bool:
    """Does this grant/revoke target name `table`? Handles lists and ALL TABLES."""
    target = target.strip()
    schema_wide = re.match(r"all tables in schema\s+(?P<schema>[\w\"]+)", target)
    if schema_wide:
        return _table(table).startswith(schema_wide.group("schema").replace('"', "") + ".")
    listed = {_table(t) for t in target.replace("table ", "").split(",")}
    return _table(table) in listed


_GRANT = re.compile(
    r"^(?P<verb>grant|revoke)\s+(?:grant option for\s+)?"
    r"(?P<privileges>.+?)\s+on\s+(?P<target>.+?)\s+(?:to|from)\s+(?P<roles>.+)$"
)


def _anon_select_effective(statements: list[str], table: str, *, default: bool) -> bool:
    """Whether anon can SELECT `table` after the whole file has run."""
    state = default
    for statement in statements:
        match = _GRANT.match(statement)
        if not match or not _targets(match.group("target"), table):
            continue
        if not _covers_select(match.group("privileges")):
            continue
        if not _reaches_anon(match.group("roles")):
            continue
        state = match.group("verb") == "grant"
    return state


_CREATE_POLICY = re.compile(
    r'^create policy\s+(?P<name>"[^"]+"|\S+)\s+on\s+(?:table\s+)?'
    r'(?P<table>"?[\w."]+"?)\s*(?P<body>.*)$'
)
_DROP_POLICY = re.compile(
    r'^drop policy\s+(?:if exists\s+)?(?P<name>"[^"]+"|\S+)\s+on\s+'
    r'(?:table\s+)?(?P<table>"?[\w."]+"?)'
)


def _surviving_select_policies(statements: list[str], table: str) -> list[str]:
    """
    Policy names that let anon SELECT `table` once the file has finished.

    Tracked through drops as well as creates, because the file's idiom is
    `drop policy if exists` immediately followed by `create policy`.
    """
    live: dict[str, bool] = {}
    for statement in statements:
        dropped = _DROP_POLICY.match(statement)
        if dropped and _table(dropped.group("table")) == _table(table):
            live.pop(dropped.group("name").replace('"', ""), None)
            continue

        created = _CREATE_POLICY.match(statement)
        if not created or _table(created.group("table")) != _table(table):
            continue

        body = created.group("body")
        if "as restrictive" in body:
            continue  # restrictive policies narrow access, they never grant it

        # No FOR clause means FOR ALL, which includes SELECT.
        command = re.search(r"\bfor\s+(all|select|insert|update|delete)\b", body)
        if command and command.group(1) not in ("all", "select"):
            continue

        # No TO clause means PUBLIC.
        roles = re.search(r"\bto\s+(.+?)(?=\busing\b|\bwith check\b|$)", body)
        if _reaches_anon(roles.group(1) if roles else "public"):
            live[created.group("name").replace('"', "")] = True

    return sorted(live)


def test_the_leaderboard_view_is_readable_by_anon():
    """
    The browser reads this view with the anon key and no auth step, so anon is
    the one role that MUST hold select on it when the file finishes.

    Starts from `default=False`: a grant on this view has to be explicit, not
    inherited from whatever Supabase did to the public schema.
    """
    statements = _statements(SCHEMA)

    assert _anon_select_effective(statements, "public.leaderboard", default=False), (
        "After schema.sql runs, anon cannot select public.leaderboard.\n"
        "The live board then falls back to MOCK_ROWS and serves invented "
        "traders to the public. Check for a later revoke undoing the grant - "
        "including `revoke ... on all tables in schema public from anon`, "
        "which also strips it.\n"
        "Closing the board is a legitimate decision, but it is a deliberate "
        "one: change this test in the same commit."
    )


def test_anon_cannot_read_participant_email():
    """
    participants holds email. Two independent things keep anon out - RLS with
    no permissive select policy, and no table grant - and either alone returns
    zero rows. Both are pinned, because the table is one `using (true)` away
    from publishing every address.

    Starts from `default=True`: Supabase default-grants select on public-schema
    tables, which is the grant the original policy rode in on, so the file has
    to revoke it rather than merely not mention it.
    """
    statements = _statements(SCHEMA)

    assert any(
        re.match(
            r"alter table (?:if exists )?(?:public\.)?participants "
            r"enable row level security",
            statement,
        )
        for statement in statements
    ), "RLS is not enabled on public.participants."

    leaks = _surviving_select_policies(statements, "public.participants")
    assert not leaks, (
        f"Policies {leaks} let anon select public.participants, which "
        "publishes every participant's email address.\n"
        "Remember that a policy with no FOR clause covers SELECT, and one "
        "with no TO clause - or `to public` - reaches anon.\n"
        "The leaderboard is a VIEW and runs with its owner's privileges, so "
        "it reads this table regardless of RLS and needs no policy here. "
        'That is what the original "leaderboard is readable" policy got wrong.'
    )

    assert not _anon_select_effective(statements, "public.participants", default=True), (
        "After schema.sql runs, anon holds select on public.participants, so "
        "every participant's email is readable over PostgREST.\n"
        "Supabase grants this by default, so the revoke must be explicit - "
        "and must not be undone by a later grant."
    )
