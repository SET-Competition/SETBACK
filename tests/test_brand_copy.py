"""
The pitch is written down once.

The sentence describing the competition appeared four times - the root
metadata, the leaderboard route's metadata, the share card's alt text, and
the card's own pixels - in four slightly different forms. One of those four
is a PNG, which is what makes this worth a test: change the pitch and the
shared card keeps advertising the old one, because nobody greps an image and
nothing renders it where a reviewer would see it.

web/lib/copy.ts is now the source. This checks nothing has quietly grown a
second copy, which is the normal way these come back - a route that wants a
slightly different wording and takes the shortest path to it.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "web"
COPY = WEB / "lib" / "copy.ts"

# Every file that describes the competition to a reader or a crawler.
CONSUMERS = (
    WEB / "app" / "layout.tsx",
    WEB / "app" / "leaderboard" / "page.tsx",
    WEB / "app" / "opengraph-image.tsx",
)


def _const(name: str) -> str:
    found = re.search(rf'export const {re.escape(name)} =\s*"([^"]+)";',
                      COPY.read_text(encoding="utf-8"))
    assert found, f"{name} is missing from lib/copy.ts"
    return found.group(1)


def test_the_description_is_not_written_out_anywhere_else():
    """
    Matched on a distinctive fragment rather than the whole sentence: a copy
    that drifted by a word is still a copy, and is in fact the case that
    hurts - two descriptions that look identical in review and differ in the
    rendered head.
    """
    description = _const("DESCRIPTION")

    # The part that carries the meaning and would survive light editing.
    fragment = "flow-adjusted Sharpe ratio"
    assert fragment in description, (
        "DESCRIPTION no longer contains the phrase this test keys on - "
        "update the fragment to something equally distinctive rather than "
        "deleting the check")

    offenders = []
    for path in CONSUMERS:
        text = path.read_text(encoding="utf-8")
        # Strip comments: the reasoning around these constants legitimately
        # quotes them, and a comment cannot drift into the rendered page.
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"^\s*//.*$", "", text, flags=re.M)

        for literal in re.findall(r'"([^"]*)"', text):
            if fragment in literal:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {literal!r}")

    assert not offenders, (
        "the competition's description is written out as a string literal in "
        "these files instead of imported from lib/copy.ts:\n  "
        + "\n  ".join(offenders)
        + "\nImport DESCRIPTION (or TAGLINE) instead - one of these is a PNG "
          "and will not be noticed when the other changes.")


def test_the_card_alt_text_is_derived_from_the_tagline():
    """
    alt is what a screen reader hears instead of the card, so it describes
    the artwork. Derived rather than written out, or it becomes a fifth
    variant that disagrees with the picture it stands for.
    """
    source = COPY.read_text(encoding="utf-8")
    assert re.search(r"export const CARD_ALT = `[^`]*\$\{TAGLINE[^}]*\}", source), (
        "CARD_ALT should be built from TAGLINE, not written out separately - "
        "otherwise the card's words and its description drift apart")


def test_the_card_renders_the_tagline_it_documents():
    """
    The pixels have to come from the constant. A card that hardcodes its own
    text is the exact failure lib/copy.ts exists to prevent, and it is
    invisible in a diff of the metadata.
    """
    card = (WEB / "app" / "opengraph-image.tsx").read_text(encoding="utf-8")
    assert "{TAGLINE}" in card, (
        "opengraph-image.tsx no longer renders {TAGLINE} - if the card's "
        "wording moved, move it into lib/copy.ts rather than inlining it")
    assert "CARD_ALT" in card, (
        "opengraph-image.tsx should export `alt = CARD_ALT`")
