"""
The board's copy agrees with the scorer it describes.

The leaderboard page tells participants what "provisional" means - "fewer
than 20 return periods" - and metrics.py decides it. Nothing in either
language checks that the two agree, and the failure is a page that
confidently contradicts the badge printed beside it: raise the threshold to
30 and a 25-period entry is flagged provisional under a legend explaining
that provisional means fewer than 20.

The same drift this repository already guards between signup_form logic,
schema.sql and sign_ups.py - see tests/test_signup_form.py - in the one other
place a number is written down twice.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = REPO_ROOT / "web" / "app" / "leaderboard" / "page.tsx"
METRICS = REPO_ROOT / "metrics.py"


def test_the_reliability_threshold_matches_metrics():
    """
    Matched on the EXPRESSION, not a line number.

    An earlier version of the page's comment pointed at "metrics.py:853",
    which stops being true the next time anything above it is edited - a
    pointer that rots silently is worse than none.
    """
    metrics = re.search(
        r'result\["reliable"\]\s*=\s*result\["periods"\]\s*>=\s*(\d+)',
        METRICS.read_text(encoding="utf-8"))
    assert metrics, (
        "could not find the `reliable` assignment in metrics.py - if the "
        "expression was rewritten, update this test to match its new shape "
        "rather than deleting it")

    page = re.search(
        r"const RELIABLE_MIN_PERIODS = (\d+);",
        PAGE.read_text(encoding="utf-8"))
    assert page, "RELIABLE_MIN_PERIODS is missing from the leaderboard page"

    assert page.group(1) == metrics.group(1), (
        f"the board says {page.group(1)} return periods, metrics.py requires "
        f"{metrics.group(1)} - every entry between the two is labelled "
        f"provisional under a legend that denies it")


def test_the_page_does_not_cite_a_line_number_for_it():
    """
    The pointer above replaces a `metrics.py:853` reference. Keeping that
    style out is the point: this test is the durable link, and a line number
    beside it would start rotting again immediately.
    """
    text = PAGE.read_text(encoding="utf-8")
    stale = re.findall(r"metrics\.py:\d+", text)
    assert not stale, (
        f"{PAGE.name} cites {stale}, which goes stale on the next edit to "
        f"that file - name the expression, and let "
        f"test_the_reliability_threshold_matches_metrics do the pinning")
