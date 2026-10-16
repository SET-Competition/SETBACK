"""
The two grants on which the public site's correctness and privacy both rest.

Both of these were wrong at once on the live database, and neither failed
loudly:

  - `leaderboard` was revoked from anon, so web/lib/leaderboard.ts fell back
    to MOCK_ROWS and the public ranking served five invented traders under a
    "sample data" banner. A leaderboard that shows fabricated standings is a
    worse failure than one that shows an error, and nothing in CI noticed.

  - `participants` carried a `for select using (true)` policy named
    "leaderboard is readable", which applied to anon because it named no
    role. Every participant's EMAIL was readable over PostgREST by anyone who
    lifted the anon key out of the JS bundle - and the leaderboard, being a
    view that runs as its owner, never needed it.

Both live in schema.sql, which is re-run by hand against the project whenever
the schema changes. So a re-run is exactly how they came back the first time,
and this test is what makes the next re-run safe.

Matched on SHAPE rather than exact lines, following tests/test_signup_form.py
and tests/test_leaderboard_page.py: reformatting the SQL should not fail
these, and neither should adding roles alongside anon.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = REPO_ROOT / "migrations" / "schema.sql"


def _sql() -> str:
    """Schema text with comments stripped, so prose cannot satisfy a match."""
    source = SCHEMA.read_text(encoding="utf-8")
    return "\n".join(
        re.sub(r"--.*$", "", line) for line in source.splitlines()
    )


def test_the_leaderboard_view_is_readable_by_anon():
    """
    The browser reads this view with the anon key and no auth step, so anon
    is the one role that MUST hold select on it.
    """
    sql = _sql()

    granted = re.search(
        r"grant\s+select\s+on\s+public\.leaderboard\s+to\s+[^;]*\banon\b",
        sql, re.IGNORECASE,
    )
    assert granted, (
        "No `grant select on public.leaderboard to anon` in schema.sql.\n"
        "Without it the live leaderboard falls back to MOCK_ROWS and serves "
        "invented traders to the public. See migrations/"
        "002_public_leaderboard_and_participant_privacy.sql."
    )

    # A later revoke would quietly undo the grant above; the original bug was
    # exactly this, sitting 40 lines below the comment that promised a board.
    revoked = re.search(
        r"revoke\s+(?:all|select)[^;]*\bon\s+public\.leaderboard\s+from\s+[^;]*\banon\b",
        sql, re.IGNORECASE,
    )
    assert not revoked, (
        "schema.sql revokes public.leaderboard from anon, which turns the "
        "live board into sample data. Closing the board is a deliberate act - "
        "if that is the intent, this test should be changed in the same commit."
    )


def test_anon_cannot_read_participant_email():
    """
    participants holds email. RLS is enabled on it with no select policy, and
    the anon grant is revoked; either alone returns zero rows, and the table
    is one `using (true)` away from leaking every address, so both are pinned.
    """
    sql = _sql()

    assert re.search(
        r"alter\s+table\s+public\.participants\s+enable\s+row\s+level\s+security",
        sql, re.IGNORECASE,
    ), "RLS is not enabled on public.participants."

    # Any permissive select policy that does not name a role reaches anon.
    for match in re.finditer(
        r"create\s+policy\s+(?P<q>[\"']?)(?P<name>[^\"']+?)(?P=q)?\s+"
        r"on\s+public\.participants\b(?P<body>.*?);",
        sql, re.IGNORECASE | re.DOTALL,
    ):
        body = match.group("body")
        if not re.search(r"\bfor\s+select\b", body, re.IGNORECASE):
            continue
        roles = re.search(r"\bto\s+([\w\s,]+?)(?:\busing\b|$)", body, re.IGNORECASE)
        reaches_anon = roles is None or "anon" in roles.group(1).lower()
        assert not reaches_anon, (
            f'Policy "{match.group("name").strip()}" grants anon select on '
            "public.participants, which exposes every participant's email "
            "address. The leaderboard is a VIEW and runs as its owner, so it "
            "does not need this policy - that is what the original "
            '"leaderboard is readable" policy got wrong.'
        )

    assert re.search(
        r"revoke\s+(?:all|[^;]*\bselect\b)[^;]*\bon\s+public\.participants\s+"
        r"from\s+[^;]*\banon\b",
        sql, re.IGNORECASE,
    ), (
        "schema.sql never revokes select on public.participants from anon. "
        "Supabase grants it on the public schema by default, so the revoke "
        "has to be explicit."
    )
